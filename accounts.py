"""账号域：官方主号、独立 Token、当前选择和 SQLite 事务。

本地 owner 是本 Bot 操作者，额外账号无需相同远端 TG 绑定。
旧共享数据保留但单人入口拒绝扩展授权；这里不执行官方网络请求。
Test: python3 -B -m unittest test_accounts test_owner_accounts test_multi_accounts"""

import hashlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing, contextmanager
from typing import Optional

from emos import clamp_sign_time as _clamp_time
from envconfig import configured_owner as _configured_owner


class DomainError(Exception):
    """面向个人页面和 Bot 的领域错误；拼接动态文本时仍由调用者脱敏。
    """


_ACCOUNT_FIELDS = (
    "id", "emos_id", "name", "enabled", "auto_sign", "sign_time", "sign_mode",
    "last_status", "last_msg", "last_carrot", "last_run", "last_day",
    "retry_count", "next_run_at", "created", "carrot", "carrot_at",
)
_USER_FIELDS = (
    "tg_id", "chat_id", "active_account_id", "primary_account_id", "login_until",
    "login_emos_id", "login_revision", "input_kind",
    "input_until", "notify_success", "notify_fail", "notify_already", "created",
)
_PREFERENCES = ("notify_success", "notify_fail", "notify_already")
# 当前根只用于选择竞态校验；全部签到另查同 owner 的有效账号。
_ROOT = """
    SELECT a.id FROM tg_users u JOIN accounts a ON a.id=u.active_account_id
    WHERE u.tg_id=? AND a.owner_tg_id=?
"""
# 旧共享视图只查一跳；单人模式不用这个范围扩展规则。
_VISIBLE_IDS = """
    SELECT id FROM root
    UNION
    SELECT l.target_id FROM account_links l JOIN root r ON l.source_id=r.id
    JOIN accounts t ON t.id=l.target_id WHERE t.owner_tg_id IS NOT NULL
"""


def _positive_id(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise DomainError("用户或账号 ID 无效")
    return value


def _official_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DomainError("缺少有效的官方账号 ID")
    value = str(value).strip()
    if not value or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DomainError("缺少有效的官方账号 ID")
    return value


def _flag(value):
    if type(value) is bool or (type(value) is int and value in (0, 1)):
        return int(value)
    if type(value) is str and value in ("0", "1"):
        return int(value)
    raise DomainError("开关值必须为 0 或 1")


def _stored_flag(value):
    return int(value in (1, "1", True))


def _safe_text(value, tokens=()):
    text = "" if value is None else str(value)
    for token in tokens:
        if token:
            text = text.replace(str(token), "[凭证已隐藏]")
    return text


def _account(row):
    result = {key: row[key] for key in _ACCOUNT_FIELDS}
    for key in ("emos_id", "name", "sign_time", "last_status", "last_msg",
                "last_run", "last_day", "created"):
        result[key] = _safe_text(result[key], (row["token"],))
    result["enabled"] = _stored_flag(result["enabled"])
    result["auto_sign"] = _stored_flag(result["auto_sign"])
    result["logged_in"] = bool(str(row["token"] or "").strip()
                               and result["enabled"] and row["last_status"] != "invalid")
    return result


def _user(row):
    result = {key: row[key] for key in _USER_FIELDS}
    for key in _PREFERENCES:
        result[key] = _stored_flag(result[key])
    return result


class AccountService:
    """在调用方提供的连接和写事务内核验账号归属；输出账号投影不包含密钥。"""
    def __init__(self, db, write, cfg):
        if not all(callable(fn) for fn in (db, write, cfg)):
            raise TypeError("db、write、cfg 必须可调用")
        self._db, self._write, self._cfg = db, write, cfg
        self._redeem_lock = threading.Lock()
        self._redeem_failures = {}
        self._owner_mode_seen = False
        self._owner_lock = threading.Lock()

    def _owner_id(self):
        """返回唯一操作者；0 表示拒绝，None 仅表示尚未见过 owner 配置的历史模式。

        一旦进入单人模式，之后配置键缺失也拒绝，不回退为开放访问。"""
        try:
            cfg = self._cfg()
        except Exception:
            return 0
        with self._owner_lock:
            if not isinstance(cfg, Mapping):
                self._owner_mode_seen = True
                return 0
            if 'tg_allowed_user_ids' not in cfg:
                return 0 if self._owner_mode_seen else None
            self._owner_mode_seen = True
        return _configured_owner(dict(cfg)) or 0

    def _actor(self, tg_id):
        owner = self._owner_id()
        if owner is not None and owner != tg_id:
            raise DomainError('仅配置的 Telegram 用户可操作；请检查唯一 TG ID')
        return owner

    @contextmanager
    def _access(self, tg_id, write=False, links=False):
        """数据库访问前、借用后及返回前核对 owner，配置变化使写事务回滚。

        links=True 在单人模式拒绝；不能因等待连接期间权限改变而继续提交。"""
        owner = self._actor(tg_id)
        if links and owner is not None:
            raise DomainError('单人模式不支持账号共享或关联；请使用官方 token 添加账号')
        context = self._write() if write else closing(self._db())
        with context as conn:
            # 验证可能等待的数据库访问前后，写入后的变化在提交前触发回滚。
            if self._actor(tg_id) != owner:
                raise DomainError('账号访问配置已变化，请重试')
            yield conn, owner
            if self._actor(tg_id) != owner:
                raise DomainError('账号访问配置已变化，请重试')

    @staticmethod
    def _primary(conn, tg_id):
        return conn.execute(
            'SELECT a.* FROM tg_users u JOIN accounts a ON a.id=u.primary_account_id '
            "WHERE u.tg_id=? AND a.owner_tg_id=? AND a.emos_id IS NOT NULL AND a.emos_id<>''",
            (tg_id, tg_id)).fetchone()

    def primary_account(self, tg_id) -> Optional[dict]:
        """返回官方主号的脱敏登记；logged_in 另行表示本地有效登录。

        退出保留登记，因此返回非 None 不等于仍有可用凭据。"""
        tg_id = _positive_id(tg_id)
        try:
            with self._access(tg_id) as (conn, _):
                row = self._primary(conn, tg_id)
                return _account(row) if row is not None else None
        except DomainError:
            return None

    def has_primary_account(self, tg_id) -> bool:
        """检查主号登记和本地凭据仍有效；不替代官方请求时的远端身份核验。"""
        primary = self.primary_account(tg_id)
        return bool(primary and primary['logged_in'])

    def _official_primary(self, conn, tg_id, official):
        primary = self._primary(conn, tg_id)
        if primary is not None and primary['emos_id'] != official:
            raise DomainError('已有官方主账号；其他账号请使用官方 token 添加')
        return primary

    def _require_primary(self, conn, tg_id):
        """要求已登记主号仍有有效本地凭据；保留的证明本身不代表登录。
        """
        primary = self._primary(conn, tg_id)
        if primary is None or not _account(primary)['logged_in']:
            raise DomainError('请先通过官方 emospg_bot 重新授权主账号；主号需有效本地凭证')
        return primary

    @staticmethod
    def init_schema(conn):
        # executescript 会改变调用者事务边界，迁移语句逐条执行。
        """在调用方事务内增量迁移结构，不用会隐式提交的 executescript。

        重复官方 ID 保留最早身份，其余清空身份待重登；Token 和历史记录不删除。"""
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DomainError("数据库连接必须在事务开始前启用外键")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, token TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_status TEXT DEFAULT '', last_msg TEXT DEFAULT '',
                last_carrot INTEGER, last_run TEXT DEFAULT '', last_day TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0, next_run_at INTEGER DEFAULT 0,
                created TEXT DEFAULT ''
            )
        """)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        if not {"id", "name", "token"}.issubset(columns):
            raise DomainError("旧账号表结构不兼容，未修改账号数据")
        additions = (
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("last_status", "TEXT DEFAULT ''"), ("last_msg", "TEXT DEFAULT ''"),
            ("last_carrot", "INTEGER"), ("last_run", "TEXT DEFAULT ''"),
            ("last_day", "TEXT DEFAULT ''"), ("retry_count", "INTEGER DEFAULT 0"),
            ("next_run_at", "INTEGER DEFAULT 0"), ("created", "TEXT DEFAULT ''"),
            ("emos_id", "TEXT"), ("owner_tg_id", "INTEGER"),
            ("base_url", "TEXT DEFAULT ''"), ("auto_sign", "INTEGER DEFAULT 1"),
            ("sign_time", "TEXT DEFAULT ''"), ("sign_mode", "TEXT NOT NULL DEFAULT 'normal'"),
            ("carrot", "INTEGER"), ("carrot_at", "INTEGER DEFAULT 0"),
        )
        for column, ddl in additions:
            if column not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN %s %s" % (column, ddl))
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_emos_id
                ON accounts(emos_id) WHERE emos_id IS NOT NULL AND emos_id<>''
            """)
        except sqlite3.IntegrityError:
            # 重复官方 ID 保留最早一行，其余清空身份但保留 token 与历史，待重登修复。
            duplicates = [row[0] for row in conn.execute(
                "SELECT emos_id FROM accounts WHERE emos_id IS NOT NULL AND emos_id<>'' "
                "GROUP BY emos_id HAVING COUNT(*)>1")]
            for emos_id in duplicates:
                keep = conn.execute("SELECT MIN(id) FROM accounts WHERE emos_id=?",
                                    (emos_id,)).fetchone()[0]
                conn.execute("UPDATE accounts SET emos_id=NULL, owner_tg_id=NULL "
                             "WHERE emos_id=? AND id<>?", (emos_id, keep))
                print("[db] 官方账号 %s 有重复记录，已保留最早一行，其余需重新登录。"
                      % emos_id[:12])
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_emos_id
                ON accounts(emos_id) WHERE emos_id IS NOT NULL AND emos_id<>''
            """)
        index = next(r for r in conn.execute("PRAGMA index_list(accounts)")
                     if r[1] == "ux_accounts_emos_id")
        indexed = [r[2] for r in conn.execute("PRAGMA index_info(ux_accounts_emos_id)")]
        definition = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                                  ("ux_accounts_emos_id",)).fetchone()[0] or ""
        definition = "".join(definition.lower().split())
        if (not index[2] or not index[4] or indexed != ["emos_id"]
                or not definition.endswith("whereemos_idisnotnullandemos_id<>''")):
            raise DomainError("官方账号 ID 索引不兼容，迁移已中止")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_tg_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tg_users (
                tg_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                active_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                primary_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                login_until INTEGER NOT NULL DEFAULT 0,
                input_kind TEXT NOT NULL DEFAULT '', input_until INTEGER NOT NULL DEFAULT 0,
                notify_success INTEGER NOT NULL DEFAULT 1,
                notify_fail INTEGER NOT NULL DEFAULT 1,
                notify_already INTEGER NOT NULL DEFAULT 0, created TEXT DEFAULT ''
            )
        """)
        user_columns = {row[1] for row in conn.execute('PRAGMA table_info(tg_users)')}
        for name, ddl in (('primary_account_id', 'INTEGER REFERENCES accounts(id) ON DELETE SET NULL'),
                          ('login_emos_id', "TEXT NOT NULL DEFAULT ''"),
                          ('login_revision', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in user_columns:
                conn.execute('ALTER TABLE tg_users ADD COLUMN %s %s' % (name, ddl))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_links (
                source_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                created TEXT DEFAULT '', PRIMARY KEY(source_id,target_id),
                CHECK(source_id<>target_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_target ON account_links(target_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_codes (
                code_hash TEXT PRIMARY KEY,
                target_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                issued_by INTEGER NOT NULL, expires_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codes_target ON link_codes(target_id)")
        conn.execute('CREATE TABLE IF NOT EXISTS link_requests ('
                     'source_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,'
                     'target_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,'
                     'source_owner INTEGER NOT NULL,target_owner INTEGER NOT NULL,'
                     'expires_at INTEGER NOT NULL,created TEXT NOT NULL,'
                     'PRIMARY KEY(source_id,target_id),CHECK(source_id<>target_id))')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_link_requests_target ON link_requests(target_id)')
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                name TEXT DEFAULT '', ts TEXT NOT NULL, status TEXT NOT NULL,
                message TEXT DEFAULT '', carrot INTEGER
            )
        """)
        conn.execute('DROP INDEX IF EXISTS idx_logs_id')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id,id DESC)')

    @staticmethod
    def _now():
        return int(time.time())

    @staticmethod
    def _created(now):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))

    def _ensure(self, conn, tg_id):
        """保证 tg_users 行存在；后续 UPDATE 才有作用对象，否则静默改 0 行。
        """
        conn.execute(
            "INSERT INTO tg_users(tg_id,chat_id,created) VALUES(?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET chat_id=excluded.chat_id",
            (tg_id, tg_id, self._created(self._now())))
        return conn.execute("SELECT * FROM tg_users WHERE tg_id=?", (tg_id,)).fetchone()

    @staticmethod
    def _active(conn, tg_id, required=True):
        row = conn.execute(
            "SELECT a.* FROM tg_users u JOIN accounts a ON a.id=u.active_account_id "
            "WHERE u.tg_id=? AND a.owner_tg_id=?", (tg_id, tg_id)).fetchone()
        if row is None and required:
            raise DomainError("请先登录并选择自己的账号")
        return row

    def ensure_user(self, tg_id, chat_id=None) -> dict:
        tg_id = _positive_id(tg_id)
        if chat_id is not None and _positive_id(chat_id) != tg_id:
            raise DomainError("仅支持 Telegram 私聊")
        with self._access(tg_id, write=True) as (conn, owner):
            return _user(self._ensure(conn, tg_id))

    def user(self, tg_id) -> Optional[dict]:
        tg_id = _positive_id(tg_id)
        with self._access(tg_id) as (conn, _):
            row = conn.execute("SELECT * FROM tg_users WHERE tg_id=?", (tg_id,)).fetchone()
            return _user(row) if row is not None else None

    def begin_login(self, tg_id, official_id, expected_root=None) -> dict:
        """在事务内记录目标官方 ID、期限和递增代际，旧回传不沿用新请求。"""
        tg_id, official_id = _positive_id(tg_id), _official_id(official_id)
        with self._access(tg_id, write=True) as (conn, owner):
            if owner is not None:
                self._official_primary(conn, tg_id, official_id)
            self._ensure(conn, tg_id)
            self._expected(conn, tg_id, expected_root)
            conn.execute('UPDATE tg_users SET login_until=?,login_emos_id=?,'
                         'login_revision=login_revision+1 WHERE tg_id=?',
                         (self._now() + 600, official_id, tg_id))
            return _user(conn.execute('SELECT * FROM tg_users WHERE tg_id=?', (tg_id,)).fetchone())

    def login_pending(self, tg_id) -> bool:
        """是否仍有未过期的待授权请求；回传 token 时据此判断请求真实性。
        """
        row = self.user(tg_id)
        return bool(row and row["login_until"] > self._now())

    def cancel_login(self, tg_id) -> None:
        """作废待授权并递增代际，阻止迟到校验落库；输入状态由 clear_input() 清理。"""
        tg_id = _positive_id(tg_id)
        with self._access(tg_id, write=True) as (conn, owner):
            conn.execute("UPDATE tg_users SET login_until=0,login_emos_id='',"
                         "login_revision=login_revision+1 WHERE tg_id=?", (tg_id,))

    def finish_login(self, tg_id, token, profile, base_url='', expected_root=None,
                     expected_login_until=None, expected_login_revision=None) -> dict:
        """消费官方回传并登记主号，必须匹配请求 ID、TG 绑定、期限和代际。

        调用方先向官方核验 Token；本函数在写事务中再次检查当前选择和账号归属。"""
        tg_id = _positive_id(tg_id)
        if not isinstance(profile, Mapping):
            raise DomainError("登录资料无效，请重新授权")
        official = profile.get("user_id")
        if official is None or (isinstance(official, str) and not official.strip()):
            official = profile.get("_user_id")
        official = _official_id(official)
        telegram_id = profile.get("telegram_user_id")
        if telegram_id in (None, ""):
            raise DomainError("官方账号尚未绑定 Telegram。请在 emos 官网绑定 TG，再重新 /login；账号未保存")
        if isinstance(telegram_id, str) and telegram_id.isascii() and telegram_id.isdigit():
            telegram_id = int(telegram_id) if len(telegram_id) <= 19 else 0
        if type(telegram_id) is not int or telegram_id != tg_id:
            raise DomainError("官方授权与当前 Telegram 用户不匹配")
        if not isinstance(token, str) or not token.strip() or len(token) > 8192:
            raise DomainError("登录凭证无效，请重新授权")
        token = token.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise DomainError("登录凭证无效，请重新授权")
        if not isinstance(base_url, str) or len(base_url) > 2048:
            raise DomainError("账号站点设置无效")
        name = str(profile.get("_display") or profile.get("username")
                   or profile.get("name") or official).strip()[:80] or official
        with self._access(tg_id, write=True) as (conn, owner):
            now = self._now()
            pending = conn.execute('SELECT login_until,login_emos_id,login_revision FROM tg_users WHERE tg_id=?',
                                   (tg_id,)).fetchone()
            if pending is None or pending['login_until'] <= now:
                raise DomainError('登录请求已过期或未发起，请重新登录')
            self._expected(conn, tg_id, expected_root)
            if (expected_login_until is not None and pending['login_until'] != expected_login_until
                    or expected_login_revision is not None and pending['login_revision'] != expected_login_revision):
                raise DomainError('登录授权请求已变化，请重新发起')
            if pending['login_emos_id'] != official:
                raise DomainError('官方回传账号 ID 与本次填写的 ID 不一致，账号未保存')
            primary = self._official_primary(conn, tg_id, official) if owner is not None else None
            row = conn.execute("SELECT * FROM accounts WHERE emos_id=?", (official,)).fetchone()
            matches = conn.execute("SELECT * FROM accounts WHERE token=? ORDER BY id",
                                   (token,)).fetchall()
            if row is not None:
                if row["owner_tg_id"] not in (None, tg_id):
                    raise DomainError("该官方账号已归属其他用户，不能接管")
                if row["owner_tg_id"] is None and row["token"] != token:
                    raise DomainError("旧账号只能凭已验证的原凭证认领，不能按 ID 接管")
            for match in matches:
                if match["owner_tg_id"] not in (None, tg_id):
                    raise DomainError("该登录凭证已归属其他用户，不能接管")
                if match["emos_id"] not in (None, "", official):
                    raise DomainError("凭证与已登记的官方账号 ID 不一致")
            if row is None and matches:
                row = matches[0]
            if primary is not None and (row is None or row['id'] != primary['id']):
                raise DomainError('官方主账号身份已变化，请重新授权')
            balance = profile.get('_carrot')
            balance_at = now if balance is not None else 0
            if row is None:
                cur = conn.execute(
                    "INSERT INTO accounts(name,token,emos_id,owner_tg_id,base_url,carrot,carrot_at,created) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (name, token, official, tg_id, base_url.strip(), balance, balance_at, self._created(now)))
                account_id = cur.lastrowid
            else:
                account_id = row["id"]
                conn.execute(
                    "UPDATE accounts SET name=?,token=?,emos_id=?,owner_tg_id=?,base_url=?, "
                    "enabled=1,last_status='',last_msg='',retry_count=0,next_run_at=0,"
                    "carrot=COALESCE(?,carrot),carrot_at=CASE WHEN ? IS NULL THEN carrot_at ELSE ? END "
                    "WHERE id=? AND (owner_tg_id IS NULL OR owner_tg_id=?)",
                    (name, token, official, tg_id, base_url.strip(), balance, balance, now, account_id, tg_id))
            columns = {item[1] for item in conn.execute("PRAGMA table_info(accounts)")}
            if "retry_day" in columns:
                conn.execute("UPDATE accounts SET retry_day='' WHERE id=? AND owner_tg_id=?",
                             (account_id, tg_id))
            if owner is not None:
                conn.execute('UPDATE tg_users SET primary_account_id=? WHERE tg_id=?',
                             (primary['id'] if primary is not None else account_id, tg_id))
            conn.execute("UPDATE tg_users SET active_account_id=?,login_until=0,login_emos_id='',"
                         "login_revision=login_revision+1 WHERE tg_id=?", (account_id, tg_id))
            return _account(conn.execute("SELECT * FROM accounts WHERE id=?",
                                         (account_id,)).fetchone())

    def login_with_token(self, tg_id, token, profile, base_url='', expected_root=None,
                         expected_login_revision=None) -> dict:
        """保存经调用方官方核验的独立 Token；单人模式先要求有效官方主号。

        本地操作者与额外号远端 TG 绑定分离，不建立或替换 primary；
        网络校验后的当前选择和登录代际必须在写事务中仍然一致。"""
        tg_id = _positive_id(tg_id)
        if not isinstance(profile, Mapping):
            raise DomainError("登录资料无效，请重新授权")
        official = profile.get("user_id")
        if official is None or (isinstance(official, str) and not official.strip()):
            official = profile.get("_user_id")
        official = _official_id(official)
        if not isinstance(token, str) or not token.strip() or len(token) > 8192:
            raise DomainError("登录凭证无效，请重新获取")
        token = token.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise DomainError("登录凭证无效，请重新获取")
        if not isinstance(base_url, str) or len(base_url) > 2048:
            raise DomainError("账号站点设置无效")
        name = str(profile.get("_display") or profile.get("username")
                   or profile.get("name") or official).strip()[:80] or official
        balance = profile.get("_carrot")
        with self._access(tg_id, write=True) as (conn, owner):
            self._expected(conn, tg_id, expected_root)
            self._login_revision(conn, tg_id, expected_login_revision)
            if owner is not None:
                self._require_primary(conn, tg_id)
            now = self._now()
            row = conn.execute("SELECT * FROM accounts WHERE emos_id=?", (official,)).fetchone()
            matches = conn.execute("SELECT * FROM accounts WHERE token=? ORDER BY id",
                                   (token,)).fetchall()
            if row is not None and row["owner_tg_id"] not in (None, tg_id):
                raise DomainError("该官方账号已归属其他用户，不能加入")
            for match in matches:
                if match["owner_tg_id"] not in (None, tg_id):
                    raise DomainError("该密钥已归属其他用户，不能加入")
                if match["emos_id"] not in (None, "", official):
                    raise DomainError("密钥与已登记的官方账号 ID 不一致")
            if row is None and matches:
                row = matches[0]
            if row is None:
                cur = conn.execute(
                    "INSERT INTO accounts(name,token,emos_id,owner_tg_id,base_url,carrot,carrot_at,created) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (name, token, official, tg_id, base_url.strip(), balance,
                     now if balance is not None else 0, self._created(now)))
                account_id = cur.lastrowid
            else:
                account_id = row["id"]
                conn.execute(
                    "UPDATE accounts SET name=?,token=?,emos_id=?,owner_tg_id=?,base_url=?,"
                    "enabled=1,last_status='',last_msg='',retry_count=0,next_run_at=0,"
                    "carrot=COALESCE(?,carrot),carrot_at=CASE WHEN ? IS NULL THEN carrot_at ELSE ? END "
                    "WHERE id=? AND (owner_tg_id IS NULL OR owner_tg_id=?)",
                    (name, token, official, tg_id, base_url.strip(), balance,
                     balance, now, account_id, tg_id))
            columns = {item[1] for item in conn.execute("PRAGMA table_info(accounts)")}
            if "retry_day" in columns:
                conn.execute("UPDATE accounts SET retry_day='' WHERE id=? AND owner_tg_id=?",
                             (account_id, tg_id))
            # 兼容旧数据没有用户行的情况；UPDATE 不能默默作用于零行。
            self._ensure(conn, tg_id)
            conn.execute("UPDATE tg_users SET active_account_id=?,login_until=0,login_emos_id='',"
                         "login_revision=login_revision+1 WHERE tg_id=?", (account_id, tg_id))
            return _account(conn.execute("SELECT * FROM accounts WHERE id=?",
                                         (account_id,)).fetchone())

    @staticmethod
    def _login_revision(conn, tg_id, expected):
        if expected is None:
            return
        row = conn.execute('SELECT login_revision FROM tg_users WHERE tg_id=?', (tg_id,)).fetchone()
        if type(expected) is not int or expected < 0 or row is None or row['login_revision'] != expected:
            raise DomainError('登录或输入会话已变化，请重新操作')

    def logout(self, tg_id, account_id=None, all_accounts=False, expected_root=None) -> dict:
        """清除指定范围的本地 Token 并关闭签到，不删除账号、余额、日志和偏好。

        调用方负责二次确认；保留主号登记，撤销当前操作者网页会话并递增登录代际。
        这里不调用官方 Token 撤销接口。"""
        tg_id = _positive_id(tg_id)
        if type(all_accounts) is not bool or (all_accounts and account_id is not None):
            raise DomainError('退出范围无效')
        if account_id is not None:
            account_id = _positive_id(account_id)
        with self._access(tg_id, write=True) as (conn, _):
            self._expected(conn, tg_id, expected_root)
            active = self._active(conn, tg_id, required=False)
            if all_accounts:
                rows = conn.execute('SELECT id,token FROM accounts WHERE owner_tg_id=? ORDER BY id',
                                    (tg_id,)).fetchall()
            else:
                target = account_id if account_id is not None else (active['id'] if active else None)
                rows = conn.execute('SELECT id,token FROM accounts WHERE id=? AND owner_tg_id=?',
                                    (target, tg_id)).fetchall()
            if not rows:
                raise DomainError('没有可退出的账号，或账号不属于当前用户')
            ids = [row['id'] for row in rows]
            cleared = sum(bool(str(row['token'] or '').strip()) for row in rows)
            conn.executemany("UPDATE accounts SET token='',enabled=0,auto_sign=0,"
                             "retry_count=0,next_run_at=0 WHERE id=? AND owner_tg_id=?",
                             ((value, tg_id) for value in ids))
            conn.execute("UPDATE tg_users SET login_until=0,login_emos_id='',"
                         "input_kind='',input_until=0,login_revision=login_revision+1 WHERE tg_id=?",
                         (tg_id,))
            # 网页授权同样是本地身份会话；只撤销当前用户的入口与会话。
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('web_auth_codes','web_sessions')")}
            for table in tables:
                conn.execute('DELETE FROM ' + table + ' WHERE tg_id=?', (tg_id,))
            return {'account_ids': ids, 'accounts': len(ids), 'tokens_cleared': cleared,
                    'all_accounts': all_accounts, 'local_only': True}

    def owned(self, tg_id) -> list:
        tg_id = _positive_id(tg_id)
        with self._access(tg_id) as (conn, _):
            rows = conn.execute(
                "SELECT * FROM accounts WHERE owner_tg_id=? "
                "AND emos_id IS NOT NULL AND emos_id<>'' ORDER BY id", (tg_id,)).fetchall()
            return [_account(row) for row in rows]

    def active(self, tg_id, required=True) -> Optional[dict]:
        tg_id = _positive_id(tg_id)
        with self._access(tg_id) as (conn, _):
            row = self._active(conn, tg_id, required)
            return _account(row) if row is not None else None

    @staticmethod
    def _expected(conn, tg_id, expected_root):
        """在现有事务内检查当前选择；过期菜单不能修改切换后的账号。"""
        if expected_root is None:
            return
        if type(expected_root) is not int or expected_root < 0:
            raise DomainError('当前账号参数无效')
        row = AccountService._active(conn, tg_id, required=False)
        if (row['id'] if row is not None else 0) != expected_root:
            raise DomainError('当前账号已切换，请刷新页面后重试')

    def switch(self, tg_id, local_account_id, expected_root=None) -> dict:
        """只切换属于当前操作者的已登记账号，不扩展授权或共享范围。"""
        tg_id, local_account_id = _positive_id(tg_id), _positive_id(local_account_id)
        with self._access(tg_id, write=True) as (conn, owner):
            self._expected(conn, tg_id, expected_root)
            row = conn.execute("SELECT * FROM accounts WHERE id=? AND owner_tg_id=? "
                               "AND emos_id IS NOT NULL AND emos_id<>''",
                               (local_account_id, tg_id)).fetchone()
            if row is None:
                raise DomainError("不能选择不属于自己的账号")
            self._ensure(conn, tg_id)
            conn.execute("UPDATE tg_users SET active_account_id=? WHERE tg_id=?",
                         (local_account_id, tg_id))
            return _account(row)

    def set_account(self, tg_id, expected_root=None, **fields) -> dict:
        """更新当前账号的白名单设置字段，在同一写事务内核对选择与归属。"""
        tg_id = _positive_id(tg_id)
        if set(fields) - {"auto_sign", "sign_time", "sign_mode"}:
            raise DomainError("只允许设置自动签到、签到时间和签到模式")
        if 'sign_mode' in fields and fields['sign_mode'] not in ('normal', 'random'):
            raise DomainError('签到模式必须为普通或随机字')
        values = {key: _flag(value) if key == "auto_sign" else
                  _clamp_time(value) if key == 'sign_time' else value
                  for key, value in fields.items()}
        with self._access(tg_id, write=True) as (conn, owner):
            self._expected(conn, tg_id, expected_root)
            row = self._active(conn, tg_id)
            if values:
                conn.execute("UPDATE accounts SET " + ",".join(key + "=?" for key in values)
                             + " WHERE id=? AND owner_tg_id=?",
                             (*values.values(), row["id"], tg_id))
            return _account(conn.execute("SELECT * FROM accounts WHERE id=?",
                                         (row["id"],)).fetchone())

    def set_preferences(self, tg_id, **fields) -> dict:
        """只更新用户级通知选项，布尔值先校验，不影响账号 Token。"""
        tg_id = _positive_id(tg_id)
        if set(fields) - set(_PREFERENCES):
            raise DomainError("通知设置字段无效")
        values = {key: _flag(value) for key, value in fields.items()}
        with self._access(tg_id, write=True) as (conn, owner):
            self._ensure(conn, tg_id)
            if values:
                conn.execute("UPDATE tg_users SET " + ",".join(key + "=?" for key in values)
                             + " WHERE tg_id=?", (*values.values(), tg_id))
            return _user(conn.execute("SELECT * FROM tg_users WHERE tg_id=?",
                                      (tg_id,)).fetchone())

    def issue_code(self, tg_id, expected_root=None) -> dict:
        """为历史关联授权生成一次性码，库内只存摘要；单人模式禁止调用。"""
        tg_id = _positive_id(tg_id)
        with self._access(tg_id, write=True, links=True) as (conn, _):
            self._expected(conn, tg_id, expected_root)
            row = self._active(conn, tg_id)
            if not row["emos_id"] or row["enabled"] != 1 or row["last_status"] == "invalid":
                raise DomainError("当前账号未验证或已失效，请重新登录")
            now = self._now()
            code = secrets.token_urlsafe(18)
            digest = hashlib.sha256(code.encode("ascii")).hexdigest()
            conn.execute("DELETE FROM link_codes WHERE target_id=?", (row["id"],))
            conn.execute("INSERT INTO link_codes(code_hash,target_id,issued_by,expires_at) "
                         "VALUES(?,?,?,?)", (digest, row["id"], tg_id, now + 600))
            return {"target_emos_id": _safe_text(row["emos_id"], (row["token"],)),
                    "code": code, "expires_at": now + 600}

    def _redemption_limit(self, tg_id):
        now = self._now()
        with self._redeem_lock:
            started, count = self._redeem_failures.get(tg_id, (now, 0))
            if now < started + 60 and count >= 10:
                raise DomainError("关联尝试过多，请稍后重试")

    def _redemption_result(self, tg_id, success):
        now = self._now()
        with self._redeem_lock:
            if success:
                self._redeem_failures.pop(tg_id, None)
                return
            if len(self._redeem_failures) >= 4096:
                self._redeem_failures = {key: value for key, value in self._redeem_failures.items()
                                         if value[0] + 60 > now}
                if len(self._redeem_failures) >= 4096:
                    self._redeem_failures.pop(next(iter(self._redeem_failures)))
            started, count = self._redeem_failures.get(tg_id, (now, 0))
            self._redeem_failures[tg_id] = ((started, count + 1) if started + 60 > now
                                           else (now, 1))

    def redeem(self, tg_id, target_emos_id, code, expected_root=None) -> dict:
        """在事务中消费一次性码并创建待目标确认申请，尚不建立代签授权。

        历史路径失败受次数限制；单人模式禁止，最终授权由 review_link() 决定。"""
        tg_id = _positive_id(tg_id)
        self._redemption_limit(tg_id)
        try:
            official = _official_id(target_emos_id)
            if not isinstance(code, str) or not code.strip() or len(code) > 256:
                raise DomainError("关联 ID 或一次性码无效、过期或已使用")
            digest = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
            with self._access(tg_id, write=True, links=True) as (conn, _):
                self._expected(conn, tg_id, expected_root)
                source = self._active(conn, tg_id)
                if source["enabled"] != 1 or source["last_status"] == "invalid" or not source["emos_id"]:
                    raise DomainError("当前账号未验证或已失效，请重新登录")
                target = conn.execute("SELECT * FROM accounts WHERE emos_id=? "
                                      "AND owner_tg_id IS NOT NULL AND enabled=1 "
                                      "AND COALESCE(last_status,'')<>'invalid'", (official,)).fetchone()
                if target is None:
                    raise DomainError("关联 ID 或一次性码无效、过期或已使用")
                if target["id"] == source["id"]:
                    raise DomainError("不能关联当前账号自身")
                consumed = conn.execute(
                    "DELETE FROM link_codes WHERE code_hash=? AND target_id=? "
                    "AND issued_by=? AND expires_at>?",
                    (digest, target["id"], target["owner_tg_id"], self._now()))
                if consumed.rowcount != 1:
                    raise DomainError("关联 ID 或一次性码无效、过期或已使用")
                now = self._now()
                conn.execute('DELETE FROM link_requests WHERE expires_at<=?', (now,))
                if conn.execute('SELECT 1 FROM account_links WHERE source_id=? AND target_id=?',
                                (source['id'], target['id'])).fetchone():
                    raise DomainError('该账号已经关联，请使用现有关联开关')
                pending = conn.execute('SELECT COUNT(*) FROM link_requests WHERE source_id=? OR target_id=?',
                                       (source['id'],target['id'])).fetchone()[0]
                if pending >= 20:
                    raise DomainError('待确认关联申请已满，请先处理现有申请')
                conn.execute('INSERT INTO link_requests VALUES(?,?,?,?,?,?) '
                             'ON CONFLICT(source_id,target_id) DO UPDATE SET '
                             'source_owner=excluded.source_owner,target_owner=excluded.target_owner,'
                             'expires_at=excluded.expires_at,created=excluded.created',
                             (source['id'],target['id'],tg_id,target['owner_tg_id'],now+600,self._created(now)))
                result = {'id':target['id'],'emos_id':_safe_text(target['emos_id'],(target['token'],)),
                          'name':_safe_text(target['name'],(target['token'],)), 'pending':True}
        except DomainError:
            self._redemption_result(tg_id, False)
            raise
        except sqlite3.IntegrityError:
            self._redemption_result(tg_id, False)
            raise DomainError("关联未完成，请重新尝试") from None
        self._redemption_result(tg_id, True)
        return result

    def links(self, tg_id) -> dict:
        tg_id = _positive_id(tg_id)
        sql = "WITH root AS (" + _ROOT + ") " + """
            SELECT r.id AS root_id, l.source_id, l.target_id,
                   s.emos_id AS source_emos_id, t.emos_id AS target_emos_id,
                   s.name AS source_name, t.name AS target_name, l.enabled,
                   s.token AS source_token, t.token AS target_token
            FROM root r JOIN account_links l ON l.source_id=r.id OR l.target_id=r.id
            JOIN accounts s ON s.id=l.source_id JOIN accounts t ON t.id=l.target_id
            WHERE s.owner_tg_id IS NOT NULL AND t.owner_tg_id IS NOT NULL
            ORDER BY l.source_id,l.target_id
        """
        result = {"outgoing": [], "incoming": []}
        with self._access(tg_id, links=True) as (conn, _):
            rows = conn.execute(sql, (tg_id, tg_id)).fetchall()
        for row in rows:
            entry = {key: row[key] for key in ("source_id", "target_id", "source_emos_id",
                                              "target_emos_id", "source_name", "target_name", "enabled")}
            for key in ("source_emos_id", "target_emos_id", "source_name", "target_name"):
                entry[key] = _safe_text(entry[key], (row["source_token"], row["target_token"]))
            entry["enabled"] = _stored_flag(entry["enabled"])
            result["outgoing" if row["source_id"] == row["root_id"] else "incoming"].append(entry)
        return result

    def toggle_link(self, tg_id, source_id, target_id, expected_root=None, enabled=None) -> bool:
        tg_id, source_id, target_id = map(_positive_id, (tg_id, source_id, target_id))
        with self._access(tg_id, write=True, links=True) as (conn, _):
            self._expected(conn, tg_id, expected_root)
            row = conn.execute("SELECT l.enabled FROM account_links l JOIN accounts s "
                               "ON s.id=l.source_id WHERE l.source_id=? AND l.target_id=? "
                               "AND s.owner_tg_id=?", (source_id, target_id, tg_id)).fetchone()
            if row is None:
                raise DomainError("只能暂停或恢复自己账号的外向关联")
            enabled = (0 if _stored_flag(row['enabled']) else 1) if enabled is None else _flag(enabled)
            conn.execute("UPDATE account_links SET enabled=? WHERE source_id=? AND target_id=?",
                         (enabled, source_id, target_id))
            return bool(enabled)

    def unlink(self, tg_id, source_id, target_id, expected_root=None) -> None:
        tg_id, source_id, target_id = map(_positive_id, (tg_id, source_id, target_id))
        with self._access(tg_id, write=True, links=True) as (conn, _):
            self._expected(conn, tg_id, expected_root)
            row = conn.execute("SELECT s.owner_tg_id AS source_owner,t.owner_tg_id AS target_owner "
                               "FROM accounts s CROSS JOIN accounts t WHERE s.id=? AND t.id=?",
                               (source_id, target_id)).fetchone()
            if row is None or tg_id not in (row["source_owner"], row["target_owner"]):
                raise DomainError("只能撤销包含自己账号的关联")
            conn.execute("DELETE FROM account_links WHERE source_id=? AND target_id=?",
                         (source_id, target_id))
            conn.execute('DELETE FROM link_requests WHERE source_id=? AND target_id=?',(source_id,target_id))

    def pending_links(self, tg_id):
        tg_id = _positive_id(tg_id)
        result = {'outgoing': [], 'incoming': []}
        with self._access(tg_id, links=True) as (conn, _):
            root = self._active(conn, tg_id, required=False)
            if root is None:
                return result
            rows = conn.execute('SELECT p.source_id,p.target_id,p.expires_at,'
                                's.name AS source_name,s.emos_id AS source_emos_id,'
                                't.name AS target_name,t.emos_id AS target_emos_id,'
                                's.token AS source_token,t.token AS target_token '
                                'FROM link_requests p JOIN accounts s ON s.id=p.source_id '
                                'JOIN accounts t ON t.id=p.target_id '
                                'WHERE (p.source_id=? OR p.target_id=?) AND p.expires_at>? '
                                'AND s.owner_tg_id=p.source_owner AND t.owner_tg_id=p.target_owner '
                                'ORDER BY p.created', (root['id'], root['id'], self._now())).fetchall()
        for row in rows:
            entry = {key: row[key] for key in ('source_id','target_id','expires_at','source_name',
                                               'source_emos_id','target_name','target_emos_id')}
            for key in ('source_name','source_emos_id','target_name','target_emos_id'):
                entry[key] = _safe_text(entry[key], (row['source_token'],row['target_token']))
            result['outgoing' if row['source_id']==root['id'] else 'incoming'].append(entry)
        return result

    def review_link(self, tg_id, source_id, target_id, approve, expected_root=None):
        tg_id, source_id, target_id = map(_positive_id, (tg_id,source_id,target_id))
        if type(approve) is not bool:
            raise DomainError('关联审核选项无效')
        with self._access(tg_id, write=True, links=True) as (conn, _):
            self._expected(conn,tg_id,expected_root)
            target = conn.execute('SELECT * FROM accounts WHERE id=? AND owner_tg_id=?',
                                  (target_id,tg_id)).fetchone()
            source = conn.execute('SELECT * FROM accounts WHERE id=?',(source_id,)).fetchone()
            pending = conn.execute('SELECT * FROM link_requests WHERE source_id=? AND target_id=?',
                                   (source_id,target_id)).fetchone()
            if target is None or source is None or pending is None or pending['expires_at']<=self._now():
                raise DomainError('关联申请不存在、已过期或不属于当前用户')
            if pending['target_owner']!=tg_id or pending['source_owner']!=source['owner_tg_id']:
                raise DomainError('关联账号归属已变化，请重新申请')
            if approve:
                if not target['enabled'] or not source['enabled'] or target['last_status']=='invalid' or source['last_status']=='invalid':
                    raise DomainError('关联账号凭证已失效，请重新登录')
                conn.execute('INSERT INTO account_links(source_id,target_id,enabled,created) VALUES(?,?,1,?) '
                             'ON CONFLICT(source_id,target_id) DO UPDATE SET enabled=1',
                             (source_id,target_id,self._created(self._now())))
            conn.execute('DELETE FROM link_requests WHERE source_id=? AND target_id=?',(source_id,target_id))
        return {'approved': approve, 'source_id':source_id, 'target_id':target_id}

    def withdraw_link(self,tg_id,source_id,target_id,expected_root=None):
        tg_id,source_id,target_id=map(_positive_id,(tg_id,source_id,target_id))
        with self._access(tg_id, write=True, links=True) as (conn, _):
            self._expected(conn,tg_id,expected_root)
            row=conn.execute('SELECT s.owner_tg_id AS source_owner,t.owner_tg_id AS target_owner '
                             'FROM accounts s JOIN accounts t ON t.id=? WHERE s.id=?',(target_id,source_id)).fetchone()
            if row is None or tg_id not in (row['source_owner'],row['target_owner']):
                raise DomainError('只能取消涉及当前账号的申请')
            conn.execute('DELETE FROM link_requests WHERE source_id=? AND target_id=?',(source_id,target_id))

    def run_scope(self, tg_id, include_links=True) -> dict:
        """返回当前根和有效账号 ID；单人模式 True 是全部号，False 仅当前号。

        历史参数 include_links 保留名称；旧模式仅根和已授权一跳，不递归。"""
        tg_id = _positive_id(tg_id)
        root_sql = _ROOT + " AND a.enabled=1 AND TRIM(COALESCE(a.token,''))<>'' " \
                           "AND COALESCE(a.last_status,'')<>'invalid' " \
                           "AND a.emos_id IS NOT NULL AND a.emos_id<>''"
        sql = "WITH root AS (" + root_sql + ") SELECT id AS root_id,id AS account_id FROM root"
        if include_links:
            sql += """
                UNION ALL SELECT r.id,l.target_id FROM root r
                JOIN account_links l ON l.source_id=r.id JOIN accounts t ON t.id=l.target_id
                WHERE l.enabled=1 AND t.enabled=1 AND t.owner_tg_id IS NOT NULL
                AND TRIM(COALESCE(t.token,''))<>''
                AND t.emos_id IS NOT NULL AND t.emos_id<>''
                AND COALESCE(t.last_status,'')<>'invalid'
            """
        sql += " ORDER BY account_id"
        with self._access(tg_id) as (conn, owner):
            if owner is not None:
                self._require_primary(conn, tg_id)
                root = conn.execute(root_sql, (tg_id, tg_id)).fetchone()
                if root is None:
                    raise DomainError('请先登录并选择有效账号')
                ids = [root['id']]
                if include_links:
                    ids.extend(row['id'] for row in conn.execute(
                        'SELECT id FROM accounts WHERE owner_tg_id=? AND enabled=1 '
                        "AND TRIM(COALESCE(token,''))<>'' AND COALESCE(last_status,'')<>'invalid' "
                        "AND emos_id IS NOT NULL AND emos_id<>'' AND id<>? ORDER BY id",
                        (tg_id, root['id'])))
                return {'root_id': root['id'], 'account_ids': ids}
            rows = conn.execute(sql, (tg_id, tg_id)).fetchall()
        if not rows:
            raise DomainError("请先登录并选择有效账号")
        root_id = rows[0]["root_id"]
        ids = [root_id] + [row["account_id"] for row in rows if row["account_id"] != root_id]
        return {"root_id": root_id, "account_ids": ids}

    def can_run(self, tg_id, root_id, target_id) -> bool:
        """每个请求前复核当前根、目标凭据和本地授权，检查失败返回 False。

        单人模式只准同 owner 且主号有效；不在此发官方网络请求。"""
        try:
            tg_id, root_id, target_id = map(_positive_id, (tg_id, root_id, target_id))
            with self._access(tg_id) as (conn, owner):
                if owner is not None:
                    self._require_primary(conn, tg_id)
                    permission, params = 't.owner_tg_id=?', (tg_id,)
                else:
                    permission, params = '''(r.id=t.id OR EXISTS (
                        SELECT 1 FROM account_links l
                        WHERE l.source_id=r.id AND l.target_id=t.id AND l.enabled=1
                    ))''', ()
                row = conn.execute('''
                    SELECT 1 FROM tg_users u JOIN accounts r ON r.id=u.active_account_id
                    JOIN accounts t ON t.id=?
                    WHERE u.tg_id=? AND r.id=? AND r.owner_tg_id=? AND r.enabled=1
                    AND TRIM(COALESCE(r.token,''))<>''
                    AND r.emos_id IS NOT NULL AND r.emos_id<>''
                    AND COALESCE(r.last_status,'')<>'invalid'
                    AND t.enabled=1 AND t.owner_tg_id IS NOT NULL
                    AND TRIM(COALESCE(t.token,''))<>''
                    AND t.emos_id IS NOT NULL AND t.emos_id<>''
                    AND COALESCE(t.last_status,'')<>'invalid' AND ''' + permission,
                    (target_id, tg_id, root_id, tg_id, *params)).fetchone()
                return row is not None
        except DomainError:
            return False

    def admin_overview(self) -> list:
        """返回脱敏管理投影；单人模式只选配置 owner 的保留账号。

        管理员认证由调用方负责，owner 缺失或读取期间变化不返回账号。"""
        owner = self._owner_id()
        if owner == 0:
            return []
        with closing(self._db()) as conn:
            sql = "SELECT * FROM accounts WHERE emos_id IS NOT NULL AND emos_id<>'' "
            params = ()
            if owner is not None:
                sql += 'AND owner_tg_id=? '
                params = (owner,)
            rows = conn.execute(sql + 'ORDER BY owner_tg_id,id', params).fetchall()
        if self._owner_id() != owner:
            return []
        result = []
        for row in rows:
            item = _account(row)
            item['owner_tg_id'] = row['owner_tg_id']
            result.append(item)
        return result

    def visible(self, tg_id) -> list:
        """按实时授权返回脱敏账号列表；单人模式仅同 owner，旧模式根及一跳。"""
        tg_id = _positive_id(tg_id)
        sql = ("WITH root AS (" + _ROOT + "), visible AS (" + _VISIBLE_IDS + ") "
               "SELECT a.* FROM accounts a JOIN visible v ON v.id=a.id "
               "ORDER BY CASE WHEN a.id=(SELECT id FROM root) THEN 0 ELSE 1 END,a.id")
        with self._access(tg_id) as (conn, owner):
            if owner is not None:
                rows = conn.execute(
                    'SELECT a.* FROM accounts a WHERE a.owner_tg_id=? '
                    "AND a.emos_id IS NOT NULL AND a.emos_id<>'' "
                    'ORDER BY CASE WHEN a.id=(SELECT active_account_id FROM tg_users WHERE tg_id=?) '
                    'THEN 0 ELSE 1 END,a.id', (tg_id, tg_id)).fetchall()
            else:
                rows = conn.execute(sql, (tg_id, tg_id)).fetchall()
            return [_account(row) for row in rows]

    def logs(self, tg_id, limit=15) -> list:
        """返回有限可见日志，清除文本中可能含有的本账号密钥，不查询外部接口。"""
        tg_id = _positive_id(tg_id)
        if type(limit) is not int or limit < 1:
            raise DomainError("记录条数必须为正整数")
        limit = min(limit, 100)
        sql = ("WITH root AS (" + _ROOT + "), visible AS (" + _VISIBLE_IDS + ") " + """
            SELECT l.id,l.account_id,l.name,l.ts,l.status,l.message,l.carrot,
                   a.emos_id,a.token AS account_token
            FROM logs l JOIN visible v ON v.id=l.account_id
            JOIN accounts a ON a.id=l.account_id ORDER BY l.id DESC LIMIT ?
        """)
        with self._access(tg_id) as (conn, owner):
            if owner is not None:
                rows = conn.execute('''
                    SELECT l.id,l.account_id,l.name,l.ts,l.status,l.message,l.carrot,
                           a.emos_id,a.token AS account_token
                    FROM logs l JOIN accounts a ON a.id=l.account_id
                    WHERE a.owner_tg_id=? AND a.emos_id IS NOT NULL AND a.emos_id<>''
                    ORDER BY l.id DESC LIMIT ?
                ''', (tg_id, limit)).fetchall()
            else:
                rows = conn.execute(sql, (tg_id, tg_id, limit)).fetchall()
        result = []
        for row in rows:
            entry = {key: row[key] for key in ("id", "account_id", "name", "ts", "status",
                                              "message", "carrot", "emos_id")}
            for key in ("name", "ts", "status", "message", "emos_id"):
                entry[key] = _safe_text(entry[key], (row["account_token"],))
            result.append(entry)
        return result

    def set_input(self, tg_id, kind, ttl=600) -> None:
        """记录白名单输入类型和期限；Token 输入在单人模式要求主号仍有效。"""
        tg_id = _positive_id(tg_id)
        if kind not in ("link", "time", "login", "token") or not isinstance(kind, str):
            raise DomainError("输入会话类型无效")
        if type(ttl) is not int or ttl <= 0:
            raise DomainError("输入会话时限必须为正整数")
        with self._access(tg_id, write=True, links=kind == 'link') as (conn, owner):
            if owner is not None and kind == 'token':
                self._require_primary(conn, tg_id)
            now = self._now()
            if ttl > 9223372036854775807 - now:
                raise DomainError("输入会话时限无效")
            self._ensure(conn, tg_id)
            conn.execute("UPDATE tg_users SET input_kind=?,input_until=? WHERE tg_id=?",
                         (kind, now + ttl, tg_id))

    def input_state(self, tg_id) -> str:
        """读取尚未过期且仍具权限的输入类型；未知类型、失效主号和共享输入返回空。"""
        tg_id = _positive_id(tg_id)
        with self._access(tg_id) as (conn, owner):
            row = conn.execute('SELECT * FROM tg_users WHERE tg_id=?', (tg_id,)).fetchone()
            if row and row['input_until'] > self._now():
                kind = row['input_kind']
                if kind not in ('link', 'time', 'login', 'token'):
                    return ''
                if owner is not None:
                    if kind == 'link':
                        return ''
                    if kind == 'token':
                        primary = self._primary(conn, tg_id)
                        if primary is None or not _account(primary)['logged_in']:
                            return ''
                return kind
            return ''

    def clear_input(self, tg_id) -> None:
        """退出等待输入；完成、取消与过期处理不能让下一条消息被旧状态消费。"""
        tg_id = _positive_id(tg_id)
        with self._access(tg_id, write=True) as (conn, owner):
            conn.execute("UPDATE tg_users SET input_kind='',input_until=0 WHERE tg_id=?", (tg_id,))

    def watchers(self, account_id) -> list:
        """按账号授权查通知接收人，返回前再核 owner；发送时还需再次核验。"""
        account_id = _positive_id(account_id)
        owner = self._owner_id()
        if owner == 0:
            return []
        with closing(self._db()) as conn:
            if owner is not None:
                rows = conn.execute(
                    'SELECT u.* FROM tg_users u JOIN accounts a ON a.owner_tg_id=u.tg_id '
                    'WHERE u.tg_id=? AND u.chat_id=u.tg_id AND a.id=? '
                    "AND a.emos_id IS NOT NULL AND a.emos_id<>''",
                    (owner, account_id)).fetchall()
            else:
                rows = conn.execute("""
                    WITH recipients(tg_id) AS (
                        SELECT owner_tg_id FROM accounts WHERE id=? AND owner_tg_id IS NOT NULL
                        UNION
                        SELECT s.owner_tg_id FROM account_links l
                        JOIN accounts s ON s.id=l.source_id JOIN accounts t ON t.id=l.target_id
                        WHERE l.target_id=? AND l.enabled=1
                        AND s.owner_tg_id IS NOT NULL AND t.owner_tg_id IS NOT NULL
                    )
                    SELECT u.* FROM tg_users u JOIN recipients r ON r.tg_id=u.tg_id
                    WHERE u.chat_id=u.tg_id ORDER BY u.tg_id
                """, (account_id, account_id)).fetchall()
        if self._owner_id() != owner:
            return []
        return [_user(row) for row in rows]
