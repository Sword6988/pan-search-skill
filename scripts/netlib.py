#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一出口（代理）选择与请求发起。

为什么需要这个模块
------------------
本机存在多条出网路径，实测质量差异明显（同一目标、各 3 次采样）：

    出口                   rrdynb 文章页            pansou /health
    ---------------------  -----------------------  ------------------------
    直连                   3.10s，2/3 成功          1.45s，3/3 成功
    环境变量代理 :59323     7.58s，2/3 成功          返回 HTTP 400，2/3 成功
    本机 Clash :7897       1.79s，3/3 成功          2.46s，3/3 成功

也就是说「选哪条出口」会实际影响速度与成功率，因此不该默默沿用
`urllib` 默认读取的环境变量，而应做成可配置、可观测、可回退的选择。

解析优先级（前者优先）
----------------------
  1. 命令行   --proxy <url|direct>
  2. 环境变量 PANSEARCH_PROXY
  3. 技能根目录 proxy.conf（首个非注释行）
  4. 环境变量 https_proxy / HTTPS_PROXY / http_proxy / HTTP_PROXY / all_proxy
  5. 直连

除「命令行显式指定」外，候选项都会先做一次 0.6s 的 TCP 可达性探测，
不可达则顺延到下一项——这样即使 Clash 没启动、或端口配错，也不会
把请求打到死端口上；而用户显式指定的值始终尊重，仅在探测不通时提示。

独立使用（诊断当前网络出口）：
    python netlib.py show                 # 只打印本次会选中哪条出口
    python netlib.py probe                # 实测各候选出口的延迟与成功率
    python netlib.py probe --url https://www.rrdynb.com/ --repeat 3
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
CONFIG_NAME = "proxy.conf"
ENV_VAR = "PANSEARCH_PROXY"

# probe 时一并探测的常见本地代理端口（Clash / V2Ray / sing-box 的默认混合端口等）
COMMON_PORTS = (7897, 7890, 10809, 10808, 1080, 8080, 20171)
TCP_PROBE_TIMEOUT = 0.6
DEFAULT_PROBE_URL = "https://so.252035.xyz/api/health"

DIRECT_TOKENS = {"", "direct", "none", "off", "no", "-", "0", "false"}
ENV_KEYS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
            "all_proxy", "ALL_PROXY")

_state = {"proxy": None, "desc": "尚未解析（默认直连）", "opener": None,
          "configured": False}
_skipped = []
_opener_cache = {}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def normalize(value):
    """把各种写法统一成 'direct' 或完整代理 URL（缺 scheme 时补 http://）。"""
    if value is None:
        return None
    v = str(value).strip()
    if v.lower() in DIRECT_TOKENS:
        return "direct"
    if "://" not in v:
        v = "http://" + v
    return v


def hostport_of(proxy_url):
    u = urllib.parse.urlsplit(proxy_url)
    if not u.hostname:
        return None
    port = u.port or (443 if u.scheme == "https" else 80)
    return (u.hostname, port)


# urllib 的 ProxyHandler 只认 http/https 代理。SOCKS 需要第三方库（PySocks），
# 直接填 socks5:// 会静默失败，故在此显式识别并给出可操作的提示。
UNSUPPORTED_SCHEMES = ("socks5", "socks4", "socks", "socks5h", "socks4a")


def unsupported_reason(proxy_url):
    """返回不支持该代理写法的原因；支持则返回 None。"""
    u = urllib.parse.urlsplit(proxy_url)
    scheme = (u.scheme or "").lower()
    if scheme in UNSUPPORTED_SCHEMES:
        return ("不支持 {} 代理（urllib 限制）。请改用客户端提供的 HTTP 端口"
                "（Clash/V2Ray 通常同时开放混合端口，如 7897），"
                "或安装 PySocks 后改用 socks5h://".format(scheme))
    if scheme and scheme not in ("http", "https"):
        return "无法识别的代理协议：{}".format(scheme)
    return None


def tcp_reachable(proxy_url, timeout=TCP_PROBE_TIMEOUT):
    """探测代理端口是否可连，返回 (是否可达, 连接耗时秒数)。"""
    hp = hostport_of(proxy_url)
    if not hp:
        return False, None
    t0 = time.perf_counter()
    try:
        with socket.create_connection(hp, timeout=timeout):
            return True, time.perf_counter() - t0
    except Exception:
        return False, None


def read_config(path=None):
    """读取 proxy.conf，返回 (值或 None, 实际路径)。支持 # 注释与空行。"""
    path = path or os.path.join(SKILL_DIR, CONFIG_NAME)
    if not os.path.isfile(path):
        return None, path
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    return s, path
    except OSError:
        return None, path
    return None, path


def candidates(explicit=None, config_path=None):
    """按优先级生成候选项 [(原始值, 来源说明)]。"""
    out = []
    if explicit is not None:
        out.append((explicit, "命令行 --proxy"))
    ev = os.environ.get(ENV_VAR)
    if ev is not None and ev.strip():
        out.append((ev, "环境变量 " + ENV_VAR))
    cv, path = read_config(config_path)
    if cv:
        out.append((cv, os.path.basename(path)))
    for k in ENV_KEYS:
        v = os.environ.get(k)
        if v and v.strip():
            out.append((v, "环境变量 " + k))
            break
    out.append(("direct", "兜底直连"))
    return out


_OPENER_CACHE_MAX = 8


def _opener_for(proxy):
    key = "\x00direct" if proxy is None else proxy
    if key not in _opener_cache:
        if proxy is None:
            handler = urllib.request.ProxyHandler({})
        else:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        # 有界缓存：候选出口数量有限，避免长期运行时长尾增长
        if len(_opener_cache) >= _OPENER_CACHE_MAX:
            _opener_cache.pop(next(iter(_opener_cache)))
        _opener_cache[key] = urllib.request.build_opener(handler)
    return _opener_cache[key]


def _apply(proxy, desc):
    _state["proxy"] = proxy
    _state["desc"] = desc
    _state["opener"] = _opener_for(proxy)
    _state["configured"] = True


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def configure(explicit=None, config_path=None, probe=True):
    """确定本次使用的出口，返回可读说明。应在 main() 解析完参数后调用一次。"""
    global _skipped
    _skipped = []
    for idx, (raw, src) in enumerate(candidates(explicit, config_path)):
        val = normalize(raw)
        forced = (idx == 0 and explicit is not None)

        if val == "direct":
            _apply(None, "{} => 直连".format(src))
            return _state["desc"]

        bad = unsupported_reason(val)
        if bad:
            # 不要静默退化成直连——把原因说清楚，否则表现为「代理没生效但没报错」
            if forced:
                _skipped.append("{} => {}（{}）".format(src, val, bad))
                _apply(None, "{} => {} 不受支持，已回退直连".format(src, val))
                return _state["desc"]
            _skipped.append("{} => {}（{}）已跳过".format(src, val, bad))
            continue

        if forced:
            ok, lat = (tcp_reachable(val) if probe else (True, None))
            note = "" if ok else "（端口探测不通，仍按指定使用）"
            _apply(val, "{} => {}{}".format(src, val, note))
            return _state["desc"]

        if probe:
            ok, lat = tcp_reachable(val)
            if not ok:
                _skipped.append("{} => {} 端口不可达，已跳过".format(src, val))
                continue
            _apply(val, "{} => {}（端口可达 {:.0f}ms）".format(src, val, lat * 1000))
        else:
            _apply(val, "{} => {}".format(src, val))
        return _state["desc"]

    _apply(None, "直连")
    return _state["desc"]


def proxy():
    return _state["proxy"]


def describe():
    return _state["desc"]


def skipped():
    return list(_skipped)


def open_url(req, timeout=None):
    """用已选定的出口发起请求。替代 urllib.request.urlopen。

    若调用方（例如把本模块当作库使用、未走 CLI）尚未显式 configure，
    这里按同一优先级补一次解析——而不是静默退化成直连。
    """
    if not _state["configured"]:
        configure()
    op = _state["opener"]
    if op is None:
        op = _opener_for(_state["proxy"])
    if timeout is None:
        return op.open(req)
    return op.open(req, timeout=timeout)


# --------------------------------------------------------------------------
# 诊断 CLI
# --------------------------------------------------------------------------

def _probe_request(url, proxy_url, timeout=15.0):
    """对单个出口做一次真实 HTTPS 请求，返回结果字典。"""
    op = _opener_for(proxy_url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "*/*",
    })
    t0 = time.perf_counter()
    row = {"proxy": proxy_url, "el": None, "status": None,
           "len": 0, "ok": False, "err": None}
    try:
        with op.open(req, timeout=timeout) as resp:
            body = resp.read()
            row.update(el=time.perf_counter() - t0, status=resp.status,
                       len=len(body), ok=True)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        row.update(el=time.perf_counter() - t0, status=e.code, len=len(body),
                   ok=False, err="HTTP {}".format(e.code))
    except Exception as e:
        row.update(el=time.perf_counter() - t0,
                   err="{}: {}".format(type(e).__name__, e)[:90])
    return row


def _probe_candidates(explicit=None, config_path=None, include_common=True):
    """枚举待测出口：解析出的候选项 + 直连 + 常见本地端口。"""
    seen = set()
    out = []
    for raw, src in candidates(explicit, config_path):
        val = normalize(raw)
        key = "\x00direct" if val == "direct" else val
        if key in seen:
            continue
        seen.add(key)
        out.append((val, src))
    if include_common:
        for p in COMMON_PORTS:
            val = "http://127.0.0.1:{}".format(p)
            if val in seen:
                continue
            seen.add(val)
            out.append((val, "本机常见代理端口"))
    return out


def cmd_show(args):
    desc = configure(args.proxy, args.config, probe=not args.no_probe)
    print("本次出口：" + desc)
    for s in skipped():
        print("  跳过：" + s)
    print("配置文件：" + read_config(args.config)[1] +
          ("" if os.path.isfile(read_config(args.config)[1]) else "（不存在）"))
    return 0


def cmd_probe(args):
    rows = []
    for val, src in _probe_candidates(args.proxy, args.config,
                                      include_common=not args.no_common):
        label = "直连" if val == "direct" else val
        if val != "direct":
            ok, lat = tcp_reachable(val)
            tcp_s = "可达 {:.0f}ms".format(lat * 1000) if ok else "不可达"
        else:
            tcp_s = "—"
        samples = []
        for _ in range(max(1, args.repeat)):
            samples.append(_probe_request(args.url, None if val == "direct" else val,
                                          args.timeout))
            time.sleep(0.2)
        oks = [s for s in samples if s["ok"]]
        errs = {}
        for s in samples:
            if not s["ok"]:
                errs[s["err"] or "?"] = errs.get(s["err"] or "?", 0) + 1
        rows.append({
            "proxy": val, "label": label, "source": src, "tcp": tcp_s,
            "success": len(oks), "total": len(samples),
            "median": statistics.median([s["el"] for s in oks]) if oks else None,
            "fastest": min([s["el"] for s in oks]) if oks else None,
            "status": ",".join(str(s["status"]) for s in oks) if oks else "—",
            "errors": errs,
        })

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("目标：" + args.url)
    print("=" * 92)
    print("{:<24} {:<16} {:>6} {:>10} {:>10} {:>8}  {}".format(
        "出口", "端口探测", "成功", "中位耗时", "最快", "HTTP", "失败原因"))
    print("-" * 92)
    for r in rows:
        med = "{:.2f}s".format(r["median"]) if r["median"] is not None else "—"
        fast = "{:.2f}s".format(r["fastest"]) if r["fastest"] is not None else "—"
        errs = "; ".join("{}×{}".format(k, v) for k, v in r["errors"].items())
        print("{:<24} {:<16} {:>6} {:>10} {:>10} {:>8}  {}".format(
            r["label"][:23], r["tcp"], "{}/{}".format(r["success"], r["total"]),
            med, fast, r["status"][:8], errs[:34]))
    print("-" * 92)
    best = [r for r in rows if r["success"] == r["total"] and r["median"] is not None]
    if best:
        b = min(best, key=lambda r: r["median"])
        print("全部成功的出口中最快：{}（{:.2f}s）".format(b["label"], b["median"]))
    else:
        print("没有出口能全部成功——网络当前不稳定，建议稍后重试。")
    print("提示：把选定的出口写入 {} 或设置环境变量 {} 即可固定使用。".format(
        os.path.join(SKILL_DIR, CONFIG_NAME), ENV_VAR))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="netlib.py",
        description="出口（代理）诊断：查看当前选中的出口，或实测各候选出口质量")
    p.add_argument("cmd", nargs="?", default="show", choices=("show", "probe"),
                   help="show=打印本次选中的出口（默认）；probe=实测各候选出口")
    p.add_argument("--proxy", default=None,
                   help="指定代理（如 127.0.0.1:7897）；direct 表示直连")
    p.add_argument("--config", default=None, help="自定义 proxy.conf 路径")
    p.add_argument("--url", default=DEFAULT_PROBE_URL, help="probe 的测试目标 URL")
    p.add_argument("--repeat", type=int, default=3, help="每个出口采样次数（默认 3）")
    p.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数")
    p.add_argument("--no-probe", action="store_true", help="show：跳过端口可达性探测")
    p.add_argument("--no-common", action="store_true", help="probe：不探测常见本地端口")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "probe":
        return cmd_probe(args)
    return cmd_show(args)


if __name__ == "__main__":
    sys.exit(main())
