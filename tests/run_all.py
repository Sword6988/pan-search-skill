# -*- coding: utf-8 -*-
"""一键跑全部测试：unittest 套件 + legacy 脚本式回归套件。

用法:
    <python> tests/run_all.py             # 全部
    <python> tests/run_all.py --quick     # 跳过 legacy（只跑 unittest）

退出码：0 全部通过；1 有失败。
"""

import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LEGACY = [
    "netlib_regress_test.py",
    "pansou_regress_test.py",
    "pansearch_regress_test.py",
]
SUM_RE = re.compile(r"==\s*汇总：(\d+) 通过 / (\d+) 失败\s*==")


def run_unittest_modules():
    print("=" * 66)
    print("unittest 套件")
    print("=" * 66)
    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    return total, failed


def run_legacy():
    print()
    print("=" * 66)
    print("legacy 脚本式回归套件")
    print("=" * 66)
    total = failed = 0
    for name in LEGACY:
        path = os.path.join(HERE, "legacy", name)
        print("\n--- {} ---".format(name))
        p = subprocess.run([sys.executable, path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        m = SUM_RE.search(out)
        if m:
            ok, bad = int(m.group(1)), int(m.group(2))
            total += ok + bad
            failed += bad
            print("  {} 通过 / {} 失败".format(ok, bad))
        else:
            failed += 1
            print("  !! 未能解析汇总，套件可能崩溃（退出码 {}）".format(p.returncode))
            print("\n".join(out.splitlines()[-12:]))
        for line in out.splitlines():
            if line.strip().startswith("FAIL"):
                print("  " + line.strip())
    return total, failed


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    quick = "--quick" in argv

    t1, f1 = run_unittest_modules()
    if quick:
        t2 = f2 = 0
    else:
        t2, f2 = run_legacy()

    print()
    print("=" * 66)
    print("总计：{} 项通过 / {} 项失败（unittest {} + legacy {}）".format(
        t1 + t2 - f1 - f2, f1 + f2, t1, t2))
    print("=" * 66)
    return 1 if (f1 + f2) else 0


if __name__ == "__main__":
    sys.exit(main())
