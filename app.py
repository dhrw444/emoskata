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
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import signal

from emos import EmosClient, EmosError, clamp_sign_time, valid_base
from accounts import AccountService, DomainError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "data.db")

# 时区兜底表（容器没装 tzdata 时用；只收无夏令时的常用区）
TZ_FALLBACK = {
    "Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "Asia/Macau": 8,
    "Asia/Singapore": 8, "Asia/Taipei": 8, "Asia/Tokyo": 9,
    "Asia/Seoul": 9, "Asia/Bangkok": 7, "Asia/Kolkata": 5.5,
    "UTC": 0, "Etc/UTC": 0,
}

SESSION_TTL = 7 * 86400          # cookie 有效期 7 天
LOGIN_MAX_FAIL = 5               # 连续失败次数
LOGIN_LOCK_SEC = 300             # 锁定时长
RETRY_DELAYS = (300, 900, 2700)  # 失败重试：5 / 15 / 45 分钟

DEFAULTS = {
    "base_url": "https://emos.best",
    "sign_time": "08:00",
    "timezone": "Asia/Shanghai",
    "content": "",
    "gap_seconds": 8,
    "tg_bot_token": "",
    "tg_chat_id": "",
    "bark_key": "",
    "notify_success": "1",
    "notify_fail": "1",
    "notify_already": "0",
    "notify_summary": "1",
    'oauth_app_name': 'emoskata',
    'emos_app_user_id': '',
}

_write_lock = threading.RLock()
_run_lock = threading.Lock()
_shutdown = threading.Event()
_notifications = queue.Queue(maxsize=256)
_notification_drops = 0


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=20000')
    return conn


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
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(accounts)')}
        if 'retry_day' not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN retry_day TEXT DEFAULT ''")
        conn.execute("DELETE FROM settings WHERE key LIKE 'oauth_state_%' OR key LIKE 'oauth_back_%'")
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
    cfg = dict(DEFAULTS)
    with db() as conn:
        for r in conn.execute("SELECT key,value FROM settings"):
            cfg[r['key']] = r['value']
    for env, key in (('TG_BOT_TOKEN', 'tg_bot_token'), ('EMOS_BASE_URL', 'base_url'),
                     ('SIGN_TIME', 'sign_time'), ('SIGN_TIMEZONE', 'timezone'),
                     ('EMOS_APP_USER_ID', 'emos_app_user_id')):
        value = os.environ.get(env)
        if value is not None and value.strip():
            cfg[key] = value.strip()
    return cfg


class Store:
    def __init__(self):
        self.accounts = AccountService(db, write_db, cfg_all)

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

    def run_all(self, cfg, trigger='manual', only_id=None):
        return run_all(cfg, trigger, only_id)

    def run_user(self, tg_id, include_links=True, expected_root=None):
        return run_user(tg_id, include_links, expected_root)


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
    stored = get_setting('panel_pw_hash')
    src = os.environ.get('PANEL_PASSWORD') or ''
    if src and len(src) < 6:
        raise ValueError('PANEL_PASSWORD 至少 6 个字符。')
    if src:
        if not verify_password(src):
            set_setting('panel_pw_hash', hash_password(src))
            drop_all_sessions()
        return None
    if stored:
        return None
    src = secrets.token_urlsafe(18)
    set_setting('panel_pw_hash', hash_password(src))
    return src


# ================================================================== session

def purge_sessions():
    now = int(time.time())
    with _write_lock, db() as conn:
        conn.execute("DELETE FROM sessions WHERE created<=?", (now - SESSION_TTL,))


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
    if not tok or len(tok) > 256:
        return False
    now = int(time.time())
    with db() as conn:
        r = conn.execute("SELECT created FROM sessions WHERE token=?", (tok,)).fetchone()
    if not r or not (now - SESSION_TTL < r["created"] <= now):
        return False
    with _write_lock, db() as conn:
        conn.execute("UPDATE sessions SET last_seen=? WHERE token=?", (now, tok))
    return True


def drop_session(tok):
    if tok:
        with _write_lock, db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (tok,))


def drop_all_sessions():
    with _write_lock, db() as conn:
        conn.execute("DELETE FROM sessions")


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
    return cnt


def rl_ok(ip):
    with _write_lock, db() as conn:
        conn.execute("DELETE FROM login_fail WHERE ip=?", (ip,))


# ================================================================== 通知

def public_base(handler=None):
    """推断对外访问地址：环境变量 > 请求头 > 本地。"""
    env = (os.environ.get("PUBLIC_BASE") or "").strip()
    if env:
        return env.rstrip("/")
    if handler is not None:
        proto = (handler.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (handler.headers.get("X-Forwarded-Host")
                or handler.headers.get("Host") or "").split(",")[0].strip()
        if host:
            return "%s://%s" % (proto, host)
    return "http://127.0.0.1:%d" % int(os.environ.get("PORT") or 8080)


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


def notify(cfg, title, text=''):
    from bot import TG
    token = str(cfg.get('tg_bot_token') or '').strip()
    chat = cfg.get('tg_chat_id')
    if not token or not chat:
        return False, '尚未配置 TG 通知目标。'
    try:
        TG(token, Store()).send(chat, '<b>%s</b>\n%s' % (_esc(title), text))
        return True, ''
    except Exception:
        return False, 'TG 通知发送失败，请检查 Bot 配置和网络。'


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
    if 1 <= count <= len(RETRY_DELAYS):
        return int(time.time()) + RETRY_DELAYS[count - 1]
    return 0


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
        done_day = today if status in ('signed', 'already') else current['last_day']
        enabled = 0 if status == 'invalid' else current['enabled']
        conn.execute('INSERT INTO logs(account_id,name,ts,status,message,carrot) VALUES(?,?,?,?,?,?)',
                     (account['id'], safe_name, stamp, status, result['message'], result.get('point')))
        conn.execute('UPDATE accounts SET last_status=?,last_msg=?,last_carrot=?,last_run=?,last_day=?,'
                     'retry_count=?,next_run_at=?,retry_day=?,enabled=? WHERE id=?',
                     (status, result['message'], result.get('point'), stamp, done_day,
                      retries, next_at, today, enabled, account['id']))
    row = {'id': account['id'], 'emos_id': account['emos_id'], 'name': safe_name,
           'status': status, 'message': result['message'], 'carrot': result.get('point')}
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


def _run_ids(cfg, account_ids, trigger='manual', actor=None, root_id=None, scopes=None):
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
                    client = EmosClient(account['token'], endpoint)
                    client.before_request = allowed
                    client.expected_user_id = account['emos_id']
                    result = client.sign_once(cfg.get('content') or '')
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


def run_user(tg_id, include_links=True, expected_root=None):
    cfg = cfg_all()
    scope = Store().accounts.run_scope(tg_id, include_links)
    if expected_root is not None and scope['root_id'] != expected_root:
        raise DomainError('当前账号已切换，本次签到未执行。')
    return _run_ids(cfg, scope['account_ids'], 'user', tg_id, scope['root_id'])


def run_all(cfg, trigger='manual', only_id=None):
    if trigger in ('auto', 'retry'):
        return scheduler_tick(cfg)
    with db() as conn:
        rows = conn.execute('SELECT id FROM accounts WHERE enabled=1 ' +
                            ('AND id=? ' if only_id is not None else '') + 'ORDER BY id',
                            (only_id,) if only_id is not None else ()).fetchall()
    return _run_ids(cfg, [row['id'] for row in rows], trigger)


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
        if account['retry_count'] > len(RETRY_DELAYS):
            return False
        if account['next_run_at'] and account['next_run_at'] > int(time.time()):
            return False
    return True


def scheduler_tick(cfg=None):
    cfg = cfg or cfg_all()
    now = now_local(cfg)
    scopes = {}
    with db() as conn:
        all_accounts = [dict(row) for row in conn.execute('SELECT * FROM accounts WHERE enabled=1 ORDER BY id')]
        grants = [dict(row) for row in conn.execute('SELECT source_id,target_id FROM account_links WHERE enabled=1')]
    by_id = {account['id']: account for account in all_accounts}
    for source in all_accounts:
        if (not source['auto_sign'] or source['last_status'] == 'invalid'
                or now.strftime('%H:%M') < clamp_sign_time(source['sign_time'] or cfg.get('sign_time'))):
            continue
        identity = (source['owner_tg_id'], source['id'])
        if _eligible(source, now, cfg, False):
            scopes.setdefault(source['id'], []).append(identity)
        if source['owner_tg_id'] is None:
            continue
        for grant in grants:
            target = by_id.get(grant['target_id'])
            if (grant['source_id'] == source['id'] and target and target['owner_tg_id'] is not None
                    and _eligible(target, now, cfg, False)):
                scopes.setdefault(target['id'], []).append(identity)
    return _run_ids(cfg, list(scopes), 'auto', scopes=scopes) if scopes else []


def scheduler_loop():
    while not _shutdown.is_set():
        try:
            scheduler_tick()
        except Exception:
            print('[scheduler] 调度失败，下次检查自动重试。')
        _shutdown.wait(30)


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


PAGE = r'''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>emoskata · Bot 管理</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0f1115;color:#e6e9ef;font:14px/1.6 system-ui,sans-serif}
main{max-width:900px;margin:auto;padding:24px 16px}h1{font-size:22px}h2{font-size:16px;margin-top:0}
section{background:#181c23;border:1px solid #272d38;border-radius:12px;padding:18px;margin:18px 0}
label{display:block;color:#9da6b6;margin-top:12px}input{width:100%;background:#0d1015;color:#fff;border:1px solid #303949;border-radius:7px;padding:10px;font:inherit}
button{border:0;border-radius:7px;background:#4c8dff;color:white;padding:9px 16px;margin:14px 8px 0 0;font:inherit;cursor:pointer}.muted{color:#9da6b6}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #303949}#message{white-space:pre-wrap;color:#83c69a}.scroll{overflow:auto}.top{display:flex;justify-content:space-between;align-items:center}
</style></head><body><main>
<div class="top"><h1>🥕 emoskata · Bot 管理</h1><button id="logout">退出</button></div>
<p class="muted">这里配置运行环境。账号通过 Telegram Bot 登录；关联、签到、撤销和个人通知全部在 Bot 菜单内操作。</p>
<section><h2>Bot 与官方授权</h2>
<label>Bot Token（留空保留现值，不会回显）<input id="token" type="password" autocomplete="off" placeholder="BotFather 签发的 Token"></label>
<p id="configured" class="muted"></p>
<label>应用创建者的 emos 用户 ID<input id="app-id" placeholder="eXXXXXXXXs"></label>
<p class="muted">用于官方授权链接的应用身份，不是 Telegram 数字 ID。可在 emos 主站「欢迎」里复制。</p>
<label>emos API 地址<input id="base" placeholder="https://emos.best"></label>
<label>默认时区<input id="zone" placeholder="Asia/Shanghai"></label>
<label>默认签到时间<input id="sign-time" placeholder="08:00"></label>
<label>账号间隔（2–300 秒）<input id="gap" type="number" min="2" max="300"></label>
<label>签到附言（留空随机，最多 10 字）<input id="content" maxlength="10"></label>
<button id="save">保存配置</button></section>
<section><h2>管理密码</h2><label>新密码（至少 6 字符）<input id="password" type="password" autocomplete="new-password"></label>
<button id="change-password">更改密码并退出所有会话</button>
<p class="muted">环境变量 PANEL_PASSWORD 非空时启动会应用该密码；更改后同步修改部署环境，避免重启时覆盖。</p></section>
<div id="message" role="status"></div>
<section><h2>账号运行状态（只读）</h2><div class="scroll"><table><thead><tr><th>账号</th><th>官方 ID</th><th>状态</th><th>最近签到</th></tr></thead><tbody id="rows"></tbody></table></div>
<p class="muted">旧版账号原样保留。未完成官方登录归属的账号不会被任何 TG 用户看见或按 ID 接管。</p></section>
</main><script>
const $=id=>document.getElementById(id),v=id=>$(id).value.trim();
async function api(path,body){const r=await fetch(path,{credentials:'same-origin',method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(r.status===401){location.href='/login';throw Error('未登录');}const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j;}
const report=e=>{$('message').textContent=e.message||String(e);};
async function load(){try{const c=await api('/api/config');$('app-id').value=c.emos_app_user_id||'';$('base').value=c.base_url||'';$('zone').value=c.timezone||'';$('sign-time').value=c.sign_time||'';$('gap').value=c.gap_seconds||8;$('content').value=c.content||'';$('configured').textContent=c.tg_bot_configured?'Bot Token 已配置。留空保存不会清除。':'Bot Token 尚未配置。';const rows=await api('/api/accounts');$('rows').replaceChildren();for(const row of rows){const tr=document.createElement('tr');for(const text of [row.name,row.emos_id||'旧版未归属',row.last_status||'未运行',row.last_run||'—']){const td=document.createElement('td');td.textContent=text;tr.appendChild(td);}$('rows').appendChild(tr);}}catch(e){report(e);}}
$('save').onclick=async()=>{try{const b={emos_app_user_id:v('app-id'),base_url:v('base'),timezone:v('zone'),sign_time:v('sign-time'),gap_seconds:Number(v('gap')),content:v('content')};if(v('token'))b.tg_bot_token=v('token');await api('/api/config',b);$('token').value='';$('message').textContent='已保存。环境变量非空时优先于网页配置。';await load();}catch(e){report(e);}};
$('change-password').onclick=async()=>{try{if(v('password').length<6)throw Error('密码至少 6 个字符。');await api('/api/config',{new_password:v('password')});location.href='/login';}catch(e){report(e);}};
$('logout').onclick=async()=>{try{await api('/api/logout',{});location.href='/login';}catch(e){report(e);}};
load();setInterval(load,30000);
</script></body></html>'''


# ================================================================== HTTP

def _cookie_sid(handler):
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, val = part.strip().partition("=")
        if k == "sid":
            return val
    return ""


def _update_config(body):
    cfg = cfg_all()
    updates = {}
    for key in ('base_url', 'timezone', 'content', 'emos_app_user_id', 'tg_chat_id'):
        if key in body:
            updates[key] = str(body[key] or '').strip()
    if 'base_url' in updates:
        updates['base_url'] = valid_base(updates['base_url'])
    if 'timezone' in updates:
        now_local(dict(cfg, timezone=updates['timezone']))
    if len(updates.get('content', '')) > 10:
        raise ValueError('签到附言最多 10 个字。')
    app_id = updates.get('emos_app_user_id')
    if app_id and (len(app_id) > 48 or not all(c.isascii() and (c.isalnum() or c == '_') for c in app_id)):
        raise ValueError('应用账号 ID 必须使用 emos 官方用户 ID。')
    if 'sign_time' in body:
        updates['sign_time'] = clamp_sign_time(body['sign_time'])
    if 'gap_seconds' in body:
        gap = int(body['gap_seconds'])
        if not 2 <= gap <= 300:
            raise ValueError('账号间隔必须为 2–300 秒。')
        updates['gap_seconds'] = str(gap)
    token = str(body.get('tg_bot_token') or '').strip()
    if token:
        if len(token) > 256 or ':' not in token or any(c.isspace() for c in token):
            raise ValueError('Bot Token 格式错误。')
        updates['tg_bot_token'] = token
    if body.get('clear_tg_token') is True:
        updates['tg_bot_token'] = ''
    password = str(body.get('new_password') or '')
    if password and len(password) < 6:
        raise ValueError('管理密码至少 6 个字符。')
    with write_db() as conn:
        for key, value in updates.items():
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        if password:
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         ('panel_pw_hash', hash_password(password)))
            conn.execute('DELETE FROM sessions')


class Handler(BaseHTTPRequestHandler):
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
        self.send_header("X-Content-Type-Options", "nosniff")
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

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
        wanted = urllib.parse.urlsplit(public_base(self))
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

        if path in ("/", "/index.html"):
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
                    'SELECT id,name,emos_id,enabled,auto_sign,sign_time,last_status,last_msg,'
                    'last_carrot,last_run,last_day,created FROM accounts ORDER BY id').fetchall()]
            return self._send(200, rows)

        if path == "/api/logs":
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM logs ORDER BY id DESC LIMIT 120").fetchall()]
            return self._send(200, rows)

        if path == '/api/config':
            cfg = cfg_all()
            safe = {key: cfg.get(key) for key in DEFAULTS if key not in ('tg_bot_token', 'bark_key')}
            safe['tg_bot_configured'] = bool(cfg.get('tg_bot_token'))
            safe['bark_configured'] = bool(cfg.get('bark_key'))
            return self._send(200, safe)

        return self._send(404, {"ok": False, "error": "not found"})

    # ---------- POST ----------

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._same_origin():
            return self._send(403, {'ok': False, 'error': '禁止跨站操作。'})
        try:
            body = self._body()
        except ValueError as error:
            return self._send(400, {'ok': False, 'error': str(error)})

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
            cookie = 'sid=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s' % (tok, SESSION_TTL, secure)
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
                _update_config(body)
            except (ValueError, EmosError) as error:
                return self._send(400, {'ok': False, 'error': str(error)})
            return self._send(200, {'ok': True})

        if path == "/api/notify-test":
            ok, err = notify(cfg_all(), "🥕 emos 签到面板",
                             "这是一条测试消息。\n配置正常。")
            return self._send(200, {"ok": ok, "error": None if ok else err})

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
    from bot import Bot
    token, current, worker = None, None, None
    try:
        while not _shutdown.is_set():
            try:
                wanted = str(cfg_all().get('tg_bot_token') or '').strip()
                dead = worker is not None and not worker.is_alive()
                if wanted != token or (wanted and dead):
                    if current:
                        current.stop(wait=True, timeout=50)
                    if worker and worker.is_alive():
                        _shutdown.wait(5)
                        continue
                    token, current, worker = wanted, None, None
                    if wanted:
                        current = Bot(wanted, Store())
                        worker = threading.Thread(target=current.run, name='emos-bot-poll', daemon=True)
                        worker.start()
                        print('[bot] 已启动，官方账号登录与关联在 TG 菜单内配置。')
                    else:
                        print('[bot] 尚未配置 TG_BOT_TOKEN，可通过管理页设置。')
            except Exception:
                print('[bot] 启动检查失败，下次自动重试。')
            _shutdown.wait(15)
    finally:
        if current:
            current.stop(wait=True, timeout=50)


def main():
    init_db()
    cfg = cfg_all()
    valid_base(cfg.get('base_url'))
    now_local(cfg)
    enabled_http = str(os.environ.get('HTTP_ENABLED', '1')).lower() not in ('0', 'false', 'off')
    generated = init_password() if enabled_http else None
    server = None
    if enabled_http:
        host = os.environ.get('HTTP_HOST', '0.0.0.0')
        port = int(os.environ.get('PORT') or 8080)
        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        server.timeout = 1
        print('管理入口：http://%s:%d/（账号操作在 Telegram Bot）' % (host, port))
        if generated:
            print('首次管理密码（仅显示一次）：%s' % generated)
    elif not cfg.get('tg_bot_token'):
        raise ValueError('纯 Bot 模式必须设置 TG_BOT_TOKEN。')
    _shutdown.clear()
    def stopping(*_):
        _shutdown.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stopping)
    workers = [threading.Thread(target=task, daemon=True, name=name) for task, name in
               ((scheduler_loop, 'emos-scheduler'), (bot_supervisor, 'emos-bot-supervisor'),
                (notification_loop, 'emos-notifications'))]
    for worker in workers:
        worker.start()
    try:
        while not _shutdown.is_set():
            if server:
                server.handle_request()
            else:
                _shutdown.wait(1)
    finally:
        _shutdown.set()
        if server:
            server.server_close()
        for worker in workers:
            worker.join(timeout=55)
        print('emoskata 已停止。')


if __name__ == "__main__":
    main()
