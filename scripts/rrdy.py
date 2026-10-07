#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rrdy.py — 人人电影网 (www.rrdynb.com) 影视资源搜索 & 网盘链接提取工具

子命令
------
  search  <关键词>            搜索站内影视资源，输出候选文章列表
  links   <文章URL | aid>     抓取单个文章页，提取其中的网盘链接
  resolve <关键词>            搜索 + 自动抓取前 N 个候选文章页 + 合并去重输出网盘链接

用法示例
--------
  python rrdy.py resolve "流浪地球"
  python rrdy.py search "流浪地球" --json
  python rrdy.py links "https://www.rrdynb.com/movie/2023/0414/34378.html"
  python rrdy.py links 34378 --json

性能说明
--------
  1. 4xx 短路：403(CF 拦截) / 404 等确定性错误不再重试，避免无谓等待。
     该站 403 页面不含 CF 挑战特征串，原先会被误判为可重试错误而重试 3 轮。
  2. 本地缓存：搜索页与文章页按 URL 缓存（HTTP 200 默认 6 小时，
     确定性 4xx 默认 10 分钟；网络级失败不缓存）。重复查询近乎瞬时返回。
  3. 端点不可用标记：搜索接口在部分网络环境恒定被 CF 拦截，判定失败后
     默认 6 小时内换新片名也直接给兜底地址，省去每次约 10s 的探测成本
     （可用 RRDY_SEARCH_BLOCK_TTL 调整，--force-search 忽略标记强制探测）。
  4. 并发抓取 + 过期缓存兜底：多目标用线程池并发，取代串行 + sleep；
     网络失败时回退到本地过期的成功副本。
  5. 链接形态校验：分享 ID 被上游截断（如迅雷 18 字符的残缺链接）等死链
     默认不展示，只提示数量；--keep-invalid 可显示。
  6. 可调参数：--timeout / --retries / --deadline / --search-budget /
     --page-budget / --force-search / --refresh / --no-cache / --timing /
     --keep-invalid。

退出码
------
  0  成功
  2  本地抓取受阻（搜索接口被 Cloudflare 拦截，或本机网络不可达），
     已打印 search_url，应改用 WebFetch 兜底（见 SKILL.md）
  3  未找到任何结果
  1  其它错误

仅依赖 Python 标准库。
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netlib   # noqa: E402  统一出口（代理）选择
import linklib  # noqa: E402  链接清洗 / 去重指纹 / 形态校验

BASE = "https://www.rrdynb.com"
SEARCH_ENDPOINT = BASE + "/plus/search.php"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

COMMON_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
    "Upgrade-Insecure-Requests": "1",
}

# 默认超时/重试。搜索接口被 CF 挡住时会返回 403，4xx 短路后单次仅数秒。
DEFAULT_TIMEOUT = 12
DEFAULT_RETRIES = 3
# 不同命令的推荐值：搜索偏保守（多数被 CF 拦截，重试无益），文章页偏耐心。
SEARCH_TIMEOUT = 10
SEARCH_RETRIES = 2

# 整体耗时上限（秒）。站点会在「秒回」与「握手挂起」之间跳变，
# 单靠 timeout×retries 无法保证上界，故用总预算兜底。
DEFAULT_DEADLINE = 60.0
DEADLINE_AT = None  # 全命令绝对单调时钟上限；None 表示不限

# 各阶段预算（秒）。阶段预算与全局预算取较小值，避免单一环节吃满总预算。
SEARCH_BUDGET = 12.0  # 搜索接口：正常约 2s；被 CF 拦截时 1 次尝试即可判定
PAGE_BUDGET = 30.0    # 单个文章页：正常 2~8s，允许少量重试

# 「搜索接口不可用」标记有效期（秒）。
# 该端点被 Cloudflare 托管挑战保护，在部分网络环境下**恒定不可达**
# （实测本机：直连与走 Clash 都无法访问 challenges.cloudflare.com，
#  换出口同样过不了挑战）。一旦判定失败即写标记，换新片名也无需再付出
# 探测成本；成功后立即清除。可用 --force-search 忽略标记强制探测。
# 默认 6 小时：日常使用中该端点几乎不可能突然恢复，过短只会白付探测成本。
SEARCH_BLOCK_TTL = float(os.environ.get("RRDY_SEARCH_BLOCK_TTL", 6 * 3600))
SEARCH_BLOCK_MARKER = "_search_endpoint_blocked.json"


def _effective_deadline(budget):
    """合并「全局预算」与「阶段预算」，返回绝对时间上限（可能为 None）。

    不修改全局变量，因而并发调用线程安全。
    """
    d = DEADLINE_AT
    if budget and budget > 0:
        b = time.monotonic() + budget
        d = b if d is None else min(d, b)
    return d

# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
CACHE_DIR = os.environ.get("RRDY_CACHE_DIR") or os.path.join(
    tempfile.gettempdir(), "rrdynb_cache"
)
OK_TTL = int(os.environ.get("RRDY_OK_TTL", 6 * 3600))     # 200 响应缓存 6 小时
NEG_TTL = int(os.environ.get("RRDY_NEG_TTL", 600))        # 非 200 响应缓存 10 分钟

_CACHE_ON = True


def cache_path(url):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, h + ".json")


def cache_read(url):
    """返回缓存对象 {ts, status, text}，无缓存或损坏返回 None。"""
    if not _CACHE_ON:
        return None
    try:
        with open(cache_path(url), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_write(url, status, text):
    if not _CACHE_ON:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = cache_path(url)
        tmp = "{}.{}.tmp".format(p, uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "status": status, "text": text}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def cache_clear():
    n = 0
    try:
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


# --------------------------------------------------------------------------
# 计时统计
# --------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.requests = 0
        self.cache_hits = 0
        self.stale_fallbacks = 0
        self.net_seconds = 0.0

    def as_dict(self):
        return {
            "network_requests": self.requests,
            "cache_hits": self.cache_hits,
            "stale_fallbacks": self.stale_fallbacks,
            "network_seconds": round(self.net_seconds, 2),
        }


STATS = Stats()

# 是否展示形态异常的链接（默认隐藏，--keep-invalid 打开）
SHOW_INVALID = False


def split_invalid(links):
    """拆分 (可用, 形态异常)。形态异常指分享 ID 被上游截断等原因导致的死链。"""
    bad = [l for l in links if l.get("invalid")]
    if SHOW_INVALID:
        return list(links), []
    return [l for l in links if not l.get("invalid")], bad


# --------------------------------------------------------------------------
# 网盘域名识别
# --------------------------------------------------------------------------
PAN_PROVIDERS = [
    (("pan.baidu.com", "yun.baidu.com"), "百度网盘"),
    (("pan.quark.cn",), "夸克网盘"),
    (("pan.xunlei.com",), "迅雷云盘"),
    (("aliyundrive.com", "alipan.com", "www.alipan.com"), "阿里云盘"),
    (("115.com", "115cdn.com", "anxia.com", "115pan.com"), "115网盘"),
    (("123pan.com", "123684.com", "123865.com", "123912.com", "123pan.cn"), "123网盘"),
    (("lanzou", "lanzn.com", "lanzo"), "蓝奏云"),
    (("cloud.189.cn",), "天翼云盘"),
    (("caiyun.139.com",), "移动云盘"),
    (("pan.wo.cn",), "联通云盘"),
    (("weiyun.com",), "腾讯微云"),
    (("uc.cn",), "UC网盘"),
    (("ctfile.com", "545c.com"), "城通网盘"),
    (("mypikpak.com", "pikpak"), "PikPak"),
    (("drive.google.com",), "Google Drive"),
    (("1drv.ms", "onedrive.live.com"), "OneDrive"),
    (("mediafire.com",), "MediaFire"),
]

# 站点通用推广位 / 混淆链接，需剔除
DECOY_HREF_PARTS = (
    "pan.baidu.com/download",
    "/download/",
)
DECOY_TEXT_PARTS = (
    "离线下载", "迅雷5.8", "utorrent", "bitcomet",
    # 早期的「客户端」会被误杀——正文里「夸克网盘客户端」也能是有效锚文本，
    # 故收窄为明确的下载引导词
    "客户端下载", "电脑客户端", "手机客户端", "软件下载",
)


def provider_of(url):
    """返回该 URL 对应的网盘名称，非网盘链接返回 None。"""
    low = url.lower()
    for domains, name in PAN_PROVIDERS:
        for d in domains:
            if d in low:
                return name
    return None


def is_decoy(href, text):
    low_href = href.lower()
    for p in DECOY_HREF_PARTS:
        if p in low_href:
            return True
    low_text = (text or "").lower()
    for p in DECOY_TEXT_PARTS:
        if p.lower() in low_text:
            return True
    return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(url, referer=None, retries=None, timeout=None, budget=None):
    """带重试的 GET，返回 (status, text)。

    重试策略（关键性能优化）：
      - 4xx（403 CF 拦截 / 404 不存在）属确定性错误，立即返回，不重试。
      - 网络异常（超时 / 连接重置 / SSL 握手失败）与 5xx 才重试。
    站点响应不稳定，对可恢复错误保留退避重试。

    retries / timeout 为 None 时在**调用时**读取模块全局值，
    以便 CLI 的 --timeout / --retries 生效。

    预算（budget / DEADLINE_AT）说明：urllib 的 timeout 是**套接字级**超时，
    域名解析到多个 IP 时 socket.create_connection 会逐地址尝试，实际阻塞可达
    timeout×地址数。故启用预算时按地址数均分剩余预算，使总耗时可控。
    """
    if retries is None:
        retries = DEFAULT_RETRIES
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    last = None
    dl = _effective_deadline(budget)
    n_addr = _addr_count(url)
    for attempt in range(max(1, retries)):
        # 预算控制：把单次超时压到不超过「剩余预算 ÷ 地址数」
        eff_timeout = timeout
        if dl is not None:
            remain = dl - time.monotonic()
            if remain <= 0:
                break
            eff_timeout = max(1.0, min(timeout, remain / max(1, n_addr)))
        headers = dict(COMMON_HEADERS)
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        try:
            with netlib.open_url(req, timeout=eff_timeout) as resp:
                raw = resp.read()
                enc = resp.headers.get("Content-Encoding", "")
                if "gzip" in enc:
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                return resp.status, decode(raw)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            last = (e.code, decode(body))
            # 4xx 确定性错误：重试无意义，直接返回（搜索被 CF 拦截即走此分支）
            if 400 <= e.code < 500:
                return last
        except Exception as e:  # 超时 / 连接重置 / SSL 握手失败等
            last = (0, str(e))
        if attempt < max(1, retries) - 1:
            nap = min(1.0 * (attempt + 1), 2.0)
            if dl is not None and time.monotonic() + nap >= dl:
                break
            time.sleep(nap)
    return last if last else (0, "")


_ADDR_CACHE = {}


def _addr_count(url):
    """该 URL 对应主机的 A/AAAA 记录数（用于预算均分）。失败按 1 计。"""
    try:
        host = urllib.parse.urlparse(url).hostname
    except Exception:
        return 1
    if not host:
        return 1
    if host in _ADDR_CACHE:
        return _ADDR_CACHE[host]
    n = 1
    try:
        import socket

        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = {i[4][0] for i in infos}
        n = max(1, len(ips))
    except Exception:
        n = 1
    _ADDR_CACHE[host] = n
    return n


def fetch(url, referer=None, retries=None, timeout=None, budget=None,
          use_cache=True, refresh=False):
    """带缓存的抓取，返回 (status, text)。命中缓存则不产生网络请求。

    兜底：网络抓取失败（0/非 200）但本地存有**已过期**的成功副本时，
    回退到该副本，避免因站点抖动导致结果为空（stale-while-error）。
    """
    if use_cache and not refresh:
        obj = cache_read(url)
        if obj is not None:
            ttl = OK_TTL if obj.get("status") == 200 else NEG_TTL
            if time.time() - obj.get("ts", 0) <= ttl:
                STATS.cache_hits += 1
                return obj.get("status", 0), obj.get("text", "")

    t0 = time.time()
    status, text = http_get(
        url, referer=referer, retries=retries, timeout=timeout, budget=budget
    )
    STATS.requests += 1
    STATS.net_seconds += time.time() - t0

    if status == 200 and text:
        if use_cache:
            cache_write(url, status, text)
        return status, text

    # 抓取失败：优先回退到已过期的成功副本
    if use_cache:
        obj = cache_read(url)
        if obj is not None and obj.get("status") == 200 and obj.get("text"):
            STATS.stale_fallbacks += 1
            return 200, obj["text"]
        # 仅缓存确定性 4xx（403 CF 拦截 / 404 不存在）。
        # 网络级失败（status 0）与 5xx 不缓存，否则站点瞬时抖动会被固化，
        # 导致后续 10 分钟内不再尝试联网。
        if 400 <= status < 500:
            cache_write(url, status, text)
    return status, text


def decode(raw):
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


# --------------------------------------------------------------------------
# Cloudflare 挑战识别
# --------------------------------------------------------------------------
# 注意：Cloudflare 会把 challenge-platform 脚本注入到**所有**页面，
# 因此不能用它做判定，否则正常页面会被全部误判。只用干扰页独有的特征。
CHALLENGE_MARKERS = (
    "window._cf_chl_opt",
    "cf_chl_rt_tk",
    "Enable JavaScript and cookies to continue",
    "正在进行安全验证",
    "正在验证您是否是自动程序",
)
CHALLENGE_TITLE_RE = re.compile(r"<title>\s*(Just a moment|请稍候)", re.I)


def looks_like_challenge(html):
    if not html:
        return False
    if any(m in html for m in CHALLENGE_MARKERS):
        return True
    # 标题匹配 + 页面极短（正常的影视页都有几十 KB 正文）
    if CHALLENGE_TITLE_RE.search(html) and len(html) < 40000:
        return True
    return False


# --------------------------------------------------------------------------
# 解析：搜索结果
# --------------------------------------------------------------------------
ARTICLE_RE = re.compile(
    r"href=[\"']("
    r"(?:https?://www\.rrdynb\.com)?"
    r"/(?:movie|dianshiju|zongyi|dongman|othera)/\d{4}/\d{4}/\d+\.html"
    r"|/(?:plus/view\.php\?aid=\d+)"
    r")[\"']",
    re.I,
)

TAG_RE = re.compile(r"<[^>]+>")
FONT_HL_RE = re.compile(r"<font[^>]*color", re.I)


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def _unescape(s):
    import html as _html

    return _html.unescape(s)


def parse_search_results(html):
    """
    通用解析：抓取所有指向文章详情页的链接，取锚文本为标题。
    兼容 DedeCMS 搜索页 / 列表页两种模板。
    """
    results = []
    seen = set()
    for m in ARTICLE_RE.finditer(html):
        href = _unescape(m.group(1))
        if href.startswith("/"):
            href = BASE + href
        href = href.split("#")[0]

        # 取 <a ...> ... </a> 的锚文本
        # 注意：必须用 -1 作为「未找到」哨兵。rfind 可能返回 0（<a> 位于串首），
        # 若按 `> 0` 判断会漏掉该结果。
        open_idx = html.rfind("<a", 0, m.start())
        close_idx = html.find("</a>", m.end())
        block = html[m.start(): close_idx + 4] if close_idx != -1 else html[m.start(): m.start() + 400]
        anchor_html = html[open_idx: close_idx + 4] if open_idx != -1 and close_idx != -1 else ""

        title = strip_tags(anchor_html)
        if not title or len(title) < 2:
            tm = re.search(r'title=[\"\']([^\"\']{2,200})[\"\']', block)
            if tm:
                title = strip_tags(_unescape(tm.group(1)))
        title = _unescape(title)
        # 去掉站点后缀噪音
        title = re.sub(r"\s*免费电影下载\s*$", "", title).strip()
        title = re.sub(r"\s*_\s*人人电影网.*$", "", title).strip()

        if not title or len(title) < 2:
            continue
        if title in seen:
            continue
        seen.add(title)

        results.append(
            {
                "title": title,
                "url": href,
                "aid": _aid_of(href),
                "highlight": bool(FONT_HL_RE.search(block)),
            }
        )
    return results


def _aid_of(url):
    m = re.search(r"/view\.php\?aid=(\d+)", url) or re.search(r"/(\d+)\.html$", url)
    return int(m.group(1)) if m else None


def rank_results(results, keyword):
    """按关键词匹配度排序：命中高亮 > 标题包含关键词 > 字符重合度。"""
    kw = (keyword or "").strip()

    def score(r):
        s = 0
        if r.get("highlight"):
            s += 100
        t = r["title"]
        if kw and kw in t:
            s += 60
        # 关键词逐字命中率
        if kw:
            hit = sum(1 for ch in set(kw) if ch in t)
            s += int(30 * hit / max(1, len(set(kw))))
        # 标题越短往往越精确
        s -= min(len(t) // 12, 8)
        return s

    return sorted(results, key=score, reverse=True)


# --------------------------------------------------------------------------
# 解析：文章页网盘链接
# --------------------------------------------------------------------------
ANCHOR_RE = re.compile(r"<a\b([^>]*?)>(.*?)</a>", re.I | re.S)
HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.I)


def parse_pan_links(html):
    """从文章页提取网盘链接（含提取码）。"""
    found = []
    seen_href = set()

    for m in ANCHOR_RE.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        hm = HREF_RE.search(attrs)
        if not hm:
            continue
        href = _unescape(hm.group(1)).strip()
        if not href.lower().startswith(("http://", "https://", "//")):
            continue
        if href.startswith("//"):
            href = "https:" + href

        provider = provider_of(href)
        if not provider:
            continue

        text = strip_tags(inner)
        if is_decoy(href, text):
            continue

        # 归一化去重（同一个分享链接可能被写多次）
        canon = href.split("#")[0]
        if canon in seen_href:
            continue
        seen_href.add(canon)

        # 提取码：优先 URL 查询参数，其次锚点后 220 字符的正文
        pwd = ""
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            for k in ("pwd", "password", "passcode"):
                if q.get(k):
                    pwd = q[k][0]
                    break
        except Exception:
            pass

        if not pwd:
            tail = html[m.end(): m.end() + 400]
            tail_text = strip_tags(tail)
            pm = re.search(r"(?:提取码|密码|访问码|提取密码)\s*[:：]?\s*([A-Za-z0-9]{3,12})", tail_text)
            if pm:
                pwd = pm.group(1)

        # 锚文本可能是空白（图标链接），退回上下文里的“资源：xxx网盘”
        label = text
        if not label or len(label) > 20:
            head_text = strip_tags(html[max(0, m.start() - 120): m.start()])
            lm = re.search(r"资源\s*[:：]\s*$", head_text)
            label = provider if lm else (text or provider)

        ok, why = linklib.validate(href)
        found.append(
            {
                "provider": provider,
                "label": label or provider,
                "url": href,
                "pwd": pwd,
                "has_pwd_in_url": bool(pwd and pwd in href),
                "invalid": "" if ok else why,
            }
        )

    # 输出顺序：按域名分组稳定，保留页面出现顺序
    return found


def parse_article_meta(html):
    """提取标题 / 年份 / 关键词 / 又名等元信息，便于用户确认命中。"""
    meta = {}
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        t = strip_tags(m.group(1))
        t = re.sub(r"_[^_]*人人电影网.*$", "", t).strip()
        meta["title"] = _unescape(t)
    m = re.search(r'<meta\s+name=["\']keywords["\']\s+content=["\']([^"\']*)', html, re.I)
    if m:
        meta["keywords"] = _unescape(m.group(1))[:300]
    m = re.search(r"又名\s*[:：]\s*([^<]{0,200})", html)
    if m:
        meta["alias"] = strip_tags(m.group(1)).rstrip('"\\/ ').strip()
    # 作品年份写在标题里：`死亡笔记(2006)_人人电影网`。此前 docstring 声称会提取
    # 「年份」，实现里却没有——而同一部片名常有多个年份的版本，这是确认命中的
    # 关键信息，故补上。
    ym = re.search(r"[（(](\d{4})[)）]", meta.get("title") or "")
    if ym:
        meta["year"] = ym.group(1)
    return meta


# --------------------------------------------------------------------------
# 业务动作
# --------------------------------------------------------------------------
def build_search_url(keyword, pagesize=11):
    return "{}?q={}&pagesize={}".format(
        SEARCH_ENDPOINT, urllib.parse.quote(keyword), pagesize
    )


def _search_block_path():
    return os.path.join(CACHE_DIR, SEARCH_BLOCK_MARKER)


def search_endpoint_blocked():
    """搜索端点是否被判定为「近期不可用」。"""
    if not _CACHE_ON:
        return False
    try:
        with open(_search_block_path(), "r", encoding="utf-8") as f:
            obj = json.load(f)
        return (time.time() - obj.get("ts", 0)) <= SEARCH_BLOCK_TTL
    except Exception:
        return False


def mark_search_endpoint_blocked():
    if not _CACHE_ON:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = _search_block_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time()}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def clear_search_block_marker():
    try:
        os.remove(_search_block_path())
    except Exception:
        pass


def do_search(keyword, pagesize=11, use_cache=True, refresh=False, force=False):
    url = build_search_url(keyword, pagesize)

    # 端点近期已判定不可用：直接给兜底地址，省去 10s 级探测成本
    if not force and use_cache and search_endpoint_blocked():
        return {
            "status": "cf_blocked",
            "search_url": url,
            "results": [],
            "fallback": True,
            "shortcut": True,
        }

    status, html = fetch(
        url,
        referer=BASE + "/index.html",
        retries=SEARCH_RETRIES,
        timeout=SEARCH_TIMEOUT,
        budget=SEARCH_BUDGET,
        use_cache=use_cache,
        refresh=refresh,
    )

    if status == 403 or looks_like_challenge(html):
        mark_search_endpoint_blocked()
        return {"status": "cf_blocked", "search_url": url, "results": [], "fallback": True}
    if status != 200 or not html:
        # 网络不可达（status 0）与 HTTP 异常同样应走 WebFetch 兜底：
        # 服务端抓取走的是另一条网络路径，通常能成功。
        mark_search_endpoint_blocked()
        return {
            "status": "error",
            "http_status": status,
            "search_url": url,
            "results": [],
            "fallback": True,
        }

    # 搜索接口可用，清除标记
    clear_search_block_marker()

    results = rank_results(parse_search_results(html), keyword)
    if not results:
        return {"status": "empty", "search_url": url, "results": []}
    return {"status": "ok", "search_url": url, "results": results}


def normalize_article_url(target):
    t = target.strip()
    if t.isdigit():
        return "{}/plus/view.php?aid={}".format(BASE, t)
    if t.startswith("/"):
        return BASE + t
    if not t.startswith("http"):
        return BASE + "/" + t.lstrip("/")
    return t


def do_links(target, referer=None, use_cache=True, refresh=False,
             retries=None, timeout=None):
    url = normalize_article_url(target)
    status, html = fetch(
        url,
        referer=referer or BASE + "/index.html",
        retries=retries if retries is not None else DEFAULT_RETRIES,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        budget=PAGE_BUDGET,
        use_cache=use_cache,
        refresh=refresh,
    )
    if status != 200 or not html or looks_like_challenge(html):
        return {"status": "error", "http_status": status, "url": url, "links": []}
    return {
        "status": "ok",
        "url": url,
        "article": parse_article_meta(html),
        "links": parse_pan_links(html),
    }


def do_links_many(targets, referer=None, use_cache=True, refresh=False, jobs=4):
    """并发抓取多个文章页，保持输入顺序返回。"""
    clean = [t.strip() for t in targets if t.strip()]
    if not clean:
        return []
    if len(clean) == 1:
        return [do_links(clean[0], referer, use_cache, refresh)]
    workers = max(1, min(jobs, len(clean)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(
            ex.map(
                lambda t: do_links(t, referer, use_cache, refresh),
                clean,
            )
        )


def do_resolve(keyword, top=3, pagesize=11, use_cache=True, refresh=False, jobs=4,
               force_search=False):
    s = do_search(keyword, pagesize, use_cache=use_cache, refresh=refresh,
                  force=force_search)
    if s["status"] != "ok":
        return {
            "status": s["status"],
            "search_url": s.get("search_url"),
            "http_status": s.get("http_status"),
            "fallback": s.get("fallback", False),
            "articles": [],
        }

    items = s["results"][:top]
    if not items:
        return {"status": "empty", "search_url": s["search_url"], "articles": []}

    def one(it):
        lk = do_links(it["url"], use_cache=use_cache, refresh=refresh)
        return {
            "title": it["title"],
            "url": it["url"],
            "article": lk.get("article", {}),
            "links": lk.get("links", []),
            "fetch_status": lk["status"],
        }

    workers = max(1, min(jobs, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        articles = list(ex.map(one, items))

    return {
        "status": "ok",
        "search_url": s["search_url"],
        "candidates": len(s["results"]),
        "articles": articles,
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def render_text_search(data):
    lines = []
    st = data["status"]
    if st == "cf_blocked":
        lines.append("!! 搜索接口被 Cloudflare 拦截（本机网络无法完成 JS 挑战）。")
        lines.append("!! 请改用 WebFetch 打开下面的搜索地址获取候选文章：")
        lines.append("   " + data["search_url"])
        return "\n".join(lines)
    if st == "empty":
        lines.append("未找到匹配的影视资源。")
        return "\n".join(lines)
    if st != "ok":
        if data.get("fallback") and data.get("search_url"):
            lines.append(
                "!! 搜索接口本地不可达（HTTP {}），请改用 WebFetch 打开下面的地址：".format(
                    data.get("http_status")
                )
            )
            lines.append("   " + data["search_url"])
            lines.append("!! 拿到候选文章后执行: rrdy.py links <文章URL>")
            return "\n".join(lines)
        lines.append("搜索失败：HTTP {}".format(data.get("http_status")))
        return "\n".join(lines)

    lines.append("搜索到 {} 个候选资源：".format(len(data["results"])))
    for i, r in enumerate(data["results"], 1):
        flag = " [高亮命中]" if r.get("highlight") else ""
        lines.append("  {}. {}{}".format(i, r["title"], flag))
        lines.append("     {}".format(r["url"]))
    return "\n".join(lines)


def render_text_resolve(data):
    lines = []
    st = data["status"]
    if st == "cf_blocked":
        lines.append("!! 搜索接口被 Cloudflare 拦截（本机网络无法完成 JS 挑战）。")
        lines.append("!! 请改用 WebFetch 打开下面的搜索地址获取候选文章，")
        lines.append("!! 再对每篇文章执行: rrdy.py links <文章URL>")
        lines.append("   " + data["search_url"])
        return "\n".join(lines)
    if st != "ok":
        if data.get("fallback") and data.get("search_url"):
            lines.append("!! 搜索接口本地不可达，请改用 WebFetch 打开下面的地址：")
            lines.append("   " + data["search_url"])
            lines.append("!! 拿到候选文章后执行: rrdy.py links <文章URL>")
            return "\n".join(lines)
        lines.append("未获取到结果（{}）。".format(st))
        return "\n".join(lines)

    any_link = False
    for a in data["articles"]:
        lines.append("=" * 62)
        year = (a.get("article") or {}).get("year") or ""
        lines.append("资源：{}{}".format(a["title"], "（{}）".format(year) if year else ""))
        lines.append("页面：{}".format(a["url"]))
        if not a["links"]:
            lines.append("  （该页未解析到可用网盘链接）")
            continue
        shown, bad = split_invalid(a["links"])
        if not shown:
            lines.append("  （该页的 {} 条链接形态异常，已隐藏；--keep-invalid 可显示）".format(len(bad)))
            continue
        any_link = True
        for lk in shown:
            pwd = "  提取码：{}".format(lk["pwd"]) if lk["pwd"] and not lk["has_pwd_in_url"] else ""
            lines.append("  • {:<8} {}{}".format(lk["provider"], lk["url"], pwd))
        if bad:
            lines.append("  （另有 {} 条形态异常链接已隐藏）".format(len(bad)))
    if not any_link:
        lines.append("")
        lines.append("提示：以上候选页均未解析到网盘链接，可尝试增加 --top 或换用更精确的名称。")
    return "\n".join(lines)


def render_text_links(data):
    if data["status"] != "ok":
        return "抓取失败：HTTP {}".format(data.get("http_status"))
    lines = []
    art = data.get("article", {})
    if art.get("title"):
        lines.append("标题：{}".format(art["title"]))
    lines.append("页面：{}".format(data["url"]))
    lines.append("-" * 50)
    if not data["links"]:
        lines.append("未解析到网盘链接。")
    shown, bad = split_invalid(data["links"])
    for lk in shown:
        pwd = "  提取码：{}".format(lk["pwd"]) if lk["pwd"] and not lk["has_pwd_in_url"] else ""
        lines.append("  • {:<8} {}{}".format(lk["provider"], lk["url"], pwd))
    if bad:
        lines.append("  （另有 {} 条形态异常链接已隐藏：{}；--keep-invalid 可显示）".format(
            len(bad), bad[0].get("invalid") or ""))
    return "\n".join(lines)


def render_text_links_many(data):
    parts = []
    for item in data.get("items", []):
        parts.append(render_text_links(item))
        parts.append("")
    return "\n".join(parts).rstrip()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    global _CACHE_ON, DEADLINE_AT, SHOW_INVALID
    p = argparse.ArgumentParser(
        prog="rrdy.py",
        description="人人电影网 (rrdynb.com) 影视资源搜索 + 网盘链接提取",
    )
    p.add_argument("--json", action="store_true", help="以 JSON 输出（也可写在子命令后）")
    p.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新抓取")
    p.add_argument("--clear-cache", action="store_true", help="清空本地缓存后退出")
    p.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数")
    p.add_argument("--retries", type=int, default=None, help="可恢复错误的重试次数")
    p.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE,
                   help="总耗时预算秒数（默认 {}；启用后单次超时按解析到的 IP 数均分，"
                        "使总耗时可控，超出即停止重试）".format(DEFAULT_DEADLINE))
    p.add_argument("--timing", action="store_true", help="在 stderr 输出网络请求耗时统计")
    p.add_argument("--cache-dir", default=None, help="自定义缓存目录")
    p.add_argument("--force-search", action="store_true",
                   help="忽略「搜索端点不可用」标记，强制探测搜索接口")
    p.add_argument("--search-budget", type=float, default=None,
                   help="搜索阶段耗时预算秒数（默认 {}）".format(SEARCH_BUDGET))
    p.add_argument("--page-budget", type=float, default=None,
                   help="单个文章页抓取预算秒数（默认 {}）".format(PAGE_BUDGET))
    p.add_argument("--keep-invalid", action="store_true",
                   help="显示形态异常的链接（默认隐藏分享 ID 被截断等死链）")
    p.add_argument("--proxy", default=None,
                   help="指定出网代理（如 127.0.0.1:7897）；direct 表示直连。"
                        "默认按 PANSEARCH_PROXY > proxy.conf > 系统环境变量 解析，"
                        "详见 netlib.py")

    sub = p.add_subparsers(dest="cmd", required=False)
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS：避免子解析器用默认值覆盖写在子命令之前的同名参数，
    # 从而让这些选项既可写在子命令前、也可写在子命令后。
    for opt, kw in (
        ("--json", dict(action="store_true")),
        ("--no-cache", dict(action="store_true")),
        ("--refresh", dict(action="store_true")),
        ("--clear-cache", dict(action="store_true")),
        ("--timing", dict(action="store_true")),
        ("--force-search", dict(action="store_true")),
        ("--timeout", dict(type=float)),
        ("--retries", dict(type=int)),
        ("--deadline", dict(type=float)),
        ("--search-budget", dict(type=float)),
        ("--page-budget", dict(type=float)),
        ("--keep-invalid", dict(action="store_true")),
        ("--cache-dir", dict()),
        ("--proxy", dict()),
    ):
        common.add_argument(opt, default=argparse.SUPPRESS, help=argparse.SUPPRESS, **kw)

    sp = sub.add_parser("search", help="搜索站内资源", parents=[common])
    sp.add_argument("keyword")
    sp.add_argument("--pagesize", type=int, default=11)

    sp = sub.add_parser("links", help="从文章页提取网盘链接", parents=[common])
    sp.add_argument("target", nargs="+", help="文章 URL 或 aid，可一次给多个")
    sp.add_argument("--referer", default=None)
    sp.add_argument("--jobs", type=int, default=4, help="并发数（多目标时生效）")

    sp = sub.add_parser("resolve", help="搜索并直接返回网盘链接", parents=[common])
    sp.add_argument("keyword")
    sp.add_argument("--top", type=int, default=3, help="检查前 N 个候选页（默认 3）")
    sp.add_argument("--pagesize", type=int, default=11)
    sp.add_argument("--jobs", type=int, default=4, help="候选页并发抓取数（默认 4）")

    args = p.parse_args(argv)

    cache_dir = getattr(args, "cache_dir", None)
    if cache_dir:
        globals()["CACHE_DIR"] = cache_dir
    if getattr(args, "no_cache", False):
        _CACHE_ON = False
    if getattr(args, "keep_invalid", False):
        SHOW_INVALID = True
    if getattr(args, "clear_cache", False):
        n = cache_clear()
        print("已清空缓存：{} 个文件（{}）".format(n, CACHE_DIR))
        return 0

    if not args.cmd:
        p.error("缺少子命令：search / links / resolve")

    # 选定出网出口。实测不同出口的成功率与耗时差异明显（详见 netlib.py 注释），
    # 因此不沿用 urllib 默认读取的环境变量，而是显式解析一次。
    proxy_desc = netlib.configure(getattr(args, "proxy", None))

    # 统一解析超时 / 重试：命令行参数优先，否则按命令取推荐值
    # 注意：do_search / do_links / do_resolve 在调用时读取这些模块全局值，
    # 因此在此处覆盖即可全局生效（含 resolve 内部派生的搜索与文章抓取）。
    user_timeout = getattr(args, "timeout", None)
    user_retries = getattr(args, "retries", None)
    if user_timeout is not None:
        globals()["DEFAULT_TIMEOUT"] = user_timeout
        globals()["SEARCH_TIMEOUT"] = user_timeout
    if user_retries is not None:
        globals()["DEFAULT_RETRIES"] = max(1, user_retries)
        globals()["SEARCH_RETRIES"] = max(1, user_retries)

    # 总耗时预算：超出后 http_get 停止重试并返回已有结果
    deadline = getattr(args, "deadline", DEFAULT_DEADLINE)
    if deadline and deadline > 0:
        DEADLINE_AT = time.monotonic() + deadline

    # 阶段预算可覆盖
    sb = getattr(args, "search_budget", None)
    if sb is not None:
        globals()["SEARCH_BUDGET"] = max(1.0, sb)
    pb = getattr(args, "page_budget", None)
    if pb is not None:
        globals()["PAGE_BUDGET"] = max(1.0, pb)

    use_cache = _CACHE_ON
    refresh = bool(getattr(args, "refresh", False))
    force_search = bool(getattr(args, "force_search", False))

    if args.cmd == "search":
        data, render = (
            do_search(args.keyword, args.pagesize, use_cache=use_cache,
                      refresh=refresh, force=force_search),
            render_text_search,
        )
    elif args.cmd == "links":
        if len(args.target) == 1:
            data, render = (
                do_links(args.target[0], args.referer, use_cache=use_cache, refresh=refresh),
                render_text_links,
            )
        else:
            items = do_links_many(
                args.target, args.referer, use_cache=use_cache, refresh=refresh, jobs=args.jobs
            )
            data = {"status": "ok", "items": items}
            render = render_text_links_many
    else:
        data, render = (
            do_resolve(
                args.keyword,
                args.top,
                args.pagesize,
                use_cache=use_cache,
                refresh=refresh,
                jobs=args.jobs,
                force_search=force_search,
            ),
            render_text_resolve,
        )

    if args.json:
        if isinstance(data, dict) and getattr(args, "timing", False):
            data["_stats"] = STATS.as_dict()
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))

    if getattr(args, "timing", False):
        st = STATS.as_dict()
        extra = " / 过期缓存兜底 {} 次".format(st["stale_fallbacks"]) if st["stale_fallbacks"] else ""
        sys.stderr.write(
            "[timing] 网络请求 {} 次 / 缓存命中 {} 次{} / 网络耗时 {:.2f}s\n".format(
                st["network_requests"], st["cache_hits"], extra, st["network_seconds"]
            )
        )
        sys.stderr.write("[timing] 出口：{}\n".format(netlib.describe()))
        for note in netlib.skipped():
            sys.stderr.write("[timing] 已跳过：{}\n".format(note))

    if data.get("status") == "cf_blocked":
        return 2
    if data.get("status") in ("ok",):
        return 0
    # 本地抓取受阻（CF 拦截 / 网络不可达）统一返回 2，提示调用方改用 WebFetch 兜底
    if data.get("fallback") and data.get("search_url"):
        return 2
    return 3 if data.get("status") == "empty" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
