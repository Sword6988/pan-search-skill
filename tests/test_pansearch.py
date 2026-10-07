# -*- coding: utf-8 -*-
"""pansearch 的集成测试：合并/剔除、Markdown 与文件导出、参数透传、
链接有效性抽查、人人电影网文章回填。全部离线（网络函数被替换）。
"""

import io
import json
import os
import sys
import tempfile
import unittest
import contextlib

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))

import pansearch  # noqa: E402


def args_of(argv):
    return pansearch.build_parser().parse_args(argv)


def res(source, links, status="ok"):
    return {"source": source, "status": status, "links": links}


def link(t, url, note="王者天下", pwd="", dt="", origin=""):
    return {"type": t, "url": url, "pwd": pwd, "note": note,
            "datetime": dt, "source": "pansou", "origin": origin,
            "invalid": "", "duplicates": 0}


class TestFormatInference(unittest.TestCase):
    def test_默认文本(self):
        self.assertEqual(pansearch._resolve_format(args_of(["x"])), "text")

    def test_json_开关(self):
        self.assertEqual(pansearch._resolve_format(args_of(["x", "--json"])), "json")

    def test_显式_format_优先(self):
        self.assertEqual(
            pansearch._resolve_format(args_of(["x", "-o", "a.md", "--format", "json"])),
            "json")

    def test_按扩展名推断(self):
        self.assertEqual(pansearch._resolve_format(args_of(["x", "-o", "a.md"])), "md")
        self.assertEqual(pansearch._resolve_format(args_of(["x", "-o", "a.markdown"])), "md")
        self.assertEqual(pansearch._resolve_format(args_of(["x", "-o", "a.json"])), "json")
        self.assertEqual(pansearch._resolve_format(args_of(["x", "-o", "a.txt"])), "text")


class TestCSV(unittest.TestCase):
    def test_逗号与换行(self):
        self.assertEqual(pansearch._csv("全集,合集"), ["全集", "合集"])
        self.assertEqual(pansearch._csv("全集\n合集"), ["全集", "合集"])
        self.assertEqual(pansearch._csv(" 全集 , ,合集 "), ["全集", "合集"])
        self.assertIsNone(pansearch._csv(None))
        self.assertIsNone(pansearch._csv(""))


class TestMarkdownExport(unittest.TestCase):
    def setUp(self):
        items, _ = pansearch.merge([
            res("pansou", [
                link("quark", "https://pan.quark.cn/s/6c615522c5d0", "死亡笔记 全37集",
                     dt="2026-08-22 13:23", origin="tg:tgbokee"),
                link("baidu", "https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A",
                     "死亡笔记 REMUX", pwd="d6kp"),
                link("magnet", "magnet:?xt=urn:btih:AAA", "死亡笔记 全集"),
            ]),
        ], "死亡笔记")
        self.items = items
        self.md = pansearch.render_markdown(
            "死亡笔记", items, [res("pansou", [])], dropped=[], per_type=None)

    def test_头部信息(self):
        self.assertIn("# 网盘资源：死亡笔记", self.md)
        self.assertIn("- 链接总数：3", self.md)
        self.assertIn("PanSou", self.md)

    def test_按网盘分节且含表格(self):
        self.assertIn("## 夸克网盘", self.md)
        self.assertIn("## 百度网盘", self.md)
        self.assertIn("| # | 链接 | 提取码 |", self.md)

    def test_提取码列(self):
        self.assertIn("| d6kp |", self.md)

    def test_标题里竖线被转义(self):
        items, _ = pansearch.merge([res("pansou", [
            link("quark", "https://pan.quark.cn/s/abcdefghijkl", "a|b")])], "a")
        md = pansearch.render_markdown("a", items, [res("pansou", [])])
        row = [l for l in md.splitlines() if l.startswith("| 1 |")][0]
        self.assertEqual(row.count("|"), 7)

    def test_剔除章节(self):
        md = pansearch.render_markdown(
            "死亡笔记", self.items, [res("pansou", [])],
            dropped=[{"url": "https://pan.xunlei.com/s/x", "type": "xunlei",
                      "note": "", "reason": "分享 ID 过短"}])
        self.assertIn("## 已剔除的形态异常链接", md)
        self.assertIn("分享 ID 过短", md)


class TestMainOutputs(unittest.TestCase):
    """通过替换数据源适配函数，完整走一遍 main() 的输出与退出码。"""

    def setUp(self):
        self._from_pansou = pansearch.from_pansou
        self._from_rrdynb = pansearch.from_rrdynb
        pansearch.from_pansou = lambda kw, a: res("pansou", [
            link("quark", "https://pan.quark.cn/s/6c615522c5d0", "死亡笔记 全37集",
                 dt="2026-08-22 13:23"),
            link("xunlei", "https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM", "死亡笔记"),
            link("baidu", "https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A",
                 "死亡笔记 REMUX", pwd="d6kp"),
        ])
        pansearch.from_rrdynb = lambda kw, a: res("rrdynb", [], status="needs_fallback")
        pansearch.netlib.configure = lambda *a, **k: "测试出口"

    def tearDown(self):
        pansearch.from_pansou = self._from_pansou
        pansearch.from_rrdynb = self._from_rrdynb

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = pansearch.main(argv)
        return rc, buf.getvalue()

    def test_文本输出含分布与剔除提示(self):
        rc, out = self._run(["死亡笔记", "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertIn("分布：", out)
        self.assertIn("已剔除 1 条形态异常的链接", out)
        self.assertNotIn("VP2QdPpoCbscUHKAM\n", out)
        self.assertIn("提取码 d6kp", out)

    def test_json_结构(self):
        rc, out = self._run(["死亡笔记", "--no-cache", "--json"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["total"], 2)
        self.assertEqual(len(d["dropped"]), 1)
        self.assertIn("verify", d)
        self.assertEqual(d["sources"][0]["source"], "pansou")

    def test_keep_invalid_保留异常项(self):
        rc, out = self._run(["死亡笔记", "--no-cache", "--keep-invalid"])
        self.assertEqual(rc, 0)
        self.assertIn("VP2QdPpoCbscUHKAM", out)
        self.assertNotIn("已剔除", out)

    def test_写出_markdown_文件(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out.md")
            rc, out = self._run(["死亡笔记", "--no-cache", "-o", p])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(p))
            with open(p, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("# 网盘资源：死亡笔记", body)
            self.assertIn("## 夸克网盘", body)

    def test_写出_json_文件(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out.json")
            rc, _ = self._run(["死亡笔记", "--no-cache", "-o", p])
            self.assertEqual(rc, 0)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["total"], 2)

    def test_无结果时退出码3(self):
        pansearch.from_pansou = lambda kw, a: res("pansou", [])
        rc, _ = self._run(["不存在的片名", "--no-cache"])
        self.assertEqual(rc, 3)


class TestPassThrough(unittest.TestCase):
    def test_过滤参数透传给_PanSou(self):
        captured = {}

        def fake(kw, bases, token=None, **kw2):
            captured.update(kw2)
            captured["kw"] = kw
            return {"status": "empty", "api_base": bases[0], "total": 0,
                    "merged_by_type": {}, "results": []}

        old = pansearch.pansou.search_with_failover
        pansearch.pansou.search_with_failover = fake
        try:
            a = args_of(["唐朝诡事录", "--include", "全集,合集",
                         "--exclude", "预告,花絮", "--src", "plugin",
                         "--res", "all", "--plugins", "wanou",
                         "--cloud-types", "quark,aliyun"])
            pansearch.from_pansou("唐朝诡事录", a)
        finally:
            pansearch.pansou.search_with_failover = old

        self.assertEqual(captured["include"], ["全集", "合集"])
        self.assertEqual(captured["exclude"], ["预告", "花絮"])
        self.assertEqual(captured["src"], "plugin")
        self.assertEqual(captured["res"], "all")
        self.assertEqual(captured["plugins"], ["wanou"])
        self.assertEqual(captured["cloud_types"], ["quark", "aliyun"])


class TestRrdynbArticles(unittest.TestCase):
    """WebFetch 兜底拿到文章 URL 后回填，跳过本地搜索（本地过不了 CF 挑战）。"""

    def test_回填并展开链接(self):
        called = {}

        def fake_many(targets, use_cache=True, refresh=False, jobs=4):
            called["targets"] = targets
            return [{
                "status": "ok",
                "url": targets[0],
                "article": {"title": "《死亡笔记》百度云网盘下载.1080P阿里下载.日语中字"},
                "links": [
                    {"provider": "夸克网盘", "label": "夸克网盘",
                     "url": "https://pan.quark.cn/s/da9f7d970d36", "pwd": "",
                     "has_pwd_in_url": False, "invalid": ""},
                    {"provider": "123网盘", "label": "123网盘",
                     "url": "https://123912.com/s/pbeojv-bLFbh", "pwd": "SSTA",
                     "has_pwd_in_url": False, "invalid": ""},
                ],
            }]

        old = pansearch.rrdy.do_links_many
        pansearch.rrdy.do_links_many = fake_many
        try:
            a = args_of(["死亡笔记", "--rrdynb-articles",
                         "https://www.rrdynb.com/dongman/2022/0213/24202.html"])
            r = pansearch.from_rrdynb("死亡笔记", a)
        finally:
            pansearch.rrdy.do_links_many = old

        self.assertEqual(r["status"], "ok")
        self.assertEqual(called["targets"],
                         ["https://www.rrdynb.com/dongman/2022/0213/24202.html"])
        self.assertEqual({l["type"] for l in r["links"]}, {"quark", "123"})
        self.assertEqual(r["links"][0]["note"], "《死亡笔记》百度云网盘下载.1080P阿里下载.日语中字")

    def test_多篇文章逗号与换行都支持(self):
        a = args_of(["x", "--rrdynb-articles", "http://a/1.html,http://a/2.html"])
        self.assertEqual(pansearch._csv(a.rrdynb_articles),
                         ["http://a/1.html", "http://a/2.html"])

    def test_全部失败时报错状态(self):
        old = pansearch.rrdy.do_links_many
        pansearch.rrdy.do_links_many = lambda t, **k: [
            {"status": "error", "url": u, "links": []} for u in t]
        try:
            a = args_of(["x", "--rrdynb-articles", "http://a/1.html"])
            r = pansearch.from_rrdynb("x", a)
        finally:
            pansearch.rrdy.do_links_many = old
        self.assertEqual(r["status"], "error")


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.items, _ = pansearch.merge([res("pansou", [
            link("quark", "https://pan.quark.cn/s/6c615522c5d0", "死亡笔记"),
            link("quark", "https://pan.quark.cn/s/5ef2be7f164e", "死亡笔记"),
        ])], "死亡笔记")

    def test_实例不支持时优雅降级(self):
        old = pansearch.pansou.do_check_links
        pansearch.pansou.do_check_links = lambda *a, **k: {
            "status": "unsupported", "http_status": 404, "detail": "该实例未提供"}
        try:
            r = pansearch.verify_links(self.items, 2, args_of(["x", "--verify", "2"]))
        finally:
            pansearch.pansou.do_check_links = old
        self.assertEqual(r["status"], "unsupported")
        txt = pansearch.render("死亡笔记", self.items, [res("pansou", [])],
                               verify_result=r)
        self.assertIn("未做链接有效性检测", txt)
        self.assertIn("仅自建实例提供", txt)

    def test_404_被识别为不支持(self):
        old = pansearch.pansou.cached_request
        pansearch.pansou.cached_request = lambda *a, **k: (404, "404 page not found")
        try:
            r = pansearch.pansou.do_check_links(["https://pan.quark.cn/s/6c615522c5d0"],
                                                "https://s")
        finally:
            pansearch.pansou.cached_request = old
        self.assertEqual(r["status"], "unsupported")
        self.assertIn("404", r["detail"])

    def test_标注失效链接并可剔除(self):
        old = pansearch.pansou.do_check_links
        pansearch.pansou.do_check_links = lambda urls, *a, **k: {
            "status": "ok",
            "data": {"results": [
                {"url": urls[0], "status": "invalid", "message": "已失效"},
                {"url": urls[1], "status": True},
            ]},
        }
        try:
            a = args_of(["x", "--verify", "2", "--drop-dead"])
            r = pansearch.verify_links(self.items, 2, a)
        finally:
            pansearch.pansou.do_check_links = old
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.items[0].get("alive"), "invalid")
        self.assertIn(self.items[0].get("alive_note"), ("已失效", "已失效"))
        self.assertEqual(self.items[1].get("alive"), "ok")
        txt = pansearch.render("死亡笔记", self.items, [res("pansou", [])],
                               verify_result=r)
        self.assertIn("！链接已失效", txt)

    def test_zero_表示不检测(self):
        self.assertIsNone(pansearch.verify_links(self.items, 0, args_of(["x"])))

    def test_布尔字段解析(self):
        m = pansearch.pansou.link_statuses({"status": "ok", "data": [
            {"url": "a", "valid": False}, {"url": "b", "valid": True},
            {"url": "c", "status": "expired"}, {"url": "d"}]})
        self.assertEqual(m["a"][0], "invalid")
        self.assertEqual(m["b"][0], "ok")
        self.assertEqual(m["c"][0], "invalid")
        self.assertEqual(m["d"][0], "unknown")


class TestSummary(unittest.TestCase):
    def test_分布按网盘优先级排序(self):
        items, _ = pansearch.merge([res("pansou", [
            link("baidu", "https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A"),
            link("quark", "https://pan.quark.cn/s/6c615522c5d0"),
            link("quark", "https://pan.quark.cn/s/5ef2be7f164e"),
        ])], "王者天下")
        s = pansearch._counts_by_type(items)
        self.assertIn("夸克网盘 2", s)
        self.assertIn("百度网盘 1", s)
        self.assertLess(s.index("夸克网盘"), s.index("百度网盘"))


class TestSortAndScore(unittest.TestCase):
    """`--sort date` 曾经是死参数、`--min-score` 曾经不存在，这里锁住行为。"""

    def _res(self):
        return [res("pansou", [
            link("quark", "https://pan.quark.cn/s/old", "死亡笔记 全集", dt="2024-01-01 10:00"),
            link("quark", "https://pan.quark.cn/s/new", "死亡笔记 4K", dt="2026-10-07 09:00"),
            link("baidu", "https://pan.baidu.com/s/mid", "死亡笔记", dt="2025-05-05 09:00"),
            link("xunlei", "https://pan.xunlei.com/s/VP2Or2wSq0m83i6uBmnnidZIA1",
                 "笔记 杂项", dt=""),
        ])]

    def test_按日期排序且无日期沉底(self):
        items, _ = pansearch.merge(self._res(), "死亡笔记", sort_by="date")
        self.assertEqual([i["note"] for i in items],
                         ["死亡笔记 4K", "死亡笔记", "死亡笔记 全集", "笔记 杂项"])

    def test_默认按相关性(self):
        items, _ = pansearch.merge(self._res(), "死亡笔记")
        self.assertEqual(items[0]["note"], "死亡笔记")

    def test_min_score_过滤弱相关并如实列出(self):
        items, dropped = pansearch.merge(self._res(), "死亡笔记", min_score=80)
        self.assertNotIn("笔记 杂项", [i["note"] for i in items])
        self.assertTrue(any("低于阈值" in d["reason"] for d in dropped))

    def test_分桶也尊重_sort(self):
        items, _ = pansearch.merge(self._res(), "死亡笔记", sort_by="date")
        buckets, order = pansearch._buckets(items, None, "date")
        self.assertEqual(buckets["quark"][0]["note"], "死亡笔记 4K")

    def test_CLI_参数真的传到_merge(self):
        """回归：`--sort` 曾在 CLI 声明、却没有任何地方读取，静默失效。"""
        seen = {}
        old = pansearch.merge

        def fake(results, kw, **kw2):
            seen.update(kw2)
            return [], []

        pansearch.merge = fake
        try:
            pansearch._search_one("x", args_of(["x", "--sort", "date", "--min-score", "40"]))
        finally:
            pansearch.merge = old
        self.assertEqual(seen.get("sort_by"), "date")
        self.assertEqual(seen.get("min_score"), 40)


class TestGrouping(unittest.TestCase):
    def test_标题归一化保留年份季数(self):
        self.assertEqual(pansearch.title_key("死亡笔记 全37集 1080P 日语中字"), "死亡笔记")
        self.assertEqual(pansearch.title_key("【夸克】死亡笔记 4K 内封简中"), "死亡笔记")
        self.assertNotEqual(pansearch.title_key("死亡笔记(2006)"),
                            pansearch.title_key("死亡笔记(2016)"))
        self.assertIn("死亡笔记", pansearch.title_key("死亡笔记(2006)"))

    def test_同网盘折叠其余挂_variants(self):
        g = pansearch.group_items([
            link("quark", "https://pan.quark.cn/s/a11", "死亡笔记 全集"),
            link("quark", "https://pan.quark.cn/s/a22", "死亡笔记 4K"),
            link("quark", "https://pan.quark.cn/s/a33", "死亡笔记 1080P"),
        ])
        self.assertEqual(len(g), 1)
        self.assertEqual(len(g[0]["variants"]), 2)
        self.assertEqual(g[0]["duplicates"], 2)

    def test_跨网盘不折叠(self):
        g = pansearch.group_items([
            link("quark", "https://pan.quark.cn/s/a11", "死亡笔记 全集"),
            link("baidu", "https://pan.baidu.com/s/b11", "死亡笔记 全集"),
        ])
        self.assertEqual(len(g), 2)

    def test_不把别的分享的提取码补到代表条目(self):
        g = pansearch.group_items([
            link("quark", "https://pan.quark.cn/s/a11", "死亡笔记 全集"),
            link("quark", "https://pan.quark.cn/s/a22", "死亡笔记 4K", pwd="xy12"),
        ])
        self.assertEqual(g[0]["pwd"], "")
        self.assertEqual(g[0]["variants"][0]["pwd"], "xy12")

    def test_渲染时列出_variants(self):
        items, _ = pansearch.merge([res("pansou", [
            link("quark", "https://pan.quark.cn/s/a11", "死亡笔记 全集"),
            link("quark", "https://pan.quark.cn/s/a22", "死亡笔记 4K"),
        ])], "死亡笔记")
        txt = pansearch.render("死亡笔记", pansearch.group_items(items),
                               [res("pansou", [])], limit=10, per_type=6)
        self.assertIn("└ https://pan.quark.cn/s/a22", txt)
        self.assertIn("另有 1 条相同资源", txt)

    def test_no_group_开关存在(self):
        self.assertTrue(args_of(["x", "--no-group"]).no_group)
        self.assertFalse(args_of(["x"]).no_group)


class TestVerifyFeedback(unittest.TestCase):
    """回归：验链成功时曾经**完全静默**，用户看不到检测结果。"""

    def _items(self):
        items, _ = pansearch.merge([res("pansou", [
            link("quark", "https://pan.quark.cn/s/6c615522c5d0", "死亡笔记")])], "死亡笔记")
        return items

    def test_成功时报告检测数量与失效数(self):
        txt = pansearch.render("死亡笔记", self._items(), [res("pansou", [])],
                               verify_result={"status": "ok", "checked": 3,
                                              "alive_ok": 2, "alive_dead": 1,
                                              "alive_unknown": 0})
        self.assertIn("检测 3 条链接", txt)
        self.assertIn("1 条已失效", txt)

    def test_全部有效时也有反馈(self):
        txt = pansearch.render("死亡笔记", self._items(), [res("pansou", [])],
                               verify_result={"status": "ok", "checked": 2,
                                              "alive_ok": 2, "alive_dead": 0,
                                              "alive_unknown": 0})
        self.assertIn("未发现失效", txt)

    def test_统计字段由_verify_links_回传(self):
        old = pansearch.pansou.do_check_links
        pansearch.pansou.do_check_links = lambda urls, *a, **k: {
            "status": "ok",
            "data": {"results": [{"url": urls[0], "status": False}]},
        }
        try:
            items = self._items()
            r = pansearch.verify_links(items, 1, args_of(["x", "--verify", "1"]))
        finally:
            pansearch.pansou.do_check_links = old
        self.assertEqual(r["checked"], 1)
        self.assertEqual(r["alive_dead"], 1)
        self.assertEqual(r["alive_ok"], 0)


class TestRrdynbFields(unittest.TestCase):
    def test_从文章_URL_取发布日期(self):
        self.assertEqual(pansearch._date_from_article_url(
            "https://www.rrdynb.com/movie/2020/0216/8515.html"), "2020-02-16 00:00")
        self.assertEqual(pansearch._date_from_article_url(
            "https://www.rrdynb.com/plus/view.php?aid=6150"), "")

    def test_条目带上日期_标签与_rrdy_的校验结论(self):
        arts = [{
            "title": "死亡笔记 迅雷下载_人人电影网",
            "url": "https://www.rrdynb.com/dongman/2022/0213/24202.html",
            "article": {"title": "死亡笔记(2006)"},
            "links": [
                {"provider": "夸克网盘", "label": "1080P 全37集",
                 "url": "https://pan.quark.cn/s/da9f7d970d36", "pwd": "",
                 "has_pwd_in_url": False, "invalid": ""},
                {"provider": "迅雷云盘", "label": "资源：迅雷云盘",
                 "url": "https://pan.xunlei.com/s/VP2Or2wSq0m83i6uBmnnidZIA1",
                 "pwd": "", "has_pwd_in_url": False,
                 "invalid": "分享 ID 过短（5 < 20 字符），疑为上游截断"},
            ],
        }]
        out = pansearch._rrdynb_links_from_articles(arts)
        self.assertEqual(out[0]["datetime"], "2022-02-13 00:00")
        self.assertIn("1080P 全37集", out[0]["note"])
        self.assertEqual(out[0]["origin"],
                         "https://www.rrdynb.com/dongman/2022/0213/24202.html")
        # 无信息量的锚文本（纯网盘名）不拼进标题
        self.assertEqual(out[1]["note"], "死亡笔记(2006)")
        # rrdy 已算好的形态判定不再被清空
        self.assertTrue(out[1]["invalid"])

    def test_日期让_rrdynb_条目参与日期排序(self):
        arts = [{
            "title": "t", "url": "https://www.rrdynb.com/movie/2026/1007/70000.html",
            "article": {"title": "死亡笔记"},
            "links": [{"provider": "夸克网盘", "label": "",
                       "url": "https://pan.quark.cn/s/newshare01", "pwd": "",
                       "has_pwd_in_url": False, "invalid": ""}],
        }]
        res_rr = {"source": "rrdynb", "status": "ok",
                  "links": pansearch._rrdynb_links_from_articles(arts)}
        res_ps = res("pansou", [link("quark", "https://pan.quark.cn/s/oldshare01",
                                     "死亡笔记 旧帖", dt="2020-02-16 00:00")])
        items, _ = pansearch.merge([res_rr, res_ps], "死亡笔记", sort_by="date")
        self.assertEqual(items[0]["datetime"], "2026-10-07 00:00")

    def test_origin_压缩但保留_tg_频道名(self):
        self.assertEqual(pansearch._short_origin(
            "https://www.rrdynb.com/dongman/2022/0213/24202.html"),
            "www.rrdynb.com/24202.html")
        self.assertEqual(pansearch._short_origin("/melost"), "/melost")
        self.assertEqual(pansearch._short_origin(""), "")


class TestMultiKeyword(unittest.TestCase):
    """一次给多个名称：逐个查询，输出依次拼接。"""

    def setUp(self):
        self._from_pansou = pansearch.from_pansou
        self._from_rrdynb = pansearch.from_rrdynb
        pansearch.from_pansou = lambda kw, a: res("pansou", [
            link("quark", "https://pan.quark.cn/s/abcdefgh", kw)])
        pansearch.from_rrdynb = lambda kw, a: res("rrdynb", [], status="needs_fallback")
        pansearch.netlib.configure = lambda *a, **k: "测试出口"

    def tearDown(self):
        pansearch.from_pansou = self._from_pansou
        pansearch.from_rrdynb = self._from_rrdynb

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = pansearch.main(argv)
        return rc, buf.getvalue()

    def test_两个关键词各出结果(self):
        rc, out = self._run(["死亡笔记", "我不是大师", "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertIn("【死亡笔记】", out)
        self.assertIn("【我不是大师】", out)

    def test_多关键词_json_为数组结构(self):
        rc, out = self._run(["死亡笔记", "我不是大师", "--no-cache", "--json"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["keywords"], ["死亡笔记", "我不是大师"])
        self.assertEqual(len(d["results"]), 2)

    def test_单关键词_json_保持原结构(self):
        rc, out = self._run(["死亡笔记", "--no-cache", "--json"])
        d = json.loads(out)
        self.assertEqual(d["keyword"], "死亡笔记")
        self.assertNotIn("results", d)

    def test_keyword_可以为空供_clear_cache_使用(self):
        self.assertEqual(args_of(["--clear-cache"]).keyword, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
