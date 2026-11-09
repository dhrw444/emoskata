"""隔离测试配置与应用导入，不读取部署者凭据。

夹具先写 .env 再导入应用，清理时自动关闭连接池并恢复环境。
single_owner=False 仅锁定历史测试语义，不代表生产开放多用户。"""
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from envconfig import ConfigStore, CFG_TO_ENV, serialize_env, template_values

ROOT = Path(__file__).resolve().parent
BUILD_FILES = ('build_single.py', 'updates.py', 'envconfig.py', 'runtime_info.py', 'emos.py',
               'accounts.py', 'webui.py', 'bot.py', 'app.py')
FIXTURE_PASSWORD = 'fixture_password'


def write_env(path, password=FIXTURE_PASSWORD, **overrides):
    """用显式测试密码写入临时配置并设 0600，模拟部署者单独上传 .env。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_env(template_values(password, **overrides)), encoding='utf-8')
    path.chmod(0o600)
    return path


def configure(app, **values):
    """把测试的运行字段名映射到 schema，再通过真实 update() 写回配置。"""
    keys = {name: key for name, key in CFG_TO_ENV.items()}
    keys.update(data_dir='DATA_DIR', panel_password='PANEL_PASSWORD',
                port='PORT', http_host='HTTP_HOST', http_enabled='HTTP_ENABLED')
    return app.CONFIG.update({keys.get(name, name): value for name, value in values.items()})


def fixture_app(case, directory, name, entry=None, single_owner=False):
    """导入隔离应用或生成入口，注册环境与连接池清理。

    历史模式包装只作用于此夹具，生产仍由真实配置唯一 owner 核权。"""
    directory = Path(directory)
    path = write_env(directory / '.env', FIXTURE_PASSWORD,
                     DATA_DIR=str(directory / 'data'), HTTP_HOST='127.0.0.1',
                     EMOS_BASE_URL='http://127.0.0.1:1', SIGN_TIMEZONE='UTC',
                     TG_BOT_TOKEN='', TG_ALLOWED_USER_IDS='101' if single_owner else '')
    config = ConfigStore(path)
    environment = patch.dict(os.environ, {'EMOS_ENV_FILE': str(config.path)})
    environment.start()
    case.addCleanup(environment.stop)
    spec = importlib.util.spec_from_file_location(name, entry or ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    application = module if hasattr(module, 'cfg_all') else module.app
    case.addCleanup(application.close_idle_db)
    if not single_owner:
        original = application.cfg_all
        def legacy_test_config():
            result = original()
            result.pop('tg_allowed_user_ids', None)
            return result
        application.cfg_all = legacy_test_config
    return module
