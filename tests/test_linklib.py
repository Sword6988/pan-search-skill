# -*- coding: utf-8 -*-
"""linklib 的单元测试：去重指纹、链接清洗、形态校验。

全部用固定字符串，不发网络请求。这里的用例大多直接取自真实跑批输出，
例如那条被上游截断的迅雷链接与粘着「提取码」的 123 云盘链接。
"""

import os
import sys
import unittest

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))

import linklib  # noqa: E402


class TestFingerprint(unittest.TestCase):
    def test_忽略_scheme_主机大小写与_www(self):
        self.assertEqual(linklib.fingerprint("https://WWW.Pan.Quark.cn/s/A"),
                         linklib.fingerprint("http://pan.quark.cn/s/A"))

    def test_分享_ID_保留大小写(self):
        # 夸克/阿里的短链 ID 大小写敏感，整段 lower() 会把两个资源误合并
        self.assertNotEqual(linklib.fingerprint("https://pan.quark.cn/s/A"),
                            linklib.fingerprint("https://pan.quark.cn/s/a"))

    def test_忽略_query_与_尾部斜杠(self):
        self.assertEqual(linklib.fingerprint("https://pan.xunlei.com/s/AAA?pwd=1"),
                         linklib.fingerprint("https://pan.xunlei.com/s/AAA?pwd=2"))
        self.assertEqual(linklib.fingerprint("https://pan.quark.cn/s/abc/"),
                         linklib.fingerprint("https://pan.quark.cn/s/abc"))

    def test_折叠别名主机(self):
        pairs = [
            ("https://www.aliyundrive.com/s/AAA", "https://www.alipan.com/s/AAA"),
            ("https://115cdn.com/s/AAA?password=1", "https://115.com/s/AAA"),
            ("https://anxia.com/s/AAA", "https://115cdn.com/s/AAA"),
            ("https://123912.com/s/aaa-bbb", "https://123684.com/s/aaa-bbb"),
            ("https://123pan.com/s/aaa-bbb", "https://123912.com/s/aaa-bbb"),
            ("https://fast.uc.cn/s/AAA", "https://drive.uc.cn/s/AAA"),
            ("https://yun.baidu.com/s/AAA", "https://pan.baidu.com/s/AAA"),
        ]
        for a, b in pairs:
            with self.subTest(a=a):
                self.assertEqual(linklib.fingerprint(a), linklib.fingerprint(b))

    def test_折叠子路径(self):
        # 同一分享的目录深链与裸链是同一个资源
        self.assertEqual(
            linklib.fingerprint("https://www.aliyundrive.com/s/AAA/folder/BBB"),
            linklib.fingerprint("https://www.aliyundrive.com/s/AAA"))
        self.assertEqual(
            linklib.fingerprint("https://pan.xunlei.com/s/AAA/sub/dir"),
            linklib.fingerprint("https://pan.xunlei.com/s/AAA"))

    def test_不同资源不混同(self):
        self.assertNotEqual(linklib.fingerprint("https://pan.quark.cn/s/aaa"),
                            linklib.fingerprint("https://pan.quark.cn/s/bbb"))
        # 不同网盘的同名 ID 不应合并
        self.assertNotEqual(linklib.fingerprint("https://pan.quark.cn/s/AAA"),
                            linklib.fingerprint("https://pan.baidu.com/s/AAA"))

    def test_磁力按_btih_归一(self):
        a = "magnet:?xt=urn:btih:ABC123&dn=foo"
        b = "magnet:?xt=urn:btih:abc123&dn=bar"
        self.assertEqual(linklib.fingerprint(a), linklib.fingerprint(b))
        self.assertNotEqual(linklib.fingerprint(a),
                            linklib.fingerprint("magnet:?xt=urn:btih:DDD"))

    def test_电驴按哈希归一(self):
        a = "ed2k://|file|a.mkv|100|ABCDEF|/"
        b = "ed2k://|file|b.mkv|200|abcdef|/"
        self.assertEqual(linklib.fingerprint(a), linklib.fingerprint(b))

    def test_异常输入(self):
        self.assertEqual(linklib.fingerprint(""), "")
        self.assertEqual(linklib.fingerprint(None), "")
        self.assertTrue(linklib.fingerprint("not a url"))

    def test_兼容旧名_url_key(self):
        self.assertEqual(linklib.url_key("https://pan.quark.cn/s/AAA"),
                         linklib.fingerprint("https://pan.quark.cn/s/AAA"))


class TestCleanUrl(unittest.TestCase):
    def test_无括号提取码后缀(self):
        # 真实跑批中出现过：https://123912.com/s/pbeojv-bLFbh提取码:SSTA
        u, pwd = linklib.clean_url("https://123912.com/s/pbeojv-bLFbh提取码:SSTA")
        self.assertEqual(u, "https://123912.com/s/pbeojv-bLFbh")
        self.assertEqual(pwd, "SSTA")

    def test_问号紧接提取码(self):
        u, pwd = linklib.clean_url("https://123912.com/s/pbeojv-7LFbh?提取码:SSTA")
        self.assertEqual(u, "https://123912.com/s/pbeojv-7LFbh")
        self.assertEqual(pwd, "SSTA")

    def test_括号形式(self):
        u, pwd = linklib.clean_url("https://pan.quark.cn/s/abc（访问码：k9m2）")
        self.assertEqual(u, "https://pan.quark.cn/s/abc")
        self.assertEqual(pwd, "k9m2")
        u, pwd = linklib.clean_url("https://pan.quark.cn/s/abc(提取码: k9m2)")
        self.assertEqual(u, "https://pan.quark.cn/s/abc")
        self.assertEqual(pwd, "k9m2")

    def test_query_里的提取码(self):
        u, pwd = linklib.clean_url("https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A?pwd=d6kp")
        self.assertEqual(u, "https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A?pwd=d6kp")
        self.assertEqual(pwd, "d6kp")

    def test_115_password_参数(self):
        u, pwd = linklib.clean_url("https://115cdn.com/s/swhai773we2?password=9999")
        self.assertEqual(pwd, "9999")
        self.assertTrue(u.endswith("password=9999"))

    def test_剥离结尾井号(self):
        u, _ = linklib.clean_url("https://pan.xunlei.com/s/AAA?pwd=1#")
        self.assertEqual(u, "https://pan.xunlei.com/s/AAA?pwd=1")

    def test_不移除正常路径内容(self):
        u, pwd = linklib.clean_url("https://pan.quark.cn/s/6c615522c5d0")
        self.assertEqual(u, "https://pan.quark.cn/s/6c615522c5d0")
        self.assertEqual(pwd, "")

    def test_清洗后的两份写法指纹一致(self):
        a, _ = linklib.clean_url("https://123912.com/s/pbeojv-bLFbh提取码:SSTA")
        b, _ = linklib.clean_url("https://123912.com/s/pbeojv-bLFbh")
        self.assertEqual(linklib.fingerprint(a), linklib.fingerprint(b))


class TestValidate(unittest.TestCase):
    def test_真实链接应通过(self):
        urls = [
            "https://pan.quark.cn/s/6c615522c5d0",
            "https://pan.quark.cn/s/5ef2be7f164e",
            "https://pan.xunlei.com/s/VOzC49mdBK4v2Owkf4KfPk8xA1?pwd=5scn",
            "https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM_hD6V2PA1?pwd=njfz",
            "https://pan.baidu.com/s/1zGaIknUvQ-KyxTE9Ozkc_A?pwd=d6kp",
            "https://www.aliyundrive.com/s/c3HbgDanhvV",
            "https://www.alipan.com/s/PP5vjhC2Sme",
            "https://www.aliyundrive.com/s/z7BPqyMn2gi/folder/632050074e39d2e623334753bb64ff7cbb66f16f",
            "https://115cdn.com/s/swhai773we2?password=9999",
            "https://anxia.com/s/swhmmui3wcq?password=assa",
            "https://115.com/s/sw3r1p933cg",
            "https://123684.com/s/MZ4HTd-vflJv",
            "https://123912.com/s/pbeojv-bLFbh",
            "https://drive.uc.cn/s/61b78555901d4?public=1",
            "https://yun.139.com/shareweb/#/w/x/2xTrFbZBi3Us2",
            "https://cloud.189.cn/web/share?code=abcdef",
            "https://pan.baidu.com/share/init?surl=abc123",
            "magnet:?xt=urn:btih:A22E24FE01EC621B623CAB8E04C93D7691499FB7",
            "ed2k://|file|movie.mkv|1234|HASH|/",
            # 未知主机不评判
            "https://example.com/s/whatever",
        ]
        for u in urls:
            with self.subTest(u=u):
                ok, why = linklib.validate(u)
                self.assertTrue(ok, "{} 被误判：{}".format(u, why))

    def test_截断的迅雷_ID_被拒(self):
        # 真实值 26 字符，上游截断成 17 字符，点开是死链
        ok, why = linklib.validate("https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM")
        self.assertFalse(ok)
        self.assertIn("截断", why)

    def test_路径形态不符被拒(self):
        ok, why = linklib.validate("https://pan.quark.cn/")
        self.assertFalse(ok)
        self.assertIn("分享形态", why)

    def test_磁力缺少哈希被拒(self):
        ok, why = linklib.validate("magnet:?dn=foo")
        self.assertFalse(ok)
        self.assertIn("btih", why)

    def test_空与非_http_被拒(self):
        self.assertFalse(linklib.validate("")[0])
        self.assertFalse(linklib.validate(None)[0])
        self.assertFalse(linklib.validate("ftp://pan.quark.cn/s/abc")[0])

    def test_未知主机放行(self):
        ok, _ = linklib.validate("https://weird-pan.example/s/abc")
        self.assertTrue(ok)


class TestDiagnose(unittest.TestCase):
    def test_诊断包含流水线字段(self):
        d = linklib.diagnose("https://123912.com/s/pbeojv-bLFbh提取码:SSTA")
        self.assertEqual(d["cleaned"], "https://123912.com/s/pbeojv-bLFbh")
        self.assertEqual(d["pwd"], "SSTA")
        self.assertEqual(d["fingerprint"], "123|/s/pbeojv-bLFbh")
        self.assertTrue(d["valid"])
        self.assertEqual(d["reason"], "")

    def test_诊断标出失效原因(self):
        d = linklib.diagnose("https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM")
        self.assertFalse(d["valid"])
        self.assertTrue(d["reason"])


class TestCanonHost(unittest.TestCase):
    def test_主机归一表(self):
        cases = {
            "www.alipan.com": "aliyun",
            "aliyundrive.com": "aliyun",
            "115cdn.com": "115",
            "anxia.com": "115",
            "123912.com": "123",
            "yun.139.com": "mobile",
            "lanzoua.com": "lanzou",
            "lanzoui.com": "lanzou",
            "unknown.example": "unknown.example",
        }
        for host, want in cases.items():
            with self.subTest(host=host):
                self.assertEqual(linklib.canon_host(host), want)

    def test_剥离端口(self):
        """回归：带端口的写法曾让已知网盘退化成「未识别主机」，
        既破坏去重（指纹里混进端口），又让 validate 走放行兜底。"""
        cases = {
            "pan.quark.cn:443": "quark",
            "pan.baidu.com:8080": "baidu",
            "www.alipan.com:443": "aliyun",
            "PAN.QUARK.CN:443": "quark",
        }
        for host, want in cases.items():
            with self.subTest(host=host):
                self.assertEqual(linklib.canon_host(host), want)

    def test_带端口与不带端口视为同一资源(self):
        self.assertEqual(linklib.fingerprint("https://pan.quark.cn:443/s/AbC"),
                         linklib.fingerprint("https://pan.quark.cn/s/AbC"))

    def test_带端口仍走形态校验(self):
        ok, why = linklib.validate("https://pan.xunlei.com:443/s/SHORT")
        self.assertFalse(ok)
        self.assertIn("过短", why)

    def test_域名表无重复项(self):
        seen = {}
        for canon, domains in linklib.HOST_GROUPS:
            for d in domains:
                self.assertNotIn(d, seen, "域名 {} 同时属于 {} 与 {}".format(d, seen.get(d), canon))
                seen[d] = canon


if __name__ == "__main__":
    unittest.main(verbosity=2)
