"""隔离配置、数据库和应用导入的测试工厂回归。

仅使用临时目录、伪凭证和本地服务；不把替身成功视为生产授权或容器验收。
Runtime: Python 3.10+ / 标准库。
Test: python3 -B -m unittest discover -p "test_*.py"
"""
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from envconfig import ConfigStore, CFG_TO_ENV

ROOT = Path(__file__).resolve().parent
BUILD_FILES = ('build_single.py', 'envconfig.py', 'runtime_info.py', 'emos.py',
               'accounts.py', 'webui.py', 'bot.py', 'app.py')


def configure(app, **values):
    keys = {name: key for name, key in CFG_TO_ENV.items()}
    keys.update(data_dir='DATA_DIR', panel_password='PANEL_PASSWORD',
                port='PORT', http_host='HTTP_HOST', http_enabled='HTTP_ENABLED')
    return app.CONFIG.update({keys.get(name, name): value for name, value in values.items()})


def fixture_app(case, directory, name, entry=None, single_owner=False):
    directory = Path(directory)
    with patch.dict(os.environ, {'TG_BOT_TOKEN': '', 'TG_ALLOWED_USER_IDS': ''}):
        config = ConfigStore(directory / '.env', create=True)
    config.update({'PANEL_PASSWORD': 'fixture_password', 'DATA_DIR': str(directory / 'data'),
                   'HTTP_HOST': '127.0.0.1', 'EMOS_BASE_URL': 'http://127.0.0.1:1',
                   'SIGN_TIMEZONE': 'UTC',
                   'TG_BOT_TOKEN': '', 'TG_ALLOWED_USER_IDS': '101' if single_owner else ''},
                  clear_secrets=['TG_BOT_TOKEN'])
    environment = patch.dict(os.environ, {'EMOS_ENV_FILE': str(config.path)})
    environment.start()
    case.addCleanup(environment.stop)
    spec = importlib.util.spec_from_file_location(name, entry or ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not single_owner:
        application = module if hasattr(module, 'cfg_all') else module.app
        original = application.cfg_all
        def legacy_test_config():
            result = original()
            result.pop('tg_allowed_user_ids', None)
            return result
        application.cfg_all = legacy_test_config
    return module
