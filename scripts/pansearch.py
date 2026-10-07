#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一网盘资源搜索入口：按名称查询，合并多个数据源返回网盘链接。

数据源
------
  PanSou （默认主源）  聚合 TG 频道 + 站点插件，覆盖最广，返回结构化多网盘链接。
  人人电影网（补充源）  文章页正文里的网盘链接，常含阿里云盘等 PanSou 缺漏的盘。

两个数据源**并行**查询，结果按 linklib 指纹去重合并，不会叠加等待时间。

关于人人电影网（重要）
----------------------
该站的搜索接口 `/plus/search.php` 受 Cloudflare 托管挑战保护，而本机
（实测：直连与走 Clash 均）**无法访问 challenges.cloudflare.com**，
因此任何本地客户端都无法通过挑战，加代理也无效。可行路径只有两条：

  1. 由支持服务端抓取的调用方（如 WorkBuddy 的 WebFetch）打开搜索地址，
     拿到候选文章 URL 后回填：`--rrdynb-articles "<url1>,<url2>"`
  2. 跳过该源，只用 PanSou（多数场景结果已足够）

用法:
    python pansearch.py "王者天下"                  # 双源合并搜索
    python pansearch.py "死亡笔记" "我不是大师"      # 一次查多个名称
    python pansearch.py "王者天下" --only pansou     # 只用 PanSou
    python pansearch.py "王者天下" --json            # 机器可读
    python pansearch.py "王者天下" --refresh --timing
    python pansearch.py "王者天下" --verify 6        # 校验前 6 条链接是否有效
    python pansearch.py "王者天下" --sort date       # 按链接日期排序
    python pansearch.py "王者天下" --min-score 80    # 剔除弱相关结果
    python pansearch.py "王者天下" --no-group        # 不折叠同网盘的重复分享
    python pansearch.py "王者天下" -o out.md         # 导出 Markdown 清单
    python pansearch.py "唐朝诡事录" --include 全集,合集 --exclude 预告,花絮
    python pansearch.py "王者天下" --rrdynb-articles "https://www.rrdynb.com/movie/..."

退出码:
    0  有结果
    2  所有数据源均不可用（调用方应改用 WebFetch 兜底，见 SKILL.md）
    3  所有数据源均可用但无结果
    1  其它错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pansou   # noqa: E402
import rrdy     # noqa: E402
import netlib   # noqa: E402  统一出口（代理）选择
import linklib  # noqa: E402  链接清洗 / 去重指纹 / 形态校验

# 人人电影网的中文网盘名 -> PanSou 的类型键（个别叫法不同，需显式对齐）
NAME_ALIAS = {
    "123网盘": "123",
    "蓝奏云": "lanzou",
    "联通云盘": "liantong",
    "腾讯微云": "weiyun",
    "城通网盘": "ctfile",
    "Google Drive": "gdrive",
    "OneDrive": "onedrive",
    "MediaFire": "mediafire",
}
CN_TO_TYPE = {v: k for k, v in pansou.TYPE_NAMES.items()}
CN_TO_TYPE.update(NAME_ALIAS)

# 未在 PanSou 类型表中的源，给个展示名
EXTRA_TYPE_NAMES = {
    "lanzou": "蓝奏云", "liantong": "联通云盘", "weiyun": "腾讯微云",
    "ctfile": "城通网盘", "gdrive": "Google Drive", "onedrive": "OneDrive",
    "mediafire": "MediaFire",
}


def type_name_of(t):
    return pansou.TYPE_NAMES.get(t) or EXTRA_TYPE_NAMES.get(t) or t


def type_order_key(t):
    order = pansou.TYPE_ORDER
    if t in order:
        return (0, order.index(t))
    if t in EXTRA_TYPE_NAMES:
        return (1, sorted(EXTRA_TYPE_NAMES).index(t))
    return (2, t)


# 去重指纹统一由 linklib 提供：忽略 scheme/query/fragment 与子路径，
# 但**保留分享 ID 的大小写**（夸克/阿里的 ID 大小写敏感），
# 并把 aliyundrive↔alipan、115cdn↔115.com↔anxia 等别名主机折叠为一类。
url_key = linklib.fingerprint


def norm_dt(s):
    if not s or str(s).startswith("0001-"):
        return ""
    s = str(s).replace("T", " ").replace("Z", "")
    return s[:16]


def _csv(v):
    if not v:
        return None
    return [x.strip() for x in str(v).replace("\n", ",").split(",") if x.strip()]


# --------------------------------------------------------------------------
# 各数据源适配
# --------------------------------------------------------------------------

def from_pansou(keyword, args):
    result = pansou.search_with_failover(
        keyword,
        pansou.api_bases(getattr(args, "api", None)),
        token=pansou.api_token(getattr(args, "token", None)),
        res=getattr(args, "res", "merge") or "merge",
        src=getattr(args, "src", "all") or "all",
        cloud_types=_csv(getattr(args, "cloud_types", None)),
        plugins=_csv(getattr(args, "plugins", None)),
        include=_csv(getattr(args, "include", None)),
        exclude=_csv(getattr(args, "exclude", None)),
        refresh_flag=bool(getattr(args, "force_search", False)),
        use_cache=not getattr(args, "no_cache", False),
        refresh=getattr(args, "refresh", False),
    )
    st = result["status"]
    if st != "ok":
        return {"source": "pansou", "status": st, "links": [],
                "detail": result.get("detail") or "",
                "http_status": result.get("http_status"),
                "api_base": result.get("api_base"), "tried": result.get("tried")}
    links = pansou.collect_links(result)
    out = []
    for l in links:
        out.append({
            "type": l["type"],
            "url": l["url"],
            "pwd": l["pwd"],
            "note": l["note"],
            "datetime": norm_dt(l["datetime"]),
            "source": "pansou",
            "origin": l["source"],
            "invalid": l.get("invalid") or "",
            "duplicates": l.get("duplicates", 0),
        })
    return {"source": "pansou", "status": "ok", "links": out,
            "total_raw": result.get("total"), "api_base": result.get("api_base")}


# 人人电影网的文章链接恒定带 /YYYY/MMDD/，可据此补出发布日期
_ART_DATE_RE = re.compile(r"/(\d{4})/(\d{2})(\d{2})/")


def _date_from_article_url(u):
    """从文章 URL 路径里取发布日期：`/movie/2020/0216/8515.html` -> `2020-02-16 00:00`。

    此前该源所有条目的 datetime 恒为空，导致两个后果：排序里永远沉底、
    导出表格「更新」列全是「—」。语义上这是**帖子发布日**而非资源本身
    的更新日，输出时会如实说明，不冒充资源更新日期。
    """
    m = _ART_DATE_RE.search(u or "")
    if not m:
        return ""
    return "{}-{}-{} 00:00".format(m.group(1), m.group(2), m.group(3))


def _rrdynb_links_from_articles(arts):
    """把 rrdy 的文章结构摊平成链接条目。"""
    out = []
    for art in arts or []:
        title = (art.get("article") or {}).get("title") or art.get("title") or ""
        art_url = art.get("url") or ""
        dt = _date_from_article_url(art_url)
        for l in art.get("links") or []:
            prov = l.get("provider") or ""
            t = CN_TO_TYPE.get(prov) or prov
            label = (l.get("label") or "").strip()
            # rrdy 解析出的锚文本（如「夸克 1080P」）此前被丢弃；它常带这一条
            # 独有的规格信息，值得拼进标题（纯网盘名或「资源：xxx网盘」不算信息）。
            note = title
            if label and label not in (prov, title) and not label.startswith("资源："):
                note = "{} · {}".format(title, label) if title else label
            out.append({
                "type": t,
                "url": l.get("url") or "",
                "pwd": (l.get("pwd") or ""),
                "note": note,
                "datetime": dt,
                "source": "rrdynb",
                "origin": art_url,
                # rrdy 已经做过形态校验，带上结论；merge 会再统一校验一遍（幂等）
                "invalid": l.get("invalid") or "",
                "duplicates": 0,
            })
    return out


def from_rrdynb(keyword, args):
    # 回填模式：调用方（WebFetch 等）已拿到文章 URL，直接解析，不再尝试本地搜索
    provided = _csv(getattr(args, "rrdynb_articles", None))
    if provided:
        arts = rrdy.do_links_many(
            provided,
            use_cache=not getattr(args, "no_cache", False),
            refresh=getattr(args, "refresh", False),
            jobs=int(getattr(args, "jobs", 4) or 4),
        )
        links = _rrdynb_links_from_articles(arts)
        ok = [a for a in arts if a.get("status") == "ok"]
        if not ok:
            return {"source": "rrdynb", "status": "error", "links": [],
                    "detail": "回填的 {} 篇文章均解析失败".format(len(provided))}
        return {"source": "rrdynb", "status": "ok", "links": links,
                "candidates": len(provided), "provided": True}

    top = int(getattr(args, "top", 3) or 3)
    r = rrdy.do_resolve(
        keyword, top=top,
        use_cache=not getattr(args, "no_cache", False),
        refresh=getattr(args, "refresh", False),
        jobs=int(getattr(args, "jobs", 4) or 4),
        force_search=bool(getattr(args, "force_search", False)),
    )
    st = r["status"]
    if st != "ok":
        # 本地搜索受阻（CF 拦截或本机不可达）时，rrdy 会带上 search_url，
        # 归一为 needs_fallback，让调用方知道该走 WebFetch 兜底而不是重试。
        if st in ("cf_blocked", "error") and r.get("search_url"):
            st = "needs_fallback"
        return {"source": "rrdynb", "status": st, "links": [],
                "search_url": r.get("search_url"),
                "detail": r.get("http_status"),
                "blocked_reason": "cf_blocked" if r.get("status") == "cf_blocked" else "unreachable"}
    return {"source": "rrdynb", "status": "ok",
            "links": _rrdynb_links_from_articles(r.get("articles")),
            "candidates": r.get("candidates")}


# --------------------------------------------------------------------------
# 合并
# --------------------------------------------------------------------------

def _dt_num(x):
    """把 'YYYY-MM-DD HH:MM' 压成可比较的整数；缺失/未知日期记 0。"""
    d = (x.get("datetime") or "")
    try:
        return int(d.replace("-", "").replace(" ", "").replace(":", ""))
    except Exception:
        return 0


def _order_key(sort_by):
    """返回条目排序键。

    relevance（默认）：相关性 > 网盘优先级 > 新链接在前。
    date：有日期的在前、日期新的在前，无日期的沉底（相关性作为同日期内的次序）。

    注意 `--sort date` 曾长期是死参数（CLI 声明了但没人读），此处是唯一的
    排序真相来源，`merge()` 与 `_buckets()` 都从这里取键，避免两处不一致。
    """
    if sort_by == "date":
        return lambda x: (0 if _dt_num(x) else 1, -_dt_num(x), -x["score"],
                          type_order_key(x["type"]))
    return lambda x: (-x["score"], type_order_key(x["type"]), -_dt_num(x))


def merge(results, keyword, keep_invalid=False, sort_by="relevance", min_score=0):
    """按 linklib 指纹跨源去重，做统一的形态校验，再排序。

    返回 (items, dropped)：
      items   最终条目（已剔除形态异常项，除非 keep_invalid）
      dropped 被剔除的条目，含 url 与原因，用于如实告知用户
    """
    merged = {}
    order = []
    for res in results:
        if res.get("status") != "ok":
            continue
        for l in res.get("links") or []:
            if not l.get("url"):
                continue
            k = url_key(l["url"])
            if not k:
                continue
            if k not in merged:
                it = dict(l)
                it["sources"] = [l["source"]]
                it["duplicates"] = int(l.get("duplicates") or 0)
                merged[k] = it
                order.append(k)
            else:
                it = merged[k]
                if l["source"] not in it["sources"]:
                    it["sources"].append(l["source"])
                if not it.get("note") and l.get("note"):
                    it["note"] = l["note"]
                if not it.get("pwd") and l.get("pwd"):
                    it["pwd"] = l["pwd"]
                if not it.get("datetime") and l.get("datetime"):
                    it["datetime"] = l["datetime"]
                # 同资源的不同写法（/s/AAA 与 /s/AAA/folder/BBB）也算一次重复
                it["duplicates"] = it.get("duplicates", 0) + 1 + int(l.get("duplicates") or 0)
                if len(l["url"]) < len(it["url"]):
                    it["url"] = l["url"]

    items = [merged[k] for k in order]

    # 统一校验（PanSou 侧已在 collect_links 标注，rrdynb 侧在此补上）
    dropped = []
    kept = []
    for it in items:
        ok, why = linklib.validate(it["url"])
        it["invalid"] = "" if ok else why
        if ok or keep_invalid:
            kept.append(it)
        else:
            dropped.append({"url": it["url"], "type": it["type"],
                            "note": it.get("note") or "", "reason": why})
    items = kept

    for it in items:
        it["score"] = pansou.relevance(it.get("note") or "", keyword)

    # 本地相关性过滤：score 早就算出来了，此前只用于排序、不能过滤，
    # 导致搜「死亡笔记」也会带出只命中「笔记」两个字的条目。
    if min_score and min_score > 0:
        low = [it for it in items if it["score"] < min_score]
        if low:
            items = [it for it in items if it["score"] >= min_score]
            dropped = dropped + [
                {"url": it["url"], "type": it["type"], "note": it.get("note") or "",
                 "reason": "相关性 {} 低于阈值 {}".format(it["score"], min_score)}
                for it in low]

    items.sort(key=_order_key(sort_by))
    return items, dropped


# --------------------------------------------------------------------------
# 链接有效性检测（--verify）
# --------------------------------------------------------------------------

def verify_links(items, count, args):
    """抽查前 count 条 http(s) 链接的有效性，原地写入 it['alive']。

    注意：该能力依赖实例提供 /api/check/links。作者提供的公开演示实例
    实测返回 404（未编译该路由），此时返回 unsupported 并原样放行，
    不会因为实例不支持而让整条命令失败。
    """
    if count <= 0:
        return None
    targets = [it for it in items
               if (it.get("url") or "").lower().startswith(("http://", "https://"))]
    targets = targets[:count]
    if not targets:
        return None
    bases = pansou.api_bases(getattr(args, "api", None))
    r = pansou.do_check_links(
        [it["url"] for it in targets], bases[0],
        token=pansou.api_token(getattr(args, "token", None)),
        use_cache=not getattr(args, "no_cache", False),
        refresh=getattr(args, "refresh", False),
    )
    if r.get("status") != "ok":
        return r
    mapping = pansou.link_statuses(r)
    for it in targets:
        got = mapping.get(it["url"])
        if got:
            it["alive"] = got[0]
            if got[1]:
                it["alive_note"] = got[1]
    # 把统计塞进返回值：渲染层据此如实报告「检测了几条、几条失效」，
    # 此前只在失败时才有提示，检测成功反而完全静默。
    r["checked"] = len(targets)
    r["alive_ok"] = sum(1 for it in targets if it.get("alive") == "ok")
    r["alive_dead"] = sum(1 for it in targets if it.get("alive") == "invalid")
    r["alive_unknown"] = len(targets) - r["alive_ok"] - r["alive_dead"]
    return r


# --------------------------------------------------------------------------
# 渲染（文本 / Markdown）
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 同资源聚类
# --------------------------------------------------------------------------
# 标题归一化用的规则。设计原则：**只删确定无信息量的片段**，宁可少合并，
# 也不要把两部不同作品并成一条。
#   · 不含数字的短标签（【夸克】【TG】）整段删掉；
#     含数字的括号内容（(2006)、（第三季））保留 —— 年份/季数正是区分版本的依据。
#   · 规格词（4K/1080P/内封简中/全37集…）删掉，使「死亡笔记 4K 内封简中」与
#     「死亡笔记 全37集 日语中字」归到同一个键。
_TAG_RE = re.compile(r"[【\[（(](?![^】\]）)]{0,24}\d)[^】\]）)]{0,24}[】\]）)]")
_SPEC_RE = re.compile(
    r"(?:4k|8k|2160p|1080p|720p|hdr10\+?|hdr|dolby\s*vision|dv|remux|blu-?ray|bdrip|bd|"
    r"web-?dl|h\.?26[45]|hevc|avc|x26[45]|10bit|8bit|60fps|50fps|"
    r"国语|国配|粤语|日语|英语|中字|简中|繁中|简繁|内嵌|内封|外挂|生肉|原盘|"
    r"高码率|高帧率|未删减|加长版|导演剪辑版|修复版|无水印|"
    r"全集|完结|全\d+[集话]|\d+[集话]全|更新至\d+[集话]|第\d+季|s\d{1,2}e\d{1,3})",
    re.I)
_WS_RE = re.compile(r"[\s\-_·•~—–|/\\,，。.、:：;；!！?？'\"]+")


def title_key(note):
    """标题归一化，用作「同一网盘内是否为同一资源」的聚类键。"""
    s = (note or "").lower()
    s = _TAG_RE.sub("", s)
    s = _SPEC_RE.sub("", s)
    return _WS_RE.sub("", s)


def group_items(items):
    """把**同一网盘内**标题相同的多个分享折叠成一条，其余挂到 `variants`。

    为什么不跨网盘折叠：夸克与迅雷是两条各自可用的链接，用户需要分别看到；
    跨网盘合并还会把链接塞进错误的分组（分组是按代表条目的网盘类型分的）。

    标题归一化后长度 < 4 时不敢合并（如「4K」「全集」这类没信息量的标题），
    按 URL 各自独立。
    """
    gmap = {}
    order = []
    for it in items:
        tk = title_key(it.get("note") or "")
        key = (it["type"], tk) if len(tk) >= 4 else ("\x00solo", it["url"])
        g = gmap.get(key)
        if g is None:
            g = dict(it)
            g["variants"] = []
            gmap[key] = g
            order.append(key)
            continue
        if it["url"] == g["url"]:
            continue
        g["variants"].append({
            "url": it["url"],
            "pwd": it.get("pwd") or "",
            "datetime": it.get("datetime") or "",
            "sources": list(it.get("sources") or []),
        })
        g["duplicates"] = int(g.get("duplicates") or 0) + 1 + int(it.get("duplicates") or 0)
        # 只补日期，**不补提取码**：variants 是另一条分享，它的码对代表条目无效，
        # 补上去会让用户拿错码（各自的码仍显示在各自的 └ 行里）。
        if not g.get("datetime") and it.get("datetime"):
            g["datetime"] = it["datetime"]
        for s in it.get("sources") or []:
            if s not in g["sources"]:
                g["sources"].append(s)
    return [gmap[k] for k in order]


def _counts_by_type(items):
    c = {}
    for it in items:
        c[it["type"]] = c.get(it["type"], 0) + 1
    order = sorted(c.keys(), key=type_order_key)
    return " · ".join("{} {}".format(type_name_of(t), c[t]) for t in order)


def _buckets(items, per_type=None, sort_by="relevance"):
    buckets = {}
    for it in items:
        buckets.setdefault(it["type"], []).append(it)
    key = _order_key(sort_by)
    for arr in buckets.values():
        arr.sort(key=key)
        if per_type:
            del arr[per_type:]

    def bucket_key(t):
        arr = buckets[t]
        if sort_by == "date":
            return (-max(_dt_num(x) for x in arr), type_order_key(t))
        return (-max(x["score"] for x in arr), type_order_key(t))

    order = sorted(buckets.keys(), key=bucket_key)
    return buckets, order


def _alive_tag(it):
    a = it.get("alive")
    if a == "invalid":
        return "！链接已失效" + (("（{}）".format(it["alive_note"])) if it.get("alive_note") else "")
    if a == "ok":
        return "有效"
    return ""


def _short_origin(u):
    """把来源标识压短。

    rrdynb 的来源是完整文章 URL（`https://www.rrdynb.com/dongman/2022/0213/24202.html`），
    原先整条拼进元信息行会把行撑得很长；这里压成 `主机/末段`。
    PanSou 的来源是 TG 频道名（形如 `/melost`）并非 URL，原样保留。
    """
    raw = (u or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        return raw
    try:
        s = urllib.parse.urlsplit(raw)
    except Exception:
        return raw
    seg = (s.path or "").rstrip("/").rsplit("/", 1)[-1]
    host = s.netloc or ""
    if seg:
        return "{}/{}".format(host, seg)
    return host or raw


def _item_meta(it, src_names, with_origin=True):
    meta = []
    if it.get("note"):
        meta.append(it["note"])
    if it.get("pwd"):
        meta.append("提取码 " + it["pwd"])
    if it.get("datetime"):
        meta.append(it["datetime"])
    srcs = "+".join(src_names.get(s, s) for s in it.get("sources") or [])
    if srcs:
        meta.append(srcs)
    if it.get("duplicates"):
        meta.append("另有 {} 条相同资源".format(it["duplicates"]))
    tag = _alive_tag(it)
    if tag:
        meta.append(tag)
    if with_origin and it.get("origin"):
        meta.append(_short_origin(it["origin"]))
    return meta


def _verify_tail(vr):
    """把验链统计拼成「，X 条已失效、Y 条未能判定」这样的尾串。"""
    parts = []
    if vr.get("alive_dead"):
        parts.append("{} 条已失效".format(vr["alive_dead"]))
    if vr.get("alive_unknown"):
        parts.append("{} 条未能判定".format(vr["alive_unknown"]))
    return ("，" + "、".join(parts)) if parts else "，未发现失效"


def render(keyword, items, results, limit=None, per_type=None, dropped=None,
           verify_result=None, proxy_desc=None, sort_by="relevance"):
    lines = []
    unavail = [r for r in results
               if r.get("status") in ("unreachable", "auth_error", "rate_limited",
                                      "cf_blocked", "needs_fallback", "error")]
    src_names = {"pansou": "PanSou", "rrdynb": "人人电影网"}
    ok_names = [src_names.get(r["source"], r["source"]) for r in results
                if r.get("status") == "ok"]
    lines.append("【{}】共 {} 条链接（数据源：{}）".format(
        keyword, len(items), "、".join(ok_names) or "无可用数据源"))

    if items:
        lines.append("分布：" + _counts_by_type(items))
    if dropped:
        lines.append("（已剔除 {} 条形态异常的链接：分享 ID 被上游截断等，"
                     "加 --keep-invalid 可保留）".format(len(dropped)))
    if verify_result is not None:
        st = verify_result.get("status")
        if st == "ok":
            lines.append("（已用实例接口检测 {} 条链接：{} 条有效{}）".format(
                verify_result.get("checked", 0),
                verify_result.get("alive_ok", 0),
                _verify_tail(verify_result)))
        elif st == "unsupported":
            lines.append("（本次未做链接有效性检测：{}；该接口仅自建实例提供）".format(
                verify_result.get("detail") or "实例未提供 /api/check/links"))
        else:
            lines.append("（链接有效性检测未完成：HTTP {} —— 该接口通常仅自建实例提供，"
                         "也可能是一次瞬时网络故障；结果未做验链）".format(
                             verify_result.get("http_status")))
    if proxy_desc:
        lines.append("出口：" + proxy_desc)

    if not items:
        for r in results:
            st = r.get("status")
            nm = src_names.get(r["source"], r["source"])
            if st == "empty":
                lines.append("  · {}：无匹配结果".format(nm))
            elif st in ("cf_blocked", "needs_fallback"):
                why = ("搜索接口被 Cloudflare 拦截" if r.get("blocked_reason") == "cf_blocked"
                       else "本地搜索接口不可达（HTTP {}）".format(r.get("detail")))
                lines.append("  · {}：{}，请用 WebFetch 打开：{}".format(
                    nm, why, r.get("search_url") or ""))
            elif st == "auth_error":
                lines.append("  · {}：实例需要 Token（HTTP 401/403）".format(nm))
            elif st == "rate_limited":
                lines.append("  · {}：实例限流或过载（HTTP {}），稍后重试".format(
                    nm, r.get("http_status")))
            elif st == "unreachable":
                lines.append("  · {}：不可达（{}）".format(nm, (r.get("detail") or "")[:80]))
            else:
                lines.append("  · {}：失败（{}）".format(nm, st))
        return "\n".join(lines)

    buckets, order = _buckets(items, per_type, sort_by)

    lines.append("=" * 62)
    shown = 0
    for t in order:
        if limit and shown >= limit:
            break
        head_printed = False
        for it in buckets[t]:
            if limit and shown >= limit:
                break
            if not head_printed:
                lines.append("")
                lines.append("【{}】".format(type_name_of(t)))
                head_printed = True
            shown += 1
            u = it["url"]
            if len(u) > 110:
                u = u[:110] + "…（长链接已截断，完整值见 --json）"
            lines.append("  • {}".format(u))
            meta = _item_meta(it, src_names)
            if meta:
                lines.append("      " + " | ".join(meta))
            for v in it.get("variants") or []:
                vu = v["url"]
                if len(vu) > 110:
                    vu = vu[:110] + "…"
                vp = "  提取码 {}".format(v["pwd"]) if v.get("pwd") else ""
                lines.append("        └ {}{}".format(vu, vp))

    # 提示「还有多少条没显示」——只取决于真实截断，与数据源是否可用无关
    if shown < len(items):
        lines.append("")
        lines.append("（另有 {} 条未显示，可用 --limit / --per-type 调整）".format(
            len(items) - shown))

    if unavail:
        lines.append("")
        for r in unavail:
            nm = src_names.get(r["source"], r["source"])
            if r.get("status") in ("cf_blocked", "needs_fallback"):
                why = ("搜索接口被 Cloudflare 拦截（本机无法完成 JS 挑战）"
                       if r.get("blocked_reason") == "cf_blocked"
                       else "本地搜索接口不可达（HTTP {}）".format(r.get("detail")))
                lines.append("!! {}：{}。".format(nm, why))
                lines.append("   如需该源结果，先用 WebFetch 打开：{}".format(
                    r.get("search_url") or ""))
                lines.append("   拿到候选文章 URL 后回填："
                             "pansearch.py \"{}\" --rrdynb-articles \"<url1>,<url2>\"".format(
                                 keyword))
            elif r.get("status") == "auth_error":
                lines.append("!! {}：实例需要 Token，请设置 PANSOU_API_TOKEN 或加 --token".format(nm))
            elif r.get("status") == "rate_limited":
                lines.append("!! {}：实例限流或过载（HTTP {}），稍后重试即可".format(
                    nm, r.get("http_status")))
            elif r.get("status") == "error":
                lines.append("!! {}：查询出错（{}）".format(nm, (r.get("detail") or "")[:80]))
            else:
                lines.append("!! {}：不可达（{}）".format(nm, (r.get("detail") or "")[:80]))

    if dropped:
        lines.append("")
        lines.append("已剔除的形态异常链接：")
        for d in dropped[:10]:
            lines.append("  × {}（{}）".format(d["url"], d["reason"]))
        if len(dropped) > 10:
            lines.append("  × …… 其余 {} 条同类".format(len(dropped) - 10))
    return "\n".join(lines)


def render_markdown(keyword, items, results, dropped=None, per_type=None, limit=None,
                    sort_by="relevance"):
    """导出用 Markdown：按网盘分节，链接与提取码列成表格。"""
    src_names = {"pansou": "PanSou", "rrdynb": "人人电影网"}
    ok_names = [src_names.get(r["source"], r["source"]) for r in results
                if r.get("status") == "ok"]
    out = ["# 网盘资源：{}".format(keyword), ""]
    out.append("- 链接总数：{}".format(len(items)))
    out.append("- 数据源：{}".format("、".join(ok_names) or "无可用数据源"))
    if items:
        out.append("- 网盘分布：{}".format(_counts_by_type(items)))
    if dropped:
        out.append("- 已剔除形态异常链接：{} 条".format(len(dropped)))
    out.append("")
    out.append("> 仅聚合公开分享链接，资源版权归原权利人。链接有效性请自行确认。")
    out.append("")

    buckets, order = _buckets(items, per_type, sort_by)
    shown = 0
    for t in order:
        # 先算出这一节真正要输出的行，再决定是否打印表头——
        # 否则被 --limit 截断后会留下「有表头没内容」的空分节。
        rows = []
        for it in buckets[t]:
            if limit and shown >= limit:
                break
            shown += 1
            rows.append(it)
        if not rows:
            continue
        out.append("## {}".format(type_name_of(t)))
        out.append("")
        out.append("| # | 链接 | 提取码 | 标题 / 说明 | 更新 | 来源 |")
        out.append("|---|---|---|---|---|---|")
        for i, it in enumerate(rows, 1):
            url = it["url"]
            if len(url) > 200:
                url = url[:200] + "…"
            # 同网盘内的其它同资源分享：折在同一格里，用 <br> 分隔，不丢链接
            for v in it.get("variants") or []:
                vu = v["url"]
                if len(vu) > 200:
                    vu = vu[:200] + "…"
                if v.get("pwd"):
                    vu += "（提取码 {}）".format(v["pwd"])
                url += "<br>" + vu
            note = (it.get("note") or "").replace("|", "/").replace("\n", " ")
            if it.get("duplicates"):
                note += "（另有 {} 条相同资源）".format(it["duplicates"])
            tag = _alive_tag(it)
            if tag:
                note = (note + " " + tag).strip()
            srcs = "+".join(src_names.get(s, s) for s in it.get("sources") or [])
            out.append("| {} | {} | {} | {} | {} | {} |".format(
                i, url, it.get("pwd") or "—", note or "—",
                it.get("datetime") or "—", srcs))
        out.append("")
    if shown < len(items):
        out.append("> 以上为前 {} 条；如需完整清单请调大 --limit / --per-type。".format(shown))
        out.append("")
    if dropped:
        out.append("## 已剔除的形态异常链接")
        out.append("")
        for d in dropped:
            out.append("- `{}` —— {}".format(d["url"], d["reason"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="pansearch.py",
                                description="按名称搜索网盘资源（PanSou + 人人电影网 合并）")
    p.add_argument("keyword", nargs="*", metavar="名称",
                   help="影视名称，可给多个（逐个查询并依次输出）；"
                        "配合 --clear-cache 时可省略")
    p.add_argument("--only", default="all", choices=["all", "pansou", "rrdynb"],
                   help="只用某一个数据源")
    p.add_argument("--limit", type=int, default=30,
                   help="最多显示多少条（默认 30，0=不限）")
    p.add_argument("--per-type", type=int, default=6,
                   help="每种网盘最多显示多少条（默认 6，避免单一网盘占满列表；0=不限）")
    p.add_argument("--top", type=int, default=3, help="人人电影网抓取前 N 篇候选文章（默认 3）")
    p.add_argument("--jobs", type=int, default=4,
                   help="人人电影网并发的文章页抓取数（**只对该源生效**；"
                        "PanSou 是单次请求，没有并发度可调）")
    p.add_argument("--rrdynb-articles", default=None,
                   help="直接传入人人电影网文章 URL（逗号分隔），跳过本地搜索。"
                        "用于 WebFetch 兜底后回填候选文章")
    p.add_argument("--cloud-types", default=None,
                   help="限定网盘类型，逗号分隔（仅对 PanSou 生效）")
    p.add_argument("--src", default="all", choices=["all", "tg", "plugin"],
                   help="PanSou 数据来源（默认 all）")
    p.add_argument("--res", default="merge", choices=["merge", "results", "all"],
                   help="PanSou 结果形态（默认 merge）")
    p.add_argument("--plugins", default=None, help="限定 PanSou 插件，逗号分隔")
    p.add_argument("--include", default=None,
                   help="标题须包含的关键词，逗号分隔（OR，仅 PanSou）。"
                        "例：--include 全集,合集")
    p.add_argument("--exclude", default=None,
                   help="标题须排除的关键词，逗号分隔（OR，仅 PanSou）。"
                        "例：--exclude 预告,花絮")
    p.add_argument("--sort", default="relevance", choices=["relevance", "date"],
                   help="排序方式：relevance 按相关性（默认）；"
                        "date 按链接日期，新的在前、无日期的沉底")
    p.add_argument("--min-score", type=int, default=0, metavar="N",
                   help="只保留相关性得分 >= N 的条目（0~100；0 表示不过滤）。"
                        "可剔除仅命中关键词个别字的弱相关结果，被剔除的会如实列出")
    p.add_argument("--verify", type=int, default=0, metavar="N",
                   help="检测前 N 条链接是否仍然有效（需实例支持 /api/check/links，"
                        "公开演示实例返回 404，仅自建实例可用）")
    p.add_argument("--drop-dead", action="store_true",
                   help="配合 --verify：把检测为已失效的链接从结果中移除")
    p.add_argument("--keep-invalid", action="store_true",
                   help="保留形态异常的链接（默认剔除分享 ID 被截断等死链）")
    p.add_argument("--no-group", action="store_true",
                   help="不把同一网盘内标题相同的多个分享折叠成一条（默认折叠，"
                        "其余链接以 └ 列在原条目下方）")
    p.add_argument("--api", default=None, help="PanSou 实例地址（或 PANSOU_API_BASE）")
    p.add_argument("--token", default=None, help="PanSou 认证 Token（或 PANSOU_API_TOKEN）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（等价 --format json）")
    p.add_argument("--format", default=None, choices=["text", "md", "json"],
                   help="输出格式（默认 text）")
    p.add_argument("-o", "--out", default=None,
                   help="把结果写入文件；未指定格式时按扩展名推断（.md/.json/text）")
    p.add_argument("--refresh", action="store_true", help="忽略本地缓存")
    p.add_argument("--no-cache", action="store_true", help="本次禁用缓存")
    p.add_argument("--clear-cache", action="store_true", help="清空两个数据源的本地缓存")
    p.add_argument("--force-search", action="store_true", help="请求服务端强制刷新")
    p.add_argument("--timing", action="store_true", help="输出耗时统计")
    p.add_argument("--deadline", type=float, default=120.0,
                   help="整条命令总预算秒数（默认 120；PanSou 冷查询本身可能耗时数十秒）")
    # 两个源的合理超时差 4 倍（PanSou 55s / rrdy 12s），必须能分别设置；
    # --timeout / --retries 保留为同时作用于两源的后备写法。
    p.add_argument("--timeout", type=float, default=None,
                   help="同时设置两源的单次请求超时（后备写法，优先用下面两个）")
    p.add_argument("--retries", type=int, default=None,
                   help="同时设置两源的重试次数（后备写法，优先用下面两个）")
    p.add_argument("--pansou-timeout", type=float, default=None,
                   help="PanSou 单次请求超时秒数（默认 {}）".format(pansou.DEFAULT_TIMEOUT))
    p.add_argument("--pansou-retries", type=int, default=None, help="PanSou 重试次数")
    p.add_argument("--rrdy-timeout", type=float, default=None,
                   help="人人电影网单次请求超时秒数（默认 {}）".format(rrdy.DEFAULT_TIMEOUT))
    p.add_argument("--rrdy-retries", type=int, default=None, help="人人电影网重试次数")
    p.add_argument("--proxy", default=None,
                   help="指定出网代理（如 127.0.0.1:7897）；direct 表示直连。"
                        "默认按 PANSEARCH_PROXY > proxy.conf > 系统环境变量 解析，"
                        "详见 netlib.py；用 netlib.py probe 可实测各出口质量")
    return p


def _resolve_format(args):
    fmt = args.format or ("json" if args.json else "text")
    if args.out and not args.format and not args.json:
        ext = os.path.splitext(args.out)[1].lower()
        if ext in (".md", ".markdown"):
            fmt = "md"
        elif ext == ".json":
            fmt = "json"
    return fmt


def _json_payload(kw, items, results, dropped, verify_result):
    """单关键词的 JSON 结构。多关键词时在外层再包一层 results 数组。"""
    verify = None
    if verify_result is not None:
        verify = {"status": verify_result.get("status"),
                  "http_status": verify_result.get("http_status"),
                  "checked": verify_result.get("checked"),
                  "alive_ok": verify_result.get("alive_ok"),
                  "alive_dead": verify_result.get("alive_dead"),
                  "alive_unknown": verify_result.get("alive_unknown")}
    return {
        "keyword": kw,
        "total": len(items),
        "sources": [{"source": r["source"], "status": r["status"],
                     "count": len(r.get("links") or [])} for r in results],
        "dropped": dropped,
        "verify": verify,
        "links": items,
    }


def _exit_code(items, results):
    """0 有结果；2 所有源不可用；3 所有源可用但无结果。"""
    if items:
        return 0
    if all(r.get("status") in ("unreachable", "auth_error", "rate_limited", "error",
                               "cf_blocked", "needs_fallback") for r in results):
        return 2
    return 3


def _search_one(kw, args):
    """查一个关键词：并行双源 -> 合并去重 -> 可选验链 -> 可选同资源聚类。"""
    sources = {"all": ["pansou", "rrdynb"], "pansou": ["pansou"],
               "rrdynb": ["rrdynb"]}[args.only]

    jobs = []
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        if "pansou" in sources:
            jobs.append(("pansou", ex.submit(from_pansou, kw, args)))
        if "rrdynb" in sources:
            jobs.append(("rrdynb", ex.submit(from_rrdynb, kw, args)))
        results = []
        for name, fut in jobs:
            try:
                results.append(fut.result())
            except Exception as e:  # 单源异常不应拖垮整体
                results.append({"source": name, "status": "error",
                                "links": [], "detail": "{}: {}".format(type(e).__name__, e)})

    items, dropped = merge(results, kw, keep_invalid=args.keep_invalid,
                           sort_by=args.sort, min_score=args.min_score)

    # 链接有效性抽查（可选）
    verify_result = verify_links(items, args.verify, args)
    if verify_result is not None and args.drop_dead:
        dead = [it for it in items if it.get("alive") == "invalid"]
        if dead:
            items = [it for it in items if it.get("alive") != "invalid"]
            dropped = dropped + [{"url": it["url"], "type": it["type"],
                                  "note": it.get("note") or "",
                                  "reason": "检测为已失效"} for it in dead]

    # 同一网盘内标题相同的多个分享折叠成一条（其余列在 variants 里）
    if not args.no_group:
        items = group_items(items)
    return items, results, dropped, verify_result


def main(argv=None):
    args = build_parser().parse_args(argv)

    # 两个数据源共用同一出口，此处统一解析一次即可（rrdy / pansou 均经 netlib 发起请求）
    proxy_desc = netlib.configure(args.proxy)

    if args.clear_cache:
        n1 = pansou.cache_clear()
        n2 = rrdy.cache_clear()
        print("已清空缓存：PanSou {} 个文件（{}）、人人电影网 {} 个文件（{}）".format(
            n1, pansou.CACHE_DIR, n2, rrdy.CACHE_DIR))
        return 0

    kw_list = [k.strip() for k in (args.keyword or []) if k.strip()]
    if not kw_list:
        sys.stderr.write("错误：关键词不能为空\n")
        return 1

    deadline = args.deadline if args.deadline and args.deadline > 0 else None

    # 超时/重试：按源设置，--timeout/--retries 作为同时设置两者的后备
    pt = args.pansou_timeout if args.pansou_timeout is not None else args.timeout
    rt = args.rrdy_timeout if args.rrdy_timeout is not None else args.timeout
    pr = args.pansou_retries if args.pansou_retries is not None else args.retries
    rr = args.rrdy_retries if args.rrdy_retries is not None else args.retries
    if pt is not None:
        pansou.DEFAULT_TIMEOUT = pt
    if rt is not None:
        rrdy.DEFAULT_TIMEOUT = rt
    if pr is not None:
        pansou.DEFAULT_RETRIES = max(1, pr)
    if rr is not None:
        rrdy.DEFAULT_RETRIES = max(1, rr)

    limit = args.limit if args.limit and args.limit > 0 else None
    per_type = args.per_type if args.per_type and args.per_type > 0 else None
    fmt = _resolve_format(args)

    outputs = []
    codes = []
    for kw in kw_list:
        # 每个关键词各获得一份完整预算。多关键词时总耗时约为 N × 单次，
        # 想收紧就自己调小 --deadline。
        start = time.monotonic()
        pansou.DEADLINE_AT = start + deadline if deadline else None
        rrdy.DEADLINE_AT = start + deadline if deadline else None

        items, results, dropped, verify_result = _search_one(kw, args)
        codes.append(_exit_code(items, results))

        if fmt == "json":
            outputs.append(_json_payload(kw, items, results, dropped, verify_result))
        elif fmt == "md":
            outputs.append(render_markdown(kw, items, results, dropped=dropped,
                                           per_type=per_type, limit=limit,
                                           sort_by=args.sort))
        else:
            outputs.append(render(kw, items, results, limit=limit, per_type=per_type,
                                  dropped=dropped, verify_result=verify_result,
                                  proxy_desc=proxy_desc, sort_by=args.sort))

    if fmt == "json":
        text = (json.dumps(outputs[0], ensure_ascii=False, indent=2) if len(outputs) == 1
                else json.dumps({"keywords": kw_list, "results": outputs},
                                ensure_ascii=False, indent=2))
    else:
        text = ("\n\n" + "=" * 62 + "\n\n").join(outputs)

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            sys.stderr.write("[out] 已写入 {}（{} 字节）\n".format(args.out, len(text)))
            if fmt == "text":
                print(text)
        except OSError as e:
            sys.stderr.write("错误：无法写入 {}：{}\n".format(args.out, e))
            print(text)
            return 1
    else:
        print(text)

    if args.timing:
        sys.stderr.write(
            "[timing] PanSou 网络 {} 次 / 缓存 {} 次 / {:.2f}s；"
            "人人电影网 网络 {} 次 / 缓存 {} 次 / {:.2f}s\n".format(
                pansou.STATS.requests, pansou.STATS.cache_hits, pansou.STATS.net_seconds,
                rrdy.STATS.requests, rrdy.STATS.cache_hits, rrdy.STATS.net_seconds,
            ))
        sys.stderr.write("[timing] 出口：{}\n".format(netlib.describe()))
        for note in netlib.skipped():
            sys.stderr.write("[timing] 已跳过：{}\n".format(note))

    if any(c == 0 for c in codes):
        return 0
    return max(codes)


if __name__ == "__main__":
    sys.exit(main())
