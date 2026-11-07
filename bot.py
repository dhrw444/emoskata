"""emos 签到 · Telegram Bot —— 纯标准库 long polling。

与 web 面板共用同一个 data.db，两边改动实时同步。

命令：
    /start          主菜单（inline 按钮）
    /status         各号状态
    /sign           全部签到
    /sign 3         单独签第 3 个号
    /add token [名] 添加账号
    /del 3          删除账号
    /logs           最近日志
    /notify         通知开关
    /id             显示当前 chat_id（用于填到面板里）
"""

import html
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from emos import EmosClient, EmosError

POLL_TIMEOUT = 30
_lock = threading.Lock()


# ------------------------------------------------------------------ TG 基础

class TG:
    def __init__(self, token, store):
        self.token = (token or "").strip()
        self.store = store          # 提供 get_setting / set_setting / db / run_all / cfg_all

    def enabled(self):
        return bool(self.token)

    def _api(self, method, payload=None, timeout=POLL_TIMEOUT + 15):
        if not self.token:
            raise RuntimeError("未配置 tg_bot_token")
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data,
                                     method="POST" if data else "GET")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False, "description": body[:200]}

    def send(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self._api("sendMessage", payload)

    def edit(self, chat_id, msg_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "message_id": msg_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self._api("editMessageText", payload)

    def answer(self, cb_id, text=""):
        return self._api("answerCallbackQuery",
                         {"callback_query_id": cb_id, "text": text[:200]})

    def get_me(self):
        r = self._api("getMe", timeout=15)
        return r.get("result", {}) if r.get("ok") else None


def _esc(s):
    return html.escape(str(s if s is not None else ""))


def _mask(token):
    t = str(token or "")
    return t[:4] + "…" + t[-3:] if len(t) > 8 else t


# ------------------------------------------------------------------ 视图

def menu_kb():
    return [
        [{"text": "📋 账号状态", "callback_data": "status"},
         {"text": "✅ 全部签到", "callback_data": "signall"}],
        [{"text": "📜 最近日志", "callback_data": "logs"},
         {"text": "🔔 通知开关", "callback_data": "notify"}],
        [{"text": "❓ 帮助", "callback_data": "help"}],
    ]


def view_status(store):
    with store.db() as conn:
        accs = [dict(r) for r in conn.execute(
            "SELECT * FROM accounts ORDER BY id").fetchall()]
    if not accs:
        return "还没有账号。\n\n用 <code>/add 密钥 [备注名]</code> 添加。", menu_kb()

    lines = ["<b>📋 账号状态</b>", ""]
    ok = skip = bad = 0
    for a in accs:
        st = a["last_status"]
        if st == "signed":
            icon, ok = "✅", ok + 1
        elif st == "already":
            icon, skip = "⚪", skip + 1
        elif st in ("failed", "invalid"):
            icon, bad = "❌", bad + 1
        else:
            icon = "·"
        name = _esc(a["name"]) if a["name"] else _mask(a["token"])
        tail = ""
        if st == "signed" and a["last_carrot"] is not None:
            tail = " +%s" % a["last_carrot"]
        elif st in ("failed", "invalid"):
            tail = " " + _esc((a["last_msg"] or "")[:28])
        flag = "" if a["enabled"] else " <i>(停用)</i>"
        lines.append("%s <b>%d</b>. %s%s%s" % (icon, a["id"], name, tail, flag))

    lines.append("")
    lines.append("成功 %d · 已签 %d · 失败 %d" % (ok, skip, bad))
    if accs:
        lines.append("<i>最近：%s</i>" % _esc(accs[0]["last_run"] or "—"))
    return "\n".join(lines), menu_kb()


def view_logs(store, limit=15):
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    if not rows:
        return "还没有日志。", menu_kb()
    lines = ["<b>📜 最近 %d 条</b>" % len(rows), ""]
    for r in rows:
        icon = {"signed": "✅", "already": "⚪"}.get(r["status"], "❌")
        tail = " +%s" % r["carrot"] if r.get("carrot") else ""
        lines.append("%s <code>%s</code> %s%s" %
                     (icon, _esc(r["ts"][5:16]), _esc(r["name"]), tail))
    return "\n".join(lines), menu_kb()


def view_notify(store):
    cfg = store.cfg_all()
    def mark(k):
        return "开" if cfg.get(k) == "1" else "关"
    text = ("<b>🔔 通知设置</b>\n\n"
            "签到成功：%s\n失败：%s\n仅已签时也通知：%s\n汇总成一条：%s\n\n"
            "推送目标 chat_id：<code>%s</code>"
            % (mark("notify_success"), mark("notify_fail"),
               mark("notify_already"), mark("notify_summary"),
               _esc(cfg.get("tg_chat_id") or "未设置")))
    kb = [
        [{"text": "成功 %s" % mark("notify_success"), "callback_data": "tg_notify_success"},
         {"text": "失败 %s" % mark("notify_fail"), "callback_data": "tg_notify_fail"}],
        [{"text": "已签也通知 %s" % mark("notify_already"), "callback_data": "tg_notify_already"},
         {"text": "汇总 %s" % mark("notify_summary"), "callback_data": "tg_notify_summary"}],
        [{"text": "⬅️ 返回", "callback_data": "menu"}],
    ]
    return text, kb


HELP = """<b>❓ emos 签到 Bot</b>

<b>账号</b>
<code>/add 密钥 [备注名]</code> — 添加账号
<code>/del 编号</code> — 删除账号
<code>/accounts</code> — 列表（含密钥前缀）

<b>签到</b>
<code>/sign</code> — 全部签到
<code>/sign 3</code> — 只签第 3 个
<code>/status</code> — 状态总览
<code>/logs</code> — 最近日志

<b>其他</b>
<code>/notify</code> — 通知开关
<code>/id</code> — 查看 chat_id
<code>/menu</code> — 主菜单

<i>密钥在 emos.best 点左上角「欢迎」获取，形如 11_xxxxxxxx</i>"""


# ------------------------------------------------------------------ Bot 主体

class Bot:
    def __init__(self, token, store):
        self.tg = TG(token, store)
        self.store = store
        self.me = None
        self.running = True

    def stop(self):
        self.running = False

    def _allowed(self, chat_id):
        """只允许配置里的 chat_id 操作；未配置则首次调用者自动绑定。"""
        cfg = self.store.cfg_all()
        want = (cfg.get("tg_chat_id") or "").strip()
        cid = str(chat_id)
        if not want:
            self.store.set_setting("tg_chat_id", cid)
            return True, "（已自动绑定为通知接收方）"
        if cid == want:
            return True, ""
        # 群组：chat_id 可能是负数，配置里存的是用户 id
        return False, ""

    def run(self):
        if not self.tg.enabled():
            return
        self.me = self.tg.get_me()
        if not self.me:
            print("[bot] token 无效，Bot 未启动")
            return
        print("[bot] @%s 已上线" % self.me.get("username", "?"))
        offset = 0
        while self.running:
            try:
                r = self.tg._api("getUpdates", {
                    "offset": offset, "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message", "callback_query"],
                }, timeout=POLL_TIMEOUT + 20)
                if not r.get("ok"):
                    time.sleep(5)
                    continue
                for upd in r.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        self._handle(upd)
                    except Exception as e:
                        print("[bot] 处理失败:", e)
            except Exception as e:
                print("[bot] 轮询异常:", e)
                time.sleep(5)

    # ---------------- 分发 ----------------

    def _handle(self, upd):
        if "callback_query" in upd:
            return self._on_callback(upd["callback_query"])
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if not text or chat_id is None:
            return
        if not text.startswith("/"):
            return
        self._on_command(chat_id, text)

    def _on_command(self, chat_id, text):
        ok, note = self._allowed(chat_id)
        if not ok:
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        if cmd in ("/start", "/menu"):
            self.tg.send(chat_id,
                         "<b>🥕 emos 签到</b>%s\n\n选一个操作：" % (("\n" + note) if note else ""),
                         menu_kb())

        elif cmd == "/help":
            self.tg.send(chat_id, HELP, menu_kb())

        elif cmd == "/id":
            self.tg.send(chat_id, "chat_id: <code>%s</code>" % chat_id)

        elif cmd in ("/status", "/accounts"):
            if cmd == "/accounts":
                with self.store.db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM accounts ORDER BY id").fetchall()]
                if not rows:
                    self.tg.send(chat_id, "还没有账号。", menu_kb())
                    return
                lines = ["<b>🔑 账号列表</b>", ""]
                for a in rows:
                    lines.append("<b>%d</b>. %s <code>%s</code>%s" % (
                        a["id"], _esc(a["name"] or "未命名"),
                        _mask(a["token"]), "" if a["enabled"] else " (停用)"))
                self.tg.send(chat_id, "\n".join(lines), menu_kb())
            else:
                t, kb = view_status(self.store)
                self.tg.send(chat_id, t, kb)

        elif cmd == "/logs":
            t, kb = view_logs(self.store)
            self.tg.send(chat_id, t, kb)

        elif cmd == "/notify":
            t, kb = view_notify(self.store)
            self.tg.send(chat_id, t, kb)

        elif cmd == "/add":
            if not args:
                self.tg.send(chat_id, "用法：<code>/add 密钥 [备注名]</code>")
                return
            token = args[0].strip()
            name = " ".join(args[1:]).strip()[:40] or "未命名"
            if "_" not in token:
                self.tg.send(chat_id, "密钥格式不对，应该形如 <code>11_xxxxxxxx</code>")
                return
            # 先验证 token 有效再入库
            self.tg.send(chat_id, "验证中…")
            try:
                prof = EmosClient(token).profile()
                disp = prof.get("_display") or ""
            except EmosError as e:
                self.tg.send(chat_id, "❌ Token 无效：%s" % _esc(e.message))
                return
            final_name = name if (name and name != "未命名") else (disp or "未命名")
            with self.store.write() as conn:
                conn.execute(
                    "INSERT INTO accounts(name,token,created) VALUES(?,?,?)",
                    (final_name, token, time.strftime("%Y-%m-%d %H:%M:%S")))
            self.tg.send(chat_id, "✅ 已添加：<b>%s</b>" % _esc(final_name), menu_kb())

        elif cmd == "/del":
            if not args or not args[0].isdigit():
                self.tg.send(chat_id, "用法：<code>/del 编号</code>")
                return
            with self.store.write() as conn:
                conn.execute("DELETE FROM accounts WHERE id=?", (int(args[0]),))
            self.tg.send(chat_id, "🗑 已删除 #%s" % args[0], menu_kb())

        elif cmd == "/sign":
            only = None
            if args and args[0].isdigit():
                only = int(args[0])
            self.tg.send(chat_id, "⏳ 开始签到…")
            self._run_and_report(chat_id, only)

    def _run_and_report(self, chat_id, only_id=None):
        cfg = self.store.cfg_all()
        rows = self.store.run_all(cfg, "manual", only_id)
        if rows is None:
            self.tg.send(chat_id, "⚠️ 已有任务在跑，稍等")
            return
        if not rows:
            self.tg.send(chat_id, "没有可签到的账号。", menu_kb())
            return
        t, kb = view_status(self.store)
        self.tg.send(chat_id, t, kb)

    def _on_callback(self, cb):
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")
        if chat_id is None:
            return

        ok, note = self._allowed(chat_id)
        if not ok:
            self.tg.answer(cb_id, "无权操作")
            return

        try:
            if data == "menu":
                self.tg.answer(cb_id)
                self.tg.edit(chat_id, msg_id,
                             "<b>🥕 emos 签到</b>\n\n选一个操作：", menu_kb())

            elif data == "status":
                self.tg.answer(cb_id, "刷新中")
                t, kb = view_status(self.store)
                self.tg.edit(chat_id, msg_id, t, kb)

            elif data == "logs":
                self.tg.answer(cb_id)
                t, kb = view_logs(self.store)
                self.tg.edit(chat_id, msg_id, t, kb)

            elif data == "notify":
                self.tg.answer(cb_id)
                t, kb = view_notify(self.store)
                self.tg.edit(chat_id, msg_id, t, kb)

            elif data.startswith("tg_notify_"):
                key = data[len("tg_notify_"):]
                cur = self.store.cfg_all().get(key, "0")
                self.store.set_setting(key, "0" if cur == "1" else "1")
                self.tg.answer(cb_id, "已切换")
                t, kb = view_notify(self.store)
                self.tg.edit(chat_id, msg_id, t, kb)

            elif data == "signall":
                if _lock.locked():
                    self.tg.answer(cb_id, "正在运行中")
                    return
                self.tg.answer(cb_id, "开始签到")
                with _lock:
                    self.tg.edit(chat_id, msg_id, "⏳ 正在签到，请稍候…")
                    self._run_and_report(chat_id)

            elif data == "help":
                self.tg.answer(cb_id)
                self.tg.edit(chat_id, msg_id, HELP, menu_kb())

            elif data.startswith("sign:"):
                aid = int(data.split(":", 1)[1])
                if _lock.locked():
                    self.tg.answer(cb_id, "正在运行中")
                    return
                self.tg.answer(cb_id, "开始")
                with _lock:
                    self._run_and_report(chat_id, aid)

            else:
                self.tg.answer(cb_id)
        except Exception as e:
            print("[bot] callback 失败:", e)
            self.tg.answer(cb_id, "操作失败")


def start(token, store):
    """在后台线程启动 Bot。"""
    if not (token or "").strip():
        return None
    b = Bot(token, store)
    threading.Thread(target=b.run, daemon=True).start()
    return b
