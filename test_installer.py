"""安装脚本不创建配置、不覆盖已有文件，以及 shell、真实网页脚本语法回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_installer"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from envconfig import ConfigStore, SCHEMA, render_env_example

ROOT = Path(__file__).resolve().parent
FIXTURE_PASSWORD = 'installer_fixture_password'

def fill_env(path):
    """按部署者的做法：复制模板，填上必填项。"""
    text = render_env_example()
    text = text.replace('PANEL_PASSWORD=""', 'PANEL_PASSWORD="%s"' % FIXTURE_PASSWORD)
    text = text.replace('TG_BOT_TOKEN=""', 'TG_BOT_TOKEN="123456:installer_fixture_token"')
    text = text.replace('TG_ALLOWED_USER_IDS=""', 'TG_ALLOWED_USER_IDS="101"')
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)
    return path

class InstallerTests(unittest.TestCase):
    def test_docker_preparation_requires_env_and_never_creates_it(self):
        with tempfile.TemporaryDirectory(prefix='emos-prep-') as directory:
            root = Path(directory)
            for name in ('emoskata.py', 'prepare_docker.sh', '.env.example'):
                shutil.copyfile(ROOT / name, root / name)
            # 没有 .env：脚本必须失败退出，且不留下任何 .env。
            result = subprocess.run(['sh', str(root / 'prepare_docker.sh')], cwd=root,
                                    text=True, capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('.env', result.stderr)
            self.assertFalse((root / 'config' / '.env').exists())
            self.assertTrue((root / 'config' / '.env.example').is_file())
            self.assertTrue((root / 'runtime' / 'emoskata.py').is_file())
            # 部署者填好后：只读校验通过，文件字节不变，不建数据库。
            config_path = fill_env(root / 'config' / '.env')
            before = config_path.read_bytes()
            result = subprocess.run(['sh', str(root / 'prepare_docker.sh')], cwd=root,
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse((root / 'runtime' / 'data' / 'data.db').exists())
            self.assertNotIn(FIXTURE_PASSWORD, result.stdout + result.stderr)
            # 再跑一次也不该覆盖部署者后来换掉的程序文件。
            runtime = root / 'runtime' / 'emoskata.py'
            runtime.write_bytes(runtime.read_bytes() + b'\n# installer fixture preserves updated py\n')
            program = runtime.read_bytes()
            result = subprocess.run(['sh', str(root / 'prepare_docker.sh')], cwd=root,
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(runtime.read_bytes(), program)
            self.assertEqual(config_path.read_bytes(), before)

    def test_prepare_docker_rejects_invalid_env_without_rewriting_it(self):
        with tempfile.TemporaryDirectory(prefix='emos-prep-bad-') as directory:
            root = Path(directory)
            for name in ('emoskata.py', 'prepare_docker.sh', '.env.example'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'config').mkdir()
            bad = root / 'config' / '.env'
            bad.write_text('PANEL_PASSWORD=""\n', encoding='utf-8')
            bad.chmod(0o600)
            before = bad.read_bytes()
            result = subprocess.run(['sh', str(root / 'prepare_docker.sh')], cwd=root,
                                    text=True, capture_output=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(bad.read_bytes(), before)

    def test_install_script_requires_env_and_keeps_existing_config(self):
        with tempfile.TemporaryDirectory(prefix='emos-install-') as directory:
            root = Path(directory)
            for name in ('emoskata.py', 'install.sh', '.env.example'):
                shutil.copyfile(ROOT / name, root / name)
            target = root / 'opt'
            binaries = root / 'bin'
            binaries.mkdir()
            for name in ('sh', 'python3', 'mkdir', 'cp', 'chmod', 'dirname'):
                binary = shutil.which(name)
                self.assertIsNotNone(binary, name)
                (binaries / name).symlink_to(binary)
            environment = dict(os.environ, PATH=str(binaries))
            result = subprocess.run(['sh', str(root / 'install.sh'), str(target)], cwd=root,
                                    env=environment, text=True, capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((target / '.env').exists())
            self.assertTrue((target / '.env.example').is_file())
            config_path = fill_env(target / '.env')
            before = config_path.read_bytes()
            result = subprocess.run(['sh', str(root / 'install.sh'), str(target)], cwd=root,
                                    env=environment, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(ConfigStore(config_path).snapshot()[0]['PANEL_PASSWORD'],
                             FIXTURE_PASSWORD)
            self.assertEqual(set(ConfigStore(config_path).snapshot()[0]), set(SCHEMA))

    def test_prepare_scripts_have_valid_shell_syntax(self):
        for script in ('install.sh', 'prepare_docker.sh'):
            result = subprocess.run(['sh', '-n', str(ROOT / script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_actual_inline_scripts_parse_with_official_javascript_parser(self):
        if shutil.which('node') is None:
            self.skipTest('Node is development/CI-only; application runtime uses Python only')
        from check_web_js import check_scripts
        self.assertEqual(set(check_scripts()), {'PAGE_0', 'LOGIN_PAGE_0', '_AUTH_JS', '_DASHBOARD_JS'})

if __name__ == '__main__':
    unittest.main()
