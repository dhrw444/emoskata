"""emoskata runtime: Telegram-owned accounts, one-hop grants and bounded daily signing.

Run: python3 app.py or generated emoskata.py; Python 3.10+ standard library.
The optional HTTP page only configures the service. Account actions live in TG.
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
                       FIXED_ZONE_OFFSETS)
from runtime_info import container_ip, snapshot as resource_snapshot
from emos import EmosClient, EmosError, clamp_sign_time, pick_content
from accounts import AccountService, DomainError
from webui import WebService, render_page

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = ConfigStore(config_path(__file__), create=True)
_RUNTIME_CONFIG = CONFIG.app_config()
_data = Path(_RUNTIME_CONFIG['data_dir']).expanduser()
DATA_DIR = str(_data if _data.is_absolute() else Path(BASE_DIR) / _data)
os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'data.db')
_EXISTING_DB = os.path.isfile(DB_PATH)
TZ_FALLBACK = FIXED_ZONE_OFFSETS
LOGIN_MAX_FAIL = 5
LOGIN_LOCK_SEC = 300
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


def request_restart():
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
_db_pool = []
_db_borrowed = 0
_DB_IDLE_LIMIT = 3
LOG_KEEP = 200


class _Connection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        if self.connection is None:
            raise sqlite3.ProgrammingError('数据库连接租约已关闭')
        return getattr(self.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            return self.connection.__exit__(*args)
        finally:
            self.close()

    def close(self):
        global _db_borrowed
        connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            connection.close()
            connection = None
        with _db_pool_lock:
            _db_borrowed -= 1
            if connection is not None and len(_db_pool) < _DB_IDLE_LIMIT:
                _db_pool.append(connection)
                return
        if connection is not None:
            connection.close()


def db():
    global _db_borrowed
    with _db_pool_lock:
        connection = _db_pool.pop() if _db_pool else None
    if connection is None:
        connection = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA cache_size=-256')
    with _db_pool_lock:
        _db_borrowed += 1
    return _Connection(connection)


def close_idle_db():
    with _db_pool_lock:
        connections, _db_pool[:] = list(_db_pool), []
    for connection in connections:
        connection.close()


@contextmanager
def write_db():
    with _write_lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn


def init_db():
    with write_db() as conn:
        for statement in (
            'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)',
            'CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY,created INTEGER,'
            'last_seen INTEGER,ip TEXT,ua TEXT)',
            'CREATE TABLE IF NOT EXISTS login_fail (ip TEXT PRIMARY KEY,count INTEGER '
            'DEFAULT 0,until INTEGER DEFAULT 0)',
        ):
            conn.execute(statement)
        AccountService.init_schema(conn)
        WebService.init_schema(conn)
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(accounts)')}
        if 'retry_day' not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN retry_day TEXT DEFAULT ''")
        conn.execute("DELETE FROM settings WHERE key LIKE 'oauth_state_%' OR key LIKE 'oauth_back_%'")
        # 限流表按来源 IP 单调增长，长期运行会积累到几万行；过期条目没有保留价值。
        conn.execute("DELETE FROM login_fail WHERE until<>0 AND until<?", (int(time.time()),))
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
    # 解析结果按 .env 的 stat 指纹缓存（见 ConfigStore.app_config），
    # 每次调用仍会 stat 一次：这是「改了配置立刻生效」的代价，不能省。
    return CONFIG.app_config()


# 旧版把系统配置存在 SQLite 的 settings 表里，新版只认 .env。只换 py 不搬配置，
# Bot Token 会静默失效、管理员密码会变成新生成的随机值。这里做一次性导入。
# 注意：只写「.env 里还是默认值」的项，用户在 .env 里配置过的一律不覆盖。
LEGACY_SETTING_KEYS = (
    ('base_url', 'EMOS_BASE_URL'),
    ('sign_time', 'SIGN_TIME'),
    ('timezone', 'SIGN_TIMEZONE'),
    ('gap_seconds', 'GAP_SECONDS'),
    ('tg_bot_token', 'TG_BOT_TOKEN'),
)


def import_legacy_config():
    """把旧版 settings 表里的系统配置搬进 .env。

    旧版管理员密码来自进程环境变量 PANEL_PASSWORD，库里只有 scrypt 哈希、无法反推。
    因此只在「.env 是这一次刚建出来的」时，把环境变量里的原密码写进去，
    这样升级后原密码继续可用。.env 已存在时一律以 .env 为准。
    """
    state = {'imported': [], 'untouched': [], 'rejected': [], 'password_inherited': False}
    if get_setting('legacy_config_imported') == '1':
        return state
    current, _ = CONFIG.snapshot()
    changes = {}
    if CONFIG.created:
        inherited = (os.environ.get('PANEL_PASSWORD') or '').strip()
        if len(inherited) >= 8:
            changes['PANEL_PASSWORD'] = inherited
    for legacy, key in LEGACY_SETTING_KEYS:
        value = (get_setting(legacy) or '').strip()
        if not value or value == SCHEMA[key].default or current.get(key) != SCHEMA[key].default:
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
    state['password_inherited'] = 'PANEL_PASSWORD' in state['imported']
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


# ================================================================== 密码

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
    source = CONFIG.password()
    if not verify_password(source):
        with write_db() as conn:
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         ('panel_pw_hash', hash_password(source)))
            conn.execute('DELETE FROM sessions')
    return source if CONFIG.created else None


# ================================================================== session

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


# ================================================================== 限流

def rl_blocked(ip):
    now = int(time.time())
    with db() as conn:
        r = conn.execute("SELECT until FROM login_fail WHERE ip=?", (ip,)).fetchone()
    return max(0, (r["until"] or 0) - now) if r else 0


def rl_fail(ip):
    now = int(time.time())
    with _write_lock, db() as conn:
        r = conn.execute("SELECT count FROM login_fail WHERE ip=?", (ip,)).fetchone()
        cnt = (r["count"] if r else 0) + 1
        until = now + LOGIN_LOCK_SEC if cnt >= LOGIN_MAX_FAIL else 0
        conn.execute(
            "INSERT INTO login_fail(ip,count,until) VALUES(?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET count=excluded.count,until=excluded.until",
            (ip, cnt, until))
        # 每 64 次失败顺手清一次过期行，避免表随扫描来源 IP 无限增长。
        global _RL_SWEEP
        _RL_SWEEP += 1
        if _RL_SWEEP % 64 == 0:
            conn.execute("DELETE FROM login_fail WHERE until<>0 AND until<?", (now,))
    return cnt


def rl_ok(ip):
    with _write_lock, db() as conn:
        conn.execute("DELETE FROM login_fail WHERE ip=?", (ip,))


# ================================================================== 通知

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
    token = str(cfg.get('tg_bot_token') or '')
    if not token:
        return False
    TG(token, Store()).send(current['chat_id'], build_report([row], 0, 0))
    return True


def notification_loop():
    while not _shutdown.is_set():
        try:
            task = _notifications.get(timeout=1)
        except queue.Empty:
            continue
        try:
            _deliver_notification(task)
        except Exception:
            print('[notify] 发送失败，详细签到结果已保存在账号日志。')
        finally:
            _notifications.task_done()


def _account_record(account_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    return dict(row) if row else None


def _next_retry(count):
    delays = cfg_all()['retry_delays']
    return int(time.time()) + delays[count - 1] if 1 <= count <= len(delays) else 0


def _store_result(account, result, cfg, trigger):
    now = now_local(cfg)
    today, stamp = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d %H:%M:%S')
    status = result['status']
    with write_db() as conn:
        current = conn.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone()
        if not current or current['token'] != account['token'] or current['emos_id'] != account['emos_id']:
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
        # 每个账号只保留最近 LOG_KEEP 条：面板只显示 15 条，长期运行不能让日志表无限增长。
        conn.execute('DELETE FROM logs WHERE account_id=? AND id <= '
                     '(SELECT id FROM logs WHERE account_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)',
                     (account['id'], account['id'], LOG_KEEP))
        # carrot 是官方账号的可用余额（/api/user 的 carrot），只有真的问到才更新；
        # last_carrot 是「本次签到得到多少」，两者不是一回事。
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
                if actor is not None:
                    return service.can_run(actor, root_id, account_id)
                if scopes is not None:
                    return _eligible(current, now_local(cfg), cfg, False) and any(
                        _auto_permission(owner, source, account_id, cfg)
                        for owner, source in scopes.get(account_id, []))
                return True
            if not allowed():
                continue
            endpoint = account.get('base_url') or cfg.get('base_url') or 'https://emos.best'
            if endpoint in unavailable:
                result = {'status': 'network', 'message': '同一接口暂不可达，已安排退避重试。', 'point': None}
            else:
                try:
                    client = EmosClient(account['token'], endpoint, timeout=cfg['emos_timeout'])
                    client.before_request = allowed
                    client.expected_user_id = account['emos_id']
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
    today = now.strftime('%Y-%m-%d')
    if not account['enabled'] or account['last_day'] == today:
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
    with db() as conn:
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


# ================================================================== 页面

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


PAGE = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>emoskata · 核心管理</title><style>\n:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101318;color:#eef2f7;font:14px/1.6 system-ui,sans-serif}main{max-width:960px;margin:auto;padding:22px 16px 60px}h1{font-size:24px}h2{font-size:17px;margin-top:0}section{background:#191f28;border:1px solid #303a49;border-radius:12px;padding:20px;margin:16px 0}label{display:block;margin-top:12px}input,select{width:100%;padding:9px;background:#111722;color:inherit;border:1px solid #303a49;border-radius:8px;font:inherit}button{padding:9px 14px;margin:12px 8px 0 0;border:0;border-radius:8px;background:#76a7ff;color:#111722;font:600 14px system-ui;cursor:pointer}button.secondary{background:#2b3646;color:inherit}button.danger{background:#51323a;color:#ffc1c7}.muted,small{color:#9caabd}small{display:block}.check{display:flex;gap:8px;align-items:center}.check input{width:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}.grid label{margin:0;padding:10px;border-bottom:1px solid #303a49}#message{white-space:pre-wrap;color:#a7d8bc}#runtime{white-space:pre-wrap}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #303a49}.scroll{overflow:auto}.top{display:flex;align-items:center;justify-content:space-between}\n</style></head><body><main><div class="top"><h1>🥕 emoskata · 核心管理</h1><button id="logout" class="secondary">退出</button></div><p class="muted">系统参数只读写同一份 .env。个人账号、签到设置、通知和关联保存在 SQLite；管理员密码不能用于个人登录。</p>\n<section><h2>Bot 服务</h2><label>Telegram Bot Token<input id="token" type="password" autocomplete="off" placeholder="留空保留，不回显"></label><label class="check"><input id="clear-token" type="checkbox">明确清除 Bot Token</label><p id="configured" class="muted"></p><p class="muted">本人 emos 用户 ID 由各用户在自己的 Bot 登录流程填写，管理员不代填、不共享。官方授权后按账号 ID 与本人 TG 绑定核验。</p><label>个人网页公开地址<input id="web-base" placeholder="https://sign.example.com"></label><small>公网 HTTPS；仅填写域名和可选端口，不含路径。</small></section>\n<section><h2>全部系统参数</h2><p class="muted">带“重启生效”的参数保存后点击下面的核心重启。改变 DATA_DIR 会切换数据库位置，不会搬迁数据；仅更新程序时保持原目录。</p><div id="fields" class="grid"></div><button id="save">保存到 .env</button><button id="reload" class="secondary">重新读取配置</button></section><div id="message" role="status" aria-live="polite"></div>\n<section><h2>应用核心</h2><div id="runtime">正在读取…</div><button id="refresh-runtime" class="secondary">刷新运行信息</button><button id="restart" class="danger">重启应用核心</button><p class="muted">不重启宿主机，不需要 root。先删除旧 emoskata.py，再把新文件上传到同一路径，确认上传完成后点击重启。文件缺失或语法错误会拒绝重启并保留旧核心。</p></section>\n<section><h2>账号运行状态 · 只读</h2><div class="scroll"><table><thead><tr><th>账号</th><th>官方 ID</th><th>状态</th><th>最近签到</th></tr></thead><tbody id="rows"></tbody></table></div><p class="muted">账号授权、代签和个人配置请从本人 TG Bot 进入。旧版账号和记录不会因更新被清空。</p></section></main><script>\nconst $=id=>document.getElementById(id);let revision=\'\',controls=new Map();\nasync function api(path,body){const r=await fetch(\'/api/\'+path,{credentials:\'same-origin\',method:body===undefined?\'GET\':\'POST\',headers:{\'Content-Type\':\'application/json\'},body:body===undefined?undefined:JSON.stringify(body)});const j=await r.json();if(r.status===401){location.replace(\'/login\');throw Error(\'管理员会话已失效\');}if(!r.ok)throw Error(j.error||\'操作失败\');return j;}\nfunction say(text){$(\'message\').textContent=text||\'\';}\nasync function runtime(){const s=await api(\'runtime\');$(\'runtime\').textContent=\'核心 PID：\'+s.pid+\' · 线程：\'+s.threads+\'\\n当前内存：\'+(s.rss_bytes==null?\'不可读取\':(s.rss_bytes/1048576).toFixed(1)+\' MiB\')+\' · 峰值：\'+(s.max_rss_bytes==null?\'不可读取\':(s.max_rss_bytes/1048576).toFixed(1)+\' MiB\')+\'\\n监听：\'+s.bind_host+\':\'+s.port+\' · 容器 IP：\'+s.container_ip+\'\\n状态：\'+s.restart_status+\' · 网页待办：\'+s.pending_web_jobs+\' · 通知待办：\'+s.notification_queue+(s.restart_error?\'\\n\'+s.restart_error:\'\');}\nasync function load(){const c=await api(\'config\');revision=c.revision;controls=new Map([[\'TG_BOT_TOKEN\',$(\'token\')],[\'WEB_BASE_URL\',$(\'web-base\')]]);$(\'token\').value=\'\';$(\'clear-token\').checked=false;$(\'web-base\').value=c.web_base_url||\'\';$(\'configured\').textContent=c.tg_bot_configured?\'Token 已配置。留空保存保持不变。\':\'尚未配置 Token。\';$(\'fields\').replaceChildren();for(const f of c.fields){if(controls.has(f.key))continue;const label=document.createElement(\'label\');label.append(document.createTextNode(f.label+(f.restart?\' · 重启生效\':\'\')));const input=document.createElement(f.kind===\'bool\'?\'select\':\'input\');if(f.kind===\'bool\'){for(const [value,text] of [[\'1\',\'开启\'],[\'0\',\'关闭\']]){const option=document.createElement(\'option\');option.value=value;option.textContent=text;input.append(option);}}else{input.type=f.secret?\'password\':f.kind===\'int\'?\'number\':\'text\';if(f.kind===\'int\'){input.min=f.minimum;input.max=f.maximum;}if(f.secret){input.autocomplete=\'new-password\';input.placeholder=f.configured?\'已配置，留空保留\':\'留空保留\';}}input.value=f.value;input.dataset.key=f.key;label.append(input);const detail=document.createElement(\'small\');detail.textContent=f.description;label.append(detail);$(\'fields\').append(label);controls.set(f.key,input);}const rows=await api(\'accounts\');$(\'rows\').replaceChildren();for(const row of rows){const tr=document.createElement(\'tr\');for(const text of [row.name,row.emos_id||\'旧版未归属\',row.last_status||\'未运行\',row.last_run||\'—\']){const td=document.createElement(\'td\');td.textContent=text;tr.append(td);}$(\'rows\').append(tr);}await runtime();}\nasync function operation(fn){document.querySelectorAll(\'button\').forEach(b=>b.disabled=true);try{await fn();}catch(e){say(e.message||\'操作失败\');}finally{document.querySelectorAll(\'button\').forEach(b=>b.disabled=false);}}\n$(\'save\').onclick=()=>operation(async()=>{const changes={};for(const [key,control] of controls)changes[key]=control.value;const clear_secrets=$(\'clear-token\').checked?[\'TG_BOT_TOKEN\']:[];const result=await api(\'config\',{changes,revision,clear_secrets});say(\'已保存到唯一配置文件 .env。\'+(result.restart_required.length?\'\\n重启生效：\'+result.restart_required.join(\', \'):\'\'));if(result.changed.includes(\'PANEL_PASSWORD\'))location.replace(\'/login\');else await load();});\n$(\'reload\').onclick=()=>operation(load);$(\'refresh-runtime\').onclick=()=>operation(runtime);\n$(\'restart\').onclick=()=>operation(async()=>{if(!confirm(\'确认新 py 已上传完成？这只重启应用核心，账号和配置不删除。\'))return;const r=await api(\'restart\',{confirm:true});say(r.message+\'\\n网络任务收尾后重启，可稍后刷新运行信息。\');});\n$(\'logout\').onclick=()=>operation(async()=>{await api(\'logout\',{});location.replace(\'/login\');});load().catch(e=>say(e.message));\n</script></body></html>\n'


# ================================================================== HTTP

def _cookie_value(handler, name):
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
            self._send(401, {'ok': False, 'error': '请从本人 Bot 重新打开网页配置。'})
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
                return self._send(400, {'ok': False, 'error': '仅接受本人一次性网页入口。'})
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

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------

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

    # ---------- GET ----------

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
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    'SELECT id,name,emos_id,enabled,auto_sign,sign_time,sign_mode,owner_tg_id,'
                    'last_status,last_msg,last_carrot,last_run,last_day,carrot,carrot_at,created '
                    'FROM accounts ORDER BY id').fetchall()]
            return self._send(200, rows)

        if path == "/api/logs":
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

    # ---------- POST ----------

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
            return self._send(410, {'ok': False, 'error': '请在 TG Bot 对本人及已授权关联账号签到。'})

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
            return self._send(410, {'ok': False, 'error': '通知属于各用户本人，请在 Bot 或本人网页配置。管理员不设全局通知目标。'})

        return self._send(404, {"ok": False, "error": "not found"})

    # ---------- PATCH / DELETE ----------

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
                          cfg['bot_workers'], cfg['bot_max_jobs'])
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
                    if wanted[0]:
                        current = Bot(wanted[0], Store(), workers=cfg['bot_workers'], max_jobs=cfg['bot_max_jobs'])
                        thread = threading.Thread(target=current.run, name='emos-bot-poll', daemon=True)
                        current._poll_thread = thread
                        with _active_bot_lock:
                            _active_bot = current
                        thread.start()
                        print('[bot] 官方登录、签到关联与本人网页入口已启用。')
                    else:
                        print('[bot] 尚未配置 Token；管理员可在网页修改 .env。')
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


def _prepare_core():
    import ast
    import tempfile
    entry = os.path.abspath(__file__)
    try:
        with open(entry, 'rb') as handle:
            source = handle.read(2097153)
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
        image.write(source)
        image.flush()
        image.seek(0)
        return image
    except EnvConfigError:
        raise
    except (OSError, ValueError, TypeError, SyntaxError, RecursionError):
        raise EnvConfigError('更新文件缺失、未上传完整或语法无效。当前核心保留运行，请重新上传 py。') from None


def _exec_core(image=None):
    # Keep validated bytes through exec: deletion/re-upload cannot race the interpreter.
    image = image or _prepare_core()
    os.environ['EMOS_ENV_FILE'] = str(CONFIG.path)
    os.set_inheritable(image.fileno(), True)
    bootstrap = ("import os,sys; p=sys.argv[1]; fd=int(sys.argv[2]); "
                 "sys.argv=[p]; sys.path[0]=os.path.dirname(p); "
                 "f=os.fdopen(fd,'rb'); s=f.read(); f.close(); "
                 "exec(compile(s,p,'exec'), {'__name__':'__main__','__file__':p,"
                 "'__package__':None,'__spec__':None})")
    try:
        os.execv(sys.executable, [sys.executable, '-u', '-c', bootstrap,
                                 os.path.abspath(__file__), str(image.fileno())])
    finally:
        image.close()


def _drain_core(timeout=0):
    if _web_service is not None:
        _web_service.begin_drain()
        with _web_service.lock:
            if _web_service.pending:
                return False
    if _run_lock.locked():
        return False
    with _db_pool_lock:
        if _db_borrowed:
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


def _start_core_threads():
    tasks = (('emos-scheduler', scheduler_loop), ('emos-supervisor', bot_supervisor),
             ('emos-notifications', notification_loop))
    existing = {thread.name: thread for thread in _CORE_THREADS}
    _CORE_THREADS[:] = []
    for name, task in tasks:
        thread = existing.get(name)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=task, daemon=False, name=name)
            thread.start()
        _CORE_THREADS.append(thread)


def _resume_core():
    _shutdown.clear()
    _draining.clear()
    if _web_service is not None:
        _web_service.resume()
    with _active_bot_lock:
        current = _active_bot
    if current is not None and not current.stop_event.is_set():
        current.resume()
    _start_core_threads()


def _stop_core(timeout):
    deadline = time.monotonic() + max(0, timeout)
    _shutdown.set()
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
        ready = ready and not _db_borrowed
    if ready:
        close_idle_db()
    return ready


def _cancel_restart(message, status='failed'):
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
    global _HTTP_SERVER, _restart_status, _restart_error
    cfg = cfg_all()
    init_db()
    legacy = import_legacy_config()
    if legacy['imported']:
        print('[config] 已把旧版系统配置搬进 .env：%s' % ', '.join(legacy['imported']))
        cfg = cfg_all()
    if legacy['rejected']:
        print('[config] 旧版配置项不合法、已忽略（请在网页重新填写）：%s'
              % ', '.join(legacy['rejected']))
    generated = init_password() if cfg['http_enabled'] else None
    if legacy['password_inherited']:
        # 密码是延续旧版的，用户本来就知道，没必要再往日志里写一遍。
        generated = None
    if not cfg['http_enabled'] and not cfg['tg_bot_token']:
        raise EnvConfigError('纯 Bot 模式需要在 .env 填入 TG_BOT_TOKEN。')
    _shutdown.clear()
    _draining.clear()
    _restart_requested.clear()
    _restart_status, _restart_error = 'running', ''
    if cfg['http_enabled']:
        server = _BoundedHTTPServer((cfg['http_host'], cfg['port']), Handler,
                                    cfg['http_max_connections'], cfg['http_request_timeout'])
        server.timeout = 1
        _HTTP_SERVER = server
        print('管理入口：http://%s:%d/admin' % (_CONTAINER_IP, cfg['port']))
        print('监听 %s:%d；容器 IP 仅用于内网显示，外网入口用 WEB_BASE_URL。' % (cfg['http_host'], cfg['port']))
        if generated:
            print('首次管理员密码（仅此一次）：%s' % generated)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: _shutdown.set())
    _start_core_threads()
    restart_deadline = None
    try:
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
                        if not _stop_core(max(0, restart_deadline - time.monotonic())):
                            raise EnvConfigError('核心线程未收尾，已取消重启并恢复运行。')
                        _restart_status = 'restarting'
                        _exec_core(_restart_image)
                        raise EnvConfigError('重启没有完成，已恢复运行。')
                    except (OSError, EnvConfigError):
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
        _shutdown.set()
        if _HTTP_SERVER is not None:
            _HTTP_SERVER.server_close()
        for worker in _CORE_THREADS:
            worker.join(timeout=_RUNTIME_CONFIG['shutdown_timeout'])
        if _web_service is not None:
            _web_service.stop()
        close_idle_db()
        print('emoskata 应用核心已停止。')


if __name__ == "__main__":
    main()
