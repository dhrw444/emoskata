"""Compatibility test entry point. Run: python3 selftest.py (standard unittest)."""
from pathlib import Path
import sys
import unittest

if __name__ == '__main__':
    directory = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(directory, pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
