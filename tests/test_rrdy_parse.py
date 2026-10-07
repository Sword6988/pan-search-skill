# -*- coding: utf-8 -*-
"""rrdy 解析层测试：用固定 HTML fixture，不联网。

fixture 覆盖了真实站点上踩过的坑：
  - 正文里 display:none 的通用推广位必须剔除
  - 锚文本含「客户端」不应被误杀（decoy 规则已收窄）
  - 上游偶发的截断分享 ID 要被标注 invalid
  - 写在链接**外面**的提取码（「提取码：SSTA」）要能解析到
"""

import os
import sys
import unittest

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))

import rrdy  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class TestParsePanLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load("article_page.html")
        cls.links = rrdy.parse_pan_links(cls.html)
        cls.by_url = {l["url"]: l for l in cls.links}

    def test_提取到全部正文链接(self):
        want = {
            "https://pan.quark.cn/s/da9f7d970d36",
            "https://www.aliyundrive.com/s/JcMCEu9xX9L/folder/64ba018e6a75e81e4c634a12aaa3f69bc8dbe76d",
            "https://pan.xunlei.com/s/VOk-rqt5f5Fi3GvWKNRIBVSAA1?pwd=v98j#",
            "https://pan.baidu.com/s/1jQnrcqWVR9JtNpXliQ84JA?pwd=ddrp",
            "https://123912.com/s/pbeojv-bLFbh",
            "https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM",
            "https://drive.uc.cn/s/61b78555901d4?public=1",
        }
        self.assertEqual(want, set(self.by_url))

    def test_推广位被剔除(self):
        for u in self.by_url:
            self.assertNotIn("pan.baidu.com/download", u)
            self.assertNotIn("1nvT6eE1", u)
            self.assertNotIn("notrelated", u)
        self.assertNotIn("https://pan.quark.cn/s/notrelated", self.by_url)

    def test_客户端锚文本不再误杀(self):
        # 旧规则把含「客户端」的锚文本一律当干扰链接，会丢掉这条有效链接
        self.assertIn("https://drive.uc.cn/s/61b78555901d4?public=1", self.by_url)

    def test_123912_域名被识别(self):
        l = self.by_url["https://123912.com/s/pbeojv-bLFbh"]
        self.assertEqual(l["provider"], "123网盘")

    def test_提取码写在链接外也可解析(self):
        l = self.by_url["https://123912.com/s/pbeojv-bLFbh"]
        self.assertEqual(l["pwd"], "SSTA")
        self.assertFalse(l["has_pwd_in_url"])

    def test_url_内的提取码不进_pwd_字段(self):
        l = self.by_url["https://pan.baidu.com/s/1jQnrcqWVR9JtNpXliQ84JA?pwd=ddrp"]
        self.assertEqual(l["pwd"], "ddrp")
        self.assertTrue(l["has_pwd_in_url"])

    def test_截断链接被标注(self):
        l = self.by_url["https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM"]
        self.assertIn("截断", l["invalid"])
        ok = self.by_url["https://pan.xunlei.com/s/VOk-rqt5f5Fi3GvWKNRIBVSAA1?pwd=v98j#"]
        self.assertEqual(ok["invalid"], "")

    def test_split_invalid_默认隐藏(self):
        shown, bad = rrdy.split_invalid(self.links)
        self.assertEqual(len(shown) + len(bad), len(self.links))
        self.assertEqual(len(bad), 1)
        rrdy.SHOW_INVALID = True
        try:
            shown2, bad2 = rrdy.split_invalid(self.links)
            self.assertEqual(bad2, [])
            self.assertEqual(len(shown2), len(self.links))
        finally:
            rrdy.SHOW_INVALID = False

    def test_渲染默认隐藏异常链接(self):
        out = rrdy.render_text_links({
            "status": "ok", "url": "http://x", "article": {"title": "t"},
            "links": self.links})
        self.assertNotIn("VP2QdPpoCbscUHKAM\n", out)
        self.assertIn("形态异常", out)


class TestParseSearchResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load("search_page.html")
        cls.results = rrdy.parse_search_results(cls.html)

    def test_文章链接被提取且补全域名(self):
        urls = {r["url"] for r in self.results}
        self.assertIn("https://www.rrdynb.com/movie/2020/0408/9843.html", urls)
        self.assertIn("https://www.rrdynb.com/movie/2020/0216/8515.html", urls)
        self.assertIn("https://www.rrdynb.com/movie/2020/0408/9842.html", urls)

    def test_搜索页自身链接不被当成文章(self):
        for r in self.results:
            self.assertNotIn("search.php", r["url"])

    def test_标题去噪并保留高亮标记(self):
        titles = [r["title"] for r in self.results]
        self.assertTrue(any("死亡笔记" in t for t in titles))
        for t in titles:
            self.assertNotIn("<font", t)
            self.assertNotIn("免费电影下载", t)

    def test_aid_解析(self):
        aids = {r["aid"] for r in self.results}
        self.assertIn(9843, aids)

    def test_rank_把高亮命中排前(self):
        ranked = rrdy.rank_results(self.results, "死亡笔记")
        self.assertTrue(ranked[0]["highlight"])
        self.assertIn("死亡笔记", ranked[0]["title"])


class TestParseArticleMeta(unittest.TestCase):
    def test_元信息(self):
        meta = rrdy.parse_article_meta(load("article_page.html"))
        self.assertIn("死亡笔记", meta["title"])
        self.assertNotIn("人人电影网", meta["title"])
        self.assertIn("Death Note", meta["alias"])
        self.assertIn("death note", meta["keywords"].lower())

    def test_从标题提取作品年份(self):
        """docstring 一直声称会提取年份，此前实现里其实没有；
        而同一片名常有多个年份的版本，年份是确认命中的关键。"""
        self.assertEqual(
            rrdy.parse_article_meta("<title>死亡笔记(2006)_人人电影网</title>")["year"],
            "2006")
        self.assertEqual(
            rrdy.parse_article_meta("<title>王者天下 第三季(2021)_人人电影网</title>")["year"],
            "2021")
        self.assertEqual(
            rrdy.parse_article_meta("<title>我不是大师_人人电影网</title>").get("year"),
            None)
        self.assertNotIn("year", rrdy.parse_article_meta("<title>x</title>"))


class TestCloudflareDetection(unittest.TestCase):
    def test_正常页面不误判(self):
        self.assertFalse(rrdy.looks_like_challenge(load("article_page.html")))
        self.assertFalse(rrdy.looks_like_challenge(load("search_page.html")))

    def test_挑战页被识别(self):
        html = "<html><head><title>Just a moment...</title></head>" \
               "<body>Enable JavaScript and cookies to continue</body></html>"
        self.assertTrue(rrdy.looks_like_challenge(html))

    def test_空输入(self):
        self.assertFalse(rrdy.looks_like_challenge(""))
        self.assertFalse(rrdy.looks_like_challenge(None))


class TestBlockMarker(unittest.TestCase):
    def test_标记读写与_TTL(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            old_dir, old_on = rrdy.CACHE_DIR, rrdy._CACHE_ON
            rrdy.CACHE_DIR = d
            rrdy._CACHE_ON = True
            try:
                self.assertFalse(rrdy.search_endpoint_blocked())
                rrdy.mark_search_endpoint_blocked()
                self.assertTrue(rrdy.search_endpoint_blocked())
                rrdy.clear_search_block_marker()
                self.assertFalse(rrdy.search_endpoint_blocked())
            finally:
                rrdy.CACHE_DIR, rrdy._CACHE_ON = old_dir, old_on

    def test_默认_TTL_为_6_小时(self):
        self.assertEqual(rrdy.SEARCH_BLOCK_TTL, 6 * 3600.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
