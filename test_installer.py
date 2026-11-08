"""安装准备不覆盖配置、shell 与网页脚本语法回归。

仅使用临时目录、伪凭证和本地服务；不把替身成功视为生产授权或容器验收。
Runtime: Python 3.10+ / 标准库。
Test: python3 -B -m unittest test_installer
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from envconfig import ConfigStore, SCHEMA

ROOT=Path(__file__).resolve().parent


class InstallerTests(unittest.TestCase):
    def test_docker_preparation_only_initializes_and_preserves_existing_py_env(self):
        with tempfile.TemporaryDirectory(prefix='emos-prep-') as directory:
            root=Path(directory)
            for name in ('emoskata.py','prepare_docker.sh'):
                shutil.copyfile(ROOT/name,root/name)
            result=subprocess.run(['sh',str(root/'prepare_docker.sh')],cwd=root,
                                  text=True,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            config=ConfigStore(root/'config'/'.env')
            self.assertEqual(set(config.snapshot()[0]),set(SCHEMA))
            before=config.path.read_bytes()
            runtime=root/'runtime'/'emoskata.py'
            runtime.write_bytes(runtime.read_bytes()+b'\n# installer fixture preserves updated py\n')
            program=runtime.read_bytes()
            result=subprocess.run(['sh',str(root/'prepare_docker.sh')],cwd=root,
                                  text=True,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(runtime.read_bytes(),program)
            self.assertEqual(config.path.read_bytes(),before)
            self.assertFalse((root/'runtime'/'data'/'data.db').exists())
            self.assertNotIn(config.password(),result.stdout)

    def test_prepare_scripts_have_valid_shell_syntax(self):
        for script in ('install.sh','prepare_docker.sh'):
            result=subprocess.run(['sh','-n',str(ROOT/script)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_all_actual_inline_scripts_parse_with_official_javascript_parser(self):
        if shutil.which('node') is None:
            self.skipTest('Node is development/CI-only; application runtime uses Python only')
        from check_web_js import check_scripts
        self.assertEqual(set(check_scripts()),{'PAGE_0','LOGIN_PAGE_0','_AUTH_JS','_DASHBOARD_JS'})


if __name__ == '__main__':
    unittest.main()
