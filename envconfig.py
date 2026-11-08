""".env 的 schema、校验、缺项补齐、原子保存和指纹缓存。

现有文件值优先；首次创建才播种环境值，显式默认值和空凭据也保留。
Runtime: Python 3.10+ / 标准库，文件锁依赖 POSIX fcntl。
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
from urllib.parse import urlsplit


class EnvConfigError(ValueError):
    """配置错误容器；本模块构造的错误只报告键或行号，不拼接配置原值。
    """


@dataclass(frozen=True)
class Field:
    default: str
    label: str
    description: str
    kind: str = 'text'
    minimum: int = 0
    maximum: int = 0
    secret: bool = False
    restart: bool = False


SCHEMA = {
    'PANEL_PASSWORD': Field('', '管理员密码', '至少 8 字符；空白保存保留现值。', secret=True),
    'PORT': Field('8080', '网页监听端口', '容器内端口；宿主端口映射需在容器配置中对应修改。', 'int', 1, 65535, restart=True),
    'HTTP_HOST': Field('0.0.0.0', '监听地址', '0.0.0.0 监听所有网卡；容器 IP 自动识别仅用于显示。', 'host', restart=True),
    'HTTP_ENABLED': Field('1', '启用网页', '关闭后网页无法操作，只保留 Bot 和调度。', 'bool', restart=True),
    'DATA_DIR': Field('./data', '数据目录', '相对路径以入口文件目录为基准；内含账号密钥，需持久化。', restart=True),
    'TG_BOT_TOKEN': Field('', 'Telegram Bot Token', 'BotFather 签发；空白保存保留，勾选清除才删除。', secret=True),
    'TG_API_BASE_URL': Field('https://api.telegram.org', 'Telegram API 地址', '官方地址或可信 Bot API 服务，不包含 /bot 或 Token。', 'origin', restart=True),
    'EMOS_BASE_URL': Field('https://emos.best', 'emos API 地址', '官方主站与 api.emos.best 互为备用；自定义站不向公共站发送密钥。', 'url'),
    'WEB_BASE_URL': Field('', '个人网页公开地址', '公网必须 HTTPS，仅填写域名与可选端口，不含路径。', 'optional_origin'),
    'PUBLIC_BASE': Field('', '管理员网页公开地址', '可选；反向代理时填写可信 HTTPS 地址供同源与 Cookie 判断。', 'optional_origin'),
    'SIGN_TIME': Field('08:00', '默认签到时间', '格式 HH:MM；官方禁止 0 点，范围 01:00–23:59。', 'time'),
    'SIGN_TIMEZONE': Field('Asia/Shanghai', '签到时区', 'IANA 时区；系统需安装 tzdata，固定偏移常用区支持兜底。', 'timezone'),
    'GAP_SECONDS': Field('8', '账号间隔（秒）', '避免账号请求集中；范围 2–300 秒。', 'int', 2, 300),
    'EMOS_TIMEOUT': Field('20', 'emos 请求超时（秒）', '单次网络请求上限。', 'int', 5, 120),
    'RETRY_DELAYS': Field('300,900,2700', '失败退避（秒）', '1–5 档递增，每档至少 300 秒；当天耗尽后停止。', 'delays'),
    'BOT_WORKERS': Field('2', 'Bot 操作线程数', '默认 2；1 更省内存，最多 8。', 'int', 1, 8, restart=True),
    'BOT_MAX_JOBS': Field('16', 'Bot 待办上限', '包含正在运行任务；达到上限后明确拒绝新操作。', 'int', 4, 128, restart=True),
    'TG_ALLOWED_USER_IDS': Field('', '唯一 TG 用户 ID',
                                 '只填一个正整数 ID；同时作为通知接收人。留空关闭 Bot 和个人网页入口。', 'owner_id'),
    'EMOS_USER_ID': Field('', 'emos 用户 ID',
                          'emos 官网账号卡片上的用户 ID；填写后 /login 直接跳转官方 Bot，不再要求输入。', 'emos_id'),
    'EMOS_AUTH_BOT': Field('emospg_bot', 'emos 官方授权 Bot',
                           '官方授权入口的 Bot 用户名，不含 @；官方变更时只改这里。', 'tg_bot'),
    'TG_POLL_TIMEOUT': Field('45', 'TG 长轮询等待（秒）', '延长空闲等待减少连接与 CPU；范围 10–50。', 'int', 10, 50, restart=True),
    'SCHEDULER_INTERVAL': Field('60', '本地调度检查（秒）', '只检查本地数据库，不轮询官方接口。', 'int', 15, 300),
    'HTTP_REQUEST_TIMEOUT': Field('15', '网页请求超时（秒）', '防止空连接永久占用线程。', 'int', 3, 60, restart=True),
    'HTTP_MAX_CONNECTIONS': Field('8', '网页并发连接上限', '超出立即返回忙碌；减少线程和内存峰值。', 'int', 2, 64, restart=True),
    'ADMIN_SESSION_HOURS': Field('24', '管理员会话（小时）', '管理员 Cookie 有效期；改密码会清除管理员会话。', 'int', 1, 168),
    'WEB_SESSION_HOURS': Field('168', '个人网页会话（小时）', '只代表当前 TG 用户，不获得其他账号登录权限。', 'int', 1, 168),
    'SHUTDOWN_TIMEOUT': Field('90', '软重启收尾上限（秒）', '停止接新任务并等待网络任务结束；未收尾不会强行 exec。', 'int', 30, 300),
}
_RETIRED_KEYS = frozenset({'EMOS_APP_USER_ID', 'SIGN_CONTENT'})
CFG_TO_ENV = {
    'tg_bot_token': 'TG_BOT_TOKEN', 'tg_api_base_url': 'TG_API_BASE_URL',
    'tg_allowed_user_ids': 'TG_ALLOWED_USER_IDS',
    'emos_user_id': 'EMOS_USER_ID', 'emos_auth_bot': 'EMOS_AUTH_BOT',
    'base_url': 'EMOS_BASE_URL',
    'web_base_url': 'WEB_BASE_URL', 'public_base': 'PUBLIC_BASE',
    'sign_time': 'SIGN_TIME', 'timezone': 'SIGN_TIMEZONE',
    'gap_seconds': 'GAP_SECONDS', 'emos_timeout': 'EMOS_TIMEOUT', 'retry_delays': 'RETRY_DELAYS',
    'bot_workers': 'BOT_WORKERS', 'bot_max_jobs': 'BOT_MAX_JOBS', 'tg_poll_timeout': 'TG_POLL_TIMEOUT',
    'scheduler_interval': 'SCHEDULER_INTERVAL', 'http_request_timeout': 'HTTP_REQUEST_TIMEOUT',
    'http_max_connections': 'HTTP_MAX_CONNECTIONS', 'admin_session_hours': 'ADMIN_SESSION_HOURS',
    'web_session_hours': 'WEB_SESSION_HOURS', 'shutdown_timeout': 'SHUTDOWN_TIMEOUT',
}
FIXED_ZONE_OFFSETS = {'Asia/Shanghai': 8, 'Asia/Hong_Kong': 8, 'Asia/Macau': 8,
                      'Asia/Singapore': 8, 'Asia/Taipei': 8, 'Asia/Tokyo': 9,
                      'Asia/Seoul': 9, 'Asia/Bangkok': 7, 'Asia/Kolkata': 5.5,
                      'UTC': 0, 'Etc/UTC': 0}


def _value(raw, number):
    raw = raw.strip()
    if not raw or raw[0] not in ('"', "'"):
        return re.split(r'\s+#', raw, maxsplit=1)[0].rstrip()
    quote, parts, position = raw[0], [], 1
    while position < len(raw):
        char = raw[position]
        if char == quote:
            tail = raw[position + 1:].strip()
            if tail and not tail.startswith('#'):
                raise EnvConfigError('配置第 %d 行：引号后有无效内容。' % number)
            return ''.join(parts)
        if char == '\\' and quote == '"' and position + 1 < len(raw) and raw[position + 1] in ('"', '\\'):
            parts.append(raw[position + 1])
            position += 2
        else:
            parts.append(char)
            position += 1
    raise EnvConfigError('配置第 %d 行：引号未闭合。' % number)


def parse_env(text):
    if '\x00' in text:
        raise EnvConfigError('配置文件包含无效字符。')
    values, seen = {}, set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, raw = line.partition('=')
        name = name.strip()
        if not separator or name not in SCHEMA and name not in _RETIRED_KEYS:
            raise EnvConfigError('配置第 %d 行：未知项或缺少等号。' % number)
        if name in seen:
            raise EnvConfigError('配置第 %d 行：配置项重复。' % number)
        seen.add(name)
        value = _value(raw, number)
        if name in SCHEMA:
            values[name] = value
    return values


def _validate_url(value, key, optional=False, origin=False):
    if optional and not value:
        return value
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise EnvConfigError('%s 地址无效。' % key) from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value)
            or (origin and parsed.path not in ('', '/'))):
        raise EnvConfigError('%s 必须是完整的 HTTP/HTTPS 地址且不含凭据或查询参数。' % key)
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise EnvConfigError('%s 公网地址必须使用 HTTPS。' % key)
    if key == 'TG_API_BASE_URL' and parsed.path not in ('', '/'):
        raise EnvConfigError('TG_API_BASE_URL 不能包含 /bot 或 Token。')
    return value.rstrip('/')


def validate_env(values, allow_unset_password=False):
    missing = set(SCHEMA) - set(values)
    extra = set(values) - set(SCHEMA)
    if missing:
        raise EnvConfigError('配置缺少项目：' + ', '.join(sorted(missing)))
    if extra:
        raise EnvConfigError('配置含未知项目：' + ', '.join(sorted(extra)))
    normalized = {}
    for key, field in SCHEMA.items():
        raw = values[key]
        if not isinstance(raw, (str, int, bool)):
            raise EnvConfigError('%s 类型无效。' % key)
        value = str(int(raw)) if type(raw) is bool else str(raw)
        if (any(ord(char) < 32 or ord(char) == 127 or char in ('\u0085', '\u2028', '\u2029') for char in value)
                or len(value) > 2048):
            raise EnvConfigError('%s 含无效字符或过长。' % key)
        if not field.secret:
            value = value.strip()
        if key == 'PANEL_PASSWORD':
            if (not value and not allow_unset_password) or (value and len(value) < 8):
                raise EnvConfigError('PANEL_PASSWORD 至少 8 个字符。')
        elif key == 'TG_BOT_TOKEN':
            if value and not re.fullmatch(r'[0-9]{3,20}:[A-Za-z0-9_-]{6,200}', value):
                raise EnvConfigError('TG_BOT_TOKEN 格式无效。')
        elif field.kind == 'int':
            if not re.fullmatch(r'[0-9]{1,8}', value) or not field.minimum <= int(value) <= field.maximum:
                raise EnvConfigError('%s 必须在 %d–%d 范围内。' % (key, field.minimum, field.maximum))
            value = str(int(value))
        elif field.kind == 'bool':
            if value.lower() not in ('1', '0', 'true', 'false', 'on', 'off'):
                raise EnvConfigError('%s 只能为 1 或 0。' % key)
            value = '1' if value.lower() in ('1', 'true', 'on') else '0'
        elif field.kind == 'host':
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                raise EnvConfigError('HTTP_HOST 必须是 IPv4 监听地址。') from None
            if address.version != 4:
                raise EnvConfigError('HTTP_HOST 当前只支持 IPv4。')
        elif field.kind in ('origin', 'optional_origin', 'url'):
            value = _validate_url(value, key, optional=field.kind == 'optional_origin',
                                  origin=field.kind != 'url')
        elif field.kind == 'time':
            if not re.fullmatch(r'(?:0[1-9]|1[0-9]|2[0-3]):[0-5][0-9]', value):
                raise EnvConfigError('SIGN_TIME 范围为 01:00–23:59。')
        elif field.kind == 'timezone':
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(value)
            except (ImportError, KeyError, ValueError):
                if value not in FIXED_ZONE_OFFSETS:
                    raise EnvConfigError('SIGN_TIMEZONE 不可用，请安装 tzdata 或选常用固定时区。') from None
        elif field.kind == 'owner_id':
            # 空值用于首次配置；权限入口必须拒绝空身份，不能当作不限用户。
            if value and (not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) >= 2**63):
                raise EnvConfigError('%s 只能填一个正整数 TG 用户 ID。' % key)
        elif field.kind == 'tg_bot':
            # Telegram 用户名规则：字母开头，5–32 位字母数字下划线。
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', value):
                raise EnvConfigError('%s 只能填 Bot 用户名，不含 @，5–32 位。' % key)
        elif field.kind == 'emos_id':
            # 与 Bot 侧同一判据：只收官网用户 ID，拒绝误填 emos Token。
            if value and (not re.fullmatch(r'[A-Za-z0-9_]{1,48}', value)
                          or re.fullmatch(r'[0-9]+_[A-Za-z0-9_-]{8,}', value)):
                raise EnvConfigError('%s 只能填 emos 官网用户 ID，不要填 Token。' % key)
        elif field.kind == 'delays':
            pieces = value.split(',')
            if not 1 <= len(pieces) <= 5 or any(not p.strip().isdigit() for p in pieces):
                raise EnvConfigError('RETRY_DELAYS 应为 1–5 个逗号分隔的秒数。')
            delays = [int(piece) for piece in pieces]
            if any(not 300 <= delay <= 21600 for delay in delays) or any(a >= b for a, b in zip(delays, delays[1:])):
                raise EnvConfigError('RETRY_DELAYS 必须严格递增，每档为 300–21600 秒。')
            value = ','.join(map(str, delays))
        elif key == 'DATA_DIR' and (not value or len(value) > 1024):
            raise EnvConfigError('DATA_DIR 不能为空或过长。')
        normalized[key] = value
    if normalized['HTTP_ENABLED'] == '0' and not normalized['TG_BOT_TOKEN']:
        raise EnvConfigError('关闭网页需要先配置 TG_BOT_TOKEN，避免核心无任何可用入口。')
    if int(normalized['BOT_MAX_JOBS']) < int(normalized['BOT_WORKERS']):
        raise EnvConfigError('BOT_MAX_JOBS 不得小于 BOT_WORKERS。')
    minimum_grace = max(int(normalized['EMOS_TIMEOUT']) + 10,
                        int(normalized['TG_POLL_TIMEOUT']) + 20,
                        int(normalized['HTTP_REQUEST_TIMEOUT']) + 10)
    if int(normalized['SHUTDOWN_TIMEOUT']) < minimum_grace:
        raise EnvConfigError('SHUTDOWN_TIMEOUT 不足以收尾当前配置的网络超时。')
    return normalized


def configured_owner(cfg):
    """读取唯一 TG 用户 ID；缺失、空白或非法均不返回可用身份。
    """
    if not isinstance(cfg, dict):
        return None
    raw = str(cfg.get('tg_allowed_user_ids') or '').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,18}', raw):
        return None
    value = int(raw)
    return value if value < 2**63 else None


def owner_permitted(cfg, tg_id):
    """运行时唯一权限判据；不把账号已登记当作访问授权。
    """
    return type(tg_id) is int and tg_id > 0 and configured_owner(cfg) == tg_id


def serialize_env(values):
    lines = ['# emoskata 系统配置唯一来源；进程环境同名值不会覆盖此文件。',
             '# 网页管理员保存修改到同一文件。个人账号设置仍在 SQLite 中。']
    for key, field in SCHEMA.items():
        lines.extend(('', '# ' + field.label + '：' + field.description,
                      key + '=' + json.dumps(str(values[key]), ensure_ascii=False)))
    return '\n'.join(lines) + '\n'


def _seed_from_environ(environ=None):
    """仅在首次建 .env 时从 SCHEMA 对应环境变量播种。

    已存在的文件不再读取环境值。默认允许为空的字段保留显式空值，
    防止之后由旧库补回；其余必填字段的空环境值不替换有效默认值。
    """
    environ = os.environ if environ is None else environ
    seeded = {}
    for key in SCHEMA:
        raw = environ.get(key)
        if raw is not None and (raw != '' or SCHEMA[key].default == ''):
            seeded[key] = raw
    return seeded


def template_values(password=None, **overrides):
    values = {key: field.default for key, field in SCHEMA.items()}
    values['PANEL_PASSWORD'] = password or secrets.token_urlsafe(24)
    unknown = set(overrides) - set(SCHEMA)
    if unknown:
        raise EnvConfigError('未知配置项：' + ', '.join(sorted(unknown)))
    values.update(overrides)
    return validate_env(values)


def _read(path):
    try:
        with path.open('rb') as handle:
            raw = handle.read(65537)
    except OSError:
        raise EnvConfigError('无法读取配置文件，请检查 .env 路径与权限。') from None
    if len(raw) > 65536:
        raise EnvConfigError('.env 不能超过 64KB。')
    try:
        return raw, parse_env(raw.decode('utf-8-sig'))
    except UnicodeError:
        raise EnvConfigError('.env 必须使用 UTF-8。') from None


class ConfigStore:
    def __init__(self, path, create=False):
        """读取配置；create=True 时创建目录和缺失文件，或补齐缺少的 schema 项。

        原有合法值优先，内容齐全且无标准化变化时不重写；缺失管理员密码才生成。
        provided_keys 区分用户显式提供与自动默认项，供旧库迁移判断。
        """
        self.path = Path(path).absolute()
        self.lock = threading.RLock()
        self._fingerprint, self._cache, self._revision = None, None, None
        self._app_cache, self._app_stamp = None, None
        self.created = False
        self.provided_keys = set(SCHEMA)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._writer():
                if not self.path.exists():
                    # 首次建文件时播种并持久化环境值；此后只读取文件。
                    seeded = {key: value for key, value in _seed_from_environ().items()}
                    self.provided_keys = set(seeded)
                    self._atomic(template_values(**seeded))
                    self.created = True
                else:
                    info = self.path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise EnvConfigError('.env 必须是可写普通文件；不支持符号链接或单文件 secret 挂载。')
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        raise EnvConfigError('无法将 .env 权限设置为 0600。') from None
                    _, existing = _read(self.path)
                    self.provided_keys = set(existing)
                    values = {key: field.default for key, field in SCHEMA.items()}
                    values.update(existing)
                    values = validate_env(values, allow_unset_password=True)
                    if not values['PANEL_PASSWORD']:
                        values['PANEL_PASSWORD'] = secrets.token_urlsafe(24)
                    if set(existing) != set(SCHEMA) or values != existing:
                        self._atomic(validate_env(values))
        self.snapshot()

    @contextmanager
    def _writer(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / (self.path.name + '.lock')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError:
            raise EnvConfigError('配置目录不可写，网页无法保存 .env。') from None
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _atomic(self, values):
        text = serialize_env(values)
        if parse_env(text) != values:
            raise EnvConfigError('新配置无法无损读回，旧文件未改变。')
        if self.path.is_symlink():
            raise EnvConfigError('.env 为符号链接，已拒绝覆盖。')
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.' + self.path.name + '-', dir=self.path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise EnvConfigError('保存 .env 失败：请检查空间、目录权限及是否错误绑定了单文件。旧配置保留。') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        self._fingerprint = None

    def snapshot(self, force=False):
        """返回独立配置字典与原文件 SHA256；指纹变化或 force 时重新读取。

        不在运行期随意补齐损坏文件；解析与校验失败明确抛错。
        """
        with self.lock:
            try:
                info = self.path.stat()
            except OSError:
                raise EnvConfigError('配置文件不存在或不可读取。') from None
            stamp = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            if force or stamp != self._fingerprint:
                raw, parsed = _read(self.path)
                values = validate_env(parsed)
                self._cache = values
                self._revision = hashlib.sha256(raw).hexdigest()
                self._fingerprint = stamp
            return dict(self._cache), self._revision

    def update(self, changes, revision=None, clear_secrets=()):
        """加锁后按当前文件合并、校验、原子保存；revision 不匹配拒绝。

        敏感字段空白保留原值，clear_secrets 才清空；显式保存也记录默认值来源。
        """
        if not isinstance(changes, dict) or set(changes) - set(SCHEMA):
            raise EnvConfigError('网页含未知配置项。')
        if not isinstance(clear_secrets, (list, tuple)) or any(k not in SCHEMA or not SCHEMA[k].secret or k == 'PANEL_PASSWORD' for k in clear_secrets):
            raise EnvConfigError('清除凭据项目无效。')
        with self.lock, self._writer():
            old, current_revision = self.snapshot(force=True)
            if revision is not None and revision != current_revision:
                raise EnvConfigError('配置已被其他操作修改，请刷新后再保存。')
            new = dict(old)
            for key, value in changes.items():
                if SCHEMA[key].secret and value == '' and key not in clear_secrets:
                    continue
                new[key] = value
            for key in clear_secrets:
                new[key] = ''
            new = validate_env(new)
            changed = [key for key in SCHEMA if new[key] != old[key]]
            if changed:
                self._atomic(new)
            # 成功保存即是显式选择，默认值和明确清空也不能再被旧库覆盖。
            self.provided_keys.update(changes)
            self.provided_keys.update(clear_secrets)
            _, new_revision = self.snapshot(force=True)
            return {'revision': new_revision, 'changed': changed,
                    'restart_required': [key for key in changed if SCHEMA[key].restart]}

    def app_config(self):
        # 热路径用文件指纹避免重复解析，返回副本防止调用方改写缓存。
        """按文件指纹复用解析结果；每次仍检测修改，不用 TTL 延迟热配置。

        """
        try:
            info = self.path.stat()
        except OSError:
            raise EnvConfigError('配置文件不存在或不可读取。') from None
        stamp = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        with self.lock:
            if self._app_cache is not None and stamp == self._app_stamp:
                return dict(self._app_cache)
        values, revision = self.snapshot()
        names = {value: key for key, value in CFG_TO_ENV.items()}
        result = {}
        for key, value in values.items():
            if key == 'PANEL_PASSWORD':
                continue
            field = SCHEMA[key]
            result[names.get(key, key.lower())] = int(value) if field.kind in ('int', 'bool') else value
        result['retry_delays'] = tuple(int(v) for v in values['RETRY_DELAYS'].split(','))
        with self.lock:
            self._app_cache, self._app_stamp = result, stamp
        return dict(result)

    def password(self):
        values, _ = self.snapshot()
        return values['PANEL_PASSWORD']

    def public_fields(self):
        values, revision = self.snapshot()
        fields = []
        for key, field in SCHEMA.items():
            fields.append({'key': key, 'label': field.label, 'description': field.description,
                           'kind': field.kind, 'minimum': field.minimum, 'maximum': field.maximum,
                           'secret': field.secret, 'configured': bool(values[key]),
                           'value': '' if field.secret else values[key], 'restart': field.restart})
        return {'fields': fields, 'revision': revision, 'source': '.env'}


def config_path(entry_file, environ=None):
    environ = os.environ if environ is None else environ
    selected = environ.get('EMOS_ENV_FILE')
    if selected is not None and not str(selected).strip():
        raise EnvConfigError('EMOS_ENV_FILE 只能用于选择有效的配置文件路径。')
    path = Path(selected) if selected is not None else Path('.env')
    return path if path.is_absolute() else Path(entry_file).absolute().parent / path
