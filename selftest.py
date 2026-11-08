"""兼容自检入口，调用正式 unittest 发现器。

Run: python3 -B selftest.py；运行依赖与正式测试相同。
"""
from pathlib import Path
import sys
import unittest

if __name__ == '__main__':
    directory = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(directory, pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
