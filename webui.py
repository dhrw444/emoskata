"""个人网页会话、一次性入口、账号视图和按需签到任务。

监听由 app 持有；有效会话在 SQLite，任务和确认在内存，范围入队后固定。
Test: python3 -B -m unittest test_web test_owner_web test_multi_accounts。
"""
import hashlib
import hmac
import json
import queue
import re
import secrets
import threading
import time
from contextlib import closing
from urllib.parse import urlsplit

from accounts import DomainError
from envconfig import owner_permitted

WEB_CODE_TTL = 120
# 运行时会从配置计算实际会话期限；此值是默认值。
WEB_SESSION_TTL = 7 * 86400
WEB_JOB_TTL = 600

# 生产单人视图文案；降级文案保留用于历史测试兼容。
_STATUS_TEXT = {'signed': '签到成功', 'already': '今日已签到', 'network': '网络异常',
                'blocked': '接口访问受阻', 'rate': '接口限流', 'invalid': '登录凭证已失效',
                'failed': '签到失败', 'revoked': '授权已撤销'}
_OTHER_TEXT = dict(_STATUS_TEXT, invalid='目标需重新授权')


def status_text(status, own=True, fallback='签到状态已更新'):
    return (_STATUS_TEXT if own else _OTHER_TEXT).get(status, fallback)




def web_origin(value):
    value = str(value or '').strip().rstrip('/')
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise DomainError('网页地址无效，请由运营者配置 WEB_BASE_URL。') from None
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path
            or len(value) > 500 or any(c.isspace() for c in value)):
        raise DomainError('请配置不含路径的完整网页地址，例如 https://sign.example.com。')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise DomainError('公网个人配置网页必须使用 HTTPS。')
    return value


def _digest(value):
    """生成网页一次性码或 sid 的库内摘要；不负责 emos Token 的存储方式。
    """
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def _secret(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value) is not None


def _local_id(value, allow_zero=False):
    if type(value) is not int or value < (0 if allow_zero else 1) or value >= 2**63:
        raise DomainError('账号参数无效，请刷新页面。')
    return value


def _only(body, allowed):
    """拒绝白名单外字段，避免客户端悄悄改到不该改的列。
    """
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise DomainError('请求字段无效。')


class WebService:
    def __init__(self, db, write, cfg, accounts, run_user):
        self.db, self.write, self.cfg = db, write, cfg
        self.accounts, self.run_user = accounts, run_user
        self.lock = threading.RLock()
        self.jobs, self.requests, self.pending = {}, {}, {}
        self.logout_confirmations = {}
        self.queue = queue.Queue(maxsize=32)
        self.worker = None
        self.accepting = True
        self.stopping = threading.Event()

    def _owner(self, tg_id, conn=None):
        """要求该 TG ID 已通过官方授权并仍有有效主账号；conn 可复用调用方事务。
        """
        cfg = self.cfg()
        if 'tg_allowed_user_ids' in cfg:
            if not owner_permitted(cfg, tg_id):
                raise DomainError('只允许已完成官方首号授权的用户访问。')
            if conn is not None:
                primary = self.accounts._primary(conn, tg_id)
                logged_in = bool(primary is not None and str(primary['token'] or '').strip()
                                 and primary['enabled'] in (1, '1', True)
                                 and primary['last_status'] != 'invalid')
            else:
                primary = self.accounts.primary_account(tg_id)
                logged_in = bool(primary and primary.get('logged_in', False))
            if not logged_in:
                raise DomainError('只允许主号有有效登录凭证时访问；请先 /login 官方重授权。')
            return True
        return False

    @staticmethod
    def init_schema(conn):
        """在调用方事务中创建缺失的入口码与会话表，不清空已有有效会话。
        """
        conn.execute('CREATE TABLE IF NOT EXISTS web_auth_codes ('
                     'code_hash TEXT PRIMARY KEY,tg_id INTEGER NOT NULL REFERENCES tg_users(tg_id) '
                     'ON DELETE CASCADE,expires_at INTEGER NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_web_codes_user ON web_auth_codes(tg_id)')
        conn.execute('CREATE TABLE IF NOT EXISTS web_sessions ('
                     'sid_hash TEXT PRIMARY KEY,tg_id INTEGER NOT NULL REFERENCES tg_users(tg_id) '
                     'ON DELETE CASCADE,created INTEGER NOT NULL,expires_at INTEGER NOT NULL,'
                     'csrf_token TEXT NOT NULL)')

    def issue_url(self, tg_id):
        """生成一次性网页入口码；库内只存码的摘要，原码放在 URL fragment。
        """
        self._owner(tg_id)
        base = web_origin(self.cfg().get('web_base_url') or '')
        if not self.accounts.owned(tg_id):
            raise DomainError('先在 Bot 通过官方授权登录自己的账号。')
        code = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.write() as conn:
            exists = conn.execute('SELECT 1 FROM accounts WHERE owner_tg_id=? AND emos_id IS NOT NULL '
                                  "AND emos_id<>''", (tg_id,)).fetchone()
            if not exists:
                raise DomainError('先在 Bot 通过官方授权登录自己的账号。')
            conn.execute('DELETE FROM web_auth_codes WHERE tg_id=? OR expires_at<=?', (tg_id, now))
            conn.execute('INSERT INTO web_auth_codes VALUES(?,?,?)', (_digest(code), tg_id, now + WEB_CODE_TTL))
        return base + '/web/auth#code=' + code

    def exchange(self, code):
        """一次性入口码换会话；码删除与发会话在同一写事务里，重放拿不到第二次。
        """
        if not _secret(code):
            raise DomainError('网页入口无效、已过期或已使用，请在 Bot 重新打开。')
        now = int(time.time())
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.write() as conn:
            row = conn.execute('SELECT tg_id FROM web_auth_codes WHERE code_hash=? AND expires_at>?',
                               (_digest(code), now)).fetchone()
            if row is None:
                raise DomainError('网页入口无效、已过期或已使用，请在 Bot 重新打开。')
            tg_id = row['tg_id']
            self._owner(tg_id, conn=conn)
            if not conn.execute('SELECT 1 FROM accounts WHERE owner_tg_id=? AND emos_id IS NOT NULL '
                                "AND emos_id<>''", (tg_id,)).fetchone():
                raise DomainError('账号归属已失效，请重新登录 Bot。')
            removed = conn.execute('DELETE FROM web_auth_codes WHERE code_hash=? AND expires_at>?',
                                   (_digest(code), now))
            if removed.rowcount != 1:
                raise DomainError('网页入口已使用。')
            conn.execute('DELETE FROM web_sessions WHERE expires_at<=?', (now,))
            conn.execute('INSERT INTO web_sessions VALUES(?,?,?,?,?)',
                         (_digest(sid), tg_id, now,
                          now + int(self.cfg().get('web_session_hours', WEB_SESSION_TTL // 3600)) * 3600, csrf))
        self._owner(tg_id)
        return {'sid': sid, 'csrf_token': csrf, 'tg_id': tg_id}

    def authenticate(self, sid):
        """校验会话并复核账号归属；主账号失效后旧会话立即作废。
        """
        if not _secret(sid):
            return None
        now = int(time.time())
        with closing(self.db()) as conn:
            row = conn.execute('SELECT tg_id,created,expires_at,csrf_token FROM web_sessions '
                               'WHERE sid_hash=? AND created<=? AND expires_at>?',
                               (_digest(sid), now, now)).fetchone()
            if (row is None or row['created'] + int(self.cfg().get('web_session_hours', WEB_SESSION_TTL // 3600)) * 3600 <= now
                    or not conn.execute('SELECT 1 FROM accounts WHERE owner_tg_id=? '
                                        "AND emos_id IS NOT NULL AND emos_id<>''", (row['tg_id'],)).fetchone()):
                return None
        try:
            self._owner(row['tg_id'])
        except DomainError:
            return None
        return {'tg_id': row['tg_id'], 'csrf_token': row['csrf_token']}

    def logout(self, sid):
        """仅撤销个人网页会话；不删除 emos Token，不退出账号登录。

        """
        if _secret(sid):
            with self.write() as conn:
                conn.execute('DELETE FROM web_sessions WHERE sid_hash=?', (_digest(sid),))

    def authorize(self, sid, csrf=None, mutating=False):
        """会话 + CSRF 校验；mutating 为真时缺 token 一律拒绝。
        """
        session = self.authenticate(sid)
        if session is None:
            raise DomainError('网页会话已失效，请从 Bot 重新打开。')
        if mutating and (not _secret(csrf) or not hmac.compare_digest(csrf, session['csrf_token'])):
            raise DomainError('网页验证失败，请刷新页面。')
        return session

    def state(self, tg_id):
        owner_mode = self._owner(tg_id)
        active = self.accounts.active(tg_id, required=False)
        prefs = self.accounts.user(tg_id) or {}
        own = self.accounts.owned(tg_id)
        visible = self.accounts.visible(tg_id)
        basic = ('id', 'emos_id', 'name', 'last_status', 'last_carrot', 'last_run')
        if owner_mode:
            basic += ('carrot', 'carrot_at', 'enabled', 'auto_sign', 'sign_mode', 'sign_time')
        targets = [{k: row.get(k) for k in basic} for row in visible
                   if not active or row['id'] != active['id']]
        empty = {'incoming': [], 'outgoing': []}
        result = {'active': active, 'owned': own, 'targets': targets,
                  'links': empty if owner_mode else self.accounts.links(tg_id),
                  'pending_links': empty if owner_mode else self.accounts.pending_links(tg_id),
                  'preferences': {key: int(prefs.get(key) or 0) for key in
                                  ('notify_success', 'notify_fail', 'notify_already')},
                  'timezone': str(self.cfg().get('timezone') or 'Asia/Shanghai'),
                  'single_owner': owner_mode}
        self._owner(tg_id)
        return result

    def logs(self, tg_id):
        self._owner(tg_id)
        own_ids = {row['id'] for row in self.accounts.owned(tg_id)}
        rows = self.accounts.logs(tg_id, limit=40)
        result = []
        for row in rows:
            entry = {key: row.get(key) for key in ('account_id', 'emos_id', 'name', 'ts', 'status', 'carrot')}
            entry['message'] = (str(row.get('message') or '')[:240] if row['account_id'] in own_ids
                                else status_text(row.get('status')))
            result.append(entry)
        return result

    def action(self, tg_id, action, body):
        """分发网页动作；调用方已验过会话，这里再验账号归属。
        """
        owner_mode = self._owner(tg_id)
        if owner_mode and action in {'code', 'link', 'toggle', 'unlink', 'review', 'withdraw'}:
            raise DomainError('跨 TG 共享已关闭；从 Bot 用 /token 添加账号。')
        fields = {
            'switch': ('account_id', 'target_id'),
            'settings': ('account_id', 'auto_sign', 'sign_time', 'sign_mode'),
            'preferences': ('notify_success', 'notify_fail', 'notify_already'),
            'code': ('account_id',),
            'link': ('account_id', 'target_emos_id', 'code'),
            'toggle': ('account_id', 'source_id', 'target_id', 'enabled'),
            'unlink': ('account_id', 'source_id', 'target_id'),
            'review': ('account_id', 'source_id', 'target_id', 'approve'),
            'withdraw': ('account_id', 'source_id', 'target_id'),
            'account-logout': ('account_id', 'all_accounts', 'confirmation'),
        }
        if action not in fields:
            raise DomainError('网页操作无效。')
        _only(body, fields[action])
        if action == 'preferences':
            self.accounts.set_preferences(tg_id, **body)
            return {'ok': True}
        root = _local_id(body.get('account_id'), allow_zero=action == 'switch')
        if action == 'account-logout':
            return self._account_logout(tg_id, root, body)
        if action == 'switch':
            self.accounts.switch(tg_id, _local_id(body.get('target_id')), expected_root=root)
        elif action == 'settings':
            updates = {key: body[key] for key in ('auto_sign', 'sign_time', 'sign_mode') if key in body}
            if 'sign_time' in updates and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', str(updates['sign_time'])):
                raise DomainError('签到时间必须为 HH:MM，范围 00:00–23:59。')
            self.accounts.set_account(tg_id, expected_root=root, **updates)
        elif action == 'code':
            return dict(self.accounts.issue_code(tg_id, expected_root=root), ok=True)
        elif action == 'link':
            target, code = body.get('target_emos_id'), body.get('code')
            if not isinstance(target, str) or len(target) > 256 or not isinstance(code, str) or len(code) > 256:
                raise DomainError('目标官方 ID 或一次性码无效。')
            pending = self.accounts.redeem(tg_id, target.strip(), code.strip(), expected_root=root)
            return {'ok': True, 'pending': pending['pending']}
        elif action in ('review', 'withdraw'):
            source, target = _local_id(body.get('source_id')), _local_id(body.get('target_id'))
            if root not in (source, target):
                raise DomainError('只能处理包含当前账号的申请。')
            if action == 'review':
                if root != target or type(body.get('approve')) is not bool:
                    raise DomainError('只有目标账号能确认或拒绝关联。')
                return dict(self.accounts.review_link(tg_id, source, target, body['approve'],
                                                      expected_root=root), ok=True)
            self.accounts.withdraw_link(tg_id, source, target, expected_root=root)
        elif action == 'toggle':
            source, target = _local_id(body.get('source_id')), _local_id(body.get('target_id'))
            if source != root or type(body.get('enabled')) is not bool:
                raise DomainError('只能设置当前来源账号的联动开关。')
            self.accounts.toggle_link(tg_id, source, target, expected_root=root, enabled=body['enabled'])
        elif action == 'unlink':
            source, target = _local_id(body.get('source_id')), _local_id(body.get('target_id'))
            if root not in (source, target):
                raise DomainError('只能解除包含当前账号的关联。')
            self.accounts.unlink(tg_id, source, target, expected_root=root)
        return {'ok': True}

    def _account_logout(self, tg_id, root, body):
        """签发短时确认凭据；执行请求必须带回同一凭据、选择和登录代际。

        确认后调用域事务撤销本地密钥，取消排队任务；不向官方撤销 Token。
        """
        all_accounts = body.get('all_accounts', False)
        if type(all_accounts) is not bool:
            raise DomainError('退出范围必须为布尔值。')
        # 准备只读取当前账号；执行时由账号域事务再次验 owner 与根。
        with self.accounts._access(tg_id) as (conn, _):
            self.accounts._expected(conn, tg_id, root)
            revision = conn.execute('SELECT login_revision FROM tg_users WHERE tg_id=?',
                                    (tg_id,)).fetchone()['login_revision']
            count = conn.execute('SELECT COUNT(*) FROM accounts WHERE owner_tg_id=?',
                                 (tg_id,)).fetchone()[0] if all_accounts else 1
        with self.lock:
            confirmation = body.get('confirmation')
            if 'confirmation' not in body:
                confirmation = secrets.token_urlsafe(24)
                self.logout_confirmations[tg_id] = (root, all_accounts, revision,
                                                    confirmation, time.monotonic() + 120)
                return {'ok': True, 'needs_confirmation': True, 'confirmation': confirmation,
                        'account_id': root, 'all_accounts': all_accounts, 'accounts': count,
                        'local_only': True}
            pending = self.logout_confirmations.get(tg_id)
            if (not _secret(confirmation) or not pending or pending[:3] != (root, all_accounts, revision)
                    or pending[4] <= time.monotonic() or not hmac.compare_digest(pending[3], confirmation)):
                raise DomainError('退出确认已失效，请重新点击退出并确认。')
            result = self.accounts.logout(tg_id, all_accounts=all_accounts, expected_root=root)
            self.logout_confirmations.pop(tg_id, None)
            # 队列标成 revoked；运行中的逐账号发送仍由即时 can_run 拦截。
            for job in self.jobs.values():
                if job['tg_id'] == tg_id and job['status'] == 'queued':
                    job.update(status='revoked', rows=[], message='账号已本地退出，排队签到已取消。')
            self.pending.pop(tg_id, None)
            return dict(result, ok=True, needs_confirmation=False)

    def _cleanup_jobs(self):
        now = time.monotonic()
        expired = [key for key, job in self.jobs.items()
                   if job['status'] not in ('queued', 'running') and job['expires'] <= now]
        for key in expired:
            self.jobs.pop(key, None)
        for key, value in list(self.requests.items()):
            if value not in self.jobs:
                self.requests.pop(key, None)

    def start_sign(self, tg_id, body):
        """入队前固定当前账号和签到范围；默认仅当前号，重复请求不得改变范围。
        """
        self._owner(tg_id)
        _only(body, ('account_id', 'request_id', 'all_accounts'))
        all_accounts = body.get('all_accounts', False)
        if type(all_accounts) is not bool:
            raise DomainError('签到范围必须为布尔值。')
        root = _local_id(body.get('account_id'))
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise DomainError('签到请求编号无效，请刷新页面。')
        scope = self.accounts.run_scope(tg_id, all_accounts)
        if scope['root_id'] != root:
            raise DomainError('当前账号已切换，本次签到未开始。')
        with self.lock:
            if self.stopping.is_set() or not self.accepting:
                raise DomainError('服务正在停止或重启，请稍后再试。')
            self._cleanup_jobs()
            known = self.requests.get((tg_id, request_id))
            if known:
                if self.jobs[known]['root_id'] != root or self.jobs[known].get('all_accounts', False) != all_accounts:
                    raise DomainError('请求编号与账号或签到范围不匹配。')
                return {'ok': True, 'job_id': known}
            if tg_id in self.pending:
                previous = self.pending[tg_id]
                if self.jobs[previous]['root_id'] != root or self.jobs[previous].get('all_accounts', False) != all_accounts:
                    raise DomainError('另一个账号或范围正在签到，请等待完成。')
                return {'ok': True, 'job_id': previous}
            if len(self.pending) >= 32 or len(self.jobs) >= 512:
                raise DomainError('签到任务已满，请稍后重试。')
            job_id = secrets.token_urlsafe(18)
            self.jobs[job_id] = {'tg_id': tg_id, 'root_id': root, 'all_accounts': all_accounts, 'status': 'queued',
                                 'login_revision': (self.accounts.user(tg_id) or {}).get('login_revision', 0),
                                 'rows': [], 'message': '', 'expires': time.monotonic() + WEB_JOB_TTL}
            self.requests[tg_id, request_id] = job_id
            self.pending[tg_id] = job_id
            self.queue.put_nowait(job_id)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._worker, name='emos-web-sign', daemon=True)
                self.worker.start()
            return {'ok': True, 'job_id': job_id}

    def _worker(self):
        """按需执行队列，空队列时退出；运行前核对 owner、选择和登录代际。

        结果写到内存任务记录。异常转换成公开状态，不暴露原始异常或保证任务跨重启恢复。
        """
        while not self.stopping.is_set():
            try:
                job_id = self.queue.get_nowait()
            except queue.Empty:
                with self.lock:
                    if self.queue.empty():
                        self.worker = None
                        return
                continue
            with self.lock:
                job = self.jobs.get(job_id)
                if self.stopping.is_set() or job is None or job['status'] != 'queued':
                    if job is not None:
                        if job['status'] == 'queued':
                            job.update(status='failed', rows=[], message='服务已停止，请重新发起签到。')
                        if self.pending.get(job['tg_id']) == job_id:
                            self.pending.pop(job['tg_id'], None)
                    self.queue.task_done()
                    continue
                job['status'] = 'running'
                actor, root, all_accounts = job['tg_id'], job['root_id'], job.get('all_accounts', False)
            try:
                self._owner(actor)
                current = self.accounts.user(actor) or {}
                if (job.get('login_revision', current.get('login_revision', 0))
                        != current.get('login_revision', 0)):
                    raise DomainError('账号登录状态已变化，排队任务已撤销。')
                if not self.accounts.can_run(actor, root, root):
                    raise DomainError('账号已退出或当前选择已变化。')
                rows = self.run_user(actor, include_links=all_accounts, expected_root=root)
                status = 'busy' if rows is None else 'done'
                message = '另一个签到任务正在运行，请稍后重新签到。' if rows is None else ''
                if rows is not None and not isinstance(rows, list):
                    raise RuntimeError('invalid runtime result')
            except DomainError:
                rows, status, message = [], 'failed', '账号已切换、失效或授权已撤销，请刷新页面。'
            except Exception:
                rows, status, message = [], 'failed', '签到暂时失败，详细状态以日志为准。'
            with self.lock:
                job.update(status=status, rows=rows or [], message=message,
                           expires=time.monotonic() + WEB_JOB_TTL)
                if self.pending.get(actor) == job_id:
                    self.pending.pop(actor, None)
            self.queue.task_done()

    def job(self, tg_id, job_id):
        self._owner(tg_id)
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{24}', job_id):
            raise DomainError('任务不存在或已过期。')
        with self.lock:
            self._cleanup_jobs()
            job = self.jobs.get(job_id)
            if job is None or job['tg_id'] != tg_id:
                raise DomainError('任务不存在或已过期。')
            job = dict(job)
        active = self.accounts.active(tg_id, required=False)
        if not active or active['id'] != job['root_id']:
            return {'ok': True, 'status': 'stale', 'message': '当前账号已改变，请刷新日志。', 'rows': []}
        visible = {row['id'] for row in self.accounts.visible(tg_id)}
        own_ids = {row['id'] for row in self.accounts.owned(tg_id)}
        fields = ('id', 'emos_id', 'name', 'status', 'message', 'carrot', 'balance')
        rows = []
        for row in job['rows']:
            if (not isinstance(row, dict) or row.get('id') not in visible
                    or not job.get('all_accounts', False) and row.get('id') != job['root_id']):
                continue
            entry = {key: row.get(key) for key in fields}
            if row['id'] not in own_ids:
                entry['message'] = status_text(row.get('status'), own=False,
                                               fallback='代签状态已更新')
            rows.append(entry)
        return {'ok': True, 'status': job['status'], 'message': job['message'],
                'all_accounts': job.get('all_accounts', False), 'rows': rows}

    def begin_drain(self):
        """只停止接受新任务；已排队和在跑任务由调用方检查并等待。
        """
        with self.lock:
            self.accepting = False

    def resume(self):
        with self.lock:
            self.stopping.clear()
            self.accepting = True

    def stop(self, timeout=55):
        """撤销尚未运行的任务，设置停止标记并有限等待当前 worker。

        本服务不拥有 HTTP 监听；超时后在跑线程仍可能存活，由 app 决定能否 exec。
        """
        self.stopping.set()
        while True:
            try:
                key = self.queue.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                job = self.jobs.get(key)
                if job:
                    job.update(status='failed', message='服务已停止，请重新发起签到。', rows=[])
                    self.pending.pop(job['tg_id'], None)
            self.queue.task_done()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=max(0, timeout))

_WEB_CSS = '''
:root{color-scheme:dark;--bg:#101318;--panel:#191f28;--line:#303a49;--ink:#eef2f7;--muted:#9caabd;--accent:#76a7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,sans-serif}
main{max-width:1050px;margin:auto;padding:24px 18px 60px}header{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:20px}
h1{font-size:25px;margin:0}h2{font-size:17px;margin:0 0 10px}p{margin:8px 0}.muted{color:var(--muted);font-size:13px}
section{background:var(--panel);border:1px solid var(--line);padding:20px;border-radius:14px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:15px}.grid>section{margin:0}
input,select{background:#111722;border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:10px;width:100%;font:inherit}label{display:block;margin:13px 0 5px}
button,a.button{background:var(--accent);color:#111722;border:0;border-radius:8px;padding:9px 14px;font:600 14px system-ui,sans-serif;cursor:pointer;margin:5px 5px 5px 0;text-decoration:none;display:inline-block}
button.secondary{background:#2b3646;color:var(--ink)}button.danger{background:#51323a;color:#ffc1c7}button:disabled{opacity:.5;cursor:wait}
.check{display:flex;align-items:center;gap:8px}.check input{width:auto}.entry{padding:12px 0;border-top:1px solid var(--line)}.entry strong{display:block}.mono,pre{font:13px/1.6 ui-monospace,monospace;word-break:break-all;white-space:pre-wrap}
#flash{white-space:pre-wrap;position:sticky;top:0;background:#182b36;border-radius:9px;padding:10px;z-index:2}#flash:empty{display:none}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{flex:1}.codebox{background:#111722;padding:14px;border-radius:8px}#result{padding:10px 0;white-space:pre-wrap}.badge{font-size:12px;padding:2px 8px;background:#2b3646;border-radius:20px}
@media(max-width:600px){main{padding:17px 12px}header{align-items:flex-start}h1{font-size:21px}section{padding:16px}.row{display:block}.grid{display:block}.grid>section{margin:14px 0}}
'''

_DASHBOARD_HTML = '''
<header><div><h1>🥕 我的签到配置</h1><p class="muted">首个账号通过官方 Bot 授权，其他账号用 emos Token 添加。这里显示全部账号。</p></div><button id="logout" class="secondary">退出网页</button></header>
<div id="flash" role="status" aria-live="polite"></div>
<section><h2>当前账号</h2><label for="accounts">选择已登录账号</label><select id="accounts"></select><p id="identity" class="muted"></p><div class="row"><button id="sign">签到当前账号</button><button id="sign-all" class="secondary">签到全部账号</button><button id="refresh" class="secondary">刷新状态</button></div><div class="row"><button id="account-logout" class="danger">退出当前账号</button><button id="accounts-logout-all" class="danger">全部账号退出</button></div><p class="muted">账号退出需再次确认：只清除本地 Token 并关闭签到，保留账号、萝卜、日志与偏好；不代表官方 Token 失效。「退出网页」只关闭网页会话。</p><div id="result" role="status"></div></section>
<div class="grid"><section><h2>我的签到设置</h2><p class="muted">此处仅改变当前账号。</p><label class="check"><input id="auto" type="checkbox">自动签到</label><label for="time">签到时间（00:xx 自动调整到 01:xx）</label><input id="time" type="time" value="08:00"><p id="timezone" class="muted"></p><label for="sign-mode">签到模式</label><select id="sign-mode"><option value="normal">普通签到（不带附言）</option><option value="random">随机字签到（自动带附言）</option></select><p class="muted">奖励由官方决定。每个账号保留自己的选择，关联方不能修改。</p><button id="save-settings">保存签到设置</button></section>
<section><h2>我的通知</h2><label class="check"><input id="n-success" type="checkbox">签到成功通知</label><label class="check"><input id="n-fail" type="checkbox">签到失败通知</label><label class="check"><input id="n-already" type="checkbox">已签到通知</label><button id="save-preferences">保存通知设置</button><p class="muted">发送到你的 TG 私聊。</p></section></div>
<section><h2>关联账号 · 我代签（已停用）</h2><p class="muted">目标必须已在同一个 Bot 登录。官方账号 ID 用来定位，目标的一次性码用来申请；目标账号二次确认后授权。</p><form id="link-form"><div class="row"><div><label for="target">目标官方账号 ID</label><input id="target" autocomplete="off" placeholder="eXXXXXXXXs" maxlength="256" required></div><div><label for="link-code">目标的一次性关联码</label><input id="link-code" type="password" autocomplete="off" maxlength="256" required></div></div><button type="submit">添加关联</button></form><div id="outgoing"></div></section>
<section><h2>让另一个账号代签我</h2><p class="muted">为当前账号生成 10 分钟、只使用一次的关联码。只有你同意的人能获得此码；重新生成会替换旧码。</p><button id="issue-code">生成关联凭证</button><button id="hide-code" class="secondary">隐藏凭证</button><div id="codebox" class="codebox" hidden><pre id="issued"></pre><button id="copy-code" class="secondary">复制 ID 与关联码</button><p id="expires" class="muted"></p></div><div id="incoming"></div></section>
<section><h2>关联申请 · 目标确认</h2><p class="muted">ID 与码只创建申请。目标账号确认后才授予直接代签；来源可撤回。确认不会共享登录或反向授权。</p><div id="pending-incoming"></div><div id="pending-outgoing"></div></section>
<section><h2>其他账号 · 余额与状态</h2><p class="muted">仅显示签到相关状态；不展示密钥、TG 身份、个人设置或目标的其他关联。</p><div id="targets"></div></section>
<section><h2>全部账号签到记录</h2><div id="logs"></div><p class="muted">账号凭证及 TG ID 不回显；变更允许 ID 后原网页会话立即失效。</p></section>
'''

_AUTH_HTML = '''<header><h1>🔐 进入个人配置页</h1></header><section><p id="auth-status">正在校验 Bot 签发的一次性入口…</p><p class="muted">登录参数不会放进 URL 查询、服务器访问日志或浏览器本地存储。请勿把本入口转发给别人。</p><a class="button" href="/web/login">重新获取入口</a></section>'''
_LOGIN_HTML = '''<header><h1>🥕 个人签到配置</h1></header><section><h2>从自己的 Telegram Bot 进入</h2><p>先在 Bot 通过官方授权 Bot 登录，再点击「网页配置」或发送 <span class="mono">/web</span>。</p><p class="muted">Bot 会签发专属的一次性网页入口，两分钟有效。这里不接受 TG ID、管理员密码或直接粘贴 emos 密钥。</p></section>'''

_AUTH_JS = '''
(async()=>{const params=new URLSearchParams(location.hash.slice(1));const code=params.get('code');history.replaceState(null,'','/web/auth');const output=document.getElementById('auth-status');if(!code){output.textContent='入口缺少验证凭证。请从 Bot 的网页配置按钮重新进入。';return;}try{const response=await fetch('/api/web/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});const value=await response.json();if(!response.ok)throw Error(value.error||'入口已过期');location.replace('/web');}catch(error){output.textContent=error.message||'登录暂时失败，请重新获取入口。';}})();
'''

_DASHBOARD_JS = r'''
const $=id=>document.getElementById(id);
let state=null,csrf='',jobTimer=null,issued=null,requestKey=null;
const labels=__STATUS_JS__;
function flash(text){$('flash').textContent=text||'';}
function node(tag,text,className){const value=document.createElement(tag);if(text!==undefined)value.textContent=String(text);if(className)value.className=className;return value;}
function hideCode(){issued=null;$('issued').textContent='';$('expires').textContent='';$('codebox').hidden=true;}
function root(){if(!state||!state.active)throw Error('请先选择已登录的账号。');return state.active.id;}
async function api(path,data){const opts={credentials:'same-origin',method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'}};if(data!==undefined){opts.body=JSON.stringify(data);if(csrf)opts.headers['X-CSRF-Token']=csrf;}const response=await fetch('/api/web/'+path,opts);const result=await response.json();if(response.status===401){location.replace('/web/login');throw Error('网页会话已失效');}if(!response.ok)throw Error(result.error||'操作失败');return result;}
async function operation(action){if(document.body.dataset.busy==='1')return;document.body.dataset.busy='1';document.querySelectorAll('button').forEach(b=>b.disabled=true);try{flash('');await action();}catch(error){flash(error.message||'操作失败，请刷新重试。');}finally{document.body.dataset.busy='0';document.querySelectorAll('button').forEach(b=>b.disabled=false);}}
function renderLinks(list,container,incoming){const output=$(container);output.replaceChildren();if(!list.length){output.append(node('p',incoming?'暂无账号被授权代签当前账号。':'暂无外向关联。','muted'));return;}for(const link of list){const entry=node('div',undefined,'entry');entry.append(node('strong',link.source_name+' ('+link.source_emos_id+') → '+link.target_name+' ('+link.target_emos_id+')'));entry.append(node('p',link.enabled?'联动已开启':'联动已暂停','muted'));if(!incoming){const toggle=node('button',link.enabled?'暂停联动':'开启联动','secondary');toggle.onclick=()=>operation(async()=>{await api('toggle',{account_id:root(),source_id:link.source_id,target_id:link.target_id,enabled:!Boolean(link.enabled)});await load();flash('联动开关已更新。');});entry.append(toggle);}const unlink=node('button','解除关联','danger');unlink.onclick=()=>operation(async()=>{if(!confirm('解除后不能继续代签或查看目标日志。确定解除？'))return;await api('unlink',{account_id:root(),source_id:link.source_id,target_id:link.target_id});await load();flash('关联已解除。');});entry.append(unlink);output.append(entry);}}
function renderPending(){for(const direction of ['incoming','outgoing']){const box=$('pending-'+direction);box.replaceChildren();const rows=state.pending_links[direction];if(!rows.length){box.append(node('p',direction==='incoming'?'没有待确认的申请。':'没有待目标确认的申请。','muted'));continue;}for(const row of rows){const entry=node('div',undefined,'entry');entry.append(node('strong',row.source_name+' ('+row.source_emos_id+') → '+row.target_name+' ('+row.target_emos_id+')'));entry.append(node('p','等待确认 · 到期 '+new Date(row.expires_at*1000).toLocaleTimeString(),'muted'));if(direction==='incoming'){for(const [approve,text] of [[true,'确认直接代签'],[false,'拒绝申请']]){const button=node('button',text,approve?'':'danger');button.onclick=()=>operation(async()=>{if(approve&&!confirm('确认让此来源账号代签当前账号？只授予直接签到与签到状态查看权限。'))return;await api('review',{account_id:root(),source_id:row.source_id,target_id:row.target_id,approve});await load();flash(approve?'已确认，直接关联生效。':'申请已拒绝，未建立权限。');});entry.append(button);}}else{const button=node('button','撤回申请','secondary');button.onclick=()=>operation(async()=>{await api('withdraw',{account_id:root(),source_id:row.source_id,target_id:row.target_id});await load();flash('申请已撤回。');});entry.append(button);}box.append(entry);}}}
function renderTargets(){const output=$('targets');output.replaceChildren();if(!state.targets.length){output.append(node('p','暂无直接授权目标。','muted'));return;}for(const row of state.targets){const entry=node('div',undefined,'entry');entry.append(node('strong',row.name+' · '+row.emos_id));entry.append(node('p','可用余额：'+(Number.isInteger(row.carrot)?row.carrot:'未知')+(row.carrot_at?' · 更新 '+new Date(row.carrot_at*1000).toLocaleString():''),'muted'));entry.append(node('p',(labels[row.last_status]||'未运行')+' · 最近 '+(row.last_run||'—')+(row.last_carrot==null?'':' · 本次 +'+row.last_carrot),'muted'));output.append(entry);}}
async function loadLogs(){const data=await api('logs');const output=$('logs');output.replaceChildren();if(!data.rows.length){output.append(node('p','暂无可见签到记录。','muted'));return;}for(const row of data.rows){const entry=node('div',undefined,'entry');entry.append(node('strong',row.name+' · '+row.emos_id));entry.append(node('p',row.ts+' · '+(labels[row.status]||row.status)+(row.carrot==null?'':' · 萝卜 '+row.carrot),'muted'));entry.append(node('div',row.message));output.append(entry);}}
async function load(){const data=await api('me');if(data.single_owner){for(const id of ['link-form','issue-code','pending-incoming','pending-outgoing']){const element=$(id);if(element)element.closest('section').hidden=true;}}const changed=state&&state.active&&(!data.active||data.active.id!==state.active.id);state=data;csrf=data.csrf_token;if(changed){hideCode();requestKey=null;if(jobTimer)clearTimeout(jobTimer);$('result').textContent='已切换账号，旧任务结果不再展示。';}const selector=$('accounts');selector.replaceChildren();for(const row of data.owned){const option=node('option',row.name+' · '+row.emos_id);option.value=row.id;option.selected=Boolean(data.active&&row.id===data.active.id);selector.append(option);}if(!data.active){selector.prepend(node('option','请选择账号'));$('identity').textContent='没有已选择的账号。';}else{$('identity').textContent=data.active.name+' · 官方 ID '+data.active.emos_id+' · '+(data.active.enabled?'有效':'需重新登录')+' · 可用余额 '+(Number.isInteger(data.active.carrot)?data.active.carrot:'未知');$('auto').checked=Boolean(data.active.auto_sign);$('time').value=data.active.sign_time||data.default_sign_time||'08:00';$('sign-mode').value=data.active.sign_mode||'normal';}$('timezone').textContent='时区：'+data.timezone;$('n-success').checked=Boolean(data.preferences.notify_success);$('n-fail').checked=Boolean(data.preferences.notify_fail);$('n-already').checked=Boolean(data.preferences.notify_already);renderLinks(data.links.outgoing,'outgoing',false);renderLinks(data.links.incoming,'incoming',true);renderPending();renderTargets();await loadLogs();}
function newKey(){return Array.from(crypto.getRandomValues(new Uint8Array(24)),n=>n.toString(16).padStart(2,'0')).join('');}
async function pollJob(id,expected){try{if(!state||!state.active||state.active.id!==expected)return;const value=await api('job?job_id='+encodeURIComponent(id));if(value.status==='queued'||value.status==='running'){$('result').textContent=value.status==='queued'?'签到已排队…':value.all_accounts?'正在签到全部账号…':'正在签到当前账号…';jobTimer=setTimeout(()=>pollJob(id,expected),2500);return;}requestKey=null;$('result').textContent=value.message||value.rows.map(row=>(labels[row.status]||row.status)+' · '+row.name+' ('+row.emos_id+')'+(row.carrot==null?'':' · 萝卜 '+row.carrot)+'\n'+row.message).join('\n\n')||'没有可显示的结果。';await load();}catch(error){flash(error.message||'查询任务失败。可刷新日志查看结果。');}}
$('accounts').onchange=()=>operation(async()=>{const current=state&&state.active?state.active.id:0;const target=Number($('accounts').value);await api('switch',{account_id:current,target_id:target});hideCode();await load();});
$('save-settings').onclick=()=>operation(async()=>{await api('settings',{account_id:root(),auto_sign:$('auto').checked,sign_time:$('time').value,sign_mode:$('sign-mode').value});await load();flash('签到设置已保存。');});
$('save-preferences').onclick=()=>operation(async()=>{await api('preferences',{notify_success:$('n-success').checked,notify_fail:$('n-fail').checked,notify_already:$('n-already').checked});await load();flash('通知设置已保存。');});
$('link-form').onsubmit=event=>{event.preventDefault();operation(async()=>{try{await api('link',{account_id:root(),target_emos_id:$('target').value.trim(),code:$('link-code').value.trim()});$('target').value='';await load();flash('申请已提交，等待目标账号二次确认。确认前不能代签。');}finally{$('link-code').value='';}});};
$('issue-code').onclick=()=>operation(async()=>{issued=await api('code',{account_id:root()});$('issued').textContent='官方账号 ID：'+issued.target_emos_id+'\n一次性关联码：'+issued.code;$('codebox').hidden=false;});
$('hide-code').onclick=hideCode;
$('copy-code').onclick=()=>operation(async()=>{if(!issued)throw Error('请先生成有效凭证。');await navigator.clipboard.writeText(issued.target_emos_id+' '+issued.code);flash('已复制 ID 和关联码，请只交给你同意的代签方。');});
async function startSign(allAccounts){const expected=root();if(!requestKey||requestKey.root!==expected||requestKey.all!==allAccounts)requestKey={root:expected,all:allAccounts,key:newKey()};const result=await api('sign',{account_id:expected,request_id:requestKey.key,all_accounts:allAccounts});if(jobTimer)clearTimeout(jobTimer);await pollJob(result.job_id,expected);}
$('sign').onclick=()=>operation(()=>startSign(false));
$('sign-all').onclick=()=>operation(()=>startSign(true));
$('refresh').onclick=()=>operation(load);
async function logoutAccounts(allAccounts){const expected=root();const prepared=await api('account-logout',{account_id:expected,all_accounts:allAccounts});const label=allAccounts?'全部 '+prepared.accounts+' 个账号':'当前账号';if(!confirm('确认退出'+label+'？\n清除本地 Token、关闭签到并取消授权与输入。\n账号、萝卜、日志与偏好保留。不代表官方 Token 失效。'))return;const result=await api('account-logout',{account_id:expected,all_accounts:allAccounts,confirmation:prepared.confirmation});hideCode();requestKey=null;if(jobTimer)clearTimeout(jobTimer);$('result').textContent='已本地退出 '+result.accounts+' 个账号。主号 /login 官方重授权，其他账号 /token 重登。';flash('本地密钥已清除；没有向官方发起 Token 撤销。');location.replace('/web/login');}
$('account-logout').onclick=()=>operation(()=>logoutAccounts(false));
$('accounts-logout-all').onclick=()=>operation(()=>logoutAccounts(true));
$('logout').onclick=()=>operation(async()=>{hideCode();await api('logout',{});location.replace('/web/login');});
setInterval(()=>{if(!issued)return;const remaining=issued.expires_at-Math.floor(Date.now()/1000);if(remaining<=0){hideCode();return;}$('expires').textContent='剩余 '+remaining+' 秒，仅能使用一次。';},1000);
load().catch(error=>flash(error.message));
'''


def render_page(kind, nonce):
    if not isinstance(nonce, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', nonce):
        raise ValueError('invalid CSP nonce')
    if kind == 'auth':
        body, script, title = _AUTH_HTML, _AUTH_JS, '进入个人配置'
    elif kind == 'login':
        body, script, title = _LOGIN_HTML, '', '个人配置入口'
    elif kind == 'dashboard':
        body, title = _DASHBOARD_HTML, '我的签到配置'
        script = _DASHBOARD_JS.replace('__STATUS_JS__',
                                       json.dumps(_STATUS_TEXT, ensure_ascii=False,
                                                  sort_keys=True))
    else:
        raise ValueError('invalid page')
    page = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>emoskata · '+title+'</title><style nonce="'+nonce+'">'+_WEB_CSS+
            '</style></head><body><main>'+body+'</main>')
    if script:
        page += '<script nonce="'+nonce+'">'+script+'</script>'
    return page + '</body></html>'
