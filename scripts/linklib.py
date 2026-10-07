#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网盘链接的规范化、去重指纹与形态校验（纯函数，不发网络请求）。

为什么单独抽一个模块
--------------------
PanSou 与人人电影网两个数据源返回的链接**写法不一致**，暴露出的问题有：

1. 同一资源被多个别名主机表示——`aliyundrive.com` 与 `alipan.com`、
   `115cdn.com` 与 `115.com` 与 `anxia.com`、`123912.com` 与 `123684.com`
   与 `123pan.com`——不去重就会在结果里重复出现。
2. 同一分享的**子路径**写法不同——`/s/AAA` 与 `/s/AAA/folder/BBB` 指向同一分享。
3. 分享 ID **大小写敏感**（夸克 `/s/5ef2be7f164e`、阿里 `/s/PP5vjhC2Sme`），
   早期实现对整个 path 做 `lower()` 会把两个不同资源误判成同一个。
4. PanSou 会把提取码**直接粘在 URL 后面**——`.../s/pbeojv-bLFbh提取码:SSTA`——
   既让链接不可点击，又因 path 不同而逃过去重。
5. 上游偶发返回**被截断的分享 ID**——`https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM`
   （正常应为 26 字符），点开是死链。

本模块把这 5 件事收敛在一处，供 pansou / rrdy / pansearch 共用，
并且**全部是纯函数**，可以用固定字符串直接做单元测试。

术语
----
- 规范主机（canon host）：把别名主机折叠成一个标识，如 `aliyun`、`115`、`123`。
- 分享路径（share path）：把 `/s/<id>/folder/<sub>` 折叠成 `/s/<id>`。
- 指纹（fingerprint）：`规范主机|分享路径`，用作去重键。
"""

from __future__ import annotations

import re
import urllib.parse

# --------------------------------------------------------------------------
# 主机归一
# --------------------------------------------------------------------------
# 同一网盘的多个域名。键是规范标识，值是域名元组（精确匹配，已去 www.）。
HOST_GROUPS = (
    ("baidu",    ("pan.baidu.com", "yun.baidu.com")),
    ("quark",    ("pan.quark.cn",)),
    ("aliyun",   ("alipan.com", "aliyundrive.com")),
    ("xunlei",   ("pan.xunlei.com",)),
    ("115",      ("115.com", "115cdn.com", "115pan.com", "anxia.com")),
    ("123",      ("123pan.com", "123684.com", "123865.com", "123912.com",
                  "123pan.cn", "123pan.com.cn")),
    ("uc",       ("drive.uc.cn", "fast.uc.cn")),
    ("tianyi",   ("cloud.189.cn",)),
    ("mobile",   ("yun.139.com", "caiyun.139.com")),
    ("pikpak",   ("mypikpak.com",)),
    ("weiyun",   ("weiyun.com",)),
    ("ctfile",   ("ctfile.com", "545c.com")),
    ("gdrive",   ("drive.google.com",)),
    ("onedrive", ("1drv.ms", "onedrive.live.com")),
    ("mediafire", ("mediafire.com",)),
)

# 域名**族**：蓝奏云把前缀塞进域名（lanzoua.com / lanzoui.com / lanzn.com ...），
# 无法穷举，用包含匹配。
HOST_FAMILIES = (
    ("lanzou", "lanzou"),
    ("lanzn", "lanzou"),
    ("lanzo", "lanzou"),
    ("lanzoux", "lanzou"),
)

_HOST_EXACT = {}
for _canon, _domains in HOST_GROUPS:
    for _d in _domains:
        _HOST_EXACT[_d] = _canon

# 中文网盘名 -> 规范标识（人人电影网侧用的是中文名）
CN_ALIASES = {
    "123网盘": "123", "123云盘": "123",
    "蓝奏云": "lanzou", "联通云盘": "liantong", "腾讯微云": "weiyun",
    "城通网盘": "ctfile", "Google Drive": "gdrive", "OneDrive": "onedrive",
    "MediaFire": "mediafire", "阿里云盘": "aliyun", "百度网盘": "baidu",
    "夸克网盘": "quark", "迅雷云盘": "xunlei", "115网盘": "115",
    "UC网盘": "uc", "天翼云盘": "tianyi", "移动云盘": "mobile",
    "PikPak": "pikpak",
}


def canon_host(host):
    """主机名 -> 规范标识。未知主机返回去掉 www. 与端口的原始主机名。

    必须**剥离端口**：`pan.quark.cn:443` 与 `pan.quark.cn` 是同一台机器，
    但若把端口带进规范标识，会同时破坏两件事——
      1. 去重失效：`quark|/s/x` 与 `pan.quark.cn:443|/s/x` 被当成两条不同链接；
      2. 校验失效：`validate()` 查不到 SHAPE_RULES[host] 就按「未识别主机」放行，
         带端口的已知网盘链接会绕过形态检查。
    上游（TG 频道 / 站点插件）确实偶发返回带 `:443` 的写法。
    """
    h = (host or "").lower().strip().strip(".")
    if not h:
        return ""
    # IPv6 字面量的端口写法是 [::1]:443，网盘域名不会出现，此处只处理 host:port
    if ":" in h:
        h = h.split(":", 1)[0]
    if h.startswith("www."):
        h = h[4:]
    if h in _HOST_EXACT:
        return _HOST_EXACT[h]
    for needle, canon in HOST_FAMILIES:
        if needle in h:
            return canon
    return h


# --------------------------------------------------------------------------
# 分享路径折叠
# --------------------------------------------------------------------------

# /s/<id> 与 /share/<id> 是绝大多数网盘的主形态
_SEG_RE = re.compile(r"^/(?:s|share)/([^/?#]+)", re.I)
# 磁力 / 电驴
_BTIH_RE = re.compile(r"xt=urn:btih:([A-Za-z0-9]+)", re.I)


def share_path(path, fragment="", host=""):
    """把 path 折叠成「分享单元」：

        /s/AAA            -> /s/AAA
        /s/AAA/folder/BB  -> /s/AAA      （同一分享的子目录）
        /share/AAA/x      -> /share/AAA
        /shareweb/#/w/i/1 -> /shareweb#/w/i/1   （移动云盘的 id 在 fragment 里）

    未匹配到分享单元时返回去掉尾部斜杠的 path（同时小写化——这类路径是
    结构性路径，如百度 `/share/init`，小写化有助于归并）。
    """
    m = _SEG_RE.match(path or "")
    if m:
        return path[:m.end()]
    if host == "mobile" and (path or "").startswith("/shareweb"):
        frag = (fragment or "").strip("/")
        if frag:
            return "/shareweb#/" + frag
        return "/shareweb"
    return (path or "").rstrip("/").lower()


def fingerprint(url):
    """去重指纹。忽略 scheme、query（?pwd= 只是提取码）、fragment 与子路径。

    注意：**分享 ID 保留原始大小写**——夸克/阿里的 ID 大小写敏感，
    早期实现整段 lower() 会把 `/s/AbC` 与 `/s/abc` 误判为同一资源。
    """
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith("magnet:"):
        m = _BTIH_RE.search(u)
        return "magnet|" + (m.group(1).lower() if m else u)
    if low.startswith("ed2k:"):
        parts = u.split("|")
        if len(parts) > 4 and parts[1] == "file":
            return "ed2k|" + parts[4].lower()
        return "ed2k|" + u
    try:
        s = urllib.parse.urlsplit(u)
    except Exception:
        return "raw|" + u
    host = canon_host(s.netloc)
    if not host:
        return "raw|" + u
    return host + "|" + share_path(s.path, s.fragment, host)


# 兼容旧命名（早期代码把指纹函数叫 url_key）
def url_key(url):
    return fingerprint(url)


# --------------------------------------------------------------------------
# 链接清洗（剥离粘在 URL 上的提取码）
# --------------------------------------------------------------------------

# 提取码可能带括号：(访问码：abcd) / （提取码 abcd）
_PWD_BRACKET_RE = re.compile(
    r"[（(]\s*(?:访问码|访问密码|提取码|提取密码|密码|提取码)\s*[:：]?\s*([A-Za-z0-9]{1,10})\s*[)）]"
)
# 也可能不带括号，直接粘在末尾：.../s/pbeojv-bLFbh提取码:SSTA
# 锚定在字符串末尾，避免破坏正常的路径内容。
_PWD_TAIL_RE = re.compile(
    r"[?&、，,;\s]*(?:访问码|访问密码|提取码|提取密码|密码)\s*[:：]?\s*([A-Za-z0-9]{1,10})\s*$"
)
_PWD_QUERY_KEYS = ("pwd", "password", "passcode", "code", "p")


def extract_pwd(url):
    """从 URL 里取出提取码（不修改 URL）。"""
    raw = (url or "").strip()
    m = _PWD_BRACKET_RE.search(raw)
    if m:
        return m.group(1)
    m = _PWD_TAIL_RE.search(raw)
    if m:
        return m.group(1)
    try:
        q = urllib.parse.urlsplit(raw).query
    except Exception:
        return ""
    for pair in q.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.lower() in _PWD_QUERY_KEYS:
                return urllib.parse.unquote(v)
    return ""


def clean_url(url):
    """清洗链接并顺带取出提取码，返回 (干净URL, 提取码)。

    处理：括号/无括号的提取码后缀、结尾的 `#`、`?`、`&` 与中英文标点。
    """
    if not url:
        return url, ""
    raw = str(url).strip()
    pwd = ""

    m = _PWD_BRACKET_RE.search(raw)
    if m:
        pwd = m.group(1)
        raw = raw[:m.start()] + raw[m.end():]

    m = _PWD_TAIL_RE.search(raw)
    if m:
        if not pwd:
            pwd = m.group(1)
        raw = raw[:m.start()]

    raw = raw.rstrip("#?&、，,;:： \t")

    if not pwd:
        q = ""
        try:
            q = urllib.parse.urlsplit(raw).query
        except Exception:
            q = ""
        for pair in q.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.lower() in _PWD_QUERY_KEYS:
                    pwd = urllib.parse.unquote(v)
                    break
    return raw, pwd


# --------------------------------------------------------------------------
# 形态校验
# --------------------------------------------------------------------------
# 规范主机 -> (分享 ID 最小长度, 允许的其它路径前缀)
# 只对**已知网盘主机**做校验；未知主机一律放行（不评判第三方链接）。
#
# 关于最小长度：只在**掌握实测证据**处收紧，避免误杀。
# 迅雷的分享 ID 实测恒为 26 字符，而上游确实返回过 17 字符的截断值
# （`https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM`，点开是死链），故设为 20。
# 其余网盘没有观察到截断现象，min=1 表示「只校验路径形态、不卡长度」，
# 避免因上游格式变化误伤有效链接。
SHAPE_RULES = {
    "baidu":    (1,  ("/share/init", "/share/link", "/wap/init")),
    "quark":    (1,  ()),
    "aliyun":   (1,  ()),
    "xunlei":   (20, ()),
    "115":      (1,  ()),
    "123":      (1,  ()),
    "uc":       (1,  ()),
    "pikpak":   (1,  ()),
    "tianyi":   (1,  ("/web/share", "/cloud/share", "/share")),
    "mobile":   (1,  ("/shareweb",)),
    "weiyun":   (1,  ("/s/", "/share")),
    "ctfile":   (1,  ("/s/", "/fs/", "/d/")),
    "lanzou":   (1,  ("/i", "/b", "/f", "/s/")),
}


def validate(url, cloud_type=None):
    """校验链接形态，返回 (是否通过, 原因)。

    只做**形态**判断，不联网、不判断资源是否还有效。
    未识别的网盘主机返回通过，避免误杀小众网盘。
    """
    u = (url or "").strip()
    if not u:
        return False, "空链接"
    low = u.lower()
    if low.startswith("magnet:"):
        if _BTIH_RE.search(u):
            return True, ""
        return False, "磁力链接缺少 btih 哈希"
    if low.startswith("ed2k:"):
        if u.count("|") >= 4:
            return True, ""
        return False, "电驴链接字段不全"
    try:
        s = urllib.parse.urlsplit(u)
    except Exception:
        return False, "无法解析的 URL"
    if s.scheme not in ("http", "https"):
        return False, "非 http(s) 链接（{}）".format(s.scheme or "无 scheme")
    host = canon_host(s.netloc)
    if not host:
        return False, "缺少主机名"
    rule = SHAPE_RULES.get(host)
    if not rule:
        return True, ""

    min_len, alts = rule
    path = s.path or ""
    m = _SEG_RE.match(path)
    if m and len(m.group(1)) >= min_len:
        return True, ""
    if m and len(m.group(1)) < min_len:
        return False, "分享 ID 过短（{} < {} 字符），疑为上游截断".format(
            len(m.group(1)), min_len)
    for p in alts:
        if path.lower().startswith(p.lower()):
            return True, ""
    return False, "路径不是 {} 的分享形态（{}）".format(host, path or "/")


# --------------------------------------------------------------------------
# 自检 / 诊断
# --------------------------------------------------------------------------

def diagnose(url):
    """给出一条链接的流水线视图，便于排障：清洗 -> 指纹 -> 校验。"""
    cleaned, pwd = clean_url(url)
    ok, why = validate(cleaned)
    return {
        "raw": url,
        "cleaned": cleaned,
        "pwd": pwd,
        "fingerprint": fingerprint(cleaned),
        "valid": ok,
        "reason": why,
    }


def main(argv=None):
    import argparse
    import json

    p = argparse.ArgumentParser(
        prog="linklib.py",
        description="网盘链接诊断：清洗、去重指纹、形态校验")
    p.add_argument("urls", nargs="+", help="一个或多个网盘链接")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = p.parse_args(argv)

    rows = [diagnose(u) for u in args.urls]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        print("原始   : " + r["raw"])
        print("清洗后 : " + r["cleaned"])
        print("指纹   : " + r["fingerprint"])
        print("提取码 : " + (r["pwd"] or "—"))
        print("校验   : {} {}".format("通过" if r["valid"] else "不通过",
                                      ("（{}）".format(r["reason"]) if r["reason"] else "")))
        print("-" * 60)
    return 0 if all(r["valid"] for r in rows) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
