"""兼容自检入口，调用正式 unittest 发现器。

不维护第二套测试或成功判据；Node 是否可用由对应开发测试判断。
Run: python3 -B selftest.py"""
from pathlib import Path
import sys
import unittest

if __name__ == '__main__':
    directory = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(directory, pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
