"""本人账号、官方主号证明、当前选择及旧关联数据的 SQLite 域服务。

生产配置始终含唯一 owner；未配置 owner 的兼容分支只供历史数据与回归测试。
写事务提交前后检查身份配置，公开投影不含 Token；旧关联入口在单人模式关闭。
Runtime: Python 3.10+ / 标准库。
Test: python3 -B -m unittest test_accounts test_owner_accounts
"""

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
    """面向个人页面和 Bot 的领域错误；拼接动态文本时仍由调用者脱敏。"""


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
# 当前选择是竞态校验根；生产单人范围另查全部有效本人账号。
_ROOT = """
    SELECT a.id FROM tg_users u JOIN accounts a ON a.id=u.active_account_id
    WHERE u.tg_id=? AND a.owner_tg_id=?
"""
# 历史兼容视图只含根和一跳；生产单人视图不使用此授权传播规则。
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
    return result


def _user(row):
    result = {key: row[key] for key in _USER_FIELDS}
    for key in _PREFERENCES:
        result[key] = _stored_flag(result[key])
    return result


class AccountService:
    def __init__(self, db, write, cfg):
        if not all(callable(fn) for fn in (db, write, cfg)):
            raise TypeError("db、write、cfg 必须可调用")
        self._db, self._write, self._cfg = db, write, cfg
        self._redeem_lock = threading.Lock()
        self._redeem_failures = {}
        self._owner_mode_seen = False
        self._owner_lock = threading.Lock()

    def _owner_id(self):
        """返回配置 owner；0 表示拒绝访问，None 仅代表历史兼容模式。

        一旦见过 owner 配置，后续缺失该键也拒绝，不回退为多人开放模式。
        """
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
            raise DomainError('仅配置的本人 Telegram 用户可操作；请检查唯一 TG ID')
        return owner

    @contextmanager
    def _access(self, tg_id, write=False, links=False):
        """在数据库访问前后复核 owner，配置变化使写事务回滚。
        """
        owner = self._actor(tg_id)
        if links and owner is not None:
            raise DomainError('单人模式不支持账号共享或关联；请使用官方 token 添加本人账号')
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
        """官方主账号的脱敏视图；无证明或不再是配置 owner 时返回 None。

        停用、密钥失效不抹掉官方首登证明，仍可对原 primary 官方重授权。
        """
        tg_id = _positive_id(tg_id)
        try:
            with self._access(tg_id) as (conn, _):
                row = self._primary(conn, tg_id)
                return _account(row) if row is not None else None
        except DomainError:
            return None

    def has_primary_account(self, tg_id) -> bool:
        return self.primary_account(tg_id) is not None

    def _official_primary(self, conn, tg_id, official):
        primary = self._primary(conn, tg_id)
        if primary is not None and primary['emos_id'] != official:
            raise DomainError('已有官方主账号；其他账号请使用官方 token 添加')
        return primary

    def _require_primary(self, conn, tg_id):
        primary = self._primary(conn, tg_id)
        if primary is None:
            raise DomainError('请先通过官方 emospg_bot 授权主账号；旧数据需官方重新授权')
        return primary

    @staticmethod
    def init_schema(conn):
        # 不用 executescript，避免提交调用者正在持有的迁移事务。
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
            raise DomainError("仅支持本人 Telegram 私聊")
        with self._access(tg_id, write=True) as (conn, owner):
            return _user(self._ensure(conn, tg_id))

    def user(self, tg_id) -> Optional[dict]:
        tg_id = _positive_id(tg_id)
        with self._access(tg_id) as (conn, _):
            row = conn.execute("SELECT * FROM tg_users WHERE tg_id=?", (tg_id,)).fetchone()
            return _user(row) if row is not None else None

    def begin_login(self, tg_id, official_id, expected_root=None) -> dict:
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
        row = self.user(tg_id)
        return bool(row and row["login_until"] > self._now())

    def cancel_login(self, tg_id) -> None:
        tg_id = _positive_id(tg_id)
        with self._access(tg_id, write=True) as (conn, owner):
            conn.execute("UPDATE tg_users SET login_until=0,login_emos_id='',"
                         "login_revision=login_revision+1 WHERE tg_id=?", (tg_id,))

    def finish_login(self, tg_id, token, profile, base_url='', expected_root=None,
                     expected_login_until=None, expected_login_revision=None) -> dict:
        tg_id = _positive_id(tg_id)
        if not isinstance(profile, Mapping):
            raise DomainError("登录资料无效，请重新授权")
        official = profile.get("user_id")
        if official is None or (isinstance(official, str) and not official.strip()):
            official = profile.get("_user_id")
        official = _official_id(official)
        telegram_id = profile.get("telegram_user_id")
        if telegram_id in (None, ""):
            raise DomainError("官方账号尚未绑定 Telegram。请在 emos 官网绑定本人 TG，再重新 /login；账号未保存")
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
                raise DomainError('官方回传账号 ID 与本人本次填写的 ID 不一致，账号未保存')
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

    def login_with_token(self, tg_id, token, profile, base_url='', expected_root=None) -> dict:
        """用调用者向官方核验的 profile 添加或更新同 TG 绑定的账号。

        单人模式必须已有官方主账号证明；token 路径永不建立或替换 primary。
        expected_root 可在网络验证结束后的写事务内再次校验选中账号。
        """
        tg_id = _positive_id(tg_id)
        if not isinstance(profile, Mapping):
            raise DomainError("登录资料无效，请重新授权")
        official = profile.get("user_id")
        if official is None or (isinstance(official, str) and not official.strip()):
            official = profile.get("_user_id")
        official = _official_id(official)
        telegram_id = profile.get("telegram_user_id")
        if telegram_id in (None, ""):
            raise DomainError("官方账号尚未绑定 Telegram。请在 emos 官网绑定本人 TG 后重试；账号未保存")
        if isinstance(telegram_id, str) and telegram_id.isascii() and telegram_id.isdigit():
            telegram_id = int(telegram_id) if len(telegram_id) <= 19 else 0
        if type(telegram_id) is not int or telegram_id != tg_id:
            raise DomainError("该密钥属于另一个 Telegram 用户，不能加入你的账号列表")
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
            # 先建用户行再更新当前选择，确保首次 token 登录也能选中账号。
            self._ensure(conn, tg_id)
            conn.execute("UPDATE tg_users SET active_account_id=?,login_until=0,login_emos_id='',"
                         "login_revision=login_revision+1 WHERE tg_id=?", (account_id, tg_id))
            return _account(conn.execute("SELECT * FROM accounts WHERE id=?",
                                         (account_id,)).fetchone())

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
        if expected_root is None:
            return
        if type(expected_root) is not int or expected_root < 0:
            raise DomainError('当前账号参数无效')
        row = AccountService._active(conn, tg_id, required=False)
        if (row['id'] if row is not None else 0) != expected_root:
            raise DomainError('当前账号已切换，请刷新页面后重试')

    def switch(self, tg_id, local_account_id, expected_root=None) -> dict:
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
                raise DomainError('关联申请不存在、已过期或不属于本人')
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
                raise DomainError('只能取消涉及本人账号的申请')
            conn.execute('DELETE FROM link_requests WHERE source_id=? AND target_id=?',(source_id,target_id))

    def run_scope(self, tg_id, include_links=True) -> dict:
        tg_id = _positive_id(tg_id)
        root_sql = _ROOT + " AND a.enabled=1 AND COALESCE(a.last_status,'')<>'invalid' " \
                           "AND a.emos_id IS NOT NULL AND a.emos_id<>''"
        sql = "WITH root AS (" + root_sql + ") SELECT id AS root_id,id AS account_id FROM root"
        if include_links:
            sql += """
                UNION ALL SELECT r.id,l.target_id FROM root r
                JOIN account_links l ON l.source_id=r.id JOIN accounts t ON t.id=l.target_id
                WHERE l.enabled=1 AND t.enabled=1 AND t.owner_tg_id IS NOT NULL
                AND t.emos_id IS NOT NULL AND t.emos_id<>''
                AND COALESCE(t.last_status,'')<>'invalid'
            """
        sql += " ORDER BY account_id"
        with self._access(tg_id) as (conn, owner):
            if owner is not None:
                self._require_primary(conn, tg_id)
                root = conn.execute(root_sql, (tg_id, tg_id)).fetchone()
                if root is None:
                    raise DomainError('请先登录并选择有效的本人账号')
                ids = [root['id']]
                if include_links:
                    ids.extend(row['id'] for row in conn.execute(
                        'SELECT id FROM accounts WHERE owner_tg_id=? AND enabled=1 '
                        "AND COALESCE(last_status,'')<>'invalid' "
                        "AND emos_id IS NOT NULL AND emos_id<>'' AND id<>? ORDER BY id",
                        (tg_id, root['id'])))
                return {'root_id': root['id'], 'account_ids': ids}
            rows = conn.execute(sql, (tg_id, tg_id)).fetchall()
        if not rows:
            raise DomainError("请先登录并选择有效的本人账号")
        root_id = rows[0]["root_id"]
        ids = [root_id] + [row["account_id"] for row in rows if row["account_id"] != root_id]
        return {"root_id": root_id, "account_ids": ids}

    def can_run(self, tg_id, root_id, target_id) -> bool:
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
                    AND r.emos_id IS NOT NULL AND r.emos_id<>''
                    AND COALESCE(r.last_status,'')<>'invalid'
                    AND t.enabled=1 AND t.owner_tg_id IS NOT NULL
                    AND t.emos_id IS NOT NULL AND t.emos_id<>''
                    AND COALESCE(t.last_status,'')<>'invalid' AND ''' + permission,
                    (target_id, tg_id, root_id, tg_id, *params)).fetchone()
                return row is not None
        except DomainError:
            return False

    def admin_overview(self) -> list:
        """脱敏管理视图；旧模式全表，单人模式仅配置 owner 的保留账号。

        管理员鉴权仍由调用者负责；owner 未配置或访问期间变化则不发数据。
        """
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
        tg_id = _positive_id(tg_id)
        with self._access(tg_id) as (conn, owner):
            row = conn.execute('SELECT * FROM tg_users WHERE tg_id=?', (tg_id,)).fetchone()
            if row and row['input_until'] > self._now():
                kind = row['input_kind']
                if kind not in ('link', 'time', 'login', 'token'):
                    return ''
                if owner is not None and (kind == 'link' or kind == 'token' and self._primary(conn, tg_id) is None):
                    return ''
                return kind
            return ''

    def clear_input(self, tg_id) -> None:
        tg_id = _positive_id(tg_id)
        with self._access(tg_id, write=True) as (conn, owner):
            conn.execute("UPDATE tg_users SET input_kind='',input_until=0 WHERE tg_id=?", (tg_id,))

    def watchers(self, account_id) -> list:
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
