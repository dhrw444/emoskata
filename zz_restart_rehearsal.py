"""演练入口删除重传、真实核心切换与持久账号保留。

复用正式 localhost 集成回归，不使用固定目录、真实凭据或模糊 PID 搜索。
Run: python3 -B zz_restart_rehearsal.py
"""
import sys
import unittest


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'test_update.UpdateTests.test_delete_py_running_reupload_exec_same_pid_and_preserve_user_data',
        'test_single_file_upgrade.SingleFileUpgradeTests.test_one_file_updates_all_modules_pages_and_docs_preserving_accounts',
        'test_single_file_upgrade.SingleFileUpgradeTests.test_failed_new_schema_restores_config_accounts_and_existing_sessions',
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
