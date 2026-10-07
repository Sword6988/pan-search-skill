#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PanSou 网盘搜索客户端（配合 rrdy.py 使用）。

PanSou 是开源的网盘资源聚合搜索服务（https://github.com/fish2018/pansou），
后端并发检索大量 Telegram 频道与站点插件，按网盘类型归并返回链接。
本脚本只做一件事：**按名称查询并整理网盘链接**，仅用 Python 标准库。

用法:
    python pansou.py search  "王者天下"                 # 查询，按网盘类型分组
    python pansou.py resolve "王者天下" --limit 8       # 整理输出（去重 + 相关性排序）
    python pansou.py health                            # 查看实例状态/插件/频道
    python pansou.py check "<网盘链接>" ...            # 批量检测链接是否有效

配置（命令行参数优先于环境变量）:
    PANSOU_API_BASE    实例地址，多个用英文逗号分隔（默认作者演示站）
    PANSOU_API_TOKEN   实例启用认证时的 JWT Token

退出码:
    0  成功
    2  服务不可达 / 认证失败（调用方应改用其它数据源兜底）
    3  查询成功但无结果
    1  其它错误
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import gzip
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netlib   # noqa: E402  统一出口（代理）选择
import linklib  # noqa: E402  链接清洗 / 去重指纹 / 形态校验

# --------------------------------------------------------------------------
# 常量与配置
# --------------------------------------------------------------------------

# 默认使用项目作者提供的公开演示实例。可用 PANSOU_API_BASE 指向自建或其它实例。
DEFAULT_API_BASES = [
    "https://so.252035.xyz",
]

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, identity",
}

# 服务端要并发检索上百个 TG 频道与插件。实测延迟：命中服务端缓存约 0.4s，
# 未命中时 12~45s 甚至更久，波动极大。因此套接字超时必须给足，
# 否则正常但较慢的响应会被误杀；总时长由 deadline 兜底。
DEFAULT_TIMEOUT = 55          # 单次请求套接字超时（秒）
DEFAULT_RETRIES = 2           # 超时后续试一次：服务端此时多半已缓存，二次通常很快
DEFAULT_DEADLINE = 120.0      # 整条命令总预算（秒）
SEARCH_BUDGET = 100.0         # 单次搜索总预算
HEALTH_BUDGET = 25.0

DEADLINE_AT = None            # 全命令绝对单调时钟上限；None 表示不限

# 网盘类型 -> 中文名
TYPE_NAMES = {
    "baidu": "百度网盘",
    "aliyun": "阿里云盘",
    "quark": "夸克网盘",
    "guangya": "光雅网盘",
    "tianyi": "天翼云盘",
    "uc": "UC网盘",
    "mobile": "移动云盘",
    "115": "115网盘",
    "pikpak": "PikPak",
    "xunlei": "迅雷云盘",
    "123": "123云盘",
    "magnet": "磁力链接",
    "ed2k": "电驴链接",
}

# 展示优先级：常用盘在前
TYPE_ORDER = [
    "quark", "aliyun", "baidu", "xunlei", "uc", "tianyi",
    "115", "123", "pikpak", "mobile", "guangya", "magnet", "ed2k",
]


def type_sort_key(t):
    try:
        return (0, TYPE_ORDER.index(t))
    except ValueError:
        return (1, t)


def type_name(t):
    return TYPE_NAMES.get(t, t or "未知")


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------

CACHE_DIR = os.environ.get("PANSOU_CACHE_DIR") or os.path.join(
    tempfile.gettempdir(), "pansou_cache"
)
OK_TTL = int(os.environ.get("PANSOU_OK_TTL", 2 * 3600))  # 成功结果缓存 2 小时
NEG_TTL = int(os.environ.get("PANSOU_NEG_TTL", 5 * 60))  # 确定性 4xx 缓存 5 分钟

_CACHE_ON = True


def cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def cache_read(key):
    p = cache_path(key)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_write(key, status, payload):
    try:
        with open(cache_path(key), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "status": status, "payload": payload}, f,
                      ensure_ascii=False)
    except Exception:
        pass


def cache_clear():
    n = 0
    if os.path.isdir(CACHE_DIR):
        for fn in os.listdir(CACHE_DIR):
            if fn.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, fn))
                    n += 1
                except Exception:
                    pass
    return n


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.requests = 0
        self.cache_hits = 0
        self.net_seconds = 0.0
        self.stale_fallbacks = 0

    def as_dict(self):
        return {
            "network_requests": self.requests,
            "cache_hits": self.cache_hits,
            "network_seconds": round(self.net_seconds, 2),
            "stale_fallbacks": self.stale_fallbacks,
        }


STATS = Stats()


def _emit_timing():
    st = STATS.as_dict()
    sys.stderr.write(
        "[timing] 网络请求 {} 次 / 缓存命中 {} 次 / 网络耗时 {:.2f}s"
        " / 过期兜底 {} 次\n".format(
            st["network_requests"], st["cache_hits"],
            st["network_seconds"], st["stale_fallbacks"],
        )
    )
    sys.stderr.write("[timing] 出口：{}\n".format(netlib.describe()))
    for note in netlib.skipped():
        sys.stderr.write("[timing] 已跳过：{}\n".format(note))


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def _effective_deadline(budget):
    """阶段预算与全局预算取较小值，返回绝对单调时钟上限。"""
    now = time.monotonic()
    cand = now + budget if budget else None
    if DEADLINE_AT is None:
        return cand
    if cand is None:
        return DEADLINE_AT
    return min(cand, DEADLINE_AT)


_ADDR_CACHE = {}


def _addr_count(url):
    """域名解析出的地址数。socket 级超时会对每个地址各生效一次，
    故启用预算时需按地址数均分，否则实际阻塞可达 timeout × 地址数。

    解析结果按主机缓存（DNS 在同一进程生命周期内基本不变），
    否则每次请求都要额外等一次 getaddrinfo。
    """
    try:
        host = urllib.parse.urlsplit(url).hostname
        if not host:
            return 1
        if host in _ADDR_CACHE:
            return _ADDR_CACHE[host]
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        n = max(1, len({i[4][0] for i in infos}))
        _ADDR_CACHE[host] = n
        return n
    except Exception:
        return 1


def decode(raw):
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def _looks_like_api_error(text):
    """判断 4xx 响应体是否来自 API 本身。

    本机 HTTPS 需经沙盒代理，代理偶发返回 HTML 错误页（如
    "400 The plain HTTP request was sent to HTTPS port"）。这类是**瞬时**故障，
    必须重试；只有 API 自身的 JSON 错误（参数为空、Token 无效）才是确定性的。
    """
    if not text:
        return False
    head = text.lstrip()[:1]
    return head in ("{", "[")


def http_request(url, method="GET", payload=None, token=None,
                 retries=None, timeout=None, budget=None, deadline=None):
    """带重试的 HTTP 请求，返回 (status, text)。

    - 4xx 中仅**响应体为 JSON**（确系 API 返回）的才短路，其余视为中间层瞬时故障重试。
    - 网络异常与 5xx 一律重试。
    - 预算控制：单次超时压到不超过「剩余预算 ÷ 地址数」。
    """
    if retries is None:
        retries = DEFAULT_RETRIES
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    dl = deadline
    if dl is None:
        dl = _effective_deadline(budget) if budget else DEADLINE_AT

    data = None
    headers = dict(COMMON_HEADERS)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token

    n_addr = _addr_count(url)
    last = None
    total = max(1, retries)
    for attempt in range(total):
        eff_timeout = timeout
        if dl is not None:
            remain = dl - time.monotonic()
            if remain <= 0:
                break
            eff_timeout = max(1.0, min(timeout, remain / max(1, n_addr)))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with netlib.open_url(req, timeout=eff_timeout) as resp:
                raw = resp.read()
                if "gzip" in (resp.headers.get("Content-Encoding") or ""):
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
            text = decode(body)
            last = (e.code, text)
            if 400 <= e.code < 500 and _looks_like_api_error(text):
                return last
        except Exception as e:
            last = (0, str(e))
        if attempt < total - 1:
            nap = min(1.0 * (attempt + 1), 3.0)
            if dl is not None and time.monotonic() + nap >= dl:
                break
            time.sleep(nap)
    return last if last else (0, "")


def cached_request(cache_key, url, method="GET", payload=None, token=None,
                   use_cache=True, refresh=False, budget=None):
    """带缓存的请求。命中新鲜缓存直接返回；网络失败回退到过期成功副本。"""
    if use_cache and not refresh:
        obj = cache_read(cache_key)
        if obj is not None:
            ttl = OK_TTL if obj.get("status") == 200 else NEG_TTL
            if time.time() - obj.get("ts", 0) <= ttl:
                STATS.cache_hits += 1
                return obj.get("status", 0), obj.get("payload")

    t0 = time.time()
    status, text = http_request(url, method=method, payload=payload, token=token,
                                budget=budget)
    STATS.requests += 1
    STATS.net_seconds += time.time() - t0

    if use_cache:
        if status == 200:
            cache_write(cache_key, status, text)
        elif 400 <= status < 500 and _looks_like_api_error(text):
            # 仅 API 自身的确定性错误写负缓存；中间层 HTML 错误页不写，
            # 否则一次代理抖动会被固化 5 分钟
            cache_write(cache_key, status, text)
        else:
            obj = cache_read(cache_key)
            if obj is not None and obj.get("status") == 200 and obj.get("payload"):
                STATS.stale_fallbacks += 1
                return 200, obj["payload"]
    return status, text


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def api_bases(cli_value=None):
    raw = cli_value or os.environ.get("PANSOU_API_BASE") or ",".join(DEFAULT_API_BASES)
    out = []
    for part in str(raw).split(","):
        part = part.strip().rstrip("/")
        if part:
            if not part.startswith("http"):
                part = "http://" + part
            out.append(part)
    return out


def api_token(cli_value=None):
    return (cli_value or os.environ.get("PANSOU_API_TOKEN") or "").strip() or None


def api_url(base, path):
    return base.rstrip("/") + path


def unwrap(text):
    """兼容两种响应结构：
      新: {"code":0,"message":"success","data":{...}}
      旧: {...} 直接返回
    返回 (data_dict, error_message)
    """
    try:
        obj = json.loads(text)
    except Exception:
        return None, "响应不是合法 JSON"
    if not isinstance(obj, dict):
        return None, "响应结构异常"
    if "data" in obj and isinstance(obj["data"], dict):
        code = obj.get("code")
        if code is not None and code != 0:
            return None, str(obj.get("message") or "服务返回 code={}".format(code))
        return obj["data"], None
    if "code" in obj and obj.get("code") not in (0, None) and "results" not in obj:
        return None, str(obj.get("message") or "服务返回错误")
    return obj, None


def normalize_search_data(data):
    """把各版本响应统一成 {total, merged_by_type, results}。"""
    if not isinstance(data, dict):
        return {"total": 0, "merged_by_type": {}, "results": []}
    merged = data.get("merged_by_type") or {}
    results = data.get("results") or []
    total = data.get("total")
    if total is None:
        total = sum(len(v) for v in merged.values() if isinstance(v, list)) or len(results)
    return {"total": total, "merged_by_type": merged, "results": results}


def do_health(base, token=None, use_cache=True, refresh=False):
    url = api_url(base, "/api/health")
    status, text = cached_request("health::" + base, url, token=token,
                                  use_cache=use_cache, refresh=refresh,
                                  budget=HEALTH_BUDGET)
    if status != 200 or not text:
        return {"status": "error", "http_status": status, "api_base": base}
    try:
        d = json.loads(text)
    except Exception:
        return {"status": "error", "http_status": status, "api_base": base}
    d = dict(d)
    d["status"] = d.get("status") or "ok"
    d["api_base"] = base
    return d


def do_search(keyword, base, token=None, res="merge", src="all", cloud_types=None,
              plugins=None, include=None, exclude=None, refresh_flag=False,
              use_cache=True, refresh=False, budget=None):
    """调用 /api/search。返回统一结构 dict。"""
    body = {"kw": keyword, "res": res or "merge"}
    if src and src != "all":
        body["src"] = src
    if cloud_types:
        body["cloud_types"] = cloud_types
    if plugins:
        body["plugins"] = plugins
    if include or exclude:
        filt = {}
        if include:
            filt["include"] = include
        if exclude:
            filt["exclude"] = exclude
        body["filter"] = filt
    if refresh_flag:
        body["refresh"] = True

    url = api_url(base, "/api/search")
    key = "search::{}::{}".format(base, json.dumps(body, ensure_ascii=False, sort_keys=True))
    status, text = cached_request(key, url, method="POST", payload=body, token=token,
                                  use_cache=use_cache, refresh=refresh,
                                  budget=budget if budget is not None else SEARCH_BUDGET)

    if status == 0:
        return {"status": "unreachable", "api_base": base, "keyword": keyword,
                "http_status": 0, "detail": (text or "")[:200]}
    if status in (401, 403) and _looks_like_api_error(text):
        # 仅有 JSON 错误体时才是真的认证问题
        return {"status": "auth_error", "api_base": base, "keyword": keyword,
                "http_status": status, "detail": (text or "")[:200]}
    if status in (403, 429):
        # 公开实例会限流，或在过载时直接拒绝；这属临时状态而非配置错误
        return {"status": "rate_limited", "api_base": base, "keyword": keyword,
                "http_status": status, "detail": (text or "")[:200]}
    if status != 200:
        return {"status": "error", "api_base": base, "keyword": keyword,
                "http_status": status, "detail": (text or "")[:200]}

    data, err = unwrap(text)
    if err:
        return {"status": "error", "api_base": base, "keyword": keyword,
                "http_status": status, "detail": err}

    norm = normalize_search_data(data)
    if norm["total"] == 0 and not norm["results"]:
        return {"status": "empty", "api_base": base, "keyword": keyword, **norm}
    return {"status": "ok", "api_base": base, "keyword": keyword, **norm}


def search_with_failover(keyword, bases, token=None, **kw):
    """多实例依次尝试，返回第一个成功的结果。"""
    tried = []
    last = None
    for b in bases:
        r = do_search(keyword, b, token=token, **kw)
        tried.append({"api_base": b, "status": r["status"]})
        last = r
        if r["status"] in ("ok", "empty"):
            r["tried"] = tried
            return r
    if last is None:
        last = {"status": "error", "keyword": keyword, "detail": "未配置任何实例"}
    last["tried"] = tried
    return last


# --------------------------------------------------------------------------
# 结果整理
# --------------------------------------------------------------------------

# 链接清洗 / 提取码识别 / 去重指纹统一由 linklib 提供（纯函数，可单测）
clean_url = linklib.clean_url
fingerprint = linklib.fingerprint


def collect_links(data, limit_per_type=None):
    """把 merged_by_type 摊平成 [{type, type_name, url, pwd, note, datetime, source}]。

    做了三件事：
      1. 清洗 URL（剥离粘在末尾的提取码，见 linklib.clean_url）；
      2. 按 linklib.fingerprint 去重——同时归并「同一分享的不同域名/子路径写法」
         （如 `.../s/AAA` 与 `.../s/AAA/folder/BBB`、`115cdn.com` 与 `115.com`），
         缺提取码的条目会从重复项里补上；
      3. 标注形态校验结果 `invalid`（空串=通过），由调用方决定是否剔除。
    """
    out = []
    index = {}
    merged = data.get("merged_by_type") or {}
    for t in sorted(merged.keys(), key=type_sort_key):
        items = merged.get(t) or []
        if not isinstance(items, list):
            continue
        kept = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            url, pwd = clean_url(it.get("url") or "")
            if not url:
                continue
            pwd = (it.get("password") or "").strip() or pwd
            note = (it.get("note") or "").strip()
            fp = fingerprint(url)
            old = index.get(fp)
            if old is not None:
                # 同资源的不同写法：补齐缺失字段，保留较短的那个 URL
                old["duplicates"] = old.get("duplicates", 0) + 1
                if not old.get("pwd") and pwd:
                    old["pwd"] = pwd
                if not old.get("note") and note:
                    old["note"] = note
                if len(url) < len(old["url"]):
                    old["url"] = url
                continue
            ok, why = linklib.validate(url, t)
            rec = {
                "type": t,
                "type_name": type_name(t),
                "url": url,
                "pwd": pwd,
                "note": note,
                "datetime": it.get("datetime") or "",
                "source": it.get("source") or "",
                "fingerprint": fp,
                "invalid": "" if ok else why,
                "duplicates": 0,
            }
            index[fp] = rec
            out.append(rec)
            kept += 1
            if limit_per_type and kept >= limit_per_type:
                break
    return out


def _fmt_date(s):
    if not s or s.startswith("0001-"):
        return "—"
    return s.replace("T", " ")[:16].replace("Z", "")


# 是否在输出里保留形态异常的链接（默认隐藏，见 linklib.validate）
SHOW_INVALID = False


def split_invalid(links):
    """拆分 (可用, 形态异常)。形态异常=分享 ID 被截断、URL 拼装错误等，
    点开必然是死链，默认不展示但会提示数量。"""
    bad = [l for l in links if l.get("invalid")]
    if SHOW_INVALID:
        return list(links), []
    return [l for l in links if not l.get("invalid")], bad


def relevance(note, keyword):
    """相关性打分：命中关键词的标题优先。"""
    if not note:
        return 0
    n = note.lower()
    k = (keyword or "").lower().strip()
    if not k:
        return 1
    if n == k:
        return 100
    if k in n:
        return 80
    # 按字符覆盖度给部分分（中文无空格分词，退化为字符命中率）
    hit = sum(1 for ch in set(k) if ch in n)
    ratio = hit / max(1, len(set(k)))
    if ratio >= 0.6:
        return int(40 * ratio)
    return int(10 * ratio)


def sort_links(links, keyword=None, by="relevance"):
    """排序。日期为 ISO 字符串，可直接字典序比较（未知日期以空串沉底）。"""
    if by == "date":
        return sorted(links, key=lambda x: (_fmt_date(x["datetime"]) == "—",
                                            _fmt_date(x["datetime"])), reverse=True)
    return sorted(links, key=lambda x: (
        -relevance(x["note"], keyword or ""),
        0 if _fmt_date(x["datetime"]) != "—" else 1,
        type_sort_key(x["type"]),
        x["note"],
    ))


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def render_text_search(data, limit=None, sort_by="relevance"):
    st = data.get("status")
    kw = data.get("keyword", "")
    if st == "unreachable":
        return ("!! 服务不可达：{}（{}）\n"
                "!! 若为代理 502 / 连接重置，属瞬时故障，重跑即可；\n"
                "!! 若持续失败，可设置 PANSOU_API_BASE 指向自建或其它实例。").format(
            data.get("api_base"), (data.get("detail") or "").strip()[:120])
    if st == "auth_error":
        return ("!! 实例已启用认证，需要 Token（HTTP {}）。\n"
                "!! 请设置环境变量 PANSOU_API_TOKEN 或加 --token。").format(
            data.get("http_status"))
    if st == "rate_limited":
        return ("!! 实例拒绝服务（HTTP {}），通常是公开实例限流或过载。\n"
                "!! 稍后重试即可；也可 --api 指向自建或其它实例。").format(
            data.get("http_status"))
    if st == "empty":
        return "未找到「{}」的网盘资源（实例 {}）。".format(kw, data.get("api_base"))
    if st != "ok":
        return "查询失败：HTTP {} {}（实例 {}）".format(
            data.get("http_status"), (data.get("detail") or "")[:120], data.get("api_base"))

    links = collect_links(data)
    links, bad = split_invalid(links)
    links = sort_links(links, kw, by=sort_by)
    if limit:
        links = links[:limit]
    if not links:
        return "未找到「{}」的网盘资源。".format(kw)

    lines = []
    lines.append("关键词：{}    结果：{} 条链接（实例命中 {}）".format(
        kw, len(links), data.get("api_base")))
    if bad:
        lines.append("（已隐藏 {} 条形态异常的链接，加 --keep-invalid 可显示）".format(len(bad)))
    lines.append("=" * 62)
    cur = None
    for l in links:
        if l["type"] != cur:
            cur = l["type"]
            lines.append("")
            lines.append("【{}】".format(l["type_name"]))
        lines.append("  • {}".format(l["url"]))
        meta = []
        if l["note"]:
            meta.append(l["note"])
        if l["pwd"]:
            meta.append("提取码 " + l["pwd"])
        d = _fmt_date(l["datetime"])
        if d != "—":
            meta.append(d)
        if l["source"]:
            meta.append(l["source"])
        if meta:
            lines.append("      " + " | ".join(meta))
    return "\n".join(lines).strip()


def render_text_resolve(data, limit=None, sort_by="relevance"):
    st = data.get("status")
    if st != "ok":
        return render_text_search(data, limit=limit, sort_by=sort_by)

    kw = data.get("keyword", "")
    links = collect_links(data)
    links, bad = split_invalid(links)
    links = sort_links(links, kw, by=sort_by)
    if limit:
        links = links[:limit]
    if not links:
        return "未找到「{}」的网盘资源。".format(kw)

    # 优先展示与关键词相关的条目；若全部低相关则照常展示（可能是不含关键词的合集）
    top_score = max(relevance(l["note"], kw) for l in links)
    note = ""
    if top_score <= 10:
        note = "（未出现与「{}」高度相关的标题，以下为实例返回的全部结果，请自行核对）".format(kw)

    lines = []
    lines.append("【{}】共 {} 条链接".format(kw, len(links)))
    if note:
        lines.append(note)
    if bad:
        lines.append("（已隐藏 {} 条形态异常的链接，加 --keep-invalid 可显示）".format(len(bad)))
    lines.append("=" * 62)
    for l in links:
        lines.append("")
        title = l["note"] or "（无标题）"
        lines.append("  {}  {}".format(title, (" | " + _fmt_date(l["datetime"]))
                                       if _fmt_date(l["datetime"]) != "—" else ""))
        lines.append("    {:<8} {}".format(l["type_name"], l["url"]))
        extra = []
        if l["pwd"]:
            extra.append("提取码 " + l["pwd"])
        if l["source"]:
            extra.append(l["source"])
        if extra:
            lines.append("    " + " | ".join(extra))
    return "\n".join(lines).strip()


def render_text_health(d):
    if d.get("status") == "error":
        return "实例不可达：{}（HTTP {}）".format(d.get("api_base"), d.get("http_status"))
    return (
        "实例：{}\n"
        "  状态：{}    认证：{}    插件：{} 个 / 频道：{} 个\n"
        "  插件：{}\n"
    ).format(
        d.get("api_base"),
        d.get("status"),
        "开启" if d.get("auth_enabled") else "关闭",
        d.get("plugin_count") or len(d.get("plugins") or []),
        d.get("channels_count") or len(d.get("channels") or []),
        ", ".join(d.get("plugins") or []) or "—",
    )


# --------------------------------------------------------------------------
# 链接检测（/api/check/links）
# --------------------------------------------------------------------------

def do_check_links(urls, base, token=None, use_cache=True, refresh=False, budget=None):
    body = {"links": [{"url": u} for u in urls]}
    url = api_url(base, "/api/check/links")
    key = "check::{}::{}".format(base, hashlib.sha1(
        json.dumps(urls, ensure_ascii=False).encode()).hexdigest())
    status, text = cached_request(key, url, method="POST", payload=body, token=token,
                                  use_cache=use_cache, refresh=refresh,
                                  budget=budget if budget is not None else SEARCH_BUDGET)
    if status in (404, 405, 501):
        # 实测：作者提供的公开演示实例未编译该路由（404 page not found），
        # 部分反代对未知路径回 405/501。这些不代表「链接有问题」，而是实例不支持。
        return {"status": "unsupported", "http_status": status, "api_base": base,
                "detail": "该实例未提供 /api/check/links 路由（HTTP {}）".format(status)}
    if status != 200 or not text:
        return {"status": "error", "http_status": status, "api_base": base}
    data, err = unwrap(text)
    if err:
        return {"status": "error", "detail": err, "api_base": base}
    return {"status": "ok", "api_base": base, "data": data}


def link_statuses(check_result):
    """把 /api/check/links 的响应解析成 {url: ('ok'|'invalid'|'unknown', 说明)}。

    不同版本字段名不一致，这里做宽松解析：只要能在条目里找到 url 字段，
    再按常见的布尔/字符串字段判断有效性；解析不出来的记为 unknown。
    """
    out = {}
    if not check_result or check_result.get("status") != "ok":
        return out
    data = check_result.get("data")
    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for k in ("results", "links", "data", "items"):
            if isinstance(data.get(k), list):
                rows = data[k]
                break
    for it in rows:
        if not isinstance(it, dict):
            continue
        u = it.get("url") or it.get("link") or ""
        if not u:
            continue
        ok_field = it.get("status", it.get("valid", it.get("ok")))
        if isinstance(ok_field, bool):
            state = "ok" if ok_field else "invalid"
        elif isinstance(ok_field, str):
            low = ok_field.lower()
            if low in ("ok", "valid", "success", "true", "alive", "normal"):
                state = "ok"
            elif low in ("invalid", "fail", "failed", "false", "dead", "expired", "error"):
                state = "invalid"
            else:
                state = "unknown"
        else:
            state = "unknown"
        note = str(it.get("message") or it.get("msg") or it.get("reason") or "")[:60]
        out[u] = (state, note)
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="pansou.py",
        description="PanSou 网盘搜索客户端：按名称查询并整理网盘链接",
    )
    p.add_argument("--api", default=None,
                   help="实例地址，多个用英文逗号分隔（或环境变量 PANSOU_API_BASE）")
    p.add_argument("--token", default=None, help="认证 Token（或环境变量 PANSOU_API_TOKEN）")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="以 JSON 输出（可写在子命令前或后）")
    p.add_argument("--no-cache", action="store_true", default=argparse.SUPPRESS,
                   help="本次禁用缓存")
    p.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS,
                   help="忽略本地缓存强制重取")
    p.add_argument("--timing", action="store_true", default=argparse.SUPPRESS,
                   help="在 stderr 输出网络耗时统计")
    p.add_argument("--deadline", type=float, default=argparse.SUPPRESS,
                   help="整条命令总预算秒数（默认 {}）".format(DEFAULT_DEADLINE))
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                   help="单次请求超时秒数")
    p.add_argument("--retries", type=int, default=argparse.SUPPRESS,
                   help="可恢复错误的重试次数")
    p.add_argument("--cache-dir", default=argparse.SUPPRESS, help="自定义缓存目录")
    p.add_argument("--keep-invalid", action="store_true", default=argparse.SUPPRESS,
                   help="显示形态异常的链接（默认隐藏分享 ID 被截断等死链）")
    p.add_argument("--clear-cache", action="store_true", default=argparse.SUPPRESS,
                   help="清空本地缓存后退出")
    p.add_argument("--proxy", default=argparse.SUPPRESS,
                   help="指定出网代理（如 127.0.0.1:7897）；direct 表示直连。"
                        "默认按 PANSEARCH_PROXY > proxy.conf > 系统环境变量 解析，"
                        "详见 netlib.py")

    sub = p.add_subparsers(dest="cmd", required=False)
    common = argparse.ArgumentParser(add_help=False)
    for opt, kw in (
        ("--json", dict(action="store_true")),
        ("--no-cache", dict(action="store_true")),
        ("--refresh", dict(action="store_true")),
        ("--timing", dict(action="store_true")),
        ("--deadline", dict(type=float)),
        ("--timeout", dict(type=float)),
        ("--retries", dict(type=int)),
        ("--cache-dir", dict()),
        ("--keep-invalid", dict(action="store_true")),
        ("--api", dict()),
        ("--token", dict()),
        ("--proxy", dict()),
    ):
        common.add_argument(opt, default=argparse.SUPPRESS, help=argparse.SUPPRESS, **kw)

    sp = sub.add_parser("search", parents=[common], help="按名称搜索")
    sp.add_argument("keyword", help="影视名称")
    sp.add_argument("--limit", type=int, default=0, help="最多显示多少条（0=不限）")
    sp.add_argument("--per-type", type=int, default=0, help="每种网盘最多显示多少条")
    sp.add_argument("--res", default="merge", choices=["merge", "results", "all"],
                    help="结果形态（默认 merge）")
    sp.add_argument("--src", default="all", choices=["all", "tg", "plugin"],
                    help="数据来源（默认 all）")
    sp.add_argument("--cloud-types", default=None,
                    help="限定网盘类型，逗号分隔，如 baidu,quark,aliyun")
    sp.add_argument("--plugins", default=None, help="限定插件，逗号分隔")
    sp.add_argument("--include", default=None, help="标题须包含的关键词，逗号分隔（OR）")
    sp.add_argument("--exclude", default=None, help="标题须排除的关键词，逗号分隔（OR）")
    sp.add_argument("--sort", default="relevance", choices=["relevance", "date"],
                    help="排序方式（默认按相关性）")
    sp.add_argument("--force-search", action="store_true",
                    help="请求服务端强制刷新（不使用服务端缓存）")

    rp = sub.add_parser("resolve", parents=[common], help="搜索并整理输出（推荐）")
    rp.add_argument("keyword", help="影视名称")
    rp.add_argument("--limit", type=int, default=12, help="最多显示多少条（默认 12）")
    rp.add_argument("--per-type", type=int, default=0, help="每种网盘最多显示多少条")
    rp.add_argument("--res", default="merge", choices=["merge", "results", "all"])
    rp.add_argument("--src", default="all", choices=["all", "tg", "plugin"])
    rp.add_argument("--cloud-types", default=None)
    rp.add_argument("--plugins", default=None)
    rp.add_argument("--include", default=None)
    rp.add_argument("--exclude", default=None)
    rp.add_argument("--sort", default="relevance", choices=["relevance", "date"])
    rp.add_argument("--force-search", action="store_true")

    hp = sub.add_parser("health", parents=[common], help="查看实例状态")
    hp.add_argument("--all", action="store_true", help="显示所有已配置实例")

    cp = sub.add_parser("check", parents=[common], help="批量检测网盘链接是否有效")
    cp.add_argument("urls", nargs="+", help="一个或多个网盘链接")

    return p


def _split_csv(v):
    if not v:
        return None
    return [x.strip() for x in str(v).split(",") if x.strip()]


def main(argv=None):
    global _CACHE_ON, DEADLINE_AT, CACHE_DIR, SHOW_INVALID

    p = build_parser()
    args = p.parse_args(argv)

    if getattr(args, "cache_dir", None):
        CACHE_DIR = args.cache_dir
    if getattr(args, "no_cache", False):
        _CACHE_ON = False
    if getattr(args, "keep_invalid", False):
        SHOW_INVALID = True
    if getattr(args, "clear_cache", False):
        n = cache_clear()
        print("已清空缓存：{} 个文件（{}）".format(n, CACHE_DIR))
        if not getattr(args, "cmd", None):
            return 0

    # 选定出网出口。实测不同出口的成功率与耗时差异明显（详见 netlib.py 注释），
    # 因此不沿用 urllib 默认读取的环境变量，而是显式解析一次。
    netlib.configure(getattr(args, "proxy", None))

    if getattr(args, "timeout", None) is not None:
        globals()["DEFAULT_TIMEOUT"] = args.timeout
    if getattr(args, "retries", None) is not None:
        globals()["DEFAULT_RETRIES"] = max(1, args.retries)
    dl = getattr(args, "deadline", None)
    deadline = dl if dl is not None else DEFAULT_DEADLINE
    if deadline and deadline > 0:
        DEADLINE_AT = time.monotonic() + deadline

    bases = api_bases(getattr(args, "api", None))
    token = api_token(getattr(args, "token", None))
    use_cache = _CACHE_ON
    refresh = bool(getattr(args, "refresh", False))
    as_json = bool(getattr(args, "json", False))
    want_timing = bool(getattr(args, "timing", False))

    cmd = getattr(args, "cmd", None)
    if not cmd:
        p.print_help()
        return 0 if getattr(args, "clear_cache", False) else 1

    if cmd == "health":
        targets = bases if getattr(args, "all", False) else bases[:1]
        out = []
        ok_any = False
        for b in targets:
            h = do_health(b, token=token, use_cache=use_cache, refresh=refresh)
            if h.get("status") != "error":
                ok_any = True
            if as_json:
                out.append(h)
            else:
                print(render_text_health(h))
        if as_json:
            print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False, indent=2))
        if want_timing:
            _emit_timing()
        return 0 if ok_any else 2

    if cmd == "check":
        r = do_check_links(args.urls, bases[0], token=token,
                           use_cache=use_cache, refresh=refresh)
        if as_json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            if r.get("status") != "ok":
                if r.get("status") == "unsupported":
                    print("当前实例不支持链接检测：{}".format(r.get("detail")))
                    print("（公开演示实例未编译该路由，自建实例才可用；"
                          "可用 --api 指向自建实例）")
                else:
                    print("检测失败：{}".format(r.get("detail") or r.get("http_status")))
            else:
                print(json.dumps(r["data"], ensure_ascii=False, indent=2))
        if want_timing:
            _emit_timing()
        return 0 if r.get("status") == "ok" else 2

    kw = args.keyword
    if not kw or not kw.strip():
        sys.stderr.write("错误：关键词不能为空\n")
        return 1

    r = search_with_failover(
        kw, bases, token=token,
        res=getattr(args, "res", "merge"),
        src=getattr(args, "src", "all"),
        cloud_types=_split_csv(getattr(args, "cloud_types", None)),
        plugins=_split_csv(getattr(args, "plugins", None)),
        include=_split_csv(getattr(args, "include", None)),
        exclude=_split_csv(getattr(args, "exclude", None)),
        refresh_flag=bool(getattr(args, "force_search", False)),
        use_cache=use_cache,
        refresh=refresh,
    )

    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "search":
        print(render_text_search(r, limit=getattr(args, "limit", 0) or None,
                                 sort_by=getattr(args, "sort", "relevance")))
    else:
        print(render_text_resolve(r, limit=getattr(args, "limit", None),
                                  sort_by=getattr(args, "sort", "relevance")))

    if want_timing:
        _emit_timing()

    st = r.get("status")
    if st == "ok":
        return 0
    if st == "empty":
        return 3
    if st in ("unreachable", "auth_error", "rate_limited"):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
