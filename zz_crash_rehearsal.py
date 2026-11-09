"""演练语法合法但导入或完整初始化失败的候选被拒绝或回滚。

复用正式预检与全启动失败回归；失败返回非零，不把打印 FAIL 当成功。
Run: python3 -B zz_crash_rehearsal.py
"""
import sys
import unittest


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'test_update.UpdateTests.test_preflight_rejects_import_time_crash_that_is_syntactically_valid',
        'test_single_file_upgrade.SingleFileUpgradeTests.test_failed_new_schema_restores_config_accounts_and_existing_sessions',
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
