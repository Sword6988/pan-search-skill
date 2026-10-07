# -*- coding: utf-8 -*-
"""pansou.py 回归测试（全部离线，注入网络层）。"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import contextlib

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))
import pansou

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


D = os.path.join(tempfile.gettempdir(), "pansou_regress")
os.makedirs(D, exist_ok=True)
pansou.CACHE_DIR = D
pansou._CACHE_ON = True

# ---------------------------------------------------------------- 1. 响应解包
print("[1] 响应解包 unwrap")
d, err = pansou.unwrap('{"code":0,"message":"success","data":{"total":2,"merged_by_type":{}}}')
check("新版信封取 data", err is None and d.get("total") == 2, (d, err))
d, err = pansou.unwrap('{"code":400,"message":"关键词不能为空"}')
check("code!=0 报错", d is None and "关键词" in (err or ""), (d, err))
d, err = pansou.unwrap('{"total":5,"results":[],"merged_by_type":{}}')
check("旧版直出结构", err is None and d.get("total") == 5, (d, err))
d, err = pansou.unwrap("<html>oops</html>")
check("非 JSON 报错", d is None and err, (d, err))

print("[2] 结果标准化")
n = pansou.normalize_search_data({"merged_by_type": {"quark": [1, 2], "baidu": [3]}})
check("缺失 total 时按条数汇总", n["total"] == 3, n["total"])
n = pansou.normalize_search_data(None)
check("非 dict 安全降级", n["total"] == 0 and n["merged_by_type"] == {})

# ---------------------------------------------------------------- 3. 链接清洗
print("[3] 链接清洗 clean_url")
u, p = pansou.clean_url("https://cloud.189.cn/t/3a2qUfMrA3If（访问码：3djx）")
check("剥离访问码后缀", u == "https://cloud.189.cn/t/3a2qUfMrA3If" and p == "3djx", (u, p))
u, p = pansou.clean_url("https://pan.xunlei.com/s/VOxa?pwd=rvwm#")
check("去尾部#并识别 pwd", u.endswith("pwd=rvwm") and p == "rvwm", (u, p))
u, p = pansou.clean_url("https://pan.quark.cn/s/abc")
check("无码时 pwd 为空", u == "https://pan.quark.cn/s/abc" and p == "", (u, p))
u, p = pansou.clean_url("")
check("空输入不崩", u == "" and p == "")

# ---------------------------------------------------------------- 4. 结果摊平
print("[4] 结果摊平 collect_links")
data = {"merged_by_type": {
    "baidu": [{"url": "https://pan.baidu.com/s/1", "password": "aaa", "note": "甲", "datetime": "2026-01-02T00:00:00Z", "source": "plugin:x"}],
    "quark": [{"url": "https://pan.quark.cn/s/2", "password": "", "note": "乙", "datetime": "0001-01-01T00:00:00Z", "source": "tg:c"}],
    "magnet": [{"url": "magnet:?xt=1", "password": "", "note": "丙", "datetime": "", "source": ""}],
}}
links = pansou.collect_links(data)
check("按优先级排序（夸克先于百度）", [l["type"] for l in links][0] == "quark", [l["type"] for l in links])
check("类型中文名映射", {l["type_name"] for l in links} == {"百度网盘", "夸克网盘", "磁力链接"})
check("未知日期格式化", pansou._fmt_date("0001-01-01T00:00:00Z") == "—")
dup = {"merged_by_type": {"quark": [{"url": "https://pan.quark.cn/s/2"}, {"url": "https://pan.quark.cn/s/2"}]}}
check("同类型同 URL 去重", len(pansou.collect_links(dup)) == 1)
lim = pansou.collect_links({"merged_by_type": {"quark": [{"url": "u%d" % i} for i in range(5)]}},
                           limit_per_type=2)
check("limit_per_type 生效", len(lim) == 2, len(lim))

# ---------------------------------------------------------------- 5. 相关性
print("[5] 相关性排序")
check("完全一致最高", pansou.relevance("王者天下", "王者天下") == 100)
check("包含次之", pansou.relevance("王者天下 第6季", "王者天下") == 80)
check("无关最低", pansou.relevance("流浪地球", "王者天下") < 40)
check("空标题为 0", pansou.relevance("", "王者天下") == 0)
s = pansou.sort_links([
    {"type": "baidu", "note": "流浪地球", "datetime": "", "url": "a", "pwd": "", "source": "", "type_name": "x"},
    {"type": "quark", "note": "王者天下", "datetime": "", "url": "b", "pwd": "", "source": "", "type_name": "x"},
], "王者天下")
check("相关项排首位", s[0]["note"] == "王者天下", [x["note"] for x in s])

# ---------------------------------------------------------------- 6. API 错误判别
print("[6] 4xx 是否来自 API 本体")
check("JSON 视为 API 错误", pansou._looks_like_api_error('{"code":400}'))
check("HTML 视为中间层错误", not pansou._looks_like_api_error("<html>400 Bad Request</html>"))
check("空体不视为 API 错误", not pansou._looks_like_api_error(""))

# ---------------------------------------------------------------- 7. 缓存
print("[7] 缓存与负缓存策略")
pansou.cache_clear()
pansou.cache_write("k1", 200, '{"a":1}')
o = pansou.cache_read("k1")
check("写入后可读", o and o["status"] == 200 and o["payload"] == '{"a":1}')
check("TTL 常量(成功2h / 负5m)", pansou.OK_TTL == 7200 and pansou.NEG_TTL == 300)

calls = {"n": 0}


def fake_http(status, text):
    def f(url, method="GET", payload=None, token=None, retries=None,
          timeout=None, budget=None, deadline=None):
        calls["n"] += 1
        return status, text
    return f


pansou.cache_clear()
calls["n"] = 0
pansou.http_request = fake_http(0, "boom")
pansou.cached_request("k2", "https://x/api/search", method="POST", payload={})
check("网络失败不写负缓存", pansou.cache_read("k2") is None)
pansou.http_request = fake_http(400, "<html>400 plain HTTP</html>")
pansou.cached_request("k3", "https://x/api/search", method="POST", payload={})
check("中间层 HTML 4xx 不写负缓存", pansou.cache_read("k3") is None)
pansou.http_request = fake_http(400, '{"code":400,"message":"关键词不能为空"}')
pansou.cached_request("k4", "https://x/api/search", method="POST", payload={})
o = pansou.cache_read("k4")
check("API JSON 4xx 写负缓存", o and o["status"] == 400)

print("[8] 过期缓存兜底")
pansou.cache_clear()
json.dump({"ts": time.time() - 99999, "status": 200, "payload": '{"total":7}'},
          open(pansou.cache_path("k5"), "w", encoding="utf-8"))
pansou.http_request = fake_http(0, "timeout")
pansou.STATS.stale_fallbacks = 0
st, text = pansou.cached_request("k5", "https://x/api/search", method="POST", payload={})
check("失败回退过期副本", st == 200 and '"total":7' in text, (st, text))
check("兜底计数递增", pansou.STATS.stale_fallbacks == 1)

# ---------------------------------------------------------------- 9. do_search
print("[9] do_search 各状态")
pansou.cache_clear()
pansou.http_request = fake_http(0, "tunnel 502")
r = pansou.do_search("x", "https://s", use_cache=True)
check("不可达 => unreachable", r["status"] == "unreachable", r["status"])
pansou.cache_clear()
pansou.http_request = fake_http(401, '{"error":"未授权"}')
r = pansou.do_search("x", "https://s", use_cache=True)
check("401 => auth_error", r["status"] == "auth_error", r["status"])
pansou.cache_clear()
pansou.http_request = fake_http(200, '{"code":0,"data":{"total":0,"merged_by_type":{},"results":[]}}')
r = pansou.do_search("x", "https://s", use_cache=True)
check("空结果 => empty", r["status"] == "empty", r["status"])
pansou.cache_clear()
pansou.http_request = fake_http(200, '{"code":0,"data":{"total":1,"merged_by_type":{"quark":[{"url":"https://pan.quark.cn/s/z","note":"王者天下"}]}}}')
r = pansou.do_search("x", "https://s", use_cache=True)
check("有结果 => ok", r["status"] == "ok" and r["total"] == 1, r.get("status"))

print("[10] 多实例故障转移")
pansou.cache_clear()
seq = []


def flaky(url, method="GET", payload=None, token=None, retries=None,
          timeout=None, budget=None, deadline=None):
    seq.append(url)
    if "bad" in url:
        return 0, "unreachable"
    return 200, '{"code":0,"data":{"total":1,"merged_by_type":{"quark":[{"url":"https://pan.quark.cn/s/z","note":"n"}]},"results":[]}}'


pansou.http_request = flaky
r = pansou.search_with_failover("x", ["https://bad.example", "https://good.example"], use_cache=True)
check("首实例失败切次实例", r["status"] == "ok" and r["api_base"] == "https://good.example", r.get("api_base"))
check("tried 记录尝试轨迹", len(r.get("tried") or []) == 2, r.get("tried"))

print("[11] 请求体构造")
captured = {}


def cap(url, method="GET", payload=None, token=None, retries=None,
        timeout=None, budget=None, deadline=None):
    captured["payload"] = payload
    captured["token"] = token
    captured["method"] = method
    return 200, '{"code":0,"data":{"total":1,"merged_by_type":{"quark":[{"url":"u","note":"n"}]},"results":[]}}'


pansou.cache_clear()
pansou.http_request = cap
pansou.do_search("甲", "https://s", token="TK", res="merge", src="plugin",
                 cloud_types=["quark", "baidu"], include=["全集"], exclude=["预告"],
                 refresh_flag=True, use_cache=True)
b = captured["payload"]
check("参数写入请求体", b["src"] == "plugin" and b["cloud_types"] == ["quark", "baidu"]
      and b["filter"] == {"include": ["全集"], "exclude": ["预告"]} and b["refresh"] is True, b)
check("Token 透传", captured["token"] == "TK" and captured["method"] == "POST")

# ---------------------------------------------------------------- 12. 渲染
print("[12] 文本渲染")
pansou.cache_clear()
pansou.http_request = fake_http(0, "tunnel 502")
out = pansou.render_text_resolve(pansou.do_search("x", "https://s", use_cache=True))
check("不可达提示含实例地址", "服务不可达" in out and "PANSOU_API_BASE" in out, out[:60])
pansou.cache_clear()
pansou.http_request = fake_http(200, '{"code":0,"data":{"total":0,"merged_by_type":{},"results":[]}}')
out = pansou.render_text_resolve(pansou.do_search("测试片名", "https://s", use_cache=True))
check("无结果提示", "未找到" in out, out[:60])
pansou.cache_clear()
pansou.http_request = fake_http(200, json.dumps({"code": 0, "data": {
    "total": 2, "merged_by_type": {
        "quark": [{"url": "https://pan.quark.cn/s/z", "note": "王者天下 第6季", "password": ""}],
        "baidu": [{"url": "https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A", "note": "王者天下4", "password": "g7mh"}],
    }, "results": []}}, ensure_ascii=False))
out = pansou.render_text_resolve(pansou.do_search("王者天下", "https://s", use_cache=True), limit=5)
check("渲染含链接与提取码", "https://pan.quark.cn/s/z" in out and "提取码 g7mh" in out, out[:200])
check("相关时不出现核对提醒", "请自行核对" not in out)
pansou.cache_clear()
pansou.http_request = fake_http(200, json.dumps({"code": 0, "data": {
    "total": 1, "merged_by_type": {"quark": [{"url": "https://x/1", "note": "完全无关的片名"}]},
    "results": []}}, ensure_ascii=False))
out = pansou.render_text_resolve(pansou.do_search("王者天下", "https://s", use_cache=True))
check("低相关时提示核对", "请自行核对" in out, out[:120])

# ---------------------------------------------------------------- 13. 预算
print("[13] 预算合并")
pansou.DEADLINE_AT = None
d1 = pansou._effective_deadline(70)
check("阶段预算生效", d1 - time.monotonic() <= 70.5)
pansou.DEADLINE_AT = time.monotonic() + 5
check("与全局取较小值", pansou._effective_deadline(70) - time.monotonic() <= 5.5)
pansou.DEADLINE_AT = None

# ---------------------------------------------------------------- 14. 配置解析
print("[14] 实例地址与参数解析")
check("多实例+去尾斜杠", pansou.api_bases("https://a/, https://b") == ["https://a", "https://b"])
check("无 scheme 自动补", pansou.api_bases("example.com:8888") == ["http://example.com:8888"])
check("逗号分隔参数", pansou._split_csv("baidu, quark ,aliyun") == ["baidu", "quark", "aliyun"])
check("空参数返回 None", pansou._split_csv("") is None)

# ---------------------------------------------------------------- 15. 退出码
print("[15] 退出码")
pansou.cache_clear()
pansou.http_request = fake_http(0, "tunnel 502")
with contextlib.redirect_stdout(io.StringIO()):
    rc = pansou.main(["resolve", "x", "--no-cache"])
check("不可达 => 2", rc == 2, rc)
pansou.http_request = fake_http(200, '{"code":0,"data":{"total":0,"merged_by_type":{},"results":[]}}')
with contextlib.redirect_stdout(io.StringIO()):
    rc = pansou.main(["resolve", "x", "--no-cache"])
check("无结果 => 3", rc == 3, rc)
pansou.http_request = fake_http(200, '{"code":0,"data":{"total":1,"merged_by_type":{"quark":[{"url":"u","note":"甲"}]},"results":[]}}')
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pansou.main(["search", "甲", "--no-cache", "--json"])
check("成功 => 0 且 JSON 可解析", rc == 0 and json.loads(buf.getvalue())["status"] == "ok", rc)

print("[16] 缓存维护")
pansou.cache_write("k9", 200, "{}")
n = pansou.cache_clear()
check("清缓存返回条数", n >= 1, n)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pansou.main(["--clear-cache"])
check("--clear-cache 可独立执行", rc == 0 and "已清空缓存" in buf.getvalue(), rc)

shutil.rmtree(D, ignore_errors=True)
print()
print("== 汇总：{} 通过 / {} 失败 ==".format(ok, fail))
sys.exit(1 if fail else 0)
