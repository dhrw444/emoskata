"""兼容自检入口，调用项目正式 unittest 发现器。

不维护第二套官方接口或语法判据；结果以实际 unittest 输出为准。
Runtime: Python 3.10+ / 标准库。
Test: python3 -B selftest.py
"""
from pathlib import Path
import sys
import unittest

if __name__ == '__main__':
    directory = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(directory, pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
