"""管理页迁移区块的接口契约与前端接线回归。

网页输入框提交的是字符串 ID，接口必须接受纯数字串；前端必须把目标 ID
转成数字并把结果写进区块内的状态行，否则用户只能看到“点了没反应”。
仅用 localhost 与临时数据库，不代表真实 TG 或官方授权。
Test: python3 -B -m unittest test_transfer_panel
"""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from test_support import fixture_app


class TransferPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-transfer-panel-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'transfer_panel_app', single_owner=True)
        self.app.init_db()
        self.app.init_password()
        self.addCleanup(self.app.close_idle_db)
        self.service = self.app.Store().accounts

    def seed(self, tg_id, emos):
        with self.app.write_db() as conn:
            conn.execute("INSERT INTO tg_users(tg_id,chat_id) VALUES(?,?) "
                         "ON CONFLICT(tg_id) DO NOTHING", (tg_id, tg_id))
            cur = conn.execute(
                "INSERT INTO accounts(name,token,emos_id,owner_tg_id) VALUES(?,?,?,?)",
                (emos, 'token_' + emos, emos, tg_id))
            conn.execute('UPDATE tg_users SET active_account_id=? WHERE tg_id=?',
                         (cur.lastrowid, tg_id))
        return cur.lastrowid

    def request(self, server, path, method='GET', body=None, headers=None):
        headers = dict(headers or {})
        if getattr(self, 'sid', ''):
            headers.setdefault('Cookie', self.sid)
        if isinstance(body, dict):
            body = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        conn = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode()
        code = response.status
        sid = response.getheader('Set-Cookie')
        conn.close()
        if sid:
            self.sid = sid.split(';')[0]
        return code, data

    def login(self, server):
        code, data = self.request(server, '/api/login', 'POST', {'password': 'fixture_password'})
        self.assertEqual(code, 200, data)

    def test_transfer_accepts_numeric_string_ids_from_input_fields(self):
        """网页 input.value 是字符串；account/owner 两种迁移都必须接受。"""
        account_id = self.seed(92349328467, 'eMain')
        self.seed(92408796370, 'eOther')
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.login(server)
            code, data = self.request(server, '/api/transfer', 'POST',
                                      {'action': 'account', 'account_id': str(account_id),
                                       'target_tg_id': '92408796370'})
            self.assertEqual(code, 200, data)
            self.assertEqual(json.loads(data)['target'], 92408796370)
            with self.app.db() as conn:
                owner = conn.execute('SELECT owner_tg_id FROM accounts WHERE id=?',
                                     (account_id,)).fetchone()[0]
            self.assertEqual(owner, 92408796370)

            code, data = self.request(server, '/api/transfer', 'POST',
                                      {'action': 'owner', 'source_tg_id': '92408796370',
                                       'target_tg_id': '999999001'})
            self.assertEqual(code, 200, data)
            self.assertEqual(json.loads(data)['target'], 999999001)
            with self.app.db() as conn:
                self.assertIsNone(conn.execute('SELECT 1 FROM tg_users WHERE tg_id=?',
                                               (92408796370,)).fetchone())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_transfer_rejects_non_numeric_ids_with_clear_error(self):
        self.seed(101, 'eMain')
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.login(server)
            for bad in ('abc', '', '123abc', '0', '-5', '９２３'):
                code, data = self.request(server, '/api/transfer', 'POST',
                                          {'action': 'owner', 'source_tg_id': '101',
                                           'target_tg_id': bad})
                self.assertEqual(code, 400, (bad, data))
                self.assertIn('ID 无效', data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_accounts_without_emos_id_still_listed_and_movable(self):
        """历史账号可能没有 emos_id；迁移面板必须能看见并搬运它们。"""
        with self.app.write_db() as conn:
            conn.execute("INSERT INTO tg_users(tg_id,chat_id) VALUES(?,?)", (910000001, 910000001))
            conn.execute("INSERT INTO tg_users(tg_id,chat_id) VALUES(?,?)", (910000002, 910000002))
            cur = conn.execute(
                "INSERT INTO accounts(name,token,emos_id,owner_tg_id) VALUES(?,?,?,?)",
                ('历史账号', 'tok_legacy', '', 910000001))
            account_id = cur.lastrowid
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.login(server)
            # 迁移清单来自 /api/owners，不受管理员策略筛选影响。
            code, data = self.request(server, '/api/owners')
            self.assertEqual(code, 200, data)
            owners = {row['tg_id']: row for row in json.loads(data)}
            items = {item['id']: item for item in owners[910000001]['items']}
            self.assertIn(account_id, items)
            self.assertEqual(items[account_id]['emos_id'], '')
            code, data = self.request(server, '/api/transfer', 'POST',
                                      {'action': 'account', 'account_id': str(account_id),
                                       'target_tg_id': '910000002'})
            self.assertEqual(code, 200, data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_unattributed_accounts_have_their_own_row_and_move_individually(self):
        """owner_tg_id 为空的历史账号归入「未归属」，只能逐个迁移。"""
        with self.app.write_db() as conn:
            conn.execute("INSERT INTO tg_users(tg_id,chat_id) VALUES(?,?)", (920000001, 920000001))
            cur = conn.execute(
                "INSERT INTO accounts(name,token,emos_id,owner_tg_id) VALUES(?,?,?,?)",
                ('游离账号', 'tok_orphan', 'eOrphan', None))
            account_id = cur.lastrowid
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.login(server)
            code, data = self.request(server, '/api/owners')
            self.assertEqual(code, 200, data)
            rows = json.loads(data)
            stray = [row for row in rows if row['tg_id'] is None]
            self.assertEqual(len(stray), 1, rows)
            self.assertEqual(stray[0]['accounts'], 1)
            self.assertEqual([item['id'] for item in stray[0]['items']], [account_id])
            code, data = self.request(server, '/api/transfer', 'POST',
                                      {'action': 'account', 'account_id': str(account_id),
                                       'target_tg_id': '920000001'})
            self.assertEqual(code, 200, data)
            with self.app.db() as conn:
                owner = conn.execute('SELECT owner_tg_id FROM accounts WHERE id=?',
                                     (account_id,)).fetchone()[0]
            self.assertEqual(owner, 920000001)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_panel_uses_section_message_and_sends_numbers(self):
        """静态接线：状态行在迁移区块内，目标 ID 以数字提交。"""
        page = self.app.PAGE
        section = page.split('账号归属迁移', 1)[1].split('账号运行状态', 1)[0]
        self.assertIn('id="transfer-msg"', section)
        self.assertIn('target_tg_id:Number(target)', page)
        self.assertIn("function tsay(text)", page)
        self.assertNotIn("target_tg_id:target}", page)
        # 迁移选择只用 /api/owners 自带的 items，避免受策略筛选影响。
        self.assertIn('for(const row of (owner.items||[]))', page)
        owners_load = page.split('async function ownersLoad', 1)[1].split('load()', 1)[0]
        self.assertNotIn("api(\\'accounts\\')", owners_load)


if __name__ == '__main__':
    unittest.main()
