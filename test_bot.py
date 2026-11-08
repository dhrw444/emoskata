"""Python 3.10+ / standard library. Run: python3 -m unittest -v test_bot.
All Telegram/Emos traffic is fake. Settings checkpoints use a temporary SQLite DB.
FakeAccounts explicitly models the contract; no production service fallback exists.
Synchronous handlers make UI tests deterministic; threaded tests use Events+join_jobs.
"""

from collections import deque
from contextlib import redirect_stdout
import copy
import io
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import bot
from accounts import DomainError
from emos import EmosError


class FakeAccounts:
    def __init__(self):
        self.users, self.rows, self.owners, self.tokens = {}, {}, {}, {}
        self.grants, self.codes, self.records, self.pending = {}, {}, [], {}
        self.calls = []
        self.serial = 0

    def ensure_user(self, tg_id, chat_id=None):
        if type(tg_id) is not int or tg_id <= 0 or chat_id not in (None, tg_id):
            raise DomainError("仅支持本人私聊")
        self.users.setdefault(tg_id, {"tg_id": tg_id, "chat_id": tg_id, "active_account_id": None,
                                     "login_until": 0, "login_emos_id": "", "login_revision": 0, "input_kind": "", "input_until": 0,
                                     "notify_success": 1, "notify_fail": 1, "notify_already": 0})
        return dict(self.users[tg_id])

    def user(self, tg_id):
        return dict(self.users[tg_id]) if tg_id in self.users else None

    def add(self, tg_id, official, name=None):
        self.ensure_user(tg_id)
        aid = max(self.rows, default=0) + 1
        self.rows[aid] = {"id": aid, "emos_id": official, "name": name or official,
                          "enabled": 1, "auto_sign": 1, "sign_time": "08:00", "sign_mode": "normal", "last_status": "",
                          "last_msg": "", "last_run": "", "last_carrot": None}
        self.owners[aid] = tg_id
        self.users[tg_id]["active_account_id"] = aid
        return dict(self.rows[aid])

    def begin_login(self, tg_id, official_id, expected_root=None):
        self.ensure_user(tg_id)
        active=self.active(tg_id, required=False)
        if expected_root is not None and (active['id'] if active else 0) != expected_root:
            raise DomainError('当前账号已切换')
        self.users[tg_id].update(login_until=int(time.time())+600, login_emos_id=str(official_id),
                                 login_revision=self.users[tg_id]['login_revision']+1)
        self.calls.append(("begin_login", tg_id))
        return self.user(tg_id)

    def login_pending(self, tg_id):
        return self.users.get(tg_id, {}).get("login_until", 0) > time.time()

    def cancel_login(self, tg_id):
        self.users[tg_id].update(login_until=0, login_emos_id='', login_revision=self.users[tg_id]['login_revision']+1)

    def finish_login(self, tg_id, token, profile, base_url="", expected_root=None, expected_login_until=None,
                     expected_login_revision=None):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        if expected_login_until is not None and self.users[tg_id]['login_until'] != expected_login_until:
            raise DomainError('登录授权请求已变化')
        if expected_login_revision is not None and self.users[tg_id]['login_revision'] != expected_login_revision:
            raise DomainError('登录授权请求已变化')
        self.calls.append(("finish_login", tg_id, base_url))
        if not self.login_pending(tg_id):
            raise DomainError("登录请求已过期")
        official = profile.get("user_id") or profile.get("_user_id")
        if not official:
            raise DomainError("缺少官方 ID")
        if str(official) != self.users[tg_id]['login_emos_id']:
            raise DomainError('本人填写与回传官方ID不一致')
        if profile.get('telegram_user_id') in (None, ''):
            raise DomainError('账号未绑定本人 Telegram')
        if int(profile['telegram_user_id']) != tg_id:
            raise DomainError("官方授权与当前 Telegram 用户不匹配")
        aid = next((aid for aid, row in self.rows.items() if row["emos_id"] == str(official)), None)
        if aid is not None and self.owners[aid] != tg_id:
            raise DomainError("官方账号已归属其他用户，不能接管")
        row = self.add(tg_id, str(official), profile.get("_display") or profile.get("name")) if aid is None else dict(self.rows[aid])
        self.tokens[row["id"]] = token
        self.users[tg_id]["active_account_id"] = row["id"]
        self.cancel_login(tg_id)
        return row

    def login_with_token(self, tg_id, token, profile, base_url=""):
        """/token 直连路径的替身。语义与生产一致：官方 TG 绑定必须等于当前用户。"""
        if not isinstance(profile, dict):
            raise DomainError("登录资料无效，请重新授权")
        official = profile.get("user_id") or profile.get("_user_id")
        if not official:
            raise DomainError("缺少官方 ID")
        if profile.get("telegram_user_id") in (None, ""):
            raise DomainError("官方账号尚未绑定 Telegram")
        if int(profile["telegram_user_id"]) != tg_id:
            raise DomainError("该密钥属于另一个 Telegram 用户，不能加入你的账号列表")
        aid = next((aid for aid, row in self.rows.items() if row["emos_id"] == str(official)), None)
        if aid is not None and self.owners[aid] != tg_id:
            raise DomainError("该官方账号已归属其他用户，不能加入")
        self.calls.append(("login_with_token", tg_id, base_url))
        row = self.add(tg_id, str(official), profile.get("_display")) if aid is None else dict(self.rows[aid])
        self.tokens[row["id"]] = token
        self.users[tg_id]["active_account_id"] = row["id"]
        return row

    def owned(self, tg_id):
        return [dict(row) for aid, row in self.rows.items() if self.owners[aid] == tg_id]

    def active(self, tg_id, required=True):
        aid = self.users.get(tg_id, {}).get("active_account_id")
        row = self.rows.get(aid) if self.owners.get(aid) == tg_id else None
        if row is None and required:
            raise DomainError("请先登录并选择自己的账号")
        return dict(row) if row is not None else None

    def switch(self, tg_id, local_account_id, expected_root=None):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        self.calls.append(("switch", tg_id, local_account_id))
        if self.owners.get(local_account_id) != tg_id:
            raise DomainError("不能选择不属于自己的账号")
        self.users[tg_id]["active_account_id"] = local_account_id
        return self.active(tg_id)

    def set_account(self, tg_id, expected_root=None, **fields):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        if set(fields) - {"auto_sign", "sign_time", "sign_mode"}:
            raise DomainError("设置字段无效")
        if 'sign_mode' in fields and fields['sign_mode'] not in ('normal', 'random'):
            raise DomainError('签到模式必须为普通或随机字')
        row = self.active(tg_id)
        self.calls.append(("set_account", tg_id, dict(fields)))
        self.rows[row["id"]].update(fields)
        return self.active(tg_id)

    def set_preferences(self, tg_id, **fields):
        if set(fields) - {"notify_success", "notify_fail", "notify_already"}:
            raise DomainError("设置字段无效")
        self.calls.append(("set_preferences", tg_id, dict(fields)))
        self.users[tg_id].update(fields)
        return self.user(tg_id)

    def issue_code(self, tg_id, expected_root=None):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        row = self.active(tg_id)
        if not row["enabled"]:
            raise DomainError("账号已失效")
        self.serial += 1
        code = "once_secret_%012d" % self.serial
        self.codes[row["emos_id"]] = (code, int(time.time()) + 600, tg_id)
        self.calls.append(("issue_code", tg_id, row["id"]))
        return {"target_emos_id": row["emos_id"], "code": code, "expires_at": int(time.time()) + 600}

    def redeem(self, tg_id, target_emos_id, code, expected_root=None):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        source = self.active(tg_id)
        target = next((row for row in self.rows.values() if row["emos_id"] == target_emos_id), None)
        issued = self.codes.get(target_emos_id)
        if not target or not issued or issued[0] != code or issued[1] <= time.time() or self.owners[target["id"]] != issued[2]:
            raise DomainError("关联码无效、过期或已使用")
        if target["id"] == source["id"]:
            raise DomainError("不能关联自己")
        del self.codes[target_emos_id]
        self.pending[source['id'], target['id']] = int(time.time()) + 600
        self.calls.append(("redeem", tg_id, source["id"], target["id"]))
        return dict(target, pending=True)

    def pending_links(self, tg_id):
        aid = self.active(tg_id)['id']
        result = {'incoming': [], 'outgoing': []}
        for (source, target), expiry in self.pending.items():
            if expiry <= time.time() or aid not in (source, target):
                continue
            result['outgoing' if aid == source else 'incoming'].append({
                'source_id': source, 'target_id': target, 'expires_at': expiry,
                'source_emos_id': self.rows[source]['emos_id'], 'source_name': self.rows[source]['name'],
                'target_emos_id': self.rows[target]['emos_id'], 'target_name': self.rows[target]['name']})
        return result

    def review_link(self, tg_id, source_id, target_id, approve, expected_root=None):
        root = self.active(tg_id)['id']
        key = source_id, target_id
        if (type(approve) is not bool or self.owners.get(target_id) != tg_id
                or expected_root is not None and root != expected_root
                or self.pending.get(key, 0) <= time.time()):
            raise DomainError('关联申请无效或不属于本人')
        if approve:
            self.grants[key] = True
        del self.pending[key]
        self.calls.append(('review_link', tg_id, source_id, target_id, approve))
        return {'approved': approve, 'source_id': source_id, 'target_id': target_id}

    def withdraw_link(self, tg_id, source_id, target_id, expected_root=None):
        if tg_id not in (self.owners.get(source_id), self.owners.get(target_id)):
            raise DomainError('不能撤回别人的申请')
        if expected_root is not None and self.active(tg_id)['id'] != expected_root:
            raise DomainError('当前账号已切换')
        self.pending.pop((source_id, target_id), None)

    def links(self, tg_id):
        aid = self.active(tg_id)["id"]
        result = {"outgoing": [], "incoming": []}
        for (source, target), enabled in self.grants.items():
            link = {"source_id": source, "target_id": target, "enabled": enabled,
                    "source_name": self.rows[source]["name"], "target_name": self.rows[target]["name"],
                    "source_emos_id": self.rows[source]["emos_id"], "target_emos_id": self.rows[target]["emos_id"]}
            if source == aid:
                result["outgoing"].append(link)
            if target == aid:
                result["incoming"].append(link)
        return result

    def toggle_link(self, tg_id, source_id, target_id, expected_root=None, enabled=None):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        if self.owners.get(source_id) != tg_id or (source_id, target_id) not in self.grants:
            raise DomainError("仅来源所有者可启停关联")
        state = (not self.grants[source_id, target_id]) if enabled is None else bool(enabled)
        self.grants[source_id, target_id] = state
        self.calls.append(("toggle_link", tg_id, source_id, target_id))
        return state

    def unlink(self, tg_id, source_id, target_id, expected_root=None):
        if expected_root is not None:
            selected = self.active(tg_id, required=False)
            if (selected['id'] if selected else 0) != expected_root:
                raise DomainError('当前账号已切换')
        if tg_id not in (self.owners.get(source_id), self.owners.get(target_id)):
            raise DomainError("无权撤销关联")
        self.grants.pop((source_id, target_id), None)
        self.calls.append(("unlink", tg_id, source_id, target_id))

    def run_scope(self, tg_id, include_links=True):
        aid = self.active(tg_id)["id"]
        targets = [target for (source, target), enabled in self.grants.items()
                   if source == aid and enabled and self.rows[target]["enabled"]] if include_links else []
        return {"root_id": aid, "account_ids": [aid] + targets}

    def can_run(self, tg_id, root_id, target_id):
        return self.active(tg_id)["id"] == root_id and target_id in self.run_scope(tg_id)["account_ids"]

    def admin_overview(self):
        return list(self.rows.values())

    def visible(self, tg_id):
        aid = self.active(tg_id)["id"]
        ids = [aid] + [target for source, target in self.grants if source == aid]
        return [dict(self.rows[index]) for index in ids]

    def logs(self, tg_id, limit=15):
        ids = {row["id"] for row in self.visible(tg_id)}
        return [dict(row) for row in self.records if row["account_id"] in ids][:limit]

    def set_input(self, tg_id, kind, ttl=600):
        if kind not in {"link", "time", "login"}:
            raise DomainError("输入类型无效")
        self.users[tg_id].update(input_kind=kind, input_until=int(time.time()) + ttl)
        self.calls.append(("set_input", tg_id, kind, ttl))

    def input_state(self, tg_id):
        user = self.users.get(tg_id, {})
        return user.get("input_kind", "") if user.get("input_until", 0) > time.time() else ""

    def clear_input(self, tg_id):
        self.users[tg_id].update(input_kind="", input_until=0)


class FakeStore:
    def __init__(self):
        self.accounts = FakeAccounts()
        self.temp = tempfile.TemporaryDirectory(prefix="emos-bot-test-")
        self.path = os.path.join(self.temp.name, "settings.db")
        self.lock = threading.Lock()
        self.config = {"base_url": "https://fake-emos.invalid"}
        self.runs = []
        self.run_contents = []
        self.run_hook = None
        self.write_failure = False
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")

    def cfg_all(self):
        with self.lock, sqlite3.connect(self.path) as conn:
            result = dict(self.config)
            result.update(conn.execute("SELECT key,value FROM settings"))
            return result

    def set_setting(self, key, value):
        if self.write_failure:
            raise OSError("test checkpoint unavailable")
        with self.lock, sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def run_user(self, tg_id, include_links=True, expected_root=None, content=None):
        scope = self.accounts.run_scope(tg_id, include_links)
        self.run_contents.append(content)
        if expected_root is not None and scope['root_id'] != expected_root:
            raise DomainError('当前账号已切换')
        self.runs.append((tg_id, include_links, scope["root_id"], list(scope["account_ids"])))
        if self.run_hook:
            return self.run_hook(tg_id, scope)
        return [{"id": aid, "emos_id": self.accounts.rows[aid]["emos_id"],
                 "name": self.accounts.rows[aid]["name"], "status": "signed", "message": "签到成功", "carrot": 3}
                for aid in scope["account_ids"]]

    def db(self):
        raise AssertionError("Bot must never read global account database")

    def close(self):
        self.temp.cleanup()


class FakeTG:
    PROFILE = {"id": 777, "username": "emos_test_bot", "is_bot": True}

    def __init__(self):
        self.actions, self.api_calls, self.polls = [], [], []
        self.me_failures = deque()
        self.updates = deque()
        self.me_calls = 0
        self.fail_send = False
        self.delete_failure = False
        self.on_send = None
        self.on_poll = None

    def enabled(self):
        return True

    def get_me(self):
        self.me_calls += 1
        if self.me_failures:
            failure = self.me_failures.popleft()
            if failure is not None:
                raise failure
        return dict(self.PROFILE)

    def send(self, chat_id, text, keyboard=None):
        self.actions.append(("send", chat_id, text, copy.deepcopy(keyboard)))
        if self.on_send:
            self.on_send(chat_id, text)
        if self.fail_send:
            raise bot.TgError()
        return {"ok": True, "result": {"message_id": len(self.actions)}}

    def edit(self, chat_id, msg_id, text, keyboard=None):
        self.actions.append(("edit", chat_id, text, copy.deepcopy(keyboard), msg_id))
        return {"ok": True, "result": {"message_id": msg_id}}

    def answer(self, cb_id, text=""):
        self.actions.append(("answer", cb_id, text))
        return {"ok": True, "result": True}

    def delete(self, chat_id, msg_id):
        self.actions.append(("delete", chat_id, msg_id))
        if self.delete_failure:
            raise bot.TgError()
        return {"ok": True, "result": True}

    def _api(self, method, payload=None, timeout=45):
        self.api_calls.append((method, copy.deepcopy(payload)))
        if method == "getUpdates":
            self.polls.append(payload["offset"])
            if self.on_poll:
                self.on_poll(len(self.polls))
            if self.updates:
                item = self.updates.popleft()
                if isinstance(item, Exception):
                    raise item
                return item
            return {"ok": True, "result": []}
        return {"ok": True, "result": True}


class ClientFactory:
    def __init__(self):
        self.calls = []
        self.profile_data = {"user_id": "eVerified", "telegram_user_id": 101, "name": "验证用户"}
        self.error = None
        self.hook = None

    def __call__(self, token, base):
        self.calls.append((token, base))
        factory = self
        class Client:
            def profile(self):
                if factory.hook:
                    factory.hook()
                if factory.error:
                    raise factory.error
                return dict(factory.profile_data)
        return Client()


class BotTests(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch.object(socket, "create_connection", side_effect=AssertionError("real network forbidden"))
        self.network_guard.start()
        self.store, self.tg, self.factory = FakeStore(), FakeTG(), ClientFactory()
        self.bots = []
        self.uid = 0
        self.b = self.make_bot()

    def test_allowed_users_empty_means_unrestricted(self):
        # 不填 = 与旧行为一致，谁都能用
        self.store.config.pop('tg_allowed_user_ids', None)
        self.assertEqual(bot._allowed_users(self.store.cfg_all()), set())
        self.assertTrue(bot.allowed_actor(101, self.store.cfg_all()))

    def test_allowed_users_parses_comma_and_space(self):
        self.store.config['tg_allowed_user_ids'] = '101, 202'
        self.assertEqual(bot._allowed_users(self.store.cfg_all()), {101, 202})
        self.store.config['tg_allowed_user_ids'] = '101 202'
        self.assertEqual(bot._allowed_users(self.store.cfg_all()), {101, 202})

    def test_allowed_users_parse_error_fails_closed(self):
        # 配置写坏时不能退化成「谁都能用」——那是把锁砸了
        self.store.config['tg_allowed_user_ids'] = '101,abc'
        self.assertFalse(bot.allowed_actor(101, self.store.cfg_all()))
        self.assertFalse(bot.allowed_actor(202, self.store.cfg_all()))

    def test_allowlist_blocks_user_outside_list(self):
        self.store.config['tg_allowed_user_ids'] = '101'
        self.message('/start', actor=202)
        self.assertNotIn('主菜单', self.text())
        # 名单外的人一条消息都不该发出去，也不该建任何用户状态
        self.assertFalse([a for a in self.tg.actions if 'menu' in str(a)])
        self.assertNotIn(202, self.store.accounts.users)

    def test_allowlist_lets_listed_user_through(self):
        self.store.config['tg_allowed_user_ids'] = '101'
        self.message('/start', actor=101)
        # 新用户先进登录页，这已经证明名单内的人被放行了（名单外的人连这个都收不到）
        self.assertIn('本人官方登录', self.text())
        self.assertIn(101, self.store.accounts.users)

    def test_allowlist_blocks_callbacks_too(self):
        self.store.config['tg_allowed_user_ids'] = '101'
        self.store.accounts.rows[1] = {'id': 1, 'emos_id': 'eA', 'name': 'A', 'enabled': 1,
                                       'owner_tg_id': 101, 'auto_sign': 1, 'sign_time': '08:00'}
        self.callback('menu', actor=202)
        self.assertNotIn('主菜单', self.text())
        answers = [a for a in self.tg.actions if a[0] == 'answer']
        self.assertTrue(answers, '被拒绝的回调必须给出原因')
        self.assertIn('允许名单', answers[-1][2])

    def make_bot(self, synchronous=True, **kwargs):
        instance = bot.Bot("fake-bot-token", self.store, tg=self.tg, client_factory=self.factory,
                           synchronous=synchronous, retry_base=0.001, retry_cap=0.005, **kwargs)
        self.bots.append(instance)
        return instance

    def tearDown(self):
        for instance in self.bots:
            instance.stop()
        for instance in self.bots:
            instance.join(3)
        self.store.close()
        self.network_guard.stop()

    def message(self, text, actor=101, chat=None, chat_type="private", b=None, update_id=None):
        self.uid += 1
        uid = self.uid if update_id is None else update_id
        (b or self.b)._handle({"update_id": uid, "message": {
            "message_id": uid + 1000, "from": {"id": actor, "is_bot": False},
            "chat": {"id": actor if chat is None else chat, "type": chat_type}, "text": text}})
        return uid

    def callback(self, data, actor=101, chat=None, chat_type="private", b=None, cb_id=None):
        self.uid += 1
        (b or self.b)._handle({"update_id": self.uid, "callback_query": {
            "id": cb_id or "cb%d" % self.uid, "data": data, "from": {"id": actor},
            "message": {"message_id": 9000, "chat": {"id": actor if chat is None else chat, "type": chat_type}}}})

    def text(self, chat=None):
        return "\n".join(action[2] for action in self.tg.actions if action[0] in {"send", "edit"}
                         and (chat is None or action[1] == chat))

    def buttons(self):
        return [button for action in self.tg.actions if action[0] in {"send", "edit"}
                for row in (action[3] or []) for button in row]

    def own(self, actor=101, official="eA", name="账号 A"):
        return self.store.accounts.add(actor, official, name)

    def test_official_login_uses_this_users_entered_id_not_admin_or_tg_id(self):
        self.message("/login eVerified")
        urls = [button["url"] for button in self.buttons() if "url" in button]
        self.assertEqual(urls, ["https://t.me/emospg_bot?start=link_eVerified-emos_test_bot"])
        self.assertNotIn("link_101-", urls[0])
        self.assertTrue(self.store.accounts.login_pending(101))
        self.assertLessEqual(self.store.accounts.user(101)["login_until"], int(time.time()) + 600)

    def test_missing_user_id_prompts_personal_input_not_admin_configuration(self):
        self.message("/login")
        self.assertFalse(any(button.get('url','').startswith('https://t.me/emospg_bot') for button in self.buttons()))
        self.assertIn('自己的 emos 用户 ID', self.text())
        self.assertNotIn('EMOS_APP_USER_ID', self.text())
        self.assertFalse(self.store.accounts.login_pending(101))
        self.assertEqual(self.store.accounts.input_state(101), 'login')
        self.message('eVerified')
        self.assertTrue(self.store.accounts.login_pending(101))
        self.assertEqual(self.store.accounts.user(101)['login_emos_id'],'eVerified')

    def test_agreement_verified_saved_deleted_not_echoed(self):
        self.message("/login eVerified")
        self.message("/start emosLinkAgree-11_private-test-secret")
        self.assertEqual(self.factory.calls, [("11_private-test-secret", "https://fake-emos.invalid")])
        self.assertEqual(self.store.accounts.active(101)["emos_id"], "eVerified")
        self.assertFalse(self.store.accounts.login_pending(101))
        self.assertTrue(any(action[0] == "delete" for action in self.tg.actions))
        self.assertNotIn("11_private-test-secret", self.text())

    def test_agreement_requires_pending_without_profile_request(self):
        self.message("/start emosLinkAgree-11_private-test-secret")
        self.assertEqual(self.factory.calls, [])
        self.assertEqual(self.store.accounts.owned(101), [])
        self.assertIn("过期", self.text())

    def test_expired_pending_denies_agreement(self):
        self.message("/login eVerified")
        self.store.accounts.users[101]["login_until"] = int(time.time()) - 1
        self.message("/start emosLinkAgree-secret")
        self.assertFalse(self.factory.calls)

    def test_missing_official_profile_id_denied(self):
        self.factory.profile_data = {"name": "No ID", "telegram_user_id": 101}
        self.message("/login eVerified")
        self.message("/start emosLinkAgree-secret")
        self.assertEqual(self.store.accounts.owned(101), [])
        self.assertIn("缺少账号 ID", self.text())

    def test_profile_tg_mismatch_denied(self):
        self.factory.profile_data["telegram_user_id"] = 202
        self.message("/login eVerified")
        self.message("/start emosLinkAgree-secret")
        self.assertEqual(self.store.accounts.owned(101), [])
        self.assertIn("不匹配", self.text())

    def test_profile_fallback_id_with_verified_tg(self):
        self.factory.profile_data = {"_user_id": "eFallback", "name": "备用", "telegram_user_id": 101}
        self.message("/login eFallback")
        self.message("/start emosLinkAgree-secret")
        self.assertEqual(self.store.accounts.active(101)["emos_id"], "eFallback")

    def test_refusal_id_must_match_actor(self):
        self.message("/login eVerified")
        self.message("/start emosLinkRefuse-202")
        self.assertTrue(self.store.accounts.login_pending(101))
        self.message("/start emosLinkRefuse-101")
        self.assertFalse(self.store.accounts.login_pending(101))

    def test_existing_owner_cannot_be_stolen(self):
        self.own(202, "eVerified", "其他人")
        self.message("/login eVerified")
        self.message("/start emosLinkAgree-secret")
        self.assertEqual(self.store.accounts.owned(101), [])
        self.assertEqual(len(self.store.accounts.owned(202)), 1)

    def test_groups_and_impersonation_denied(self):
        self.message("/login eVerified", chat=-55, chat_type="group")
        self.message("/login eVerified", actor=202, chat=101)
        self.callback("sign:1", actor=202, chat=101)
        self.callback("sign:1", chat=-55, chat_type="supergroup")
        self.assertEqual(self.store.accounts.users, {})
        self.assertEqual(self.store.runs, [])
        self.assertEqual(len([action for action in self.tg.actions if action[0] == "answer"]), 2)

    def test_two_users_independent_no_admin_binding(self):
        self.own(101, "eA", "Alice")
        self.own(202, "eB", "Bob")
        self.message("/accounts")
        self.message("/accounts", actor=202)
        self.assertIn("Alice", self.text(101))
        self.assertNotIn("Bob", self.text(101))
        self.assertNotIn("tg_chat_id", self.store.cfg_all())

    def test_owned_selector_rejects_other_owner(self):
        a = self.own()
        other = self.own(202, "eB")
        self.callback("switch:%d:%d" % (a["id"], other["id"]))
        self.assertEqual(self.store.accounts.active(101)["id"], a["id"])
        self.assertIn("本人", self.text())

    def test_old_root_callbacks_reject_after_switch(self):
        a = self.own()
        b = self.own(101, "eA2", "账号 A2")
        self.store.accounts.switch(101, a["id"])
        self.callback("switch:%d:%d" % (a["id"], b["id"]))
        before = len(self.store.accounts.calls)
        for data in ("sign:%d", "code:%d", "settings:%d", "auto:%d", "login:%d"):
            self.callback(data % a["id"])
        for data in ("accounts:%d:0", "links:%d:0", "logs:%d:0", "guide:link:%d", "guide:time:%d"):
            self.callback(data % a["id"])
        self.assertEqual(len(self.store.accounts.calls), before)
        self.assertEqual(self.store.runs, [])
        self.assertIn("切换", self.text())

    def test_inline_buttons_have_correct_fields_and_limits(self):
        a = self.own()
        target = self.own(202, "eTarget")
        self.store.accounts.grants[a["id"], target["id"]] = True
        for command in ("/menu", "/accounts", "/links", "/settings", "/logs", "/code"):
            self.message(command)
        buttons = self.buttons()
        self.assertTrue(buttons)
        for button in buttons:
            self.assertIn("text", button)
            self.assertEqual(int("url" in button) + int("callback_data" in button), 1)
            self.assertNotIn("callback", button)
            if "callback_data" in button:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)
        self.assertIn("pref:notify_success", [button.get("callback_data") for button in buttons])

    def test_generate_code_only_issuer_and_valid_link_roundtrip(self):
        self.own(101, "eTarget")
        self.own(202, "eSource")
        self.message("/code")
        code = self.store.accounts.codes["eTarget"][0]
        self.assertIn(code, self.text(101))
        self.assertNotIn(code, self.text(202))
        urls = [button["url"] for button in self.buttons() if "url" in button]
        payload = urls[-1].split("start=", 1)[1]
        self.assertRegex(payload, r"^[A-Za-z0-9_-]+$")
        self.assertLessEqual(len(payload), 64)
        self.assertEqual(bot.Bot._association_args(payload), ("eTarget", code))
        self.message("/start " + payload, actor=202)
        self.assertFalse(self.store.accounts.grants)
        self.callback('review:1:2:1:1', actor=101)
        self.assertTrue(self.store.accounts.grants[2, 1])
        self.assertNotIn(code, self.text(202))
        self.assertTrue(any(action[:2] == ("delete", 202) for action in self.tg.actions))

    def test_id_and_code_direct_import_one_use_and_message_deleted(self):
        a = self.own()
        b = self.own(202, "eB")
        issued = self.store.accounts.issue_code(202)
        self.message("/link eB")
        self.assertFalse(self.store.accounts.grants)
        self.message("/link eB " + issued["code"])
        self.assertFalse(self.store.accounts.grants)
        self.callback('review:%d:%d:%d:1' % (b['id'], a['id'], b['id']), actor=202)
        self.assertEqual(self.store.accounts.grants, {(a["id"], b["id"]): True})
        self.message("/link eB " + issued["code"])
        self.assertEqual(len([call for call in self.store.accounts.calls if call[0] == "redeem"]), 1)
        self.assertNotIn(issued["code"], self.text())
        self.assertTrue(any(action[0] == "delete" for action in self.tg.actions))

    def test_guided_input_expires_and_never_uses_new_root(self):
        a = self.own()
        b = self.own(101, "eA2")
        self.store.accounts.switch(101, a["id"])
        self.message("/time")
        self.store.accounts.users[101]["input_until"] = int(time.time()) - 1
        self.message("12:00")
        self.assertEqual(self.store.accounts.rows[a["id"]]["sign_time"], "08:00")
        self.message("/time")
        self.store.accounts.switch(101, b["id"])
        self.message("13:00")
        self.assertEqual(self.store.accounts.rows[b["id"]]["sign_time"], "08:00")

    def test_guided_link_input_redeems_and_clears(self):
        a = self.own()
        target = self.own(202, "eTarget")
        code = self.store.accounts.issue_code(202)["code"]
        self.callback("guide:link:%d" % a["id"])
        self.message("eTarget " + code)
        self.assertFalse(self.store.accounts.grants)
        self.callback('review:%d:%d:%d:1' % (target['id'], a['id'], target['id']), actor=202)
        self.assertTrue(self.store.accounts.grants[a["id"], target["id"]])
        self.assertEqual(self.store.accounts.input_state(101), "")
        self.assertNotIn(code, self.text())

    def test_input_restart_fails_closed(self):
        self.own()
        self.message("/time")
        newer = self.make_bot()
        self.message("14:00", b=newer)
        self.assertEqual(self.store.accounts.active(101)["sign_time"], "08:00")
        self.assertIn("重启", self.text())

    def test_time_strict_validation_and_01_minimum(self):
        self.own()
        for invalid in ("25:10", "23:60", "8:00", "12:00:59"):
            self.message("/time " + invalid)
        self.assertEqual(self.store.accounts.active(101)["sign_time"], "08:00")
        self.message("/time 00:30")
        self.assertEqual(self.store.accounts.active(101)["sign_time"], "01:30")

    def test_bot_fake_matches_production_signatures(self):
        # test_bot 的 FakeAccounts 复刻了 27 个 AccountService 方法。复刻一旦和生产
        # 签名漂移，test_bot 就会对着假逻辑通过（曾出现：删掉生产 sign_mode 校验，
        # test_bot/test_accounts 全绿）。这里把签名对齐变成显式约束。
        import ast, pathlib
        def signatures(filename, marker):
            tree = ast.parse(pathlib.Path(filename).read_text(encoding='utf-8'))
            found = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and marker in node.name:
                    for fn in node.body:
                        if isinstance(fn, ast.FunctionDef):
                            args = fn.args
                            found[fn.name] = (tuple(a.arg for a in args.args),
                                              args.vararg.arg if args.vararg else None,
                                              tuple(a.arg for a in args.kwonlyargs),
                                              args.kwarg.arg if args.kwarg else None)
            return found
        prod = signatures('accounts.py', 'Service')
        fake = signatures('test_bot.py', 'Fake')
        # __init__ 不参与：替身是无参夹具，生产要 db/write/cfg，这是有意的隔离。
        shared = sorted(set(prod) & set(fake) - {'__init__'})
        self.assertGreater(len(shared), 20, '替身与生产服务几乎没有重叠，检查是否改名')
        for name in shared:
            with self.subTest(method=name):
                production, stand_in = prod[name], fake[name]
                self.assertEqual(stand_in, production,
                                 'test_bot 的替身 %s 与生产签名不一致，测试会假通过' % name)
        # 反向也要守：生产新增了 Bot 会调用的公开方法而替身没有，一调用就 AttributeError。
        # 私有实现细节（_now/_active 等）和 init_schema 不在替身职责内。
        missing = sorted(name for name in set(prod) - set(fake)
                         if not name.startswith('_') and name not in ('init_schema', 'watchers'))
        self.assertEqual(missing, [], '这些生产方法在 test_bot 替身里缺失：%s' % missing)

    def test_sign_mode_toggle_persists_and_shows_mark(self):
        a = self.own()
        self.assertEqual(self.store.accounts.user(101)['active_account_id'], a['id'])
        self.callback("mode:%d:random" % a['id'])
        self.assertEqual(self.store.accounts.rows[a['id']]['sign_mode'], 'random')
        self.assertIn('签到模式：随机字签到', self.text())
        labels = [button['text'] for button in self.buttons()]
        self.assertIn('✓ 随机字签到', labels)
        self.assertIn('普通签到', labels)
        self.callback("mode:%d:normal" % a['id'])
        self.assertEqual(self.store.accounts.rows[a['id']]['sign_mode'], 'normal')
        self.assertIn('签到模式：普通签到（不带附言）', self.text())
        self.assertIn('✓ 普通签到', [button['text'] for button in self.buttons()])

    def test_sign_mode_toggle_rejects_other_account_and_bad_mode(self):
        a = self.own()
        other = self.own(202, 'eB')
        self.store.accounts.set_account(101, expected_root=a['id'], sign_mode='normal')
        self.callback("mode:%d:random" % other['id'])
        self.assertIn('当前账号已切换', self.text())
        self.assertEqual(self.store.accounts.rows[a['id']]['sign_mode'], 'normal')
        self.callback("mode:%d:weird" % a['id'])
        self.assertIn('签到模式必须为普通或随机字', self.text())
        self.assertEqual(self.store.accounts.rows[a['id']]['sign_mode'], 'normal')

    def test_auto_and_preferences_fields_no_enabled_revive(self):
        a = self.own()
        self.store.accounts.rows[a["id"]]["enabled"] = 0
        self.callback("auto:%d" % a["id"])
        self.callback("pref:notify_success")
        self.callback("pref:notify_fail")
        self.callback("pref:notify_already")
        self.assertEqual(self.store.accounts.rows[a["id"]]["enabled"], 0)
        fields = [call[2] for call in self.store.accounts.calls if call[0] == "set_account"]
        self.assertEqual(fields, [{"auto_sign": False}])
        self.assertEqual(self.store.accounts.user(101)["notify_already"], True)
        self.callback("pref:tg_bot_token")
        self.assertNotIn("tg_bot_token", self.store.accounts.user(101))

    def test_link_source_toggle_both_endpoint_revocation(self):
        a = self.own()
        target = self.own(202, "eB")
        self.store.accounts.grants[a["id"], target["id"]] = True
        self.callback("toggle:%d:%d" % (a["id"], target["id"]), actor=202)
        self.assertTrue(self.store.accounts.grants[a["id"], target["id"]])
        self.callback("toggle:%d:%d" % (a["id"], target["id"]))
        self.assertFalse(self.store.accounts.grants[a["id"], target["id"]])
        self.callback("unlink:%d:%d:%d" % (target["id"], a["id"], target["id"]), actor=202)
        self.assertEqual(self.store.accounts.grants, {})
        self.store.accounts.grants[a["id"], target["id"]] = True
        self.callback("unlink:%d:%d:%d" % (a["id"], a["id"], target["id"]))
        self.assertEqual(self.store.accounts.grants, {})

    def test_sign_only_active_and_direct_not_reverse_or_transitive(self):
        a = self.own()
        target = self.own(202, "eB")
        c = self.own(303, "eC")
        d = self.own(404, "eD")
        self.store.accounts.grants.update({(a["id"], target["id"]): True,
                                          (target["id"], c["id"]): True,
                                          (d["id"], a["id"]): True})
        self.message("/sign")
        self.assertEqual(self.store.runs, [(101, True, a["id"], [a["id"], target["id"]])])
        self.assertEqual(self.store.run_contents, [None])
        # 附言就是用户输入的文字，官方用它做随机签到；不再当作非法参数拒绝。
        self.message("/sign 今天也在")
        self.assertEqual(len(self.store.runs), 2)
        self.assertEqual(self.store.run_contents[-1], "今天也在")

    def test_revoke_during_sign_removes_target_report(self):
        a = self.own()
        target = self.own(202, "eTarget", "秘密目标")
        self.store.accounts.grants[a["id"], target["id"]] = True
        def run_hook(actor, scope):
            self.store.accounts.unlink(202, a["id"], target["id"])
            return [{"id": a["id"], "name": "账号 A", "status": "signed"},
                    {"id": target["id"], "name": "秘密目标", "status": "signed"}]
        self.store.run_hook = run_hook
        self.message("/sign")
        self.assertNotIn("秘密目标", self.text())

    def test_logs_visible_and_escaped(self):
        a = self.own()
        other = self.own(202, "eB")
        self.store.accounts.records = [
            {"account_id": a["id"], "name": "<A&B>", "status": "failed", "message": "<boom>", "ts": "2026-11-07"},
            {"account_id": other["id"], "name": "秘密", "status": "signed", "message": "hidden"}]
        self.message("/logs")
        self.assertIn("&lt;A&amp;B&gt;", self.text())
        self.assertNotIn("<boom>", self.text())
        self.assertNotIn("秘密", self.text())

    def test_deprecated_add_del_never_touch_accounts(self):
        self.own()
        self.message("/add 11_secret-secret")
        self.message("/del 1")
        self.assertEqual(len(self.store.accounts.owned(101)), 1)
        self.assertNotIn("11_secret-secret", self.text())
        self.assertEqual(self.factory.calls, [])

    def test_checkpoint_persists_before_action_and_restart_skips_replay(self):
        self.own()
        self.b._accept_me(dict(FakeTG.PROFILE))
        uid = self.message('/sign')
        checkpoint = self.store.cfg_all()['tg_update_offset_777']
        self.assertEqual(checkpoint, str(uid + 1))
        newer = self.make_bot()
        newer._accept_me(dict(FakeTG.PROFILE))
        self.message('/sign', b=newer, update_id=uid)
        self.assertEqual(len(self.store.runs), 1)
        self.assertEqual(newer._offset, uid + 1)
        self.b._accept_me(dict(FakeTG.PROFILE))
        self.assertGreaterEqual(self.b._offset, uid + 1)

    def test_checkpoint_failure_never_executes(self):
        self.own()
        self.store.write_failure = True
        with self.assertRaises(OSError):
            self.message("/sign")
        self.assertEqual(self.store.runs, [])

    def test_duplicate_callback_not_reexecuted(self):
        a = self.own()
        self.callback("auto:%d" % a["id"], cb_id="same-callback")
        self.callback("auto:%d" % a["id"], cb_id="same-callback")
        self.assertFalse(self.store.accounts.active(101)["auto_sign"])
        self.assertEqual(len([call for call in self.store.accounts.calls if call[0] == "set_account"]), 1)
        self.assertEqual(len([action for action in self.tg.actions if action[0] == "answer"]), 2)

    def test_send_failure_never_retries_destructive_action(self):
        self.own()
        self.tg.fail_send = True
        self.message("/code")
        self.assertEqual(len([call for call in self.store.accounts.calls if call[0] == "issue_code"]), 1)

    def test_profile_failure_never_leaks_secret_to_stdout_or_ui(self):
        secret = "11_super-secret-value"
        self.factory.error = EmosError("https://api.telegram.org/botfake-token/path Bearer " + secret)
        self.message("/login eVerified")
        capture = io.StringIO()
        with redirect_stdout(capture):
            self.message("/start emosLinkAgree-" + secret)
        self.assertNotIn(secret, capture.getvalue() + self.text())
        self.assertNotIn("fake-token", capture.getvalue() + self.text())

    def test_missing_service_method_precisely_reported(self):
        self.store.accounts.redeem = None
        with self.assertRaisesRegex(TypeError, "redeem"):
            self.make_bot()

    def test_get_me_outage_retries_and_event_stop(self):
        self.tg.me_failures.extend([bot.TgError(), OSError("fake network failure")])
        self.tg.on_poll = lambda count: self.b.stop()
        self.b.run()
        self.assertEqual(self.tg.me_calls, 3)
        self.assertTrue(self.b.stop_event.is_set())
        self.assertEqual(self.b.me["username"], "emos_test_bot")

    def test_stop_interrupts_get_me_backoff(self):
        self.tg.me_failures.extend([bot.TgError()] * 500)
        self.b.retry_base, self.b.retry_cap = 10, 10
        thread = threading.Thread(target=self.b.run)
        thread.start()
        self.b.stop()
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_ok_false_poll_retries_and_scoped_offset_survives_restart(self):
        self.store.set_setting("tg_update_offset_777", "40")
        self.tg.updates.append({"ok": False, "description": "fake failure"})
        self.tg.on_poll = lambda count: self.b.stop() if count >= 2 else None
        self.b.run()
        self.assertEqual(self.tg.polls[:2], [40, 40])

    def test_async_profile_does_not_block_poll_or_callbacks(self):
        entered, release, answered = threading.Event(), threading.Event(), threading.Event()
        self.store.accounts.begin_login(101, "eVerified")
        asynchronous = self.make_bot(False, workers=2, max_jobs=4)
        self.factory.hook = lambda: (entered.set(), release.wait(2))
        old_answer = self.tg.answer
        def answer(cb_id, text=""):
            answered.set()
            return old_answer(cb_id, text)
        self.tg.answer = answer
        self.tg.updates.extend([
            {"ok": True, "result": [{"update_id": 1, "message": {"message_id": 100,
             "from": {"id": 101}, "chat": {"id": 101, "type": "private"}, "text": "/start emosLinkAgree-secret"}}]},
            {"ok": True, "result": [{"update_id": 2, "callback_query": {"id": "fast-answer", "data": "help",
             "from": {"id": 202}, "message": {"message_id": 200, "chat": {"id": 202, "type": "private"}}}}]},
        ])
        polled = threading.Event()
        self.tg.on_poll = lambda count: polled.set() if count >= 3 else None
        thread = threading.Thread(target=asynchronous.run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(polled.wait(1))
            self.assertTrue(answered.wait(1))
            self.assertFalse(release.is_set())
            self.assertFalse(asynchronous.join_jobs(0.01))
            release.set()
            self.assertTrue(asynchronous.join_jobs(2))
        finally:
            release.set()
            asynchronous.stop()
            thread.join(2)

    def test_bounded_jobs_per_user_dedup_and_join(self):
        entered, release = threading.Event(), threading.Event()
        asynchronous = self.make_bot(False, workers=1, max_jobs=1)
        self.own()
        self.own(202, "eB")
        def block(actor, scope):
            entered.set()
            release.wait(2)
            return []
        self.store.run_hook = block
        self.message("/sign", b=asynchronous)
        self.assertTrue(entered.wait(1))
        self.message("/sign", b=asynchronous)
        self.message("/sign", actor=202, b=asynchronous)
        self.assertEqual(len(self.store.runs), 1)
        self.assertLessEqual(len(asynchronous._jobs.keys), 1)
        self.assertFalse(asynchronous.join_jobs(0.01))
        release.set()
        self.assertTrue(asynchronous.join_jobs(2))

    def test_queued_command_root_snapshot_rejects_external_switch(self):
        entered, release = threading.Event(), threading.Event()
        a = self.own()
        other = self.own(101, "eA2")
        self.store.accounts.switch(101, a["id"])
        self.own(202, "eBlocker")
        asynchronous = self.make_bot(False, workers=1, max_jobs=3)
        def block(actor, scope):
            entered.set()
            release.wait(2)
            return []
        self.store.run_hook = block
        self.message("/sign", actor=202, b=asynchronous)
        self.assertTrue(entered.wait(1))
        self.message("/sign", b=asynchronous)
        self.store.accounts.switch(101, other["id"])
        release.set()
        self.assertTrue(asynchronous.join_jobs(2))
        self.assertFalse(any(run[0] == 101 for run in self.store.runs))

    def test_login_expiring_during_profile_cannot_finish(self):
        self.message("/login eVerified")
        self.factory.hook = lambda: self.store.accounts.cancel_login(101)
        self.message("/start emosLinkAgree-secret")
        self.assertEqual(self.store.accounts.owned(101), [])

    def test_sign_http_status_message_switch_rechecked(self):
        a = self.own()
        second = self.own(101, "eSecond")
        self.store.accounts.switch(101, a["id"])
        self.tg.on_send = lambda chat, text: self.store.accounts.switch(101, second["id"]) if "正在签到" in text else None
        self.message("/sign")
        self.assertEqual(self.store.runs, [])

    def test_empty_and_busy_run_results_safe(self):
        self.own()
        self.store.run_hook = lambda actor, scope: None
        self.message("/sign")
        self.assertIn("本次未执行", self.text())
        self.store.run_hook = lambda actor, scope: []
        self.message("/sign")
        self.assertIn("没有可报告", self.text())


class Response:
    def __init__(self, payload):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    def read(self, limit=-1):
        return self.raw if limit < 0 else self.raw[:limit]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class TGTests(unittest.TestCase):
    def test_ok_false_and_network_exception_safe_no_token(self):
        tg = bot.TG("sensitive-bot-key", None)
        for response in ({"ok": False, "description": "Bearer secret https://api.telegram.org/botsensitive-bot-key/getMe", "error_code": 401},
                         ["unexpected"]):
            with patch.object(bot.urllib.request, "urlopen", return_value=Response(response)):
                with self.assertRaises(bot.TgError) as caught:
                    tg.get_me()
            self.assertNotIn("sensitive-bot-key", str(caught.exception))
            self.assertNotIn("Bearer", str(caught.exception))
        with patch.object(bot.urllib.request, "urlopen", side_effect=OSError("URL contains sensitive-bot-key")):
            with self.assertRaises(bot.TgError) as caught:
                tg.get_me()
        self.assertNotIn("sensitive-bot-key", str(caught.exception))

    def test_http_error_retry_after_and_delete_wrapper(self):
        import urllib.error
        tg = bot.TG("fake-key", None)
        raw = io.BytesIO(json.dumps({"ok": False, "error_code": 429, "parameters": {"retry_after": 120}}).encode())
        error = urllib.error.HTTPError("https://invalid/fake-key", 429, "fake-key", {}, raw)
        with patch.object(bot.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(bot.TgError) as caught:
                tg.delete(101, 42)
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertNotIn("fake-key", str(caught.exception))

    def test_long_html_messages_balanced_utf16_entity_safe(self):
        from html.parser import HTMLParser
        class Balance(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
            def handle_starttag(self, tag, attrs):
                self.stack.append(tag)
            def handle_endtag(self, tag):
                if not self.stack or self.stack.pop() != tag:
                    raise AssertionError("unbalanced HTML")
        tg = bot.TG("fake-key", None)
        payloads = []
        def api(method, payload=None, timeout=45):
            payloads.append((method, payload))
            return {"ok": True, "result": {"message_id": 1}}
        tg._api = api
        text = "<b>" + "🥕 &lt;x&amp;y&gt;" * 1400 + "</b>"
        tg.send(101, text, [[{"text": "菜单", "callback_data": "menu"}]])
        self.assertGreater(len(payloads), 1)
        for _, payload in payloads:
            self.assertLessEqual(len(payload["text"].encode("utf-16-le")) // 2, bot.HTML_LIMIT)
            parser = Balance()
            parser.feed(payload["text"])
            self.assertEqual(parser.stack, [])
        self.assertNotIn("reply_markup", payloads[0][1])
        self.assertIn("reply_markup", payloads[-1][1])

    def test_edit_not_modified_is_not_retried(self):
        tg = bot.TG("fake-key", None)
        methods = []
        def api(method, payload=None, timeout=45):
            methods.append(method)
            raise bot.TgError(not_modified=True)
        tg._api = api
        self.assertTrue(tg.edit(101, 20, "<b>same</b>", []) ["ok"])
        self.assertEqual(methods, ["editMessageText"])

    def test_api_request_markup_and_no_global_chat(self):
        tg = bot.TG("fake-key", None)
        requests = []
        def open_fake(request, timeout=0):
            requests.append(json.loads(request.data))
            return Response({"ok": True, "result": {"message_id": 1}})
        with patch.object(bot.urllib.request, "urlopen", side_effect=open_fake):
            tg.send(101, "<b>hello</b>", [[{"text": "登录", "url": "https://t.me/emospg_bot"}]])
            tg.answer("callback-42", "已处理")
            tg.delete(101, 12)
        self.assertEqual(requests[0]["chat_id"], 101)
        self.assertEqual(requests[0]["parse_mode"], "HTML")
        self.assertIn("inline_keyboard", requests[0]["reply_markup"])
        self.assertEqual(requests[1]["callback_query_id"], "callback-42")
        self.assertEqual(requests[2]["message_id"], 12)


if __name__ == "__main__":
    unittest.main()
