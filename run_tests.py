"""一键测试入口：python -I run_tests.py [-v]

显式把项目根与 tests 目录注入 sys.path，保证隔离模式（-I）下也能发现全部用例。
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
TESTS = ROOT / "tests"
for path in (str(ROOT), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS), pattern="test_*.py")
    verbosity = 2 if "-v" in sys.argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
