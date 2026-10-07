# -*- coding: utf-8 -*-
"""netlib 出口选择模块的回归测试（离线，不依赖真实网络）。"""
import io
import os
import sys
import tempfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))
import netlib  # noqa: E402
import rrdy    # noqa: E402
import pansou  # noqa: E402

ok = 0
fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  " + name)
    else:
        fail += 1
        print("  FAIL  " + name + ("  " + str(extra) if extra else ""))


def reset_env(**kw):
    for k in ("PANSEARCH_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy",
              "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = v


print("[1] normalize 归一化")
check("None -> None", netlib.normalize(None) is None)
for token in ("", "direct", "DIRECT", "none", "off", "no", "-", "0"):
    check("'{0}' -> direct".format(token), netlib.normalize(token) == "direct")
check("host:port 补 http://", netlib.normalize("127.0.0.1:7897") == "http://127.0.0.1:7897")
check("完整 URL 不变", netlib.normalize("http://127.0.0.1:7897") == "http://127.0.0.1:7897")
check("https URL 保留 scheme", netlib.normalize("https://p:8080") == "https://p:8080")
check("两侧空白被去除", netlib.normalize("  127.0.0.1:7897  ") == "http://127.0.0.1:7897")

print("[2] hostport_of 解析")
check("http 默认端口 80", netlib.hostport_of("http://a.b") == ("a.b", 80))
check("https 默认端口 443", netlib.hostport_of("https://a.b") == ("a.b", 443))
check("显式端口", netlib.hostport_of("http://127.0.0.1:7897") == ("127.0.0.1", 7897))
check("无 host 返回 None", netlib.hostport_of("http://") is None)

print("[3] proxy.conf 解析（注释/空行/首行有效）")
d = tempfile.mkdtemp(prefix="netlib_cfg")
cfg = os.path.join(d, "proxy.conf")
with open(cfg, "w", encoding="utf-8") as f:
    f.write("# 注释行\n\n   \n# 又一个注释\nhttp://127.0.0.1:7897\nhttp://9.9.9.9:1\n")
val, path = netlib.read_config(cfg)
check("取首个非注释行", val == "http://127.0.0.1:7897", val)
check("返回路径正确", path == cfg)
with open(cfg, "w", encoding="utf-8") as f:
    f.write("# 只有注释\n")
check("全注释 -> None", netlib.read_config(cfg)[0] is None)
check("文件不存在 -> None", netlib.read_config(os.path.join(d, "nope.conf"))[0] is None)

print("[4] candidates 优先级顺序")
# 上一节把 cfg 改成了「全注释」，这里恢复成有效配置再测优先级
with open(cfg, "w", encoding="utf-8") as f:
    f.write("# 注释\nhttp://127.0.0.1:7897\n")
reset_env()
cands = netlib.candidates(explicit=None, config_path=cfg)
check("无 CLI 时首项不含 --proxy", all(s != "命令行 --proxy" for _, s in cands))
cands = netlib.candidates(explicit="http://x:1", config_path=cfg)
check("CLI 优先级最高", cands[0] == ("http://x:1", "命令行 --proxy"), cands[0])
reset_env(PANSEARCH_PROXY="http://env:2")
cands = netlib.candidates(explicit="http://x:1", config_path=cfg)
check("CLI > 环境变量 > 配置文件",
      [v for v, _ in cands[:3]] == ["http://x:1", "http://env:2", "http://127.0.0.1:7897"],
      [v for v, _ in cands[:3]])
reset_env(https_proxy="http://sysproxy:3")
cands = netlib.candidates(explicit=None, config_path=cfg)
check("系统环境变量排在配置文件之后",
      [v for v, _ in cands] == ["http://127.0.0.1:7897", "http://sysproxy:3", "direct"],
      [v for v, _ in cands])
cands = netlib.candidates(explicit=None, config_path=os.path.join(d, "nope.conf"))
check("最后兜底为直连", cands[-1] == ("direct", "兜底直连"), cands[-1])

print("[5] configure：显式指定始终被尊重")
reset_env()
desc = netlib.configure(explicit="direct", config_path=cfg)
check("--proxy direct -> 直连", netlib.proxy() is None and "直连" in desc, desc)
desc = netlib.configure(explicit="127.0.0.1:9", config_path=cfg)
check("显式死端口仍被采用", netlib.proxy() == "http://127.0.0.1:9", netlib.proxy())
check("死端口会给出提示", "仍按指定使用" in desc, desc)

print("[6] configure：不可达自动顺延")
reset_env(PANSEARCH_PROXY="127.0.0.1:9")
desc = netlib.configure(config_path=cfg)
check("跳过不可达项并顺延到下一候选", netlib.proxy() == "http://127.0.0.1:7897", desc)
check("记录了跳过原因", any("PANSEARCH_PROXY" in s for s in netlib.skipped()),
      netlib.skipped())

print("[7] probe=False 时不做端口探测")
reset_env(PANSEARCH_PROXY="127.0.0.1:9")
desc = netlib.configure(config_path=cfg, probe=False)
check("不探测则直接采用首个候选",
      netlib.proxy() == "http://127.0.0.1:9" and not netlib.skipped(), desc)

print("[8] tcp_reachable")
check("本机 7897 可达", netlib.tcp_reachable("http://127.0.0.1:7897")[0] is True)
check("返回连接耗时", isinstance(netlib.tcp_reachable("http://127.0.0.1:7897")[1], float))
check("死端口不可达", netlib.tcp_reachable("http://127.0.0.1:9")[0] is False)
check("非法地址不可达", netlib.tcp_reachable("http://")[0] is False)

print("[9] open_url 未配置时自动解析（不静默退化为直连）")
reset_env(PANSEARCH_PROXY="direct")
netlib._state["configured"] = False
netlib._state["opener"] = None
netlib._state["proxy"] = None
captured = {}


class FakeOpener:
    def open(self, req, timeout=None):
        captured["timeout"] = timeout
        captured["url"] = req.full_url if hasattr(req, "full_url") else req
        raise RuntimeError("stop-here")


netlib._opener_cache["\x00direct"] = FakeOpener()
from urllib.request import Request
try:
    netlib.open_url(Request("http://example.invalid/x"), timeout=7)
except RuntimeError:
    pass
check("open_url 触发了自动 configure", netlib._state["configured"] is True)
check("超时参数被透传", captured.get("timeout") == 7, captured)
check("自动解析遵循优先级（此处为 direct）", netlib.proxy() is None, netlib.describe())
netlib._opener_cache.pop("\x00direct", None)  # 清掉假 opener，避免影响后续用例

print("[10] 两个脚本确实改走 netlib")
netlib.configure(explicit="direct", config_path=cfg)
hit = {"n": 0}
real_open = netlib.open_url


def spy(req, timeout=None):
    hit["n"] += 1
    raise RuntimeError("spy")


netlib.open_url = spy
try:
    rrdy.http_get("https://www.rrdynb.com/x.html", retries=1, timeout=3,
                  budget=None)
except Exception:
    pass
check("rrdy.http_get 经 netlib.open_url", hit["n"] >= 1, hit["n"])
hit["n"] = 0
try:
    pansou.http_request("https://so.252035.xyz/api/health", retries=1, timeout=3)
except Exception:
    pass
check("pansou.http_request 经 netlib.open_url", hit["n"] >= 1, hit["n"])
netlib.open_url = real_open

print("[11] CLI 参数已注册")
import subprocess
P = sys.executable
SKILL = SKILL_ROOT
for script in ("rrdy.py", "pansou.py", "pansearch.py"):
    p = subprocess.run([P, os.path.join(SKILL, "scripts", script), "--help"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    check("{} --help 含 --proxy".format(script), "--proxy" in (p.stdout or ""),
          (p.stderr or "")[:150])

# 子命令里的 --proxy 是隐藏参数（不出现在 help），故改为功能验证：
# 真正跑一次，若参数未被识别，argparse 会输出 "unrecognized arguments"。
runs = [
    ("rrdy.py", ["search", "不存在的片名xyz", "--proxy", "direct", "--no-cache",
                 "--timeout", "2", "--retries", "1", "--deadline", "3",
                 "--search-budget", "2"]),
    ("pansou.py", ["search", "不存在的片名xyz", "--proxy", "direct",
                   "--api", "http://127.0.0.1:9", "--no-cache",
                   "--timeout", "2", "--retries", "1", "--deadline", "4"]),
    ("pansearch.py", ["不存在的片名xyz", "--proxy", "direct", "--no-cache",
                      "--only", "rrdynb", "--deadline", "4"]),
]
for script, argv in runs:
    p = subprocess.run([P, os.path.join(SKILL, "scripts", script)] + argv,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    err = (p.stderr or "")
    check("{} 子命令接受 --proxy（无 unrecognized）".format(script),
          "unrecognized" not in err and "invalid" not in err.lower(), err[:150])
p = subprocess.run([P, os.path.join(SKILL, "scripts", "netlib.py"), "--help"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("netlib.py --help 正常", p.returncode == 0 and "probe" in (p.stdout or ""),
      (p.stderr or "")[:150])

print("[12] probe 候选去重")
cands = list(netlib._probe_candidates(explicit="http://127.0.0.1:7897",
                                      config_path=cfg, include_common=True))
vals = [v for v, _ in cands]
check("无重复项", len(vals) == len(set(vals)), vals)
check("含直连", "direct" in vals)
check("含常见端口", any(v.endswith(":7890") for v in vals))

reset_env()
print()
print("== 汇总：{} 通过 / {} 失败 ==".format(ok, fail))
sys.exit(1 if fail else 0)
