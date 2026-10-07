# -*- coding: utf-8 -*-
"""pansearch.py 回归测试（离线，注入两个数据源）。"""
import io
import json
import os
import shutil
import sys
import tempfile
import contextlib

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.join(SKILL_ROOT, "scripts")
sys.path.insert(0, ROOT)
import pansou    # noqa: E402
import rrdy      # noqa: E402
import pansearch  # noqa: E402

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


D = os.path.join(tempfile.gettempdir(), "pansearch_regress")
os.makedirs(D, exist_ok=True)
pansou.CACHE_DIR = os.path.join(D, "pansou")
rrdy.CACHE_DIR = os.path.join(D, "rrdy")
pansou._CACHE_ON = True
rrdy._CACHE_ON = True

print("[1] 去重指纹 url_key")
check("忽略 query(pwd)", pansearch.url_key("https://pan.xunlei.com/s/AAA?pwd=1") ==
      pansearch.url_key("https://pan.xunlei.com/s/AAA?pwd=2"))
check("忽略尾部斜杠", pansearch.url_key("https://pan.quark.cn/s/a/") ==
      pansearch.url_key("https://pan.quark.cn/s/a"))
check("忽略 www 与主机大小写", pansearch.url_key("https://WWW.Pan.Quark.cn/s/A") ==
      pansearch.url_key("http://pan.quark.cn/s/A"))
# 分享 ID 大小写敏感：夸克/阿里的 ID 是短链标识，/s/A 与 /s/a 是两个不同资源。
# 早期实现整段 lower() 会把它们误合并，这里作为回归固定下来。
check("分享 ID 保留大小写", pansearch.url_key("https://pan.quark.cn/s/A") !=
      pansearch.url_key("https://pan.quark.cn/s/a"))
# 别名主机与子路径应折叠（同一分享的不同写法）
check("别名主机折叠(aliyun)", pansearch.url_key("https://www.aliyundrive.com/s/AAA") ==
      pansearch.url_key("https://www.alipan.com/s/AAA"))
check("子路径折叠(/folder)", pansearch.url_key("https://www.alipan.com/s/AAA") ==
      pansearch.url_key("https://www.alipan.com/s/AAA/folder/BBB"))
check("别名主机折叠(115)", pansearch.url_key("https://115cdn.com/s/AAA?password=1") ==
      pansearch.url_key("https://115.com/s/AAA"))
check("别名主机折叠(123)", pansearch.url_key("https://123912.com/s/aaa-bbb") ==
      pansearch.url_key("https://123684.com/s/aaa-bbb"))
check("不同资源不混同", pansearch.url_key("https://pan.quark.cn/s/a") !=
      pansearch.url_key("https://pan.quark.cn/s/b"))

print("[2] 网盘名映射")
check("人人电影网中文名 -> PanSou 键", pansearch.CN_TO_TYPE["夸克网盘"] == "quark"
      and pansearch.CN_TO_TYPE["迅雷云盘"] == "xunlei")
check("别名对齐（123网盘）", pansearch.CN_TO_TYPE["123网盘"] == "123")
check("未知类型回退原名", pansearch.type_name_of("whatever") == "whatever")
check("扩展类型有中文名", pansearch.type_name_of("lanzou") == "蓝奏云")

print("[3] 跨源合并去重")
pres = {"source": "pansou", "status": "ok", "links": [
    {"type": "quark", "url": "https://pan.quark.cn/s/a", "pwd": "", "note": "王者天下",
     "datetime": "2026-10-07 00:00", "source": "pansou", "origin": "plugin:x"},
    {"type": "quark", "url": "https://pan.quark.cn/s/b", "pwd": "", "note": "其他片",
     "datetime": "", "source": "pansou", "origin": "plugin:y"},
]}
rres = {"source": "rrdynb", "status": "ok", "links": [
    {"type": "quark", "url": "https://pan.quark.cn/s/a?pwd=xy", "pwd": "xy",
     "note": "王者天下", "datetime": "", "source": "rrdynb", "origin": "http://a.html"},
]}
items, dropped = pansearch.merge([pres, rres], "王者天下")
check("同链接跨源合并为 1 条", len(items) == 2, len(items))
check("无形态异常项时 dropped 为空", dropped == [], dropped)
a = [x for x in items if x["url"] == "https://pan.quark.cn/s/a"][0]
check("来源并列标注", set(a["sources"]) == {"pansou", "rrdynb"}, a["sources"])
check("缺失字段被另一源补全", a["pwd"] == "xy", a["pwd"])
check("相关性最高者排首位", items[0]["url"] == "https://pan.quark.cn/s/a", items[0]["url"])

print("[3b] 形态异常链接被剔除（P0-4）")
bres = {"source": "pansou", "status": "ok", "links": [
    {"type": "xunlei", "url": "https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM", "pwd": "",
     "note": "王者天下", "datetime": "", "source": "pansou", "origin": "tg:x"},
    {"type": "xunlei", "url": "https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM_hD6V2PA1",
     "pwd": "njfz", "note": "王者天下", "datetime": "", "source": "pansou", "origin": "tg:x"},
]}
items2, dropped2 = pansearch.merge([bres], "王者天下", keep_invalid=False)
check("截断的迅雷 ID 被剔除", len(items2) == 1 and len(dropped2) == 1,
      (len(items2), len(dropped2)))
check("剔除原因可读", "截断" in dropped2[0]["reason"], dropped2[0]["reason"])
items2k, dropped2k = pansearch.merge([bres], "王者天下", keep_invalid=True)
check("--keep-invalid 时保留", len(items2k) == 2 and dropped2k == [],
      (len(items2k), len(dropped2k)))

print("[3c] 跨源/别名去重（P0-2/P0-5）")
dres = {"source": "rrdynb", "status": "ok", "links": [
    {"type": "aliyun", "url": "https://www.aliyundrive.com/s/AAA/folder/BBB", "pwd": "",
     "note": "甲", "datetime": "", "source": "rrdynb", "origin": "http://a.html"},
]}
pres2 = {"source": "pansou", "status": "ok", "links": [
    {"type": "aliyun", "url": "https://www.alipan.com/s/AAA", "pwd": "",
     "note": "甲", "datetime": "", "source": "pansou", "origin": "tg:y"},
]}
items3, _ = pansearch.merge([pres2, dres], "甲")
check("别名主机+子路径合并为 1 条", len(items3) == 1, len(items3))
check("合并后标注双来源", set(items3[0]["sources"]) == {"pansou", "rrdynb"}, items3[0]["sources"])
check("重复计数被记录", items3[0].get("duplicates", 0) >= 1, items3[0].get("duplicates"))

print("[3d] 渲染截断提示（P0-1：与数据源是否可用无关）")
many = {"source": "pansou", "status": "ok", "links": [
    {"type": "quark", "url": "https://pan.quark.cn/s/%s" % ("a" * 12 + str(i)), "pwd": "",
     "note": "王者天下 %d" % i, "datetime": "", "source": "pansou", "origin": "tg:x"}
    for i in range(5)]}
its4, _ = pansearch.merge([many], "王者天下")
ok_only = [{"source": "pansou", "status": "ok", "links": many["links"]}]
txt = pansearch.render("王者天下", its4, ok_only, limit=2, per_type=None)
check("两源都可用时仍提示未显示条数", "另有 3 条未显示" in txt, txt[-160:])

print("[4] 数据源适配：PanSou")
pansearch.pansou.search_with_failover = lambda kw, bases, token=None, **kw2: {
    "status": "ok", "total": 1, "api_base": "https://s",
    "merged_by_type": {"quark": [{"url": "https://pan.quark.cn/s/z",
                                  "note": "王者天下", "password": "",
                                  "datetime": "2026-01-01T00:00:00Z",
                                  "source": "plugin:x"}]},
    "results": [],
}


class A:
    api = None
    token = None
    cloud_types = None
    force_search = False
    no_cache = True
    refresh = False


r = pansearch.from_pansou("王者天下", A())
check("PanSou 结果转统一结构", r["status"] == "ok" and r["links"][0]["type"] == "quark"
      and r["links"][0]["source"] == "pansou", r["links"][:1])
check("日期格式化", r["links"][0]["datetime"] == "2026-01-01 00:00", r["links"][0]["datetime"])

print("[5] 数据源适配：人人电影网")
pansearch.rrdy.do_resolve = lambda kw, top=3, **kw2: {
    "status": "ok", "candidates": 2, "articles": [
        {"title": "《王者天下》", "url": "http://a.html", "links": [
            {"provider": "夸克网盘", "url": "https://pan.quark.cn/s/q"},
            {"provider": "123网盘", "url": "https://www.123pan.com/s/1"},
        ]},
    ],
}


class B:
    top = 3
    jobs = 4
    no_cache = True
    refresh = False
    force_search = False


r2 = pansearch.from_rrdynb("王者天下", B())
check("文章内链接全部展开", len(r2["links"]) == 2, len(r2["links"]))
check("中文网盘名转类型键", {l["type"] for l in r2["links"]} == {"quark", "123"},
      {l["type"] for l in r2["links"]})
check("标题作为 note", r2["links"][0]["note"] == "《王者天下》")

print("[6] 渲染：按网盘分桶")
pansearch.pansou.search_with_failover = lambda kw, bases, token=None, **kw2: {
    "status": "ok", "total": 4, "api_base": "https://s",
    "merged_by_type": {
        "quark": [{"url": "https://pan.quark.cn/s/%d" % i, "note": "王者天下", "password": ""}
                  for i in range(3)],
        "baidu": [{"url": "https://pan.baidu.com/s/1", "note": "王者天下", "password": "aa11"}],
    }, "results": [],
}
res = [pansearch.from_pansou("王者天下", A())]
items, _ = pansearch.merge(res, "王者天下")
out = pansearch.render("王者天下", items, res, limit=30, per_type=2)
check("同一网盘只出现一个分组标题", out.count("【夸克网盘】") == 1, out.count("【夸克网盘】"))
check("per_type 截断生效", out.count("pan.quark.cn") == 2, out.count("pan.quark.cn"))
check("提取码展示", "提取码 aa11" in out)
check("表头含条数", "共 4 条链接" in out)

print("[7] 渲染：长链接截断与不可用提示")
long_url = "magnet:?xt=urn:btih:" + "a" * 200
res2 = [{"source": "pansou", "status": "ok", "links": [
    {"type": "magnet", "url": long_url, "pwd": "", "note": "王者天下", "datetime": "",
     "source": "pansou", "origin": "plugin:nyaa"}]}]
it2, _ = pansearch.merge(res2, "王者天下")
o2 = pansearch.render("王者天下", it2, res2, limit=30, per_type=6)
check("长链接被截断", "已截断" in o2 and long_url not in o2, o2[:80])

res3 = [{"source": "pansou", "status": "unreachable", "links": [], "detail": "tunnel 502"},
        {"source": "rrdynb", "status": "needs_fallback", "links": [],
         "blocked_reason": "cf_blocked",
         "search_url": "https://www.rrdynb.com/plus/search.php?q=x"}]
o3 = pansearch.render("王者天下", [], res3)
check("无结果时说明各源状态", "不可达" in o3 and "Cloudflare" in o3, o3[:200])
o4 = pansearch.render("王者天下", it2, res3)
check("有结果时同时提示失效源", "Cloudflare" in o4)

print("[7b] rrdynb 受阻时归一为 needs_fallback")
pansearch.rrdy.do_resolve = lambda kw, top=3, **kw2: {
    "status": "error", "http_status": 0, "fallback": True,
    "search_url": "https://www.rrdynb.com/plus/search.php?q=x", "articles": []}
r_fb = pansearch.from_rrdynb("王者天下", B())
check("本地不可达 -> needs_fallback 且带 search_url",
      r_fb["status"] == "needs_fallback" and "search.php" in (r_fb["search_url"] or ""),
      r_fb["status"])
pansearch.rrdy.do_resolve = lambda kw, top=3, **kw2: {
    "status": "cf_blocked", "search_url": "https://x/plus/search.php", "articles": []}
r_cf = pansearch.from_rrdynb("王者天下", B())
check("CF 拦截 -> needs_fallback 且原因可区分",
      r_cf["status"] == "needs_fallback" and r_cf["blocked_reason"] == "cf_blocked",
      r_cf.get("blocked_reason"))

print("[8] 退出码")
pansearch.pansou.search_with_failover = lambda kw, bases, token=None, **kw2: {
    "status": "unreachable", "detail": "tunnel 502"}
pansearch.rrdy.do_resolve = lambda kw, top=3, **kw2: {"status": "cf_blocked",
                                                     "search_url": "https://x/plus/search.php"}
with contextlib.redirect_stdout(io.StringIO()):
    rc = pansearch.main(["王者天下", "--no-cache"])
check("全部源不可用 => 2", rc == 2, rc)

pansearch.pansou.search_with_failover = lambda kw, bases, token=None, **kw2: {
    "status": "empty", "total": 0, "merged_by_type": {}, "results": []}
pansearch.rrdy.do_resolve = lambda kw, top=3, **kw2: {"status": "empty", "articles": []}
with contextlib.redirect_stdout(io.StringIO()):
    rc = pansearch.main(["王者天下", "--no-cache"])
check("源可用但无结果 => 3", rc == 3, rc)

pansearch.pansou.search_with_failover = lambda kw, bases, token=None, **kw2: {
    "status": "ok", "total": 1, "api_base": "https://s",
    "merged_by_type": {"quark": [{"url": "https://pan.quark.cn/s/z", "note": "王者天下"}]},
    "results": []}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pansearch.main(["王者天下", "--no-cache", "--json"])
d = json.loads(buf.getvalue())
check("有结果 => 0 且 JSON 合法", rc == 0 and d["total"] == 1 and d["links"][0]["type"] == "quark", rc)

print("[9] 单源异常不拖垮整体")
def boom(*a, **k):
    raise RuntimeError("source exploded")
pansearch.rrdy.do_resolve = boom
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pansearch.main(["王者天下", "--no-cache"])
check("另一源有结果即可成功", rc == 0 and "pan.quark.cn" in buf.getvalue(), rc)

print("[10] 清缓存可独立执行")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pansearch.main(["--clear-cache"])
check("--clear-cache 无需关键词", rc == 0 and "已清空缓存" in buf.getvalue(), rc)

shutil.rmtree(D, ignore_errors=True)
print()
print("== 汇总：{} 通过 / {} 失败 ==".format(ok, fail))
sys.exit(1 if fail else 0)
