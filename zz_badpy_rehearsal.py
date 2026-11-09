"""演练外层或内嵌语法错误被拒绝，旧核心继续服务且数据保留。

复用临时目录内的正式回归，不启动真实 Bot，不另造一套通过判据。
Run: python3 -B zz_badpy_rehearsal.py
"""
import sys
import unittest


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'test_update.UpdateTests.test_preflight_checks_embedded_module_syntax_not_only_outer_py',
        'test_update.UpdateTests.test_delete_py_running_reupload_exec_same_pid_and_preserve_user_data',
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
