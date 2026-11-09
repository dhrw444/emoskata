"""持久数据、HTTP、调度、通知和单进程核心生命周期。

配置与 SQLite 独立于程序；先隔离预检并排空，更新就绪前失败恢复快照。
Run: python3 app.py，或使用生成入口 emoskata.py；仅标准库。
"""

import hashlib
import html
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import signal
import sys
from pathlib import Path

from envconfig import (ConfigStore, EnvConfigError, CFG_TO_ENV, SCHEMA, config_path,
                       FIXED_ZONE_OFFSETS, configured_owner, owner_permitted)
from runtime_info import container_ip, snapshot as resource_snapshot
from emos import EmosClient, EmosError, clamp_sign_time, pick_content
from accounts import AccountService, DomainError
from webui import WebService, render_page
from updates import (recover_entry, capture_image, mark_ready, exec_update,
                     restore_missing_entry, snapshot_database)

# 恢复未完成的更新必须发生在读取配置、打开数据库和创建线程之前。
recover_entry(__file__)
capture_image(__file__, globals().get('__emos_image__'))
_STARTUP_PROBE = os.environ.get('EMOS_STARTUP_PROBE') == '1'
_CORE_START_GATE = threading.Event()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    CONFIG = ConfigStore(config_path(__file__))
except EnvConfigError as _config_error:
    # .env 由部署者提供；缺失时给出一句能照做的提示，不打印堆栈也不代为创建。
    print('[config] %s' % _config_error)
    raise SystemExit(2) from None
_RUNTIME_CONFIG = CONFIG.app_config()
_data = Path(_RUNTIME_CONFIG['data_dir']).expanduser()
DATA_DIR = str(_data if _data.is_absolute() else Path(BASE_DIR) / _data)
os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'data.db')
_EXISTING_DB = os.path.isfile(DB_PATH)
TZ_FALLBACK = FIXED_ZONE_OFFSETS
LOGIN_MAX_FAIL = 5
LOGIN_LOCK_SEC = 300
LOGIN_FAIL_TTL = 86400
_RL_SWEEP = 0

_write_lock = threading.RLock()
_run_lock = threading.Lock()
_shutdown = threading.Event()
_draining = threading.Event()
_restart_requested = threading.Event()
_restart_status = 'running'
_restart_error = ''
_restart_lock = threading.Lock()
_CONTAINER_IP = container_ip()
_HTTP_SERVER = None
_active_bot = None
_active_bot_lock = threading.Lock()
_notifications = queue.Queue(maxsize=256)
_notification_drops = 0
_notification_wakeup = threading.Event()


def request_restart():
    """在锁内固定并预检候选字节，成功后进入排空状态。

    已有重启或正在停机返回 False；候选失败抛配置错误，旧核心继续运行。
    """
    global _restart_status, _restart_error, _restart_image
    with _restart_lock:
        if _restart_requested.is_set() or _shutdown.is_set():
            return False
        _restart_image = _prepare_core()
        _restart_error = ''
        _restart_status = 'draining'
        _draining.set()
        _restart_requested.set()
        return True


_db_pool_lock = threading.Lock()
_db_available = threading.Condition(_db_pool_lock)
_db_pool = []
_db_owners = set()
_db_quarantine = []
_db_borrowed = 0
_db_total = 0
_db_closing = 0
_db_waiters = 0
_db_sealed = False
_db_purging = False
_DB_IDLE_LIMIT = 3
_DB_TOTAL_LIMIT = 8
_DB_WAIT_TIMEOUT = 20.0
LOG_KEEP = 200


class DatabaseBusy(sqlite3.OperationalError):
    """数据库达到容量或正在收尾；不暴露 SQL、凭据和底层异常。
    """


def _close_physical(connection):
    global _db_total, _db_closing
    failed = False
    try:
        connection.close()
    except BaseException:
        failed = True
    finally:
        with _db_available:
            _db_closing -= 1
            if failed:
                _db_quarantine.append(connection)
            else:
                _db_total -= 1
            _db_available.notify_all()
    if failed:
        raise DatabaseBusy('数据库连接关闭未完成；暂不接收新租约。') from None


class _Connection:
    def __init__(self, connection, owner):
        self.connection = connection
        self.owner = owner
        self._lock = threading.Lock()
        self._discard = False

    def __getattr__(self, name):
        if self.connection is None:
            raise sqlite3.ProgrammingError('数据库连接租约已关闭')
        return getattr(self.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            return self.connection.__exit__(*args)
        except BaseException:
            self._discard = True
            raise
        finally:
            self.close()

    def close(self):
        global _db_borrowed, _db_closing
        with self._lock:
            connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
        except BaseException:
            self._discard = True
        with _db_available:
            _db_borrowed -= 1
            _db_owners.discard(self.owner)
            if not self._discard and not _db_sealed and len(_db_pool) < _DB_IDLE_LIMIT:
                _db_pool.append(connection)
                _db_available.notify_all()
                return
            _db_closing += 1
        _close_physical(connection)


def db():
    """最多八个物理 SQLite 连接；含创建和关闭中的连接，空闲缓存最多三个。

    满容量有限等待；禁止同线程嵌套借用，内部查询必须复用已有 conn。
    创建失败归还预约；物理关闭失败隔离连接，不能继续补建绕过上限。
    """
    global _db_borrowed, _db_total, _db_waiters, _db_closing
    owner = threading.get_ident()
    connection = None
    reserved_new = False
    deadline = time.monotonic() + _DB_WAIT_TIMEOUT
    with _db_available:
        if owner in _db_owners:
            raise sqlite3.ProgrammingError('嵌套查询必须复用当前数据库连接。')
        _db_waiters += 1
        try:
            while True:
                if _db_sealed or _db_quarantine:
                    raise DatabaseBusy('数据库正在收尾，请稍后重试。')
                if not _db_purging and _db_borrowed < _DB_TOTAL_LIMIT:
                    if _db_pool:
                        connection = _db_pool.pop()
                        break
                    if _db_total < _DB_TOTAL_LIMIT:
                        _db_total += 1
                        reserved_new = True
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabaseBusy('数据库并发已满，请稍后重试。')
                _db_available.wait(remaining)
            _db_borrowed += 1
            _db_owners.add(owner)
        finally:
            _db_waiters -= 1
    try:
        if reserved_new:
            connection = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA cache_size=-256')
        return _Connection(connection, owner)
    except BaseException:
        with _db_available:
            _db_borrowed -= 1
            _db_owners.discard(owner)
            if connection is None:
                _db_total -= int(reserved_new)
            else:
                _db_closing += 1
            _db_available.notify_all()
        if connection is not None:
            _close_physical(connection)
        raise


def close_idle_db():
    """同步关闭空闲和隔离连接，关闭完成前暂停新的预约。
    """
    global _db_closing, _db_purging
    with _db_available:
        while _db_purging:
            _db_available.wait()
        _db_purging = True
        connections = list(_db_pool) + list(_db_quarantine)
        _db_pool.clear()
        _db_quarantine.clear()
        _db_closing += len(connections)
    error = None
    try:
        for connection in connections:
            try:
                _close_physical(connection)
            except DatabaseBusy as failure:
                error = failure
    finally:
        with _db_available:
            _db_purging = False
            _db_available.notify_all()
    if error is not None:
        raise error


def _seal_db_pool(sealed):
    global _db_sealed
    with _db_available:
        _db_sealed = sealed
        _db_available.notify_all()



@contextmanager
def write_db():
    """串行持有 BEGIN IMMEDIATE 写事务；退出时提交或回滚，并归还连接租约。

    事务中的其他查询必须复用 conn，不能再次 db()。
    """
    with _write_lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn


def init_db():
    """在一个写事务内补齐 schema、索引和限流记录；不清空账号及签到历史。

    迁移不兼容时抛错；目录及数据库权限的 chmod 为尽力执行。
    """
    with write_db() as conn:
        for statement in (
            'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)',
            'CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY,created INTEGER,'
            'last_seen INTEGER,ip TEXT,ua TEXT)',
            'CREATE TABLE IF NOT EXISTS login_fail (ip TEXT PRIMARY KEY,count INTEGER '
            'DEFAULT 0,until INTEGER DEFAULT 0,updated_at INTEGER NOT NULL DEFAULT 0)',
        ):
            conn.execute(statement)
        failure_columns = {row['name'] for row in conn.execute('PRAGMA table_info(login_fail)')}
        if 'updated_at' not in failure_columns:
            conn.execute('ALTER TABLE login_fail ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0')
        now = int(time.time())
        # 旧表没有最后失败时间，迁移时开始一个保守保留窗口，不提前解锁。
        conn.execute('UPDATE login_fail SET updated_at=? WHERE updated_at=0', (now,))
        AccountService.init_schema(conn)
        WebService.init_schema(conn)
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(accounts)')}
        if 'retry_day' not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN retry_day TEXT DEFAULT ''")
        conn.execute("DELETE FROM settings WHERE key LIKE 'oauth_state_%' OR key LIKE 'oauth_back_%'")
        _purge_login_failures(conn, now)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def get_setting(key, default=None):
    with db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with _write_lock, db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)))


def cfg_all():
    # 配置复用文件指纹缓存，仍逐次检测变更以保持热更新。
    return CONFIG.app_config()


# 兼容旧 settings 配置；迁移优先级和失败行为见 import_legacy_config。
LEGACY_SETTING_KEYS = (
    ('base_url', 'EMOS_BASE_URL'),
    ('sign_time', 'SIGN_TIME'),
    ('timezone', 'SIGN_TIMEZONE'),
    ('gap_seconds', 'GAP_SECONDS'),
    ('tg_bot_token', 'TG_BOT_TOKEN'),
)


def import_legacy_config():
    """一次性把旧 SQLite settings 补进 .env 里缺失的键；老库升级用，不创建文件。

    只写文件里没有的键，已有值（包括显式默认值和空 Token）一律不动；
    非法旧值逐项拒绝，不拖垮启动。迁移完成即打标记，之后不再触发。
    """
    state = {'imported': [], 'untouched': [], 'rejected': []}
    if get_setting('legacy_config_imported') == '1':
        return state
    current, _ = CONFIG.snapshot()
    changes = {}
    for legacy, key in LEGACY_SETTING_KEYS:
        value = (get_setting(legacy) or '').strip()
        if (key in CONFIG.provided_keys or not value or value == SCHEMA[key].default
                or current.get(key) != SCHEMA[key].default):
            continue
        changes[key] = value
    for key, value in changes.items():
        try:
            CONFIG.update({key: value})
        except EnvConfigError:
            state['rejected'].append(key)   # 单个旧值不合法不该拖垮启动
        else:
            state['imported'].append(key)
    state['untouched'] = [key for _, key in LEGACY_SETTING_KEYS
                          if key not in changes and key not in state['rejected']]
    set_setting('legacy_config_imported', '1')
    return state


_web_service = None
_web_service_lock = threading.Lock()


def personal_web():
    global _web_service
    with _web_service_lock:
        if _web_service is None:
            accounts = AccountService(db, write_db, cfg_all)
            _web_service = WebService(db, write_db, cfg_all, accounts, run_user)
        return _web_service


class Store:
    def __init__(self):
        self.accounts = AccountService(db, write_db, cfg_all)

    def web_login(self, tg_id):
        return personal_web().issue_url(tg_id)

    def db(self):
        return db()

    def write(self):
        return write_db()

    def cfg_all(self):
        return cfg_all()

    def get_setting(self, key, default=None):
        return get_setting(key, default)

    def set_setting(self, key, value):
        set_setting(key, value)

    def run_user(self, tg_id, include_links=True, expected_root=None, content=None):
        return run_user(tg_id, include_links, expected_root, content)


# 密码校验

def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt, dk.hex())


def verify_password(pw):
    stored = get_setting("panel_pw_hash") or ""
    if not stored.startswith("scrypt$"):
        return False
    try:
        _, salt, want = stored.split("$", 2)
        dk = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


def init_password():
    """把 .env 里的密码同步成 scrypt 校验值；密码变化时清除管理员会话。

    密码由部署者在 .env 里自己填写，程序不再生成或打印明文。
    """
    source = CONFIG.password()
    if not verify_password(source):
        with write_db() as conn:
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         ('panel_pw_hash', hash_password(source)))
            conn.execute('DELETE FROM sessions')


# 会话

def purge_sessions():
    now = int(time.time())
    ttl = cfg_all()['admin_session_hours'] * 3600
    with write_db() as conn:
        conn.execute('DELETE FROM sessions WHERE created<=?', (now - ttl,))


def create_session(ip, ua):
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    purge_sessions()
    with _write_lock, db() as conn:
        conn.execute(
            "INSERT INTO sessions(token,created,last_seen,ip,ua) VALUES(?,?,?,?,?)",
            (tok, now, now, (ip or "")[:64], (ua or "")[:160]))
    return tok


def session_ok(tok):
    if not isinstance(tok, str) or not tok or len(tok) > 256:
        return False
    now = int(time.time())
    ttl = cfg_all()['admin_session_hours'] * 3600
    with db() as conn:
        row = conn.execute('SELECT created FROM sessions WHERE token=?', (tok,)).fetchone()
    return bool(row and now - ttl < row['created'] <= now)


def drop_session(tok):
    if tok:
        with _write_lock, db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (tok,))


# 登录限流

def rl_blocked(ip):
    now = int(time.time())
    with db() as conn:
        r = conn.execute("SELECT until FROM login_fail WHERE ip=?", (ip,)).fetchone()
    return max(0, (r["until"] or 0) - now) if r else 0


def _purge_login_failures(conn, now):
    """回收过期锁定和超过保留窗口的零散错误；仍有效的锁定不删除。
    """
    conn.execute('DELETE FROM login_fail WHERE (until>0 AND until<=?) '
                 'OR (until=0 AND updated_at<=?)', (now, now - LOGIN_FAIL_TTL))


def rl_fail(ip):
    """记录一次认证失败；保留窗口或锁定过期后重新计数。

    每 64 次失败清理过期记录；当前有效锁定不被清除。
    """
    now = int(time.time())
    with _write_lock, db() as conn:
        row = conn.execute('SELECT count,until,updated_at FROM login_fail WHERE ip=?', (ip,)).fetchone()
        expired = row and ((row['until'] > 0 and row['until'] <= now)
                           or (row['until'] == 0 and row['updated_at'] <= now - LOGIN_FAIL_TTL))
        cnt = 1 if not row or expired else row['count'] + 1
        until = now + LOGIN_LOCK_SEC if cnt >= LOGIN_MAX_FAIL else 0
        conn.execute('INSERT INTO login_fail(ip,count,until,updated_at) VALUES(?,?,?,?) '
                     'ON CONFLICT(ip) DO UPDATE SET count=excluded.count,until=excluded.until,'
                     'updated_at=excluded.updated_at', (ip, cnt, until, now))
        global _RL_SWEEP
        _RL_SWEEP += 1
        if _RL_SWEEP % 64 == 0:
            _purge_login_failures(conn, now)
    return cnt


def rl_ok(ip):
    with _write_lock, db() as conn:
        conn.execute("DELETE FROM login_fail WHERE ip=?", (ip,))


# 通知队列

def public_base(handler=None):
    cfg = cfg_all()
    if cfg['public_base']:
        return cfg['public_base'].rstrip('/')
    if handler is not None:
        proto = (handler.headers.get('X-Forwarded-Proto') or 'http').split(',')[0].strip()
        host = (handler.headers.get('X-Forwarded-Host') or handler.headers.get('Host') or '').split(',')[0].strip()
        if host:
            return '%s://%s' % (proto, host)
    return 'http://127.0.0.1:%d' % _RUNTIME_CONFIG['port']


def _esc(s):
    return html.escape(str(s if s is not None else ''))


def now_local(cfg):
    name = cfg.get('timezone') or 'Asia/Shanghai'
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(name))
    except (ImportError, KeyError):
        offset = TZ_FALLBACK.get(name)
        if offset is None:
            raise ValueError('时区不可用：请安装 tzdata 或选择 Asia/Shanghai / UTC。') from None
        return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=offset)))


def build_report(rows, started, ended):
    icons = {'signed': '✅', 'already': '⚪', 'network': '📡', 'blocked': '🚧',
             'rate': '⏳', 'invalid': '🔑', 'failed': '❌', 'revoked': '🔒'}
    lines = ['<b>🥕 签到结果</b>']
    for row in rows:
        label = row.get('emos_id') or row['name']
        suffix = ' +%s 萝卜' % row['carrot'] if row.get('carrot') is not None else ''
        lines.append('%s %s%s\n  %s' % (icons.get(row['status'], '❌'), _esc(label),
                                      suffix, _esc(row['message'][:120])))
    lines.append('耗时 %.1f 秒' % max(0, ended - started))
    return '\n'.join(lines)


def _queue_result(row, cfg):
    global _notification_drops
    service = Store().accounts
    for user in service.watchers(row['id']):
        status = row['status']
        want = ('notify_success' if status == 'signed' else
                'notify_already' if status == 'already' else 'notify_fail')
        if not int(user.get(want) or 0):
            continue
        task = (row['id'], user['tg_id'], want, dict(row))
        try:
            _notifications.put_nowait(task)
        except queue.Full:
            _notification_drops += 1
            print('[notify] 队列已满，发送失败次数 %d' % _notification_drops)


def _deliver_notification(task):
    from bot import TG
    account_id, tg_id, preference, row = task
    current = next((user for user in Store().accounts.watchers(account_id) if user['tg_id'] == tg_id), None)
    if current is None or not int(current.get(preference) or 0):
        return False
    cfg = cfg_all()
    owner_mode = 'tg_allowed_user_ids' in cfg
    if owner_mode and not owner_permitted(cfg, tg_id):
        return False
    token = str(cfg.get('tg_bot_token') or '')
    if not token:
        return False
    recipient = configured_owner(cfg) if owner_mode else current['chat_id']
    TG(token, Store()).send(recipient, build_report([row], 0, 0))
    return True


def _request_shutdown():
    """设置停机标记，并给通知队列送入一次唤醒。

    信号处理器只设标记；这里由正常收尾路径调用，避免信号中重入队列锁。
    """
    _shutdown.set()
    if _notification_wakeup.is_set():
        return
    _notification_wakeup.set()
    try:
        _notifications.put_nowait(None)
    except queue.Full:
        pass


def notification_loop():
    """等待通知而不定时空转；None 只用于停机唤醒，任务计数始终归还。
    """
    while not _shutdown.is_set():
        task = _notifications.get()
        try:
            if task is not None and not _shutdown.is_set():
                _deliver_notification(task)
        except Exception:
            print('[notify] 发送失败，详细签到结果已保存在账号日志。')
        finally:
            _notifications.task_done()


def _account_record(account_id):
    with db() as conn:
        row = conn.execute('SELECT a.*,u.login_revision AS owner_login_revision, '
                           'u.primary_account_id AS owner_primary_account_id '
                           'FROM accounts a LEFT JOIN tg_users u ON u.tg_id=a.owner_tg_id '
                           'WHERE a.id=?', (account_id,)).fetchone()
    return dict(row) if row else None


def _next_retry(count):
    delays = cfg_all()['retry_delays']
    return int(time.time()) + delays[count - 1] if 1 <= count <= len(delays) else 0


def _store_result(account, result, cfg, trigger):
    """在写事务中核对凭据、归属和登录代际，再写结果与有限日志。

    过期快照返回 None；invalid 停用账号，未知余额不覆盖已有余额。
    """
    now = now_local(cfg)
    today, stamp = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d %H:%M:%S')
    status = result['status']
    with write_db() as conn:
        current = conn.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone()
        if not current or current['token'] != account['token'] or current['emos_id'] != account['emos_id']:
            return None
        if current['owner_tg_id'] != account.get('owner_tg_id'):
            return None
        revision = account.get('owner_login_revision')
        if revision is not None:
            owner = conn.execute('SELECT login_revision FROM tg_users WHERE tg_id=?',
                                 (current['owner_tg_id'],)).fetchone()
            if owner is None or owner['login_revision'] != revision:
                return None
        safe_name = str(current['name'] or '').replace(current['token'], '[凭证已隐藏]')
        result = dict(result)
        result['message'] = str(result.get('message') or '').replace(current['token'], '[凭证已隐藏]')
        retries = current['retry_count'] if current['retry_day'] == today else 0
        next_at = 0
        if status in ('network', 'blocked', 'rate', 'failed'):
            retries += 1
            next_at = _next_retry(retries)
            requested = int(result.get('retry_after') or 0)
            if next_at and requested:
                next_at = max(next_at, int(time.time()) + min(requested, 86400))
        if status in ('config', 'rejected'):
            retries, next_at = len(cfg['retry_delays']) + 1, 0
        done_day = today if status in ('signed', 'already') else current['last_day']
        enabled = 0 if status == 'invalid' else current['enabled']
        conn.execute('INSERT INTO logs(account_id,name,ts,status,message,carrot) VALUES(?,?,?,?,?,?)',
                     (account['id'], safe_name, stamp, status, result['message'], result.get('point')))
        # 按账号裁剪到 LOG_KEEP 条；不让长期签到日志无限增长。
        conn.execute('DELETE FROM logs WHERE account_id=? AND id <= '
                     '(SELECT id FROM logs WHERE account_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)',
                     (account['id'], account['id'], LOG_KEEP))
        # carrot 为官方缓存余额；None 不覆盖已知值，last_carrot 仅为单次所得。
        balance = result.get('carrot')
        if balance is None:
            conn.execute('UPDATE accounts SET last_status=?,last_msg=?,last_carrot=?,last_run=?,'
                         'last_day=?,retry_count=?,next_run_at=?,retry_day=?,enabled=? WHERE id=?',
                         (status, result['message'], result.get('point'), stamp, done_day,
                          retries, next_at, today, enabled, account['id']))
        else:
            conn.execute('UPDATE accounts SET last_status=?,last_msg=?,last_carrot=?,last_run=?,'
                         'last_day=?,retry_count=?,next_run_at=?,retry_day=?,enabled=?,'
                         'carrot=?,carrot_at=? WHERE id=?',
                         (status, result['message'], result.get('point'), stamp, done_day,
                          retries, next_at, today, enabled, balance,
                          int(time.time()), account['id']))
    row = {'id': account['id'], 'emos_id': account['emos_id'], 'name': safe_name,
           'status': status, 'message': result['message'], 'carrot': result.get('point'),
           'balance': result.get('carrot')}
    if trigger != 'user' or status == 'invalid':
        _queue_result(row, cfg)
    return row


def _auto_permission(owner, source_id, target_id, cfg):
    """自动签到前的即时授权复核；主账号失效或归属改变即拒绝，不依赖启动时的快照。
    """
    live = cfg_all()
    if 'tg_allowed_user_ids' in live:
        if (not owner_permitted(live, owner) or source_id != target_id
                or not Store().accounts.has_primary_account(owner)):
            return False
    now = now_local(cfg)
    with db() as conn:
        source = conn.execute('SELECT * FROM accounts WHERE id=?', (source_id,)).fetchone()
        target = conn.execute('SELECT * FROM accounts WHERE id=?', (target_id,)).fetchone()
        if not source or not target or source['owner_tg_id'] != owner:
            return False
        if not source['enabled'] or source['last_status'] == 'invalid' or not source['auto_sign']:
            return False
        if now.strftime('%H:%M') < clamp_sign_time(source['sign_time'] or cfg.get('sign_time')):
            return False
        if not target['enabled'] or target['last_status'] == 'invalid':
            return False
        if owner is not None and (not source['emos_id'] or not target['emos_id'] or target['owner_tg_id'] is None):
            return False
        if source_id == target_id:
            return True
        if owner is None:
            return False
        return conn.execute('SELECT 1 FROM account_links WHERE source_id=? AND target_id=? AND enabled=1',
                            (source_id, target_id)).fetchone() is not None


def _run_ids(cfg, account_ids, trigger='manual', actor=None, root_id=None, scopes=None, content=None):
    """串行执行账号列表；请求前复核权限，写回时再核对账号快照。

    已有执行返回 None。撤销可拦截后续请求和旧结果，不能撤回已发送的 HTTP。
    """
    if not _run_lock.acquire(blocking=False):
        return None
    rows, unavailable = [], set()
    try:
        gap = min(300, max(2, int(cfg.get('gap_seconds') or 8)))
        service = Store().accounts
        for account_id in dict.fromkeys(account_ids):
            if _shutdown.is_set():
                break
            if rows and _shutdown.wait(gap):
                break
            account = _account_record(account_id)
            if not account or not account['enabled']:
                continue
            def allowed(account=account, account_id=account_id):
                if _shutdown.is_set():
                    return False
                current = _account_record(account_id)
                if not current or not current['enabled'] or current['token'] != account['token']:
                    return False
                if current['emos_id'] != account['emos_id']:
                    return False
                live = cfg_all()
                if 'tg_allowed_user_ids' in live:
                    if (not owner_permitted(live, current['owner_tg_id'])
                            or not service.has_primary_account(current['owner_tg_id'])):
                        return False
                if actor is not None:
                    return service.can_run(actor, root_id, account_id)
                if scopes is not None:
                    return _eligible(current, now_local(cfg), cfg, False) and any(
                        _auto_permission(owner, source, account_id, cfg)
                        for owner, source in scopes.get(account_id, []))
                return True
            if not allowed():
                continue
            endpoint = (account.get('base_url') or cfg.get('base_url')
                        or SCHEMA['EMOS_BASE_URL'].default)
            if endpoint in unavailable:
                result = {'status': 'network', 'message': '同一接口暂不可达，已安排退避重试。', 'point': None}
            else:
                try:
                    client = EmosClient(account['token'], endpoint, timeout=cfg['emos_timeout'])
                    client.before_request = allowed
                    client.expected_user_id = account['emos_id']
                    # owner_tg_id 是本地管理人；仅官方首号的远端 TG 绑定要求一致。
                    if account['id'] == account.get('owner_primary_account_id'):
                        client.expected_telegram_id = account['owner_tg_id']
                    override = content if actor is not None and account_id == root_id else None
                    result = client.sign_once(override or '', mode=account['sign_mode'])
                except EmosError as error:
                    result = {'status': error.kind, 'message': error.message, 'point': None}
                except Exception:
                    result = {'status': 'failed', 'message': '签到处理失败，已记录并安排重试。', 'point': None}
                if result['status'] == 'network':
                    unavailable.add(endpoint)
            if result['status'] == 'revoked':
                continue
            row = _store_result(account, result, cfg, trigger)
            if row:
                rows.append(row)
        return rows
    finally:
        _run_lock.release()


def run_user(tg_id, include_links=True, expected_root=None, content=None):
    live = cfg_all()
    if 'tg_allowed_user_ids' in live and not owner_permitted(live, tg_id):
        raise DomainError('只允许配置的 TG 用户操作。')
    if _draining.is_set():
        raise DomainError('应用核心正在重启，暂不接收新的签到任务。')
    if content is not None:
        try:
            content = pick_content(content)
        except EmosError as error:
            raise DomainError(str(error)) from None
    cfg = cfg_all()
    scope = Store().accounts.run_scope(tg_id, include_links)
    if expected_root is not None and scope['root_id'] != expected_root:
        raise DomainError('当前账号已切换，本次签到未执行。')
    return _run_ids(cfg, scope['account_ids'], 'user', tg_id, scope['root_id'], content=content)



def _eligible(account, now, cfg, automatic):
    """本地调度候选判据：启用、有凭据、当天未完成且退避允许。

    automatic=True 才检查自动开关和时间；网络身份和授权另行核验。
    """
    today = now.strftime('%Y-%m-%d')
    if (not account['enabled'] or not str(account['token'] or '').strip()
            or account['last_day'] == today):
        return False
    if automatic:
        if not account['auto_sign']:
            return False
        moment = clamp_sign_time(account['sign_time'] or cfg.get('sign_time'))
        if now.strftime('%H:%M') < moment:
            return False
    if account['retry_day'] == today:
        if account['retry_count'] > len(cfg['retry_delays']):
            return False
        if account['next_run_at'] and account['next_run_at'] > int(time.time()):
            return False
    return True


def scheduler_tick(cfg=None):
    if _draining.is_set():
        return []
    cfg = cfg or cfg_all()
    now = now_local(cfg)
    scopes = {}
    owner_mode = 'tg_allowed_user_ids' in cfg
    owner = configured_owner(cfg) if owner_mode else None
    if owner_mode and (owner is None or not Store().accounts.has_primary_account(owner)):
        return []
    with db() as conn:
        if owner_mode:
            all_accounts = [dict(row) for row in conn.execute(
                'SELECT * FROM accounts WHERE enabled=1 AND owner_tg_id=? ORDER BY id', (owner,))]
            grants = []
        else:
            all_accounts = [dict(row) for row in conn.execute('SELECT * FROM accounts WHERE enabled=1 ORDER BY id')]
            grants = [dict(row) for row in conn.execute('SELECT source_id,target_id FROM account_links WHERE enabled=1')]
    by_id = {account['id']: account for account in all_accounts}
    by_source = {}
    for grant in grants:
        by_source.setdefault(grant['source_id'], []).append(grant['target_id'])
    moment = now.strftime('%H:%M')
    for source in all_accounts:
        if (not source['auto_sign'] or source['last_status'] == 'invalid'
                or moment < clamp_sign_time(source['sign_time'] or cfg.get('sign_time'))):
            continue
        identity = (source['owner_tg_id'], source['id'])
        if _eligible(source, now, cfg, False):
            scopes.setdefault(source['id'], []).append(identity)
        if source['owner_tg_id'] is None:
            continue
        for target_id in by_source.get(source['id'], ()):
            target = by_id.get(target_id)
            if (target and target['owner_tg_id'] is not None
                    and _eligible(target, now, cfg, False)):
                scopes.setdefault(target['id'], []).append(identity)
    return _run_ids(cfg, list(scopes), 'auto', scopes=scopes) if scopes else []


def scheduler_loop():
    while not _shutdown.is_set():
        try:
            scheduler_tick()
            interval = cfg_all()['scheduler_interval']
        except Exception:
            print('[scheduler] 本地调度暂时失败，账号与授权未改变。')
            interval = _RUNTIME_CONFIG['scheduler_interval']
        _shutdown.wait(interval)


# 页面模板

LOGIN_PAGE = r"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · emos 签到</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e9ef;display:grid;place-items:center;
min-height:100vh;font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
.box{width:min(340px,90vw);background:#181c23;border:1px solid #272d38;
border-radius:14px;padding:26px 22px}
h1{font-size:16px;margin:0 0 18px;font-weight:600}
input{width:100%;background:#0d1015;border:1px solid #272d38;color:#e6e9ef;
border-radius:9px;padding:11px 12px;font-size:14px;margin-bottom:12px;font-family:inherit}
input:focus{outline:none;border-color:#4c8dff}
button{width:100%;background:#4c8dff;color:#fff;border:0;border-radius:9px;
padding:11px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
.err{color:#f85149;font-size:12px;margin-bottom:10px;min-height:16px}
</style></head><body>
<form class="box" onsubmit="return go(event)">
  <h1>🥕 emos 签到面板</h1>
  <div class="err" id="e"></div>
  <input id="pw" type="password" placeholder="面板密码" autocomplete="current-password" autofocus>
  <button>登录</button>
</form>
<script>
async function go(ev){
  ev.preventDefault();
  const r = await fetch('/api/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:document.getElementById('pw').value})});
  const j = await r.json();
  if(j.ok) location.href='/';
  else document.getElementById('e').textContent = j.error || '登录失败';
  return false;
}
</script></body></html>"""


PAGE = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>emoskata · 核心管理</title><style>\n:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101318;color:#eef2f7;font:14px/1.6 system-ui,sans-serif}main{max-width:960px;margin:auto;padding:22px 16px 60px}h1{font-size:24px}h2{font-size:17px;margin-top:0}section{background:#191f28;border:1px solid #303a49;border-radius:12px;padding:20px;margin:16px 0}label{display:block;margin-top:12px}input,select{width:100%;padding:9px;background:#111722;color:inherit;border:1px solid #303a49;border-radius:8px;font:inherit}button{padding:9px 14px;margin:12px 8px 0 0;border:0;border-radius:8px;background:#76a7ff;color:#111722;font:600 14px system-ui;cursor:pointer}button.secondary{background:#2b3646;color:inherit}button.danger{background:#51323a;color:#ffc1c7}.muted,small{color:#9caabd}small{display:block}.check{display:flex;gap:8px;align-items:center}.check input{width:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}.grid label{margin:0;padding:10px;border-bottom:1px solid #303a49}#message{white-space:pre-wrap;color:#a7d8bc}#runtime{white-space:pre-wrap}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #303a49}.scroll{overflow:auto}.top{display:flex;align-items:center;justify-content:space-between}\n</style></head><body><main><div class="top"><h1>🥕 emoskata · 核心管理</h1><button id="logout" class="secondary">退出</button></div><p class="muted">系统参数只读写同一份 .env。账号、签到设置和通知保存在 SQLite；历史共享数据保留但不启用；管理员密码不能用于个人登录。</p>\n<section><h2>Bot 服务</h2><label>Telegram Bot Token<input id="token" type="password" autocomplete="off" placeholder="留空保留，不回显"></label><label class="check"><input id="clear-token" type="checkbox">明确清除 Bot Token</label><p id="configured" class="muted"></p><p class="muted">emos 用户 ID 在下方系统参数的 EMOS_USER_ID 配置；填写后 Bot 的 /login 直接跳转官方授权，不再索要。官方授权后按账号 ID 与 TG 绑定核验。</p><label>个人网页公开地址<input id="web-base" placeholder="https://sign.example.com"></label><small>公网 HTTPS；仅填写域名和可选端口，不含路径。</small></section>\n<section><h2>全部系统参数</h2><p class="muted">带“重启生效”的参数保存后点击下面的核心重启。改变 DATA_DIR 会切换数据库位置，不会搬迁数据；仅更新程序时保持原目录。</p><div id="fields" class="grid"></div><button id="save">保存到 .env</button><button id="reload" class="secondary">重新读取配置</button></section><div id="message" role="status" aria-live="polite"></div>\n<section><h2>应用核心</h2><div id="runtime">正在读取…</div><button id="refresh-runtime" class="secondary">刷新运行信息</button><button id="restart" class="danger">重启应用核心</button><p class="muted">不重启宿主机，不需要 root。先删除旧 emoskata.py，再把新文件上传到同一路径，确认上传完成后点击重启。文件缺失或语法错误会拒绝重启并保留旧核心。</p></section>\n<section><h2>账号运行状态 · 只读</h2><div class="scroll"><table><thead><tr><th>账号</th><th>官方 ID</th><th>状态</th><th>可用余额</th><th>本次所得</th><th>最近签到</th></tr></thead><tbody id="rows"></tbody></table></div><p class="muted">首号官方授权、其他账号 Token 添加及账号设置请从 TG Bot 进入。旧版账号和记录不会因更新被清空。</p></section></main><script>\nconst $=id=>document.getElementById(id);let revision=\'\',controls=new Map();\nasync function api(path,body){const r=await fetch(\'/api/\'+path,{credentials:\'same-origin\',method:body===undefined?\'GET\':\'POST\',headers:{\'Content-Type\':\'application/json\'},body:body===undefined?undefined:JSON.stringify(body)});const j=await r.json();if(r.status===401){location.replace(\'/login\');throw Error(\'管理员会话已失效\');}if(!r.ok)throw Error(j.error||\'操作失败\');return j;}\nfunction say(text){$(\'message\').textContent=text||\'\';}\nasync function runtime(){const s=await api(\'runtime\');$(\'runtime\').textContent=\'核心 PID：\'+s.pid+\' · 线程：\'+s.threads+\'\\n当前内存：\'+(s.rss_bytes==null?\'不可读取\':(s.rss_bytes/1048576).toFixed(1)+\' MiB\')+\' · 峰值：\'+(s.max_rss_bytes==null?\'不可读取\':(s.max_rss_bytes/1048576).toFixed(1)+\' MiB\')+\'\\n监听：\'+s.bind_host+\':\'+s.port+\' · 容器 IP：\'+s.container_ip+\'\\n状态：\'+s.restart_status+\' · 网页待办：\'+s.pending_web_jobs+\' · 通知待办：\'+s.notification_queue+(s.restart_error?\'\\n\'+s.restart_error:\'\');}\nasync function load(){const c=await api(\'config\');revision=c.revision;controls=new Map([[\'TG_BOT_TOKEN\',$(\'token\')],[\'WEB_BASE_URL\',$(\'web-base\')]]);$(\'token\').value=\'\';$(\'clear-token\').checked=false;$(\'web-base\').value=c.web_base_url||\'\';$(\'configured\').textContent=c.tg_bot_configured?\'Token 已配置。留空保存保持不变。\':\'尚未配置 Token。\';$(\'fields\').replaceChildren();for(const f of c.fields){if(controls.has(f.key))continue;const label=document.createElement(\'label\');label.append(document.createTextNode(f.label+(f.restart?\' · 重启生效\':\'\')));const input=document.createElement(f.kind===\'bool\'?\'select\':\'input\');if(f.kind===\'bool\'){for(const [value,text] of [[\'1\',\'开启\'],[\'0\',\'关闭\']]){const option=document.createElement(\'option\');option.value=value;option.textContent=text;input.append(option);}}else{input.type=f.secret?\'password\':f.kind===\'int\'?\'number\':\'text\';if(f.kind===\'int\'){input.min=f.minimum;input.max=f.maximum;}if(f.secret){input.autocomplete=\'new-password\';input.placeholder=f.configured?\'已配置，留空保留\':\'留空保留\';}}input.value=f.value;input.dataset.key=f.key;label.append(input);const detail=document.createElement(\'small\');detail.textContent=f.description;label.append(detail);$(\'fields\').append(label);controls.set(f.key,input);}const rows=await api(\'accounts\');$(\'rows\').replaceChildren();for(const row of rows){const tr=document.createElement(\'tr\');for(const text of [row.name,row.emos_id||\'待重新授权\',row.last_status||\'未运行\',Number.isInteger(row.carrot)?row.carrot:\'未知\',Number.isInteger(row.last_carrot)?\'+\'+row.last_carrot:\'—\',row.last_run||\'—\']){const td=document.createElement(\'td\');td.textContent=text;tr.append(td);}$(\'rows\').append(tr);}await runtime();}\nasync function operation(fn){document.querySelectorAll(\'button\').forEach(b=>b.disabled=true);try{await fn();}catch(e){say(e.message||\'操作失败\');}finally{document.querySelectorAll(\'button\').forEach(b=>b.disabled=false);}}\n$(\'save\').onclick=()=>operation(async()=>{const changes={};for(const [key,control] of controls)changes[key]=control.value;const clear_secrets=$(\'clear-token\').checked?[\'TG_BOT_TOKEN\']:[];const result=await api(\'config\',{changes,revision,clear_secrets});say(\'已保存到唯一配置文件 .env。\'+(result.restart_required.length?\'\\n重启生效：\'+result.restart_required.join(\', \'):\'\'));if(result.changed.includes(\'PANEL_PASSWORD\'))location.replace(\'/login\');else await load();});\n$(\'reload\').onclick=()=>operation(load);$(\'refresh-runtime\').onclick=()=>operation(runtime);\n$(\'restart\').onclick=()=>operation(async()=>{if(!confirm(\'确认新 py 已上传完成？这只重启应用核心，账号和配置不删除。\'))return;const r=await api(\'restart\',{confirm:true});say(r.message+\'\\n网络任务收尾后重启，可稍后刷新运行信息。\');});\n$(\'logout\').onclick=()=>operation(async()=>{await api(\'logout\',{});location.replace(\'/login\');});load().catch(e=>say(e.message));\n</script></body></html>\n'


# HTTP 入口

def _cookie_value(handler, name):
    """读取请求中的第一个同名 Cookie；缺失返回空字符串。
    """
    raw = handler.headers.get('Cookie') or ''
    for part in raw.split(';'):
        key, _, value = part.strip().partition('=')
        if key == name:
            return value
    return ''


def _cookie_sid(handler):
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, val = part.strip().partition("=")
        if k == "sid":
            return val
    return ""


def _update_config(body):
    allowed = set(CFG_TO_ENV) | {'changes', 'revision', 'clear_secrets', 'new_password', 'clear_tg_token'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise EnvConfigError('管理员请求包含未知配置项。')
    changes = dict(body.get('changes') or {})
    if not isinstance(body.get('changes', {}), dict):
        raise EnvConfigError('配置更改必须是对象。')
    for name, key in CFG_TO_ENV.items():
        if name in body:
            changes[key] = body[name]
    if body.get('new_password'):
        changes['PANEL_PASSWORD'] = body['new_password']
    clear = list(body.get('clear_secrets') or [])
    if body.get('clear_tg_token') is True:
        clear.append('TG_BOT_TOKEN')
    outcome = CONFIG.update(changes, revision=body.get('revision'), clear_secrets=clear)
    if 'PANEL_PASSWORD' in outcome['changed']:
        init_password()
    return outcome


class Handler(BaseHTTPRequestHandler):
    def _web_page(self, kind):
        nonce = secrets.token_urlsafe(18)
        self._web_nonce = nonce
        return self._send(200, render_page(kind, nonce), 'text/html; charset=utf-8')

    def _web_cookie(self, sid='', clear=False):
        base = cfg_all().get('web_base_url') or public_base(self)
        secure = '; Secure' if str(base).startswith('https://') else ''
        age = 0 if clear else cfg_all()['web_session_hours'] * 3600
        return 'wsid=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s' % (sid, age, secure)

    def _web_identity(self, mutating=False):
        sid = _cookie_value(self, 'wsid')
        session = personal_web().authenticate(sid)
        if session is None:
            self._send(401, {'ok': False, 'error': '请从 Bot 重新打开网页配置。'})
            return None
        if mutating:
            try:
                personal_web().authorize(sid, self.headers.get('X-CSRF-Token'), mutating=True)
            except DomainError:
                self._send(403, {'ok': False, 'error': '网页验证失败，请刷新页面。'})
                return None
        return session

    def _web_get(self, path):
        if path in ('/web/login', '/web/auth'):
            return self._web_page('auth' if path.endswith('/auth') else 'login')
        if path in ('/web', '/web/'):
            if personal_web().authenticate(_cookie_value(self, 'wsid')) is None:
                return self._send(302, '', 'text/plain', location='/web/login')
            return self._web_page('dashboard')
        session = self._web_identity()
        if session is None:
            return
        actor = session['tg_id']
        try:
            if path == '/api/web/me':
                result = personal_web().state(actor)
                result.update(ok=True, csrf_token=session['csrf_token'], default_sign_time=cfg_all().get('sign_time'))
                return self._send(200, result)
            if path == '/api/web/logs':
                return self._send(200, {'ok': True, 'rows': personal_web().logs(actor)})
            if path == '/api/web/job':
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                job_id = (query.get('job_id') or [''])[0]
                return self._send(200, personal_web().job(actor, job_id))
            return self._send(404, {'ok': False, 'error': '网页接口不存在。'})
        except DomainError as error:
            return self._send(400, {'ok': False, 'error': str(error)})

    def _web_post(self, path, body):
        if path == '/api/web/login':
            ip = 'web:' + self._ip()
            if rl_blocked(ip):
                return self._send(429, {'ok': False, 'error': '尝试过多，请稍后再试。'})
            if set(body) != {'code'}:
                rl_fail(ip)
                return self._send(400, {'ok': False, 'error': '仅接受一次性网页入口。'})
            try:
                login = personal_web().exchange(body.get('code'))
            except DomainError as error:
                rl_fail(ip)
                return self._send(400, {'ok': False, 'error': str(error)})
            rl_ok(ip)
            return self._send(200, {'ok': True}, cookie=self._web_cookie(login['sid']))
        session = self._web_identity(mutating=True)
        if session is None:
            return
        actor = session['tg_id']
        try:
            action = path.removeprefix('/api/web/')
            if action == 'logout':
                if body:
                    raise DomainError('退出请求不接受额外字段。')
                personal_web().logout(_cookie_value(self, 'wsid'))
                return self._send(200, {'ok': True}, cookie=self._web_cookie(clear=True))
            if action == 'sign':
                return self._send(202, personal_web().start_sign(actor, body))
            result = personal_web().action(actor, action, body)
            return self._send(200, result)
        except DomainError as error:
            return self._send(400, {'ok': False, 'error': str(error)})

    server_version = "emos-panel/1.0"

    def handle_one_request(self):
        try:
            return super().handle_one_request()
        except DatabaseBusy:
            self.close_connection = True
            return self._send(503, {'ok': False, 'error': '数据库繁忙，请稍后重试。'})

    def log_message(self, fmt, *args):
        pass

    # 请求工具

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None, location=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        nonce = getattr(self, '_web_nonce', None)
        if nonce:
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'nonce-%s'; "
                             "style-src 'nonce-%s'; connect-src 'self'; form-action 'self'; "
                             "base-uri 'none'; frame-ancestors 'none'" % (nonce, nonce))
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass
        finally:
            self._web_nonce = None

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ValueError('请求长度无效。') from None
        if n < 0 or n > 65536:
            raise ValueError('请求体最多 64KB。')
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ValueError('请求体必须是 JSON 对象。') from None
        if not isinstance(body, dict):
            raise ValueError('请求体必须是 JSON 对象。')
        return body

    def _same_origin(self):
        site = self.headers.get('Sec-Fetch-Site')
        if site in ('cross-site', 'same-site'):
            return False
        origin = self.headers.get('Origin')
        if not origin:
            return True
        endpoint = urllib.parse.urlsplit(self.path).path
        configured = cfg_all().get('web_base_url') if endpoint.startswith('/api/web/') else None
        wanted = urllib.parse.urlsplit(configured or public_base(self))
        actual = urllib.parse.urlsplit(origin)
        return (actual.scheme, actual.netloc) == (wanted.scheme, wanted.netloc)

    def _ip(self):
        return (self.client_address[0] if self.client_address else "") or "?"

    def _authed(self):
        return session_ok(_cookie_sid(self))

    def _deny(self):
        self._send(401, {"ok": False, "error": "未登录"})

    # GET 路由

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/web', '/web/', '/web/login', '/web/auth') or path.startswith('/api/web/'):
            try:
                return self._web_get(path)
            except Exception:
                return self._send(500, {'ok': False, 'error': '网页服务暂不可用，请稍后重试。'})

        if path in ("/", "/index.html", "/admin"):
            if not self._authed():
                return self._send(302, "", "text/plain", location="/login")
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if path == "/login":
            return self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")

        if path in ('/oauth/start', '/oauth/callback'):
            return self._send(410, {'ok': False, 'error': '网页登录已停用，请在 Telegram Bot 使用官方授权登录。'})

        if path == "/api/health":
            return self._send(200, {"ok": True, "time": datetime.now().isoformat()})

        if not self._authed():
            return self._deny()

        if path == '/api/accounts':
            if 'tg_allowed_user_ids' in cfg_all():
                return self._send(200, Store().accounts.admin_overview())
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    'SELECT id,name,emos_id,enabled,auto_sign,sign_time,sign_mode,owner_tg_id,'
                    'last_status,last_msg,last_carrot,last_run,last_day,carrot,carrot_at,created '
                    'FROM accounts ORDER BY id').fetchall()]
            return self._send(200, rows)

        if path == "/api/logs":
            if "tg_allowed_user_ids" in cfg_all():
                owner = configured_owner(cfg_all())
                return self._send(200, Store().accounts.logs(owner, limit=100) if owner else [])
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM logs ORDER BY id DESC LIMIT 120").fetchall()]
            return self._send(200, rows)

        if path == '/api/config':
            cfg = cfg_all()
            safe = {key: value for key, value in cfg.items() if key not in ('tg_bot_token', 'panel_password')}
            safe['tg_bot_configured'] = bool(cfg['tg_bot_token'])
            safe.update(CONFIG.public_fields())
            return self._send(200, safe)

        if path == '/api/runtime':
            info = resource_snapshot()
            info.update(restart_status=_restart_status, restart_error=_restart_error,
                        accepting_tasks=not _draining.is_set(), container_ip=_CONTAINER_IP,
                        bind_host=_RUNTIME_CONFIG['http_host'], port=_RUNTIME_CONFIG['port'],
                        pending_web_jobs=len(_web_service.pending) if _web_service else 0,
                        notification_queue=_notifications.qsize())
            return self._send(200, info)

        return self._send(404, {"ok": False, "error": "not found"})

    # POST 路由

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if _draining.is_set():
            return self._send(503, {'ok': False, 'error': '应用核心正在重启收尾，请稍后重试。'})
        if not self._same_origin():
            return self._send(403, {'ok': False, 'error': '禁止跨站操作。'})
        try:
            body = self._body()
        except ValueError as error:
            return self._send(400, {'ok': False, 'error': str(error)})
        if path.startswith('/api/web/'):
            try:
                return self._web_post(path, body)
            except Exception:
                return self._send(500, {'ok': False, 'error': '网页服务暂不可用，请稍后重试。'})

        if path == "/api/login":
            ip = self._ip()
            left = rl_blocked(ip)
            if left:
                return self._send(429, {"ok": False,
                                        "error": "尝试过多，%d 秒后再试" % left})
            if not verify_password(str(body.get('password') or '')):
                cnt = rl_fail(ip)
                rest = max(0, LOGIN_MAX_FAIL - cnt)
                return self._send(401, {"ok": False,
                                        "error": "密码错误" + ("，还剩 %d 次" % rest if rest else "，已锁定")})
            rl_ok(ip)
            tok = create_session(ip, self.headers.get("User-Agent"))
            secure = '; Secure' if public_base(self).startswith('https://') else ''
            cookie = 'sid=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s' % (tok, cfg_all()['admin_session_hours'] * 3600, secure)
            return self._send(200, {"ok": True}, cookie=cookie)

        if path == "/api/logout":
            drop_session(_cookie_sid(self))
            return self._send(200, {"ok": True}, cookie="sid=; Path=/; Max-Age=0")

        if not self._authed():
            return self._deny()

        if path == '/api/accounts':
            return self._send(410, {'ok': False, 'error': '请在 TG Bot 通过官方 Bot 登录添加账号。'})

        if path == '/api/run':
            return self._send(410, {'ok': False, 'error': '请在 TG Bot 内签到。'})

        if path == '/api/config':
            try:
                outcome = _update_config(body)
            except (ValueError, EmosError, DomainError) as error:
                return self._send(400, {'ok': False, 'error': str(error)})
            return self._send(200, dict(outcome, ok=True))

        if path == '/api/restart':
            if body != {'confirm': True}:
                return self._send(400, {'ok': False, 'error': '需明确确认重启应用核心。'})
            try:
                CONFIG.snapshot(force=True)
            except EnvConfigError as error:
                return self._send(400, {'ok': False, 'error': str(error)})
            try:
                accepted = request_restart()
            except EnvConfigError as error:
                return self._send(400, {'ok': False, 'error': str(error)})
            if not accepted:
                return self._send(409, {'ok': False, 'error': '应用核心已在重启收尾。'})
            return self._send(202, {'ok': True, 'message': '正在收尾任务并重启应用核心，不重启宿主机。'})

        if path == '/api/notify-test':
            return self._send(410, {'ok': False, 'error': '通知请在 Bot 或配置网页里设置。管理员不设全局通知目标。'})

        return self._send(404, {"ok": False, "error": "not found"})

    # PATCH / DELETE 路由

    def do_PATCH(self):
        if not self._authed():
            return self._deny()
        if not self._same_origin():
            return self._send(403, {'ok': False, 'error': '禁止跨站操作。'})
        return self._send(410, {'ok': False, 'error': '请在 Bot 内设置自己的账号。'})

    def do_DELETE(self):
        if not self._authed():
            return self._deny()
        if not self._same_origin():
            return self._send(403, {'ok': False, 'error': '禁止跨站操作。'})
        return self._send(410, {'ok': False, 'error': '账号关联由双方在 Bot 内撤销。'})


def bot_supervisor():
    global _active_bot
    from bot import Bot
    identity, current = None, None
    try:
        while not _shutdown.is_set():
            try:
                cfg = cfg_all()
                if _draining.is_set():
                    if current:
                        current.begin_drain()
                    _shutdown.wait(1)
                    continue
                wanted = (cfg['tg_bot_token'], cfg['tg_api_base_url'], cfg['tg_poll_timeout'],
                          cfg['bot_workers'], cfg['bot_max_jobs'], configured_owner(cfg))
                dead = current is not None and current._poll_thread is not None and not current._poll_thread.is_alive()
                if wanted != identity or (wanted[0] and dead):
                    if current:
                        current.begin_drain()
                        if not current.join_jobs(cfg['shutdown_timeout']):
                            _shutdown.wait(5)
                            continue
                        current.stop(wait=True, timeout=cfg['tg_poll_timeout'] + 20)
                        if current._poll_thread and current._poll_thread.is_alive():
                            _shutdown.wait(5)
                            continue
                    identity, current = wanted, None
                    if wanted[0] and wanted[-1] is not None:
                        current = Bot(wanted[0], Store(), workers=cfg['bot_workers'], max_jobs=cfg['bot_max_jobs'])
                        thread = threading.Thread(target=current.run, name='emos-bot-poll', daemon=True)
                        current._poll_thread = thread
                        with _active_bot_lock:
                            _active_bot = current
                        thread.start()
                        print('[bot] 官方首登、Token 多账号及网页入口已启用。')
                    else:
                        print('[bot] 请在 .env 配置 Bot Token 和唯一 TG ID。')
            except Exception:
                print('[bot] 当前配置或网络暂不可用，稍后重试。')
            _shutdown.wait(5)
    finally:
        if current:
            current.begin_drain()
            current.join_jobs(_RUNTIME_CONFIG['shutdown_timeout'])
            current.stop(wait=True, timeout=_RUNTIME_CONFIG['shutdown_timeout'])
        with _active_bot_lock:
            _active_bot = None


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, maximum=8, request_timeout=15):
        self.slots = threading.BoundedSemaphore(maximum)
        self.request_timeout = request_timeout
        self._requests_lock = threading.Lock()
        self._active_requests = 0
        self.request_idle = threading.Event()
        self.request_idle.set()
        super().__init__(address, handler)

    @property
    def active_requests(self):
        with self._requests_lock:
            return self._active_requests

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._requests_lock:
            self._active_requests += 1
            self.request_idle.clear()
        try:
            super().process_request(request, address)
        except BaseException:
            with self._requests_lock:
                self._active_requests -= 1
                if not self._active_requests:
                    self.request_idle.set()
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self._requests_lock:
                self._active_requests -= 1
                if not self._active_requests:
                    self.request_idle.set()
            self.slots.release()


_PROBE_MARK = '__emoskata_probe_ok__'
# 预检引导：以 __probe__ 导入候选后调用 main()，用 EMOS_STARTUP_PROBE 让 main 在就绪处提前返回。
_PROBE_BOOTSTRAP = "import os,sys\npath,mark,entry=sys.argv[1:4]\nwith open(path,'rb') as handle: source=handle.read()\nsys.argv=[entry]\nsys.path[0]=os.path.dirname(path)\nnamespace={'__name__':'__probe__','__file__':path,'__package__':None,'__spec__':None,\n           '__emos_image__':source}\nexec(compile(source,path,'exec'),namespace)\nmain=namespace.get('main')\nif callable(main): main()\nprint(mark)\n"


def _probe_core(image, entry, module_dir=None):
    """在临时子进程执行候选初始化，验证数据库迁移、监听与核心线程创建。

    配置和 SQLite 都使用副本；业务线程停在门闩，不进入 Bot、调度或通知。
    候选沿用当前端口时改绑临时端口，真实切换仍要重新绑定目标端口。
    失败抛配置错误；不返回镜像，也不是任意候选代码的安全沙箱。
    """
    import shutil
    import subprocess
    import tempfile
    workdir = tempfile.mkdtemp(prefix='.emos-probe-')
    try:
        try:
            raw = CONFIG.path.read_bytes()
        except OSError:
            raise EnvConfigError('配置文件不可读取，无法做启动自检。当前核心保留运行。') from None
        env_copy = os.path.join(workdir, '.env')
        with open(env_copy, 'wb') as handle:
            handle.write(raw)
        os.chmod(env_copy, 0o600)
        # 预检用真实 SQLite 的一致性副本，不迁移生产数据。
        candidate = ConfigStore(env_copy)
        candidate_cfg = candidate.app_config()
        selected = Path(candidate_cfg['data_dir']).expanduser()
        selected = selected if selected.is_absolute() else Path(entry).parent / selected
        if selected.exists() and not selected.is_dir():
            raise EnvConfigError('数据目录不是目录；更新未切换。')
        probe_data = Path(workdir) / 'data'
        probe_data.mkdir(mode=0o700)
        selected_db = selected / 'data.db'
        if selected_db.exists():
            try:
                snapshot_database(str(selected_db), str(probe_data / 'data.db'))
            except (sqlite3.Error, OSError, RuntimeError):
                raise EnvConfigError('现有数据库无法生成一致性副本；更新未切换。') from None
        candidate.update({'DATA_DIR': str(probe_data)})
        image.seek(0)
        source_copy = os.path.join(workdir, os.path.basename(entry))
        with open(source_copy, 'wb') as handle:
            handle.write(image.read())
        image.seek(0)
        environment = dict(os.environ, EMOS_ENV_FILE=env_copy, PYTHONDONTWRITEBYTECODE='1',
                           EMOS_STARTUP_PROBE='1')
        environment.pop('EMOS_UPDATE_ACTIVE', None)
        # 自己仍占有的端口只能在同地址的临时端口验证；新端口实际试绑定。
        environment['EMOS_PROBE_REUSE_BIND'] = '1' if (
            _HTTP_SERVER is not None and candidate_cfg['port'] == _HTTP_SERVER.server_port) else '0'
        # 隔离调用方的导入路径；仅分模块入口需要显式提供同目录依赖。
        environment.pop('PYTHONPATH', None)
        if module_dir is not None:
            environment['PYTHONPATH'] = module_dir
        for key in ('PORT', 'HTTP_ENABLED', 'DATA_DIR', 'TG_BOT_TOKEN', 'TG_ALLOWED_USER_IDS'):
            environment.pop(key, None)
        try:
            done = subprocess.run(
                [sys.executable, '-u', '-c', _PROBE_BOOTSTRAP, source_copy, _PROBE_MARK, entry],
                cwd=workdir, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
        except subprocess.TimeoutExpired:
            raise EnvConfigError('新 py 启动自检 90 秒未完成，已拒绝重启，当前核心保留运行。') from None
        except OSError:
            raise EnvConfigError('无法启动自检进程，已拒绝重启，当前核心保留运行。') from None
        output = (done.stdout or b'').decode('utf-8', 'replace')
        if done.returncode != 0 or _PROBE_MARK not in output:
            tail = [line.strip() for line in output.strip().splitlines() if line.strip()][-3:]
            raise EnvConfigError(
                '新 py 语法合法，但启动自检失败（退出码 %s）：%s。当前核心保留运行，文件未切换。'
                % (done.returncode, ' | '.join(tail) if tail else '无输出'))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _prepare_core():
    """固定候选字节并完成语法及隔离启动预检，返回游标为零的临时镜像。

    只有入口本身缺失时尝试恢复成功快照，并要求再次重启；依赖缺失不覆盖入口。
    异常关闭镜像。接受后再删或重传入口不改变本次使用的字节。
    """
    import ast
    import tempfile
    entry = os.path.abspath(__file__)
    try:
        try:
            with open(entry, 'rb') as handle:
                source = handle.read(2097153)
        except FileNotFoundError:
            restored = _restore_last_good(entry)
            if restored is None:
                raise EnvConfigError('更新文件缺失且没有可用的成功快照；当前核心保留运行，请重新上传 py。') from None
            raise EnvConfigError('入口文件缺失，已从最近一次成功启动的快照恢复到 %s。请再次发起重启。' % restored) from None
        if not source or len(source) > 2097152:
            raise EnvConfigError('更新文件为空或过大，请先上传完整 py。当前核心保留运行。')
        tree = ast.parse(source, filename=entry)
        embedded = next((node.value for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == '_SOURCES'
                                 for target in node.targets)), None)
        if embedded is not None:
            modules = ast.literal_eval(embedded)
            required = {'envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app'}
            if not isinstance(modules, dict) or not required.issubset(modules):
                raise EnvConfigError('单文件内嵌模块不完整，请重新生成 py。当前核心保留运行。')
            for name, text in modules.items():
                if not isinstance(name, str) or not isinstance(text, str):
                    raise EnvConfigError('更新文件格式无效。当前核心保留运行。')
                compile(text, entry + ':' + name, 'exec')
        else:
            for name in ('envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot'):
                path = os.path.join(BASE_DIR, name + '.py')
                with open(path, 'rb') as handle:
                    compile(handle.read(2097152), path, 'exec')
        CONFIG.snapshot(force=True)
        image = tempfile.TemporaryFile()
        try:
            image.write(source)
            image.flush()
            image.seek(0)
            _probe_core(image, entry, BASE_DIR if embedded is None else None)
        except BaseException:
            # 预检失败时关闭镜像句柄，再把错误交给重启请求返回。
            image.close()
            raise
        return image
    except EnvConfigError:
        raise
    except (OSError, ValueError, TypeError, SyntaxError, RecursionError):
        raise EnvConfigError('更新文件缺失、未上传完整或语法无效。当前核心保留运行，请重新上传 py。') from None


def _restore_last_good(entry):
    """只恢复缺失入口；快照定位与排他写入由 updates 统一维护。
    """
    return restore_missing_entry(entry, str(CONFIG.path))

def _exec_core(image=None):
    """切换已验证字节；启动失败时由更新事务恢复旧代码、配置和数据库。
    """
    image = image or _prepare_core()
    cfg = CONFIG.app_config()
    selected = Path(cfg['data_dir']).expanduser()
    selected = selected if selected.is_absolute() else Path(BASE_DIR) / selected
    exec_update(image, os.path.abspath(__file__), str(CONFIG.path),
                list(dict.fromkeys((DB_PATH, str(selected / 'data.db')))))


def _drain_core(timeout=0):
    """开始拒收新业务，检查队列、请求、连接和网络任务是否都已归还。

    这是一次即时判定，不等待；main 负责重试和截止时间。
    """
    if _web_service is not None:
        _web_service.begin_drain()
        with _web_service.lock:
            if _web_service.pending:
                return False
    if _run_lock.locked():
        return False
    with _db_pool_lock:
        if _db_borrowed or _db_closing or _db_waiters or _db_quarantine:
            return False
    with _active_bot_lock:
        current = _active_bot
    if current is not None:
        current.begin_drain()
        if not current.join_jobs(0):
            return False
        if current._poll_thread and current._poll_thread.is_alive():
            return False
    if _HTTP_SERVER is not None and _HTTP_SERVER.active_requests:
        return False
    with _notifications.all_tasks_done:
        if _notifications.unfinished_tasks:
            return False
    return True


def _gated_core_task(task):
    _CORE_START_GATE.wait()
    if not _shutdown.is_set():
        task()


def _start_core_threads():
    tasks = (('emos-scheduler', scheduler_loop), ('emos-supervisor', bot_supervisor),
             ('emos-notifications', notification_loop))
    existing = {thread.name: thread for thread in _CORE_THREADS}
    _CORE_THREADS[:] = []
    for name, task in tasks:
        thread = existing.get(name)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_gated_core_task, args=(task,), daemon=False, name=name)
            thread.start()
        _CORE_THREADS.append(thread)


def _resume_core():
    _seal_db_pool(False)
    _shutdown.clear()
    _notification_wakeup.clear()
    _draining.clear()
    _CORE_START_GATE.set()
    if _web_service is not None:
        _web_service.resume()
    with _active_bot_lock:
        current = _active_bot
    if current is not None and not current.stop_event.is_set():
        current.resume()
    _start_core_threads()


def _stop_core(timeout):
    """通知核心退出并等待线程、连接收尾；返回是否允许安全 exec。

    没有收完返回 False，调用方取消更新并恢复旧核心。
    """
    deadline = time.monotonic() + max(0, timeout)
    _request_shutdown()
    _CORE_START_GATE.set()
    for worker in _CORE_THREADS:
        worker.join(max(0, deadline - time.monotonic()))
    if _web_service is not None:
        _web_service.stop(timeout=max(0, deadline - time.monotonic()))
    threads = [thread for thread in threading.enumerate()
               if thread is not threading.current_thread() and thread.name.startswith('emos-')]
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    ready = not any(thread.is_alive() for thread in threads)
    with _db_pool_lock:
        ready = ready and not (_db_borrowed or _db_closing or _db_waiters or _db_quarantine)
    if ready:
        _seal_db_pool(True)
        close_idle_db()
    return ready


def _cancel_restart(message, status='failed'):
    """拒绝重启时统一回滚状态，避免界面显示"正在重启"但实际没动。
    """
    global _restart_status, _restart_error, _restart_image
    _restart_status, _restart_error = status, message
    if _restart_image is not None:
        _restart_image.close()
        _restart_image = None
    _restart_requested.clear()
    _resume_core()


_CORE_THREADS = []
_restart_image = None


def main():
    """初始化持久化、监听和核心线程；就绪提交后才释放业务门闩。

    预检模式在就绪点返回；正常模式处理停机与同 PID 更新，finally 收尾自己创建的资源。
    """
    global _HTTP_SERVER, _restart_status, _restart_error
    _CORE_START_GATE.clear()
    _HTTP_SERVER = None
    server = None
    restart_deadline = None
    try:
        cfg = cfg_all()
        init_db()
        legacy = import_legacy_config()
        if legacy['imported']:
            if not _STARTUP_PROBE:
                print('[config] 已导入旧配置：%s' % ', '.join(legacy['imported']))
            cfg = cfg_all()
        if legacy['rejected'] and not _STARTUP_PROBE:
            print('[config] 部分旧配置无效，请在管理页检查。')
        if cfg['http_enabled']:
            init_password()
        if not cfg['http_enabled'] and (not cfg['tg_bot_token'] or configured_owner(cfg) is None):
            raise EnvConfigError('纯 Bot 模式需要 Bot Token 和唯一 TG ID。')
        _shutdown.clear()
        _notification_wakeup.clear()
        _draining.clear()
        _restart_requested.clear()
        _restart_status, _restart_error = 'running', ''
        if cfg['http_enabled']:
            bind_port = 0 if (_STARTUP_PROBE and os.environ.get('EMOS_PROBE_REUSE_BIND') == '1') else cfg['port']
            server = _BoundedHTTPServer((cfg['http_host'], bind_port), Handler,
                                       cfg['http_max_connections'], cfg['http_request_timeout'])
            server.timeout = 1
            _HTTP_SERVER = server
            if not _STARTUP_PROBE:
                print('管理入口：http://%s:%d/admin' % (_CONTAINER_IP, cfg['port']))
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: _shutdown.set())
        _start_core_threads()
        if len(_CORE_THREADS) != 3 or not all(t.is_alive() for t in _CORE_THREADS):
            raise EnvConfigError('核心线程未成功启动。')
        if _STARTUP_PROBE:
            return
        # 真配置/数据初始化、监听和线程创建均成功；之后才允许调度和 TG 请求。
        mark_ready(os.path.abspath(__file__), str(CONFIG.path))
        _CORE_START_GATE.set()
        while not _shutdown.is_set():
            if _restart_requested.is_set():
                if restart_deadline is None:
                    restart_deadline = time.monotonic() + cfg_all()['shutdown_timeout']
                if _HTTP_SERVER is not None:
                    _HTTP_SERVER.request_idle.wait(0.05)
                elapsed = time.monotonic() >= restart_deadline
                if _drain_core():
                    try:
                        CONFIG.snapshot(force=True)
                        # 排空耗尽截止时间时仍给健康线程一个短暂退出调度窗口。
                        if not _stop_core(max(2.0, restart_deadline - time.monotonic())):
                            raise EnvConfigError('核心线程未收尾，已取消重启并恢复运行。')
                        _restart_status = 'restarting'
                        _exec_core(_restart_image)
                        raise EnvConfigError('重启没有完成，已恢复运行。')
                    except (OSError, EnvConfigError, RuntimeError):
                        _cancel_restart('核心重启未完成，旧核心已恢复运行。请检查更新文件与配置。')
                    restart_deadline = None
                elif elapsed:
                    _cancel_restart('当前网络任务未结束，已取消重启，保留运行。', 'timeout')
                    restart_deadline = None
            else:
                _start_core_threads()
            if _HTTP_SERVER is not None:
                if _restart_requested.is_set():
                    _HTTP_SERVER.request_idle.wait(0.05)
                _HTTP_SERVER.handle_request()
            else:
                _shutdown.wait(1)
    finally:
        _request_shutdown()
        _CORE_START_GATE.set()
        if server is not None:
            server.server_close()
        for worker in _CORE_THREADS:
            worker.join(timeout=2 if _STARTUP_PROBE else _RUNTIME_CONFIG['shutdown_timeout'])
        if _web_service is not None:
            _web_service.stop(timeout=2 if _STARTUP_PROBE else 55)
        close_idle_db()
        if not _STARTUP_PROBE:
            print('emoskata 应用核心已停止。')


if __name__ == "__main__":
    main()
