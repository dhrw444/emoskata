"""账号域归属、历史迁移、字段投影与 SQLite 事务回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_accounts"""

import sys
sys.dont_write_bytecode = True

import hashlib
import json
import queue
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from accounts import AccountService, DomainError


OLD_SCHEMA = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, token TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, last_status TEXT DEFAULT '', last_msg TEXT DEFAULT '',
    last_carrot INTEGER, last_run TEXT DEFAULT '', last_day TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0, next_run_at INTEGER DEFAULT 0, created TEXT DEFAULT ''
);
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
    name TEXT DEFAULT '', ts TEXT NOT NULL, status TEXT NOT NULL,
    message TEXT DEFAULT '', carrot INTEGER
);
"""


class TempStore:
    def __init__(self, path, config=None):
        self.path = str(path)
        # 默认刻意缺 owner 键以锁住旧 multi 回归语义；新测试显式传唯一 owner。
        self.config = dict(config) if config is not None else {
            "sign_time": "08:00", "base_url": "https://example.invalid"}
        self.lock = threading.RLock()
        self.next_write = None
        self.writes = 0

    def db(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def write(self):
        with self.lock, closing(self.db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.writes += 1
            try:
                if self.next_write is not None:
                    hook, self.next_write = self.next_write, None
                    hook(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def cfg(self):
        return dict(self.config)


class AccountCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="emoskata-account-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = TempStore(Path(self.temp.name) / "test.sqlite3")
        self.service = AccountService(self.store.db, self.store.write, self.store.cfg)
        with self.store.write() as conn:
            AccountService.init_schema(conn)

    def login(self, tg_id, official, token=None, **extra):
        self.service.begin_login(tg_id, official)
        profile = {"user_id": official, "telegram_user_id": tg_id, "name": "账号" + str(official)}
        profile.update(extra)
        return self.service.finish_login(tg_id, token or "fake-token-" + str(official), profile)

    def trio(self):
        return self.login(101, "A-100"), self.login(202, "B-200"), self.login(303, "C-300")

    def link(self, source_owner, target_owner):
        issued = self.service.issue_code(target_owner)
        result = self.service.redeem(source_owner, issued["target_emos_id"], issued["code"])
        source = self.service.active(source_owner)
        target = self.service.active(target_owner)
        self.assertTrue(result['pending'])
        self.assertFalse(self.service.can_run(source_owner, source['id'], target['id']))
        self.service.review_link(target_owner, source['id'], target['id'], True,
                                 expected_root=target['id'])
        return result

    def query(self, sql, params=()):
        with closing(self.store.db()) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def mutate(self, sql, params=()):
        with self.store.write() as conn:
            conn.execute(sql, params)

    def count(self, table):
        return self.query("SELECT COUNT(*) AS n FROM " + table)[0]["n"]

    def add_log(self, account, message="已签到"):
        self.mutate("INSERT INTO logs(account_id,name,ts,status,message,carrot) "
                    "VALUES(?,?,'2026-11-07 08:00:00','signed',?,1)",
                    (account["id"], account["name"], message))

    def assert_no_credentials(self, value, tokens=()):
        rendered = json.dumps(value, ensure_ascii=False)
        for token in tokens:
            self.assertNotIn(token, rendered)
        if isinstance(value, dict):
            self.assertTrue({"token", "owner_tg_id", "base_url", "code_hash"}.isdisjoint(value))
            for nested in value.values():
                self.assert_no_credentials(nested, tokens)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                self.assert_no_credentials(nested, tokens)

    def test_ensure_user_private_validation_and_integer_preferences(self):
        self.assertIsNone(self.service.user(101))
        user = self.service.ensure_user(101)
        self.assertEqual((user["tg_id"], user["chat_id"], user["active_account_id"]), (101, 101, None))
        self.assertIsNone(user['primary_account_id'])
        self.assertIsNone(self.service.primary_account(101))
        self.assertFalse(self.service.has_primary_account(101))
        self.assertEqual([user[key] for key in ("notify_success", "notify_fail", "notify_already")], [1, 1, 0])
        for key in ("notify_success", "notify_fail", "notify_already"):
            self.assertIs(type(user[key]), int)
        self.service.ensure_user(101, 101)
        self.assertEqual(self.count("tg_users"), 1)
        for tg_id in (0, -1, True, "101", 1.0, None, 2**63):
            with self.subTest(tg_id=tg_id), self.assertRaises(DomainError):
                self.service.ensure_user(tg_id)
        for chat_id in (-101, 202, "101", True):
            with self.subTest(chat_id=chat_id), self.assertRaises(DomainError):
                self.service.ensure_user(101, chat_id)
        self.assertEqual(self.count("tg_users"), 1)

    def test_login_pending_expiration_and_cancel_are_isolated(self):
        with patch("accounts.time.time", return_value=1000):
            self.service.begin_login(101, 'A')
            self.assertEqual(self.service.user(101)["login_until"], 1600)
            self.assertTrue(self.service.login_pending(101))
            self.assertFalse(self.service.login_pending(202))
        with patch("accounts.time.time", return_value=1599):
            self.assertTrue(self.service.login_pending(101))
        with patch("accounts.time.time", return_value=1600):
            self.assertFalse(self.service.login_pending(101))
            with self.assertRaises(DomainError):
                self.service.finish_login(101, "fake", {"user_id": "A"})
        self.assertEqual(self.count("accounts"), 0)
        self.service.begin_login(101, 'A')
        self.service.begin_login(202, 'A')
        self.service.cancel_login(101)
        self.assertFalse(self.service.login_pending(101))
        self.assertTrue(self.service.login_pending(202))

    def test_finish_requires_pending_for_actor(self):
        with self.assertRaises(DomainError):
            self.service.finish_login(101, "fake", {"user_id": "A"})
        self.service.begin_login(101, 'A')
        with self.assertRaises(DomainError):
            self.service.finish_login(202, "fake", {"user_id": "A", "telegram_user_id": 202})
        self.assertTrue(self.service.login_pending(101))
        self.assertEqual(self.count("accounts"), 0)

    def test_profile_id_and_telegram_identity_are_enforced(self):
        self.service.begin_login(101, '123')
        for profile in ({}, {"id": "A"}, {"user_id": " "}, {"user_id": False},
                        {"user_id": []}, {"user_id": "A", "telegram_user_id": 202},
                        {"user_id": "A", "telegram_user_id": True}):
            with self.subTest(profile=profile), self.assertRaises(DomainError):
                self.service.finish_login(101, "fake", profile)
        self.assertTrue(self.service.login_pending(101))
        self.assertEqual(self.count("accounts"), 0)
        account = self.service.finish_login(101, "fake", {"_user_id": 123, "telegram_user_id": "101"})
        self.assertEqual(account["emos_id"], "123")
        self.assertFalse(self.service.login_pending(101))
        self.service.begin_login(101, 'ABC')
        account = self.service.finish_login(101, "fake-two", {"user_id": "ABC", "telegram_user_id": 101})
        self.assertEqual(account["emos_id"], "ABC")

    def test_login_stores_verified_identity_and_only_safe_account_fields(self):
        self.service.begin_login(101, '00123')
        account = self.service.finish_login(101, " fake-secret-A ",
                                            {"user_id": "00123", "telegram_user_id": 101,
                                             "_display": "已验证名字"}, " https://example.invalid ")
        self.assertEqual((account["emos_id"], account["name"]), ("00123", "已验证名字"))
        row = self.query("SELECT * FROM accounts")[0]
        self.assertEqual((row["token"], row["owner_tg_id"], row["base_url"]),
                         ("fake-secret-A", 101, "https://example.invalid"))
        self.assertEqual(self.service.active(101)["id"], account["id"])
        self.assert_no_credentials(account, ("fake-secret-A",))
        self.assertFalse(self.service.login_pending(101))

    def test_repeated_login_updates_token_without_duplicate_or_history_loss(self):
        account = self.login(101, "A-100", "fake-old-token")
        self.add_log(account)
        self.mutate("UPDATE accounts SET enabled=0,last_status='invalid',last_msg='失效', "
                    "retry_count=3,next_run_at=999,last_run='old-run',last_day='old-day', "
                    "last_carrot=8,auto_sign=0,sign_time='09:30',created='old-created' WHERE id=?",
                    (account["id"],))
        updated = self.login(101, "A-100", "fake-new-token", username="new-name", name="new-name")
        self.assertEqual(updated["id"], account["id"])
        self.assertEqual(self.count("accounts"), 1)
        row = self.query("SELECT * FROM accounts")[0]
        self.assertEqual((row["token"], row["enabled"], row["last_status"], row["last_msg"],
                          row["retry_count"], row["next_run_at"]), ("fake-new-token", 1, "", "", 0, 0))
        self.assertEqual((row["last_run"], row["last_day"], row["last_carrot"], row["created"],
                          row["auto_sign"], row["sign_time"]),
                         ("old-run", "old-day", 8, "old-created", 0, "09:30"))
        self.assertEqual(self.count("logs"), 1)
        with self.assertRaises(DomainError):
            self.service.finish_login(101, "fake-new-token", {"user_id": "A-100"})

    def test_login_resets_optional_runtime_retry_day_without_schema_addition(self):
        account = self.login(101, "A")
        self.assertNotIn("retry_day", {row["name"] for row in self.query("PRAGMA table_info(accounts)")})
        self.mutate("ALTER TABLE accounts ADD COLUMN retry_day TEXT DEFAULT ''")
        self.mutate("UPDATE accounts SET retry_day='2026-11-07',enabled=0,last_status='invalid' WHERE id=?",
                    (account["id"],))
        updated = self.login(101, "A", "fake-rotated")
        row = self.query("SELECT retry_day,enabled FROM accounts WHERE id=?", (account["id"],))[0]
        self.assertEqual(row, {"retry_day": "", "enabled": 1})
        self.assertNotIn("retry_day", updated)

    def test_cannot_steal_registered_official_id_or_token(self):
        a = self.login(101, "A", "fake-owner-A")
        self.service.begin_login(202, 'A')
        for token, official in (("fake-new", "A"), ("fake-owner-A", "A"), ("fake-owner-A", "B")):
            self.service.begin_login(202, official)
            with self.subTest(token=token, official=official), self.assertRaises(DomainError):
                self.service.finish_login(202, token, {"user_id": official, "telegram_user_id": 202})
        self.assertEqual(self.count("accounts"), 1)
        self.assertEqual(self.query("SELECT owner_tg_id,token FROM accounts WHERE id=?", (a["id"],))[0],
                         {"owner_tg_id": 101, "token": "fake-owner-A"})
        self.assertTrue(self.service.login_pending(202))
        self.assertIsNone(self.service.active(202, required=False))
        with self.assertRaises(DomainError):
            self.service.set_account(101, owner_tg_id=202)

    def test_conflicting_verified_id_for_same_owned_token_is_rejected(self):
        a = self.login(101, "A", "fake-shared")
        self.service.begin_login(101, 'B')
        with self.assertRaises(DomainError):
            self.service.finish_login(101, "fake-shared", {"user_id": "B", "telegram_user_id": 101})
        self.assertEqual(self.count("accounts"), 1)
        self.assertEqual(self.service.active(101)["id"], a["id"])
        self.assertTrue(self.service.login_pending(101))

    def test_legacy_official_id_cannot_be_claimed_with_different_token(self):
        self.mutate("INSERT INTO accounts(name,token,emos_id) VALUES('旧账号','fake-legacy','OLD')")
        self.service.begin_login(101, 'OLD')
        with self.assertRaises(DomainError):
            self.service.finish_login(101, "fake-other", {"user_id": "OLD", "telegram_user_id": 101})
        self.assertEqual(self.query("SELECT owner_tg_id FROM accounts")[0]["owner_tg_id"], None)
        account = self.service.finish_login(101, "fake-legacy", {"user_id": "OLD", "telegram_user_id": 101})
        self.assertEqual(account["id"], 1)
        self.assertEqual(self.count("accounts"), 1)

    def test_multiple_owned_accounts_require_explicit_selection_and_grants(self):
        a = self.login(101, "A")
        b = self.login(101, "B")
        stranger = self.login(202, "C")
        self.assertEqual([item["id"] for item in self.service.owned(101)], [a["id"], b["id"]])
        self.assertEqual(self.service.active(101)["id"], b["id"])
        code = self.service.issue_code(101)
        self.service.switch(101, a["id"])
        self.assertFalse(self.service.can_run(101, a["id"], b["id"]))
        self.assertEqual(self.service.run_scope(101)["account_ids"], [a["id"]])
        self.assertEqual(self.service.redeem(101, "B", code["code"])["id"], b["id"])
        with self.assertRaises(DomainError):
            self.service.switch(101, stranger["id"])
        self.assertFalse(self.service.can_run(202, a["id"], a["id"]))
        self.assertEqual(self.service.active(101)["id"], a["id"])

    def test_settings_whitelist_clamp_and_do_not_revive_invalid_account(self):
        a = self.login(101, "A")
        self.mutate("UPDATE accounts SET enabled=0,last_status='invalid' WHERE id=?", (a["id"],))
        updated = self.service.set_account(101, auto_sign=True, sign_time="00:30")
        self.assertEqual((updated["auto_sign"], updated["sign_time"], updated["enabled"]), (1, "01:30", 0))
        self.assertEqual(self.service.set_account(101, sign_time="-1:99")["sign_time"], "01:59")
        self.assertEqual(self.service.set_account(101, sign_time="99:70")["sign_time"], "23:59")
        self.assertEqual(self.service.set_account(101, sign_time="bad")["sign_time"], "08:00")
        for fields in ({"enabled": True}, {"enabled": False}, {"token": "fake"},
                       {"owner_tg_id": 202}, {"name": "hijack"}, {"auto_sign": "false"}):
            with self.subTest(fields=fields), self.assertRaises(DomainError):
                self.service.set_account(101, **fields)
        with self.assertRaises(DomainError):
            self.service.set_account(202, auto_sign=True)
        with self.assertRaises(DomainError):
            self.service.run_scope(101)

    def test_set_account_rechecks_owner_inside_write_transaction(self):
        a = self.login(101, "A")
        self.store.next_write = lambda conn: conn.execute(
            "UPDATE accounts SET owner_tg_id=202 WHERE id=?", (a["id"],))
        with self.assertRaises(DomainError):
            self.service.set_account(101, auto_sign=False)
        # 注入变更与失败操作必须在同一事务内回滚。
        row = self.query("SELECT owner_tg_id,auto_sign FROM accounts")[0]
        self.assertEqual(row, {"owner_tg_id": 101, "auto_sign": 1})

    def test_set_account_sign_mode_whitelist_and_persistence(self):
        # 直接测试生产域规则；Bot 替身只验证交互，不能覆盖此判据。
        a = self.login(101, "A")
        self.assertEqual(a["sign_mode"], "normal")
        self.assertEqual(self.service.set_account(101, sign_mode="random")["sign_mode"], "random")
        self.assertEqual(self.query("SELECT sign_mode FROM accounts WHERE id=?", (a["id"],))[0],
                         {"sign_mode": "random"})
        for bad in ("weird", "", None, "RANDOM", 1, True):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                self.service.set_account(101, sign_mode=bad)
        self.assertEqual(self.query("SELECT sign_mode FROM accounts WHERE id=?", (a["id"],))[0],
                         {"sign_mode": "random"})
        with self.assertRaises(DomainError):
            self.service.set_account(101, unknown_field=1)
        # 没归属人的账号不能被改
        self.store.next_write = lambda conn: conn.execute(
            "UPDATE accounts SET owner_tg_id=202 WHERE id=?", (a["id"],))
        with self.assertRaises(DomainError):
            self.service.set_account(101, sign_mode="normal")
        self.assertEqual(self.query("SELECT sign_mode FROM accounts WHERE id=?", (a["id"],))[0],
                         {"sign_mode": "random"})

    def test_preferences_whitelist_normalizes_all_flags_and_isolates_users(self):
        self.service.ensure_user(202)
        user = self.service.set_preferences(101, notify_success=False, notify_fail="1", notify_already=1)
        self.assertEqual([user[key] for key in ("notify_success", "notify_fail", "notify_already")], [0, 1, 1])
        for key in ("notify_success", "notify_fail", "notify_already"):
            self.assertIs(type(user[key]), int)
        self.assertEqual(self.service.user(202)["notify_success"], 1)
        for fields in ({"chat_id": 202}, {"active_account_id": 1}, {"notify_fail": "yes"}):
            with self.subTest(fields=fields), self.assertRaises(DomainError):
                self.service.set_preferences(101, **fields)
        self.assertEqual(self.service.user(101)["notify_success"], 0)

    def test_issue_code_hash_only_rotates_and_has_600_second_ttl(self):
        a = self.login(101, "A")
        with patch("accounts.time.time", return_value=1000):
            one = self.service.issue_code(101)
            stored = self.query("SELECT * FROM link_codes")[0]
            self.assertEqual(set(one), {"target_emos_id", "code", "expires_at"})
            self.assertEqual(one["expires_at"], 1600)
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{24}", one["code"]))
            self.assertEqual(stored["code_hash"], hashlib.sha256(one["code"].encode()).hexdigest())
            self.assertEqual((stored["target_id"], stored["issued_by"]), (a["id"], 101))
            self.assertNotIn(one["code"], json.dumps(stored))
            two = self.service.issue_code(101)
        self.assertNotEqual(one["code"], two["code"])
        self.assertEqual(self.count("link_codes"), 1)
        self.assertEqual(self.query("SELECT code_hash FROM link_codes")[0]["code_hash"],
                         hashlib.sha256(two["code"].encode()).hexdigest())

    def test_issue_code_requires_valid_owned_active_account(self):
        with self.assertRaises(DomainError):
            self.service.issue_code(101)
        a = self.login(101, "A")
        for sql in ("UPDATE accounts SET enabled=0", "UPDATE accounts SET last_status='invalid'",
                    "UPDATE accounts SET owner_tg_id=NULL"):
            with self.subTest(sql=sql):
                self.mutate("UPDATE accounts SET enabled=1,last_status='',owner_tg_id=101 WHERE id=?", (a["id"],))
                self.mutate(sql)
                with self.assertRaises(DomainError):
                    self.service.issue_code(101)
        self.assertEqual(self.count("link_codes"), 0)

    def test_wrong_official_id_or_local_id_does_not_consume_code(self):
        a, b, c = self.trio()
        issued = self.service.issue_code(202)
        for target in ("wrong", str(b["id"]), "C-300"):
            with self.subTest(target=target), self.assertRaises(DomainError):
                self.service.redeem(101, target, issued["code"])
            self.assertEqual(self.count("link_codes"), 1)
            self.assertEqual(self.count("account_links"), 0)
        linked = self.service.redeem(101, "B-200", issued["code"])
        self.assertEqual(linked["id"], b["id"])
        self.assertEqual(self.count("link_codes"), 0)
        self.assertTrue(linked['pending'])
        self.assertEqual(self.count('account_links'), 0)
        self.assertFalse(self.service.can_run(101, a['id'], b['id']))
        self.service.review_link(202, a['id'], b['id'], True)
        self.assertEqual(self.service.run_scope(101), {"root_id": a["id"], "account_ids": [a["id"], b["id"]]})

    def test_id_alone_bad_code_and_stale_code_never_authorize(self):
        a, b, _ = self.trio()
        old = self.service.issue_code(202)
        new = self.service.issue_code(202)
        for code in ("", "invalid", old["code"], None):
            with self.subTest(code=code), self.assertRaises(DomainError):
                self.service.redeem(101, "B-200", code)
        self.assertEqual(self.count("account_links"), 0)
        self.assertFalse(self.service.can_run(101, a["id"], b["id"]))
        self.assertEqual(self.count("link_codes"), 1)
        self.service.redeem(101, "B-200", new["code"])
        with self.assertRaises(DomainError):
            self.service.redeem(101, "B-200", new["code"])
        self.assertEqual(self.count("account_links"), 0)
        self.assertEqual(self.count('link_requests'), 1)
        self.service.review_link(202, a['id'], b['id'], True)
        self.assertEqual(self.count("account_links"), 1)

    def test_code_expiration_boundary_and_issuer_ownership_match(self):
        a, b, _ = self.trio()
        with patch("accounts.time.time", return_value=1000):
            code = self.service.issue_code(202)
        with patch("accounts.time.time", return_value=1600), self.assertRaises(DomainError):
            self.service.redeem(101, "B-200", code["code"])
        self.assertEqual(self.count("link_codes"), 1)
        self.assertEqual(self.count("account_links"), 0)
        self.mutate("UPDATE link_codes SET expires_at=9999999999,issued_by=303")
        with self.assertRaises(DomainError):
            self.service.redeem(101, "B-200", code["code"])
        self.assertEqual(self.count("link_codes"), 1)
        self.mutate("UPDATE link_codes SET issued_by=202")
        self.service.redeem(101, "B-200", code["code"])
        self.assertEqual(self.count("link_codes"), 0)
        self.assertEqual(self.count('account_links'), 0)
        self.service.review_link(202, a['id'], b['id'], True)
        self.assertEqual(self.query("SELECT target_id FROM account_links")[0]["target_id"], b["id"])

    def test_self_link_rejected_without_consuming_own_code(self):
        a = self.login(101, "A")
        code = self.service.issue_code(101)
        with self.assertRaises(DomainError):
            self.service.redeem(101, "A", code["code"])
        self.assertEqual(self.count("link_codes"), 1)
        self.assertEqual(self.count("account_links"), 0)
        self.assertEqual(self.service.run_scope(101)["account_ids"], [a["id"]])

    def test_concurrent_one_code_has_exactly_one_consumer(self):
        a, b, c = self.trio()
        issued = self.service.issue_code(202)
        # 独立连接和锁验证 SQLite 竞争，而非仅验证进程内互斥。
        other = TempStore(self.store.path)
        service2 = AccountService(other.db, other.write, other.cfg)
        barrier = threading.Barrier(3)
        outcomes = queue.Queue()

        def redeem(service, actor):
            try:
                barrier.wait(timeout=10)
                account = service.redeem(actor, "B-200", issued["code"])
                outcomes.put((actor, "ok", account["id"]))
            except DomainError:
                outcomes.put((actor, "denied", None))
            except BaseException as exc:
                outcomes.put((actor, type(exc).__name__, None))

        threads = [threading.Thread(target=redeem, args=(self.service, 101)),
                   threading.Thread(target=redeem, args=(service2, 303))]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
        actual = [outcomes.get_nowait(), outcomes.get_nowait()]
        self.assertEqual(sorted(row[1] for row in actual), ["denied", "ok"])
        winner = next(row[0] for row in actual if row[1] == "ok")
        self.assertEqual(self.count('account_links'), 0)
        links = self.query("SELECT source_id,target_id FROM link_requests")
        self.assertEqual(links, [{"source_id": a["id"] if winner == 101 else c["id"], "target_id": b["id"]}])
        self.service.review_link(202, links[0]['source_id'], b['id'], True)
        self.assertEqual(self.count('account_links'), 1)
        self.assertEqual(self.count("link_codes"), 0)

    def test_code_consume_and_insert_roll_back_together(self):
        a, b, _ = self.trio()
        issued = self.service.issue_code(202)
        self.mutate("CREATE TRIGGER reject_link BEFORE INSERT ON link_requests BEGIN "
                    "SELECT RAISE(ABORT,'synthetic insertion failure'); END")
        with self.assertRaises(DomainError):
            self.service.redeem(101, "B-200", issued["code"])
        self.assertEqual(self.count("link_codes"), 1)
        self.assertEqual(self.count("account_links"), 0)
        self.mutate("DROP TRIGGER reject_link")
        self.service.redeem(101, "B-200", issued["code"])
        self.assertFalse(self.service.can_run(101, a["id"], b["id"]))
        self.service.review_link(202, a['id'], b['id'], True)
        self.assertTrue(self.service.can_run(101, a["id"], b["id"]))

    def test_one_hop_no_reverse_or_transitive_visibility_logs_or_run(self):
        a, b, c = self.trio()
        self.link(101, 202)
        self.link(202, 303)
        for item in (a, b, c):
            self.add_log(item)
        self.assertEqual(self.service.run_scope(101), {"root_id": a["id"], "account_ids": [a["id"], b["id"]]})
        self.assertFalse(self.service.can_run(101, a["id"], c["id"]))
        self.assertFalse(self.service.can_run(202, b["id"], a["id"]))
        self.assertFalse(self.service.can_run(303, c["id"], b["id"]))
        self.assertEqual([item["id"] for item in self.service.visible(101)], [a["id"], b["id"]])
        self.assertEqual({item["account_id"] for item in self.service.logs(101)}, {a["id"], b["id"]})
        self.assertEqual([item["id"] for item in self.service.visible(303)], [c["id"]])
        self.assertEqual(self.service.run_scope(101, False)["account_ids"], [a["id"]])
        self.assertEqual(self.service.links(303)["outgoing"], [])

    def test_links_dict_exact_contract_shape_and_no_owner_or_tokens(self):
        a, b, _ = self.trio()
        self.link(101, 202)
        outgoing = self.service.links(101)
        incoming = self.service.links(202)
        expected = {"source_id": a["id"], "target_id": b["id"], "source_emos_id": "A-100",
                    "target_emos_id": "B-200", "source_name": a["name"], "target_name": b["name"], "enabled": 1}
        self.assertEqual(outgoing, {"outgoing": [expected], "incoming": []})
        self.assertEqual(incoming, {"outgoing": [], "incoming": [expected]})
        self.assert_no_credentials(outgoing, ("fake-token-A-100", "fake-token-B-200"))
        self.assertEqual(self.service.links(404), {"outgoing": [], "incoming": []})

    def test_toggle_only_source_owner_pauses_run_not_existing_visibility(self):
        a, b, _ = self.trio()
        self.link(101, 202)
        self.add_log(b)
        for actor in (202, 303):
            with self.subTest(actor=actor), self.assertRaises(DomainError):
                self.service.toggle_link(actor, a["id"], b["id"])
        self.assertFalse(self.service.toggle_link(101, a["id"], b["id"]))
        self.assertFalse(self.service.can_run(101, a["id"], b["id"]))
        self.assertEqual(self.service.run_scope(101)["account_ids"], [a["id"]])
        self.assertIn(b["id"], [item["id"] for item in self.service.visible(101)])
        self.assertEqual([row["account_id"] for row in self.service.logs(101)], [b["id"]])
        self.assertEqual([row["tg_id"] for row in self.service.watchers(b["id"])], [202])
        self.assertTrue(self.service.toggle_link(101, a["id"], b["id"]))
        self.assertTrue(self.service.can_run(101, a["id"], b["id"]))

    def test_revoke_either_endpoint_immediately_hides_target_logs_and_run(self):
        a, b, c = self.trio()
        for item in (a, b, c):
            self.add_log(item)
        for actor in (101, 202):
            with self.subTest(actor=actor):
                self.link(101, 202)
                with self.assertRaises(DomainError):
                    self.service.unlink(303, a["id"], b["id"])
                snapshot = self.service.run_scope(101)
                self.assertIn(b["id"], snapshot["account_ids"])
                self.service.unlink(actor, a["id"], b["id"])
                self.assertFalse(self.service.can_run(101, snapshot["root_id"], b["id"]))
                self.assertEqual(self.service.run_scope(101)["account_ids"], [a["id"]])
                self.assertEqual([row["id"] for row in self.service.visible(101)], [a["id"]])
                self.assertEqual({row["account_id"] for row in self.service.logs(101)}, {a["id"]})
                self.assertEqual(self.service.links(101)["outgoing"], [])
                self.service.unlink(actor, a["id"], b["id"])

    def test_toggle_and_unlink_recheck_endpoint_owner_under_write(self):
        a, b, _ = self.trio()
        self.link(101, 202)
        self.store.next_write = lambda conn: conn.execute(
            "UPDATE accounts SET owner_tg_id=303 WHERE id=?", (a["id"],))
        with self.assertRaises(DomainError):
            self.service.toggle_link(101, a["id"], b["id"])
        self.assertEqual(self.service.links(101)["outgoing"][0]["enabled"], 1)
        self.store.next_write = lambda conn: conn.execute(
            "UPDATE accounts SET owner_tg_id=303 WHERE id=?", (b["id"],))
        with self.assertRaises(DomainError):
            self.service.unlink(202, a["id"], b["id"])
        self.assertEqual(self.count("account_links"), 1)

    def test_unlink_any_owned_endpoint_even_when_another_account_active(self):
        a, b, _ = self.trio()
        self.link(101, 202)
        self.login(202, "B-second")
        self.assertEqual(self.service.links(202), {"outgoing": [], "incoming": []})
        self.service.unlink(202, a["id"], b["id"])
        self.assertEqual(self.count("account_links"), 0)
        self.assertFalse(self.service.can_run(101, a["id"], b["id"]))

    def test_per_request_run_rechecks_selection_grant_owner_and_eligibility(self):
        a, b, _ = self.trio()
        self.link(101, 202)
        snapshot = self.service.run_scope(101)
        self.assertTrue(self.service.can_run(101, snapshot["root_id"], b["id"]))
        self.mutate("UPDATE accounts SET enabled=0 WHERE id=?", (b["id"],))
        self.assertFalse(self.service.can_run(101, snapshot["root_id"], b["id"]))
        self.mutate("UPDATE accounts SET enabled=1,last_status='invalid' WHERE id=?", (b["id"],))
        self.assertFalse(self.service.can_run(101, snapshot["root_id"], b["id"]))
        self.mutate("UPDATE accounts SET last_status='',owner_tg_id=NULL WHERE id=?", (b["id"],))
        self.assertFalse(self.service.can_run(101, snapshot["root_id"], b["id"]))
        self.mutate("UPDATE accounts SET owner_tg_id=202 WHERE id=?", (b["id"],))
        self.assertTrue(self.service.can_run(101, snapshot["root_id"], b["id"]))
        self.mutate("UPDATE accounts SET owner_tg_id=202 WHERE id=?", (a["id"],))
        self.assertFalse(self.service.can_run(101, snapshot["root_id"], a["id"]))
        self.assertEqual(self.service.visible(101), [])
        self.assertEqual(self.service.logs(101), [])
        self.mutate("UPDATE accounts SET owner_tg_id=101 WHERE id=?", (a["id"],))
        self.login(101, "A-second")
        self.assertFalse(self.service.can_run(101, snapshot["root_id"], b["id"]))
        self.assertFalse(self.service.can_run(101, -1, b["id"]))
        self.assertFalse(self.service.can_run(101, a["id"], "bad"))

    def test_inactive_or_unowned_target_does_not_consume_code(self):
        a, b, _ = self.trio()
        issued = self.service.issue_code(202)
        for change in ("enabled=0", "last_status='invalid'", "owner_tg_id=NULL"):
            with self.subTest(change=change):
                self.mutate("UPDATE accounts SET enabled=1,last_status='',owner_tg_id=202 WHERE id=?", (b["id"],))
                self.mutate("UPDATE accounts SET " + change + " WHERE id=?", (b["id"],))
                with self.assertRaises(DomainError):
                    self.service.redeem(101, "B-200", issued["code"])
                self.assertEqual(self.count("link_codes"), 1)
        self.mutate("UPDATE accounts SET enabled=1,last_status='',owner_tg_id=202 WHERE id=?", (b["id"],))
        self.mutate("UPDATE accounts SET enabled=0 WHERE id=?", (a["id"],))
        with self.assertRaises(DomainError):
            self.service.redeem(101, "B-200", issued["code"])
        self.assertEqual(self.count("link_codes"), 1)

    def test_redeem_rechecks_source_owner_in_transaction(self):
        a, b, _ = self.trio()
        issued = self.service.issue_code(202)
        self.store.next_write = lambda conn: conn.execute(
            "UPDATE accounts SET owner_tg_id=303 WHERE id=?", (a["id"],))
        with self.assertRaises(DomainError):
            self.service.redeem(101, "B-200", issued["code"])
        self.assertEqual(self.count("link_codes"), 1)
        self.assertEqual(self.count("account_links"), 0)
        self.assertEqual(self.query("SELECT owner_tg_id FROM accounts WHERE id=?", (a["id"],))[0]["owner_tg_id"], 101)

    def test_read_payloads_hide_credential_even_in_free_text(self):
        a = self.login(101, "A", "fake-payload-secret-A", name="name fake-payload-secret-A")
        self.mutate("UPDATE accounts SET last_msg='error fake-payload-secret-A' WHERE id=?", (a["id"],))
        self.add_log(a, "failure fake-payload-secret-A")
        for payload in (self.service.active(101), self.service.owned(101), self.service.visible(101),
                        self.service.logs(101), self.service.watchers(a["id"]), self.service.user(101)):
            self.assert_no_credentials(payload, ("fake-payload-secret-A",))
        self.service.begin_login(202, 'A')
        with self.assertRaises(DomainError) as caught:
            self.service.finish_login(202, "fake-payload-secret-A", {"user_id": "A", "telegram_user_id": 202})
        self.assertNotIn("fake-payload-secret-A", str(caught.exception))

    def test_logs_limit_filtering_order_and_no_selection_are_private(self):
        a, b, c = self.trio()
        self.link(101, 202)
        for i in range(18):
            self.add_log((a, b, c)[i % 3], "记录" + str(i))
        actual = self.service.logs(101, 4)
        self.assertEqual([row["message"] for row in actual], ["记录16", "记录15", "记录13", "记录12"])
        self.assertEqual(len(self.service.logs(101)), 12)
        self.assertEqual(self.service.logs(404), [])
        self.assertEqual(self.service.visible(404), [])
        self.assertIsNone(self.service.active(404, required=False))
        with self.assertRaises(DomainError):
            self.service.active(404)
        for limit in (0, -1, "5", True):
            with self.subTest(limit=limit), self.assertRaises(DomainError):
                self.service.logs(101, limit)

    def test_input_ttl_allowed_kinds_clear_and_multiuser_isolation(self):
        with patch("accounts.time.time", return_value=1000):
            self.service.set_input(101, "link")
            self.service.set_input(202, "time", ttl=10)
            self.assertEqual(self.service.input_state(101), "link")
            self.assertEqual(self.service.input_state(202), "time")
            self.assertEqual(self.service.user(101)["input_until"], 1600)
        with patch("accounts.time.time", return_value=1010):
            self.assertEqual(self.service.input_state(202), "")
            self.assertEqual(self.service.input_state(101), "link")
        with patch("accounts.time.time", return_value=1600):
            self.assertEqual(self.service.input_state(101), "")
        for kind, ttl in (("invalid", 600), ("", 1), ("link", 0), ("link", True), ("time", 2**64)):
            with self.subTest(kind=kind, ttl=ttl), self.assertRaises(DomainError):
                self.service.set_input(101, kind, ttl)
        self.service.set_input(101, "time")
        self.service.clear_input(101)
        self.assertEqual((self.service.input_state(101), self.service.user(101)["input_until"]), ("", 0))
        self.assertEqual(self.service.input_state(404), "")

    def test_watchers_owner_plus_direct_sources_not_recursive_and_deduplicate(self):
        a, b, c = self.trio()
        self.link(101, 202)
        self.link(202, 303)
        self.service.set_preferences(202, notify_success=0, notify_fail=1, notify_already=1)
        self.assertEqual([row["tg_id"] for row in self.service.watchers(c["id"])], [202, 303])
        self.assertEqual([row["tg_id"] for row in self.service.watchers(b["id"])], [101, 202])
        prefs = self.service.watchers(c["id"])[0]
        self.assertEqual([prefs[key] for key in ("notify_success", "notify_fail", "notify_already")], [0, 1, 1])
        for row in self.service.watchers(c["id"]):
            for key in ("notify_success", "notify_fail", "notify_already"):
                self.assertIs(type(row[key]), int)
        second = self.login(202, "B-second")
        self.link(202, 303)
        self.assertEqual([row["tg_id"] for row in self.service.watchers(c["id"])], [202, 303])
        self.service.toggle_link(202, b["id"], c["id"])
        self.assertEqual([row["tg_id"] for row in self.service.watchers(c["id"])], [202, 303])
        self.service.unlink(303, second["id"], c["id"])
        self.assertEqual([row["tg_id"] for row in self.service.watchers(c["id"])], [303])
        self.assertEqual(self.service.watchers(99999), [])

    def test_invalid_redemption_rate_limit_is_per_user_and_expires(self):
        _, _, c = self.trio()
        issued = self.service.issue_code(202)
        with patch("accounts.time.time", return_value=1000):
            for i in range(10):
                with self.assertRaises(DomainError):
                    self.service.redeem(101, "B-200", "wrong" + str(i))
            with self.assertRaisesRegex(DomainError, "尝试过多"):
                self.service.redeem(101, "B-200", issued["code"])
            self.assertEqual(self.count("link_codes"), 1)
            self.service.redeem(303, "B-200", issued["code"])
        replacement = self.service.issue_code(202)
        with patch("accounts.time.time", return_value=1060):
            self.service.redeem(101, "B-200", replacement["code"])
        self.assertEqual(self.count("account_links"), 0)
        self.assertEqual(self.count('link_requests'), 2)
        with patch('accounts.time.time', return_value=1060):
            for row in self.query('SELECT source_id,target_id FROM link_requests'):
                self.service.review_link(202, row['source_id'], row['target_id'], True)
        self.assertEqual(self.count('account_links'), 2)

    def test_cascade_delete_clears_active_links_and_codes_not_legacy_logs(self):
        a, b, _ = self.trio()
        self.link(101, 202)
        self.service.issue_code(202)
        self.add_log(b)
        self.mutate("DELETE FROM accounts WHERE id=?", (b["id"],))
        self.assertEqual(self.count("account_links"), 0)
        self.assertEqual(self.count("link_codes"), 0)
        self.assertIsNone(self.service.user(202)["active_account_id"])
        self.assertFalse(self.service.can_run(101, a["id"], b["id"]))
        self.assertEqual(self.service.logs(101), [])
        self.assertEqual(self.count("logs"), 1)

    def test_all_state_transitions_use_supplied_write_context(self):
        before = self.store.writes
        self.service.ensure_user(101)
        self.service.begin_login(101, 'A')
        self.service.set_preferences(101, notify_already=True)
        self.service.set_input(101, "link")
        self.service.clear_input(101)
        self.service.cancel_login(101)
        self.assertEqual(self.store.writes - before, 6)
        before = self.store.writes
        self.service.user(101)
        self.service.login_pending(101)
        self.service.input_state(101)
        self.service.visible(101)
        self.service.logs(101)
        self.service.links(101)
        self.assertEqual(self.store.writes, before)


    def test_admin_overview_sees_all_accounts_with_balances_but_users_do_not(self):
        # 历史管理投影包含余额；个人投影遵守其可见范围。
        prof = {'_user_id': 'eA1', '_display': '甲', 'telegram_user_id': 101, '_carrot': 256}
        self.service.login_with_token(101, 'abcdefgh12345678', prof, 'https://emos.best')
        prof2 = {'_user_id': 'eB2', '_display': '乙', 'telegram_user_id': 202, '_carrot': 7}
        self.service.login_with_token(202, 'abcdefgh87654321', prof2, 'https://emos.best')

        overview = {row['emos_id']: row for row in self.service.admin_overview()}
        self.assertEqual(set(overview), {'eA1', 'eB2'})
        self.assertEqual(overview['eA1']['carrot'], 256)
        self.assertEqual(overview['eB2']['carrot'], 7)
        self.assertEqual(overview['eB2']['owner_tg_id'], 202)
        # 普通视角互不可见
        self.assertEqual([r['emos_id'] for r in self.service.visible(101)], ['eA1'])
        self.assertEqual([r['emos_id'] for r in self.service.visible(202)], ['eB2'])
        # 密钥永远不出现在任何返回里
        for row in overview.values():
            self.assertNotIn('token', row)

    def test_login_with_token_creates_user_row_on_first_use(self):
        # 历史 token 模式首次建用户后才能持久化当前选择。
        profile = {'user_id': 'eFirst', '_user_id': 'eFirst', '_display': '首个',
                   'telegram_user_id': 101, '_carrot': 5}
        self.service.login_with_token(101, 'abcdefgh12345678', profile, 'https://emos.best')
        active = self.service.active(101)
        self.assertEqual(active['emos_id'], 'eFirst')
        self.assertEqual(self.service.user(101)['active_account_id'], active['id'])

    def test_login_with_token_stores_official_carrot_balance(self):
        profile = {'user_id': 'eCarrot', '_user_id': 'eCarrot', '_display': '萝卜',
                   'telegram_user_id': 101, '_carrot': 128}
        row = self.service.login_with_token(101, 'abcdefgh12345678', profile, 'https://emos.best')
        self.assertEqual(row['carrot'], 128)
        self.assertTrue(row['carrot_at'])
        self.assertEqual(self.service.active(101)['carrot'], 128)

    def test_carrot_unknown_does_not_wipe_the_stored_balance(self):
        # 未知余额不覆盖已知缓存，也不伪装为零。
        profile = {'user_id': 'eKeep', '_user_id': 'eKeep', '_display': '保持',
                   'telegram_user_id': 101, '_carrot': 128}
        self.service.login_with_token(101, 'abcdefgh12345678', profile, 'https://emos.best')
        again = dict(profile, _carrot=None)
        self.service.login_with_token(101, 'abcdefgh12345678', again, 'https://emos.best')
        self.assertEqual(self.service.active(101)['carrot'], 128)

    def test_token_login_accepts_independent_telegram_binding(self):
        profile = {'user_id': 'eOther', '_user_id': 'eOther', '_display': '独立账号',
                   'telegram_user_id': 202, '_carrot': 1}
        account = self.service.login_with_token(101, 'abcdefgh12345678', profile, 'https://emos.best')
        self.assertEqual(account['emos_id'], 'eOther')
        self.assertEqual([row['emos_id'] for row in self.service.owned(101)], ['eOther'])
        self.assertEqual(self.service.owned(202), [])

    def test_token_login_accepts_account_without_telegram_binding(self):
        profile = {'user_id': 'eNoTg', '_user_id': 'eNoTg', '_display': '未绑', '_carrot': 1}
        account = self.service.login_with_token(101, 'abcdefgh12345678', profile, 'https://emos.best')
        self.assertEqual(account['emos_id'], 'eNoTg')
        self.assertEqual(self.service.active(101)['id'], account['id'])

    def test_token_login_cannot_take_over_another_owners_account(self):
        profile = {'user_id': 'eMine', '_user_id': 'eMine', '_display': '我的',
                   'telegram_user_id': 101, '_carrot': 1}
        self.service.login_with_token(101, 'abcdefgh12345678', profile, 'https://emos.best')
        stolen = dict(profile, telegram_user_id=202)
        with self.assertRaises(DomainError):
            self.service.login_with_token(202, 'abcdefgh12345678', stolen, 'https://emos.best')


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="emoskata-migration-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = TempStore(Path(self.temp.name) / "legacy.sqlite3")
        self.service = AccountService(self.store.db, self.store.write, self.store.cfg)
        with closing(self.store.db()) as conn:
            conn.executescript(OLD_SCHEMA)
            conn.execute("INSERT INTO accounts(name,token,enabled,last_status,last_msg,last_carrot,last_run,"
                         "last_day,retry_count,next_run_at,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("旧账号", "fake-old-legacy", 0, "invalid", "旧状态", 7, "旧运行", "旧日期", 3, 999, "旧创建"))
            conn.execute("INSERT INTO accounts(name,token) VALUES('另一个旧账号','fake-unclaimed')")
            conn.execute("INSERT INTO logs(account_id,name,ts,status,message,carrot) "
                         "VALUES(1,'旧账号','2025-01-01','signed','历史记录',7)")

    def rows(self, sql, params=()):
        with closing(self.store.db()) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def migrate(self):
        with self.store.write() as conn:
            AccountService.init_schema(conn)

    def test_old_columns_and_history_preserved_and_no_implicit_ownership(self):
        before = self.rows("SELECT * FROM accounts ORDER BY id")
        logs = self.rows("SELECT * FROM logs")
        self.migrate()
        self.migrate()
        after = self.rows("SELECT * FROM accounts ORDER BY id")
        for old, new in zip(before, after):
            self.assertEqual({key: new[key] for key in old}, old)
            self.assertIsNone(new["emos_id"])
            self.assertIsNone(new["owner_tg_id"])
            self.assertEqual((new["base_url"], new["auto_sign"], new["sign_time"]), ("", 1, ""))
        self.assertEqual(self.rows("SELECT * FROM logs"), logs)
        self.service.ensure_user(101)
        self.assertEqual(self.service.owned(101), [])
        self.assertEqual(self.service.visible(101), [])
        self.assertEqual(self.service.logs(101), [])
        self.assertIsNone(self.service.active(101, required=False))
        with self.assertRaises(DomainError):
            self.service.run_scope(101)
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def test_verified_exact_legacy_token_claims_same_row_without_deleting_history(self):
        self.migrate()
        self.service.begin_login(101, 'OFFICIAL')
        account = self.service.finish_login(101, "fake-old-legacy",
                                            {"user_id": "OFFICIAL", "telegram_user_id": 101, "name": "新名字"})
        self.assertEqual(account["id"], 1)
        self.assertEqual(len(self.rows("SELECT * FROM accounts")), 2)
        legacy = self.rows("SELECT * FROM accounts WHERE id=1")[0]
        self.assertEqual((legacy["owner_tg_id"], legacy["emos_id"], legacy["enabled"], legacy["retry_count"],
                          legacy["next_run_at"]), (101, "OFFICIAL", 1, 0, 0))
        self.assertEqual((legacy["last_carrot"], legacy["last_run"], legacy["last_day"], legacy["created"]),
                         (7, "旧运行", "旧日期", "旧创建"))
        self.assertEqual(self.rows("SELECT owner_tg_id FROM accounts WHERE id=2")[0]["owner_tg_id"], None)
        self.assertEqual(self.service.logs(101)[0]["message"], "历史记录")

    def test_older_table_missing_retry_and_other_columns_is_additive(self):
        other = TempStore(Path(self.temp.name) / "minimal.sqlite3")
        with closing(other.db()) as conn:
            conn.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY,name TEXT NOT NULL,token TEXT NOT NULL)")
            conn.execute("INSERT INTO accounts VALUES(9,'古老账号','fake-minimal')")
        with other.write() as conn:
            AccountService.init_schema(conn)
        with closing(other.db()) as conn:
            row = dict(conn.execute("SELECT * FROM accounts").fetchone())
            self.assertEqual((row["id"], row["name"], row["token"]), (9, "古老账号", "fake-minimal"))
            self.assertEqual((row["retry_count"], row["next_run_at"]), (0, 0))
            self.assertIsNone(row["owner_tg_id"])

    def test_every_new_column_individually_missing_is_added_without_resetting_existing(self):
        declarations = {
            "emos_id": "TEXT", "owner_tg_id": "INTEGER", "base_url": "TEXT DEFAULT ''",
            "auto_sign": "INTEGER DEFAULT 1", "sign_time": "TEXT DEFAULT ''",
        }
        sample = {"emos_id": "kept-id", "owner_tg_id": 777, "base_url": "https://example.invalid",
                  "auto_sign": 0, "sign_time": "17:22"}
        for missing in declarations:
            with self.subTest(missing=missing):
                other = TempStore(Path(self.temp.name) / (missing + ".sqlite3"))
                with closing(other.db()) as conn:
                    conn.executescript(OLD_SCHEMA)
                    conn.execute("INSERT INTO accounts(name,token,created) VALUES('保留','fake-kept','保留时间')")
                    for key, ddl in declarations.items():
                        if key != missing:
                            conn.execute("ALTER TABLE accounts ADD COLUMN " + key + " " + ddl)
                            conn.execute("UPDATE accounts SET " + key + "=?", (sample[key],))
                with other.write() as conn:
                    AccountService.init_schema(conn)
                with closing(other.db()) as conn:
                    row = dict(conn.execute("SELECT * FROM accounts").fetchone())
                    self.assertEqual(row["created"], "保留时间")
                    for key in declarations:
                        self.assertIn(key, row)
                        if key != missing:
                            self.assertEqual(row[key], sample[key])
                    if missing == "owner_tg_id":
                        self.assertIsNone(row["owner_tg_id"])

    def test_partial_unique_official_id_allows_null_empty_but_blocks_duplicate(self):
        self.migrate()
        with self.store.write() as conn:
            conn.execute("INSERT INTO accounts(name,token,emos_id) VALUES('空','fake-empty-one','')")
            conn.execute("INSERT INTO accounts(name,token,emos_id) VALUES('空','fake-empty-two','')")
            conn.execute("INSERT INTO accounts(name,token,emos_id) VALUES('官方','fake-id','ID-ONE')")
        before = self.rows("SELECT * FROM accounts")
        with self.assertRaises(sqlite3.IntegrityError), self.store.write() as conn:
            conn.execute("INSERT INTO accounts(name,token,emos_id) VALUES('重复','fake-dup','ID-ONE')")
        self.assertEqual(self.rows("SELECT * FROM accounts"), before)
        index = next(row for row in self.rows("PRAGMA index_list(accounts)") if row["name"] == "ux_accounts_emos_id")
        self.assertEqual((index["unique"], index["partial"]), (1, 1))

    def test_migration_duplicate_official_ids_keeps_earliest_and_clears_the_rest(self):
        # 重复官方 ID 迁移仅清除重复身份；原 Token 和历史记录保留。
        with self.store.write() as conn:
            conn.execute("ALTER TABLE accounts ADD COLUMN emos_id TEXT")
            conn.execute("UPDATE accounts SET emos_id='DUPLICATE'")
        tokens = {row["id"]: row["token"] for row in self.rows("SELECT id,token FROM accounts")}
        self.migrate()
        rows = self.rows("SELECT id,emos_id,owner_tg_id,token FROM accounts ORDER BY id")
        self.assertEqual([row["emos_id"] for row in rows], ["DUPLICATE", None])
        self.assertEqual([row["token"] for row in rows], [tokens[1], tokens[2]])
        self.assertIn("owner_tg_id", {row["name"] for row in self.rows("PRAGMA table_info(accounts)")})
        index = next(row for row in self.rows("PRAGMA index_list(accounts)")
                     if row["name"] == "ux_accounts_emos_id")
        self.assertEqual((index["unique"], index["partial"]), (1, 1))
        # 迁移必须幂等：再跑一次不能改动任何东西
        self.migrate()
        self.assertEqual([row["emos_id"] for row in self.rows(
            "SELECT emos_id FROM accounts ORDER BY id")], ["DUPLICATE", None])

    def test_duplicate_legacy_tokens_claim_one_row_not_every_unowned_row(self):
        self.migrate()
        with self.store.write() as conn:
            conn.execute("INSERT INTO accounts(name,token,created) VALUES('旧重复','fake-old-legacy','重复历史')")
        self.service.begin_login(101, 'OFFICIAL')
        account = self.service.finish_login(101, "fake-old-legacy", {"user_id": "OFFICIAL", "telegram_user_id": 101})
        self.assertEqual(account["id"], 1)
        self.assertEqual(len(self.rows("SELECT * FROM accounts")), 3)
        self.assertEqual(len(self.service.owned(101)), 1)
        self.assertIsNone(self.rows("SELECT owner_tg_id FROM accounts WHERE id=3")[0]["owner_tg_id"])
        self.assertEqual(self.rows("SELECT created FROM accounts WHERE id=3")[0]["created"], "重复历史")

    def test_wrong_same_named_partial_index_is_rejected_without_dropping_it(self):
        with self.store.write() as conn:
            conn.execute("ALTER TABLE accounts ADD COLUMN emos_id TEXT")
            conn.execute("CREATE UNIQUE INDEX ux_accounts_emos_id ON accounts(name) WHERE enabled=1")
        before = self.rows("SELECT * FROM accounts")
        with self.assertRaisesRegex(DomainError, "索引不兼容"):
            self.migrate()
        self.assertEqual(self.rows("SELECT * FROM accounts"), before)
        index = self.rows("PRAGMA index_info(ux_accounts_emos_id)")
        self.assertEqual([row["name"] for row in index], ["name"])
        self.assertNotIn("owner_tg_id", {row["name"] for row in self.rows("PRAGMA table_info(accounts)")})

    def test_migration_preserves_caller_transaction_and_rollback(self):
        with self.assertRaisesRegex(RuntimeError, "abort migration"), self.store.write() as conn:
            AccountService.init_schema(conn)
            conn.execute("UPDATE accounts SET name='不能提交'")
            raise RuntimeError("abort migration")
        columns = {row["name"] for row in self.rows("PRAGMA table_info(accounts)")}
        self.assertNotIn("emos_id", columns)
        self.assertEqual(self.rows("SELECT name FROM accounts WHERE id=1")[0]["name"], "旧账号")
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='tg_users'"), [])


def probes():
    """先输出不含密钥的实际观测，再运行 unittest 断言。
    """
    with tempfile.TemporaryDirectory(prefix="emoskata-account-probe-") as temp:
        store = TempStore(Path(temp) / "probe.sqlite3")
        service = AccountService(store.db, store.write, store.cfg)
        with store.write() as conn:
            AccountService.init_schema(conn)
        accounts = {}
        for actor, official in ((101, "A"), (202, "B"), (303, "C")):
            service.begin_login(actor, official)
            accounts[official] = service.finish_login(
                actor, "probe-fake-" + official, {"user_id": official, "telegram_user_id": actor, "name": official})
        code = service.issue_code(202)
        with closing(store.db()) as conn:
            stored = dict(conn.execute("SELECT * FROM link_codes").fetchone())
        print("PROBE code_storage=" + json.dumps({"hash_matches": stored["code_hash"] == hashlib.sha256(code["code"].encode()).hexdigest(),
                                                 "hash_length": len(stored["code_hash"]),
                                                 "raw_code_present": code["code"] in json.dumps(stored)}, sort_keys=True), flush=True)
        wrong = "accepted"
        try:
            service.redeem(101, "C", code["code"])
        except DomainError:
            wrong = "DomainError"
        with closing(store.db()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM link_codes").fetchone()[0]
        print("PROBE wrong_id=" + json.dumps({"outcome": wrong, "remaining_codes": count}, sort_keys=True), flush=True)
        service.redeem(101, "B", code["code"])
        print('PROBE before_confirmation=' + json.dumps(service.run_scope(101)), flush=True)
        service.review_link(202, accounts['A']['id'], accounts['B']['id'], True)
        code_c = service.issue_code(303)
        service.redeem(202, "C", code_c["code"])
        service.review_link(303, accounts['B']['id'], accounts['C']['id'], True)
        with store.write() as conn:
            for account in accounts.values():
                conn.execute("INSERT INTO logs(account_id,name,ts,status,message) VALUES(?,?,'probe','signed','probe')",
                             (account["id"], account["name"]))
        print("PROBE one_hop=" + json.dumps({"scope_A": service.run_scope(101),
                                            "A_can_C": service.can_run(101, accounts["A"]["id"], accounts["C"]["id"]),
                                            "B_can_A": service.can_run(202, accounts["B"]["id"], accounts["A"]["id"]),
                                            "watchers_C": [row["tg_id"] for row in service.watchers(accounts["C"]["id"])]}, sort_keys=True), flush=True)
        service.unlink(202, accounts["A"]["id"], accounts["B"]["id"])
        print("PROBE after_revoke=" + json.dumps({"A_can_B": service.can_run(101, accounts["A"]["id"], accounts["B"]["id"]),
                                                 "visible_A": [row["id"] for row in service.visible(101)],
                                                 "logs_A": [row["account_id"] for row in service.logs(101)]}, sort_keys=True), flush=True)
        shared = TempStore(store.path)
        service2 = AccountService(shared.db, shared.write, shared.cfg)
        race_code = service.issue_code(202)
        barrier = threading.Barrier(3)
        results = queue.Queue()

        def race(service, actor):
            try:
                barrier.wait(timeout=10)
                service.redeem(actor, "B", race_code["code"])
                results.put("ok")
            except DomainError:
                results.put("denied")
            except BaseException as exc:
                results.put(type(exc).__name__)

        threads = [threading.Thread(target=race, args=(service, 101)),
                   threading.Thread(target=race, args=(service2, 303))]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("probe race did not finish")
        print("PROBE concurrent=" + json.dumps(sorted([results.get_nowait(), results.get_nowait()])), flush=True)


if __name__ == "__main__":
    probes()
    unittest.main()
