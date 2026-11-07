#!/usr/bin/env python3
"""emoskata —— emos 自动签到 · 网页面板 + Telegram Bot（单文件版）

由 emos.py + bot.py + app.py 合并生成，改动请改源文件后重跑 build_single.py。

运行：
    PANEL_PASSWORD='你的密码' PORT=8080 python3 emoskata.py

环境变量：
    PANEL_PASSWORD  面板密码（不填随机生成并打印）
    PORT            监听端口，默认 8080
    DATA_DIR        数据目录，默认 ./data
    PUBLIC_BASE     OAuth 回调用，反代后填 https://你的域名
"""

# ==================================================================
# 来自 emos.py
# ==================================================================
"""emos API client — 纯标准库，零第三方依赖。

接口契约已按 xlzs001/emos-loon-sign 的实际实现核对（源码混淆，字段名从 base64 解出）：

    GET  /api/sign/check          → {"is_sign": bool}        今日是否已签
    PUT  /api/user/sign?content=  → {"earn_point", "continuous_days", "sign_index"}
    GET  /api/user                → {"data": {"username", "name", "user_id", ...}}

关键行为（实测于该脚本）：
    401/403  → token 失效
    409/429  → 今天已签（重复签到被拒）
    域名降级 → emos.best 失败自动切 api.emos.best
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

BASES = ("https://emos.best", "https://api.emos.best")

# 实测可用的 UA：真机 Safari，避免被 CF 判为脚本
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")

# 签到附言候选池 —— 随机取一条，比空附言更容易拿到随机奖励
CONTENTS = (
    "签到", "打卡", "今日签到", "来签到", "顺手签到", "元气满满",
    "今天也在", "醒来签到", "梦里签到", "路过一下", "悄悄签到",
    "看剧营业", "保持热爱", "平安喜乐", "万事顺意", "元气在线",
    "好运敲门", "认真生活", "悄悄变好", "继续发光", "今日达成",
    "任务完成", "签到打卡", "我又来了",
)

TIMEOUT = 20


class EmosError(Exception):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload

    def __str__(self):
        return "%s (HTTP %s)" % (self.message, self.status) if self.status else self.message


def _safe_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"_raw": raw[:600].decode("utf-8", "replace")}


def pick_content(preferred=""):
    return (preferred or "").strip() or random.choice(CONTENTS)


def clamp_sign_time(hhmm):
    """官方要求：不要选 0 点签到。最小 01:00。"""
    try:
        h, m = [int(x) for x in str(hhmm).split(":")[:2]]
    except Exception:
        return "08:00"
    h = min(max(h, 0), 23)
    m = min(max(m, 0), 59)
    if h == 0:
        h = 1
    return "%02d:%02d" % (h, m)


class EmosClient:
    """单账号客户端。base 可为空，自动在 BASES 间降级。"""

    def __init__(self, token, base="", timeout=TIMEOUT):
        self.token = (token or "").strip()
        self.bases = ([base.rstrip("/")] if base else []) + list(BASES)
        self.timeout = timeout

    # ---------------- 底层 ----------------

    def _once(self, method, path, base, params=None):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Referer", base + "/")
        req.add_header("User-Agent", UA)
        if method in ("PUT", "POST"):
            req.data = b""
            req.add_header("Content-Length", "0")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, _safe_json(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _safe_json(e.read())
        except Exception as e:
            raise EmosError("网络错误: %s" % e)

    def call(self, method, path, params=None):
        """带域名降级的调用。网络层失败才换域名，HTTP 错误原样返回。"""
        last = None
        for base in self.bases:
            try:
                return self._once(method, path, base, params)
            except EmosError as e:
                last = e
                continue
        raise last or EmosError("所有域名均不可达")

    # ---------------- 业务 ----------------

    def check(self):
        """GET /api/sign/check → (is_sign, raw)。token 失效抛异常。"""
        st, body = self.call("GET", "/api/sign/check")
        if st in (401, 403):
            raise EmosError("Token 失效", st, body)
        if st != 200:
            raise EmosError("查询失败", st, body)
        return bool(body.get("is_sign")), body

    def profile(self):
        """GET /api/user → dict（已剥掉 data 外壳）。"""
        st, body = self.call("GET", "/api/user")
        if st in (401, 403):
            raise EmosError("Token 失效", st, body)
        if st != 200:
            raise EmosError("读取用户信息失败", st, body)
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        name = (data.get("username") or data.get("name")
                or data.get("user_id") or "")
        out = dict(data)
        out["_display"] = name
        return out

    def do_sign(self, content=""):
        """PUT /api/user/sign?content=xxx → (status, body)

        400 = 留言不合规，换一句重试一次（社区实现同样是换词重试）。
        """
        st, body = self.call("PUT", "/api/user/sign",
                             {"content": pick_content(content)})
        if st == 400 and not content:
            st, body = self.call("PUT", "/api/user/sign",
                                 {"content": pick_content()})
        return st, body

    def reset_token(self):
        """PUT /api/user/resetToken → 新 token。用户手动重置后旧 token 失效，用这个换。"""
        st, body = self.call("PUT", "/api/user/resetToken")
        if st != 200:
            raise EmosError("重置密钥失败", st, body)
        tok = (body.get("token") or "").strip()
        if not tok:
            raise EmosError("响应里没有 token", st, body)
        self.token = tok
        return tok

    # ---------------- 组合 ----------------

    def sign_once(self, content=""):
        """先查后签，幂等。

        返回:
            status   signed | already | invalid | failed
            message  中文说明
            point    本次获得萝卜
            days     连续天数
            rank     今日第几位
            name     账号名
        """
        res = {"status": "failed", "message": "", "point": None,
               "days": None, "rank": None, "name": ""}

        # 0) 先用 /api/user 验 token 并拿名字
        #    不能只靠 /api/sign/check —— 它无 token 时也返回 200
        try:
            prof = self.profile()
            res["name"] = prof.get("_display") or ""
        except EmosError as e:
            res["status"] = "invalid" if e.status in (401, 403) else "failed"
            res["message"] = e.message
            return res

        # 1) 查今日状态
        try:
            is_sign, _ = self.check()
        except EmosError as e:
            res["status"] = "invalid" if e.status in (401, 403) else "failed"
            res["message"] = e.message
            return res

        if is_sign:
            res["status"] = "already"
            res["message"] = "今日已签到"
            return res

        # 2) 执行签到
        st, body = self.do_sign(content)

        if st == 200:
            res["status"] = "signed"
            res["message"] = "签到成功"
            res["point"] = body.get("earn_point")
            res["days"] = body.get("continuous_days")
            res["rank"] = body.get("sign_index")
        elif st in (409, 429):
            res["status"] = "already"
            res["message"] = "今日已签到"
        elif st in (401, 403):
            res["status"] = "invalid"
            res["message"] = "Token 失效"
        else:
            msg = body.get("message") if isinstance(body, dict) else ""
            res["message"] = "签到失败 %s%s" % (st, ("：" + str(msg)) if msg else "")

        return res


# ==================================================================
# 来自 bot.py
# ==================================================================
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


# ==================================================================
# 来自 app.py
# ==================================================================
"""emos 签到面板 —— 纯 Python 标准库，零第三方依赖。

    python3 app.py            # 默认端口 8080
    PORT=9000 python3 app.py

首次运行自动生成 data.db。面板密码：
    环境变量 PANEL_PASSWORD  > 自动生成（只打印一次）

设计参考 MakkaPakka518/TG-Persona 的工程结构：
    - scrypt 哈希存密码，明文不落盘
    - session token 存库 + cookie，带 TTL
    - 登录失败限流
    - 通知分类开关
    - 失败退避重试，重启不误记成功
"""

import hashlib
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
    "oauth_app_name": "emoskata",
}

_write_lock = threading.Lock()
_run_lock = threading.Lock()


# ================================================================== DB

def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _write_lock, db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, created INTEGER, last_seen INTEGER,
            ip TEXT, ua TEXT
        );

        CREATE TABLE IF NOT EXISTS login_fail (
            ip TEXT PRIMARY KEY, count INTEGER DEFAULT 0, until INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            token       TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            last_status TEXT DEFAULT '',
            last_msg    TEXT DEFAULT '',
            last_carrot INTEGER,
            last_run    TEXT DEFAULT '',
            last_day    TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            next_run_at INTEGER DEFAULT 0,
            created     TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            name       TEXT DEFAULT '',
            ts         TEXT NOT NULL,
            status     TEXT NOT NULL,
            message    TEXT DEFAULT '',
            carrot     INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_logs_id ON logs(id DESC);
        """)
        # 迁移：老库补列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
        for col, ddl in (("retry_count", "INTEGER DEFAULT 0"),
                         ("next_run_at", "INTEGER DEFAULT 0")):
            if col not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN %s %s" % (col, ddl))


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
            cfg[r["key"]] = r["value"]
    return cfg


class Store:
    """给 bot.py 用的适配层，与面板共用同一个 data.db。"""

    def db(self):
        return db()

    def write(self):
        class _W:
            def __enter__(_s):
                _write_lock.acquire()
                _s.c = db()
                return _s.c

            def __exit__(_s, *a):
                try:
                    _s.c.commit()
                finally:
                    _s.c.close()
                    _write_lock.release()
                return False
        return _W()

    def cfg_all(self):
        return cfg_all()

    def set_setting(self, k, v):
        set_setting(k, v)

    def run_all(self, cfg, trigger="manual", only_id=None):
        return run_all(cfg, trigger, only_id)


def add_log(account_id, name, status, message, carrot=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _write_lock, db() as conn:
        conn.execute(
            "INSERT INTO logs(account_id,name,ts,status,message,carrot) "
            "VALUES(?,?,?,?,?,?)", (account_id, name, ts, status, message, carrot))
        conn.execute(
            "UPDATE accounts SET last_status=?,last_msg=?,last_carrot=?,last_run=? "
            "WHERE id=?", (status, message, carrot, ts, account_id))


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
    if get_setting("panel_pw_hash"):
        return None
    src = os.environ.get("PANEL_PASSWORD") or ""
    generated = False
    if len(src) < 6:
        src = secrets.token_urlsafe(9)
        generated = True
    set_setting("panel_pw_hash", hash_password(src))
    return src if generated else None


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

def notify(cfg, title, text=""):
    """TG Bot + Bark，任一配置即可。"""
    errors = []
    token = (cfg.get("tg_bot_token") or "").strip()
    chat = (cfg.get("tg_chat_id") or "").strip()
    if token and chat:
        body = json.dumps({
            "chat_id": chat,
            "text": ("<b>%s</b>\n%s" % (_esc(title), text)) if text else ("<b>%s</b>" % _esc(title)),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except Exception as e:
            errors.append("TG: %s" % e)

    bark = (cfg.get("bark_key") or "").strip()
    if bark:
        base = bark if "://" in bark else "https://api.day.app/" + bark
        url = base.rstrip("/") + "/" + urllib.parse.quote(title)
        if text:
            url += "/" + urllib.parse.quote(text[:400])
        try:
            urllib.request.urlopen(url, timeout=12).read()
        except Exception as e:
            errors.append("Bark: %s" % e)

    return (not errors), "; ".join(errors)


def build_report(rows, started, ended):
    total = len(rows)
    ok = sum(1 for r in rows if r["status"] == "signed")
    skip = sum(1 for r in rows if r["status"] == "already")
    bad = total - ok - skip
    gained = sum(r["carrot"] or 0 for r in rows if r["status"] == "signed")

    lines = ["成功 %d · 已签 %d · 失败 %d" % (ok, skip, bad)]
    if gained:
        lines.append("本次进账 <b>+%d</b> 萝卜" % gained)
    lines.append("耗时 %.1fs" % max(0.0, ended - started))
    lines.append("")
    for r in rows:
        icon = {"signed": "✅", "already": "⚪"}.get(r["status"], "❌")
        tail = ""
        if r["status"] == "signed" and r["carrot"] is not None:
            tail = " +%d" % r["carrot"]
        elif r["status"] not in ("signed", "already"):
            tail = " " + _esc((r["message"] or "")[:70])
        lines.append("%s %s%s" % (icon, _esc(r["name"]), tail))
    return "\n".join(lines)


# ================================================================== 签到

def _next_retry(count):
    if count <= 0 or count > len(RETRY_DELAYS):
        return 0
    return int(time.time()) + RETRY_DELAYS[count - 1]


def run_all(cfg, trigger="manual", only_id=None):
    """串行跑签到。trigger: manual / auto / retry。返回结果列表，占用中返回 None。"""
    if not _run_lock.acquire(blocking=False):
        return None
    started = time.time()
    rows = []
    try:
        with db() as conn:
            if only_id:
                accs = conn.execute("SELECT * FROM accounts WHERE id=?", (only_id,)).fetchall()
            elif trigger == "retry":
                accs = conn.execute(
                    "SELECT * FROM accounts WHERE enabled=1 AND retry_count>0 "
                    "AND next_run_at>0 AND next_run_at<=? ORDER BY id",
                    (int(time.time()),)).fetchall()
            else:
                accs = conn.execute(
                    "SELECT * FROM accounts WHERE enabled=1 ORDER BY id").fetchall()

        if not accs:
            return rows

        gap = max(2, int(cfg.get("gap_seconds") or 8))
        content = cfg.get("content") or ""

        for idx, a in enumerate(accs):
            if idx:
                time.sleep(gap)
            client = EmosClient(a["token"], cfg.get("base_url"))
            try:
                res = client.sign_once(content)
            except Exception as e:
                res = {"status": "failed", "message": "异常: %s" % e, "point": None}

            row = {"id": a["id"], "name": a["name"],
                   "status": res.get("status", "failed"),
                   "message": res.get("message", ""),
                   "carrot": res.get("point")}
            add_log(a["id"], a["name"], row["status"], row["message"], row["carrot"])

            # 退避重试登记
            with _write_lock, db() as conn:
                if row["status"] == "failed":
                    cnt = (a["retry_count"] or 0) + 1
                    conn.execute(
                        "UPDATE accounts SET retry_count=?, next_run_at=? WHERE id=?",
                        (cnt, _next_retry(cnt), a["id"]))
                else:
                    conn.execute(
                        "UPDATE accounts SET retry_count=0, next_run_at=0, last_day=? WHERE id=?",
                        (datetime.now().strftime("%Y-%m-%d"), a["id"]))

            rows.append(row)

        ended = time.time()
        _maybe_notify(cfg, rows, started, ended, trigger)
        return rows
    finally:
        _run_lock.release()


def _maybe_notify(cfg, rows, started, ended, trigger):
    if not rows:
        return
    ok = any(r["status"] == "signed" for r in rows)
    bad = any(r["status"] == "failed" for r in rows)
    skip = any(r["status"] == "already" for r in rows)

    want = ((ok and cfg.get("notify_success") == "1")
            or (bad and cfg.get("notify_fail") == "1")
            or (skip and not ok and not bad and cfg.get("notify_already") == "1"))
    if not want:
        return

    title = "🥕 emos 签到 · %s" % datetime.now().strftime("%m-%d %H:%M")
    if not (cfg.get("notify_summary") == "1"):
        title = "🥕 emos 签到结果"
    threading.Thread(
        target=notify, args=(cfg, title, build_report(rows, started, ended)),
        daemon=True).start()


# ================================================================== 调度

def now_local(cfg):
    """当前时间（按配置时区）。

    精简容器里没有 tzdata 时 ZoneInfo 会抛 ZoneInfoNotFoundError，
    这时退回固定偏移表 —— 宁可偏移正确也不要静默用 UTC 错 8 小时。
    """
    tz = cfg.get("timezone") or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz))
    except Exception:
        pass

    # 固定偏移兜底（不处理夏令时；中国/香港/新加坡/台湾本身就没有夏令时）
    off = TZ_FALLBACK.get(tz)
    if off is not None:
        return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=off)))
    return datetime.now()


def scheduler_loop():
    """每分钟检查一次。到点跑一次，同一天不重复；失败的按退避重试。"""
    while True:
        try:
            cfg = cfg_all()
            now = now_local(cfg)
            today = now.strftime("%Y-%m-%d")
            hh, mm = [int(x) for x in clamp_sign_time(cfg.get("sign_time")).split(":")]

            # 到点执行
            if now.hour == hh and now.minute == mm:
                with db() as conn:
                    pending = conn.execute(
                        "SELECT COUNT(*) c FROM accounts WHERE enabled=1 AND last_day!=?",
                        (today,)).fetchone()["c"]
                if pending:
                    run_all(cfg, trigger="auto")

            # 退避重试（任何时候都可触发，条件写在 SQL 里）
            run_all(cfg, trigger="retry")

        except Exception:
            traceback.print_exc()
        time.sleep(30)


# ================================================================== 页面

OAUTH_DONE_PAGE = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权完成 · emoskata</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e9ef;display:grid;place-items:center;
min-height:100vh;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
padding:24px}
.box{max-width:420px;width:100%;background:#171a21;border:1px solid #252a34;
border-radius:14px;padding:28px;text-align:center}
.ok{font-size:40px;margin-bottom:8px}
h1{font-size:17px;margin:0 0 14px;font-weight:600}
p{margin:0 0 20px;color:#9aa4b2;font-size:14px;word-break:break-all}
a{display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;
padding:10px 22px;border-radius:9px;font-size:14px}
a:hover{background:#2563eb}
</style></head><body><div class="box">
<div class="ok">✅</div><h1>授权完成</h1>
<p>%s</p><a href="%s">返回面板</a>
</div></body></html>"""

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


PAGE = r"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>emos 签到面板</title><style>
:root{--bg:#0f1115;--card:#181c23;--line:#272d38;--fg:#e6e9ef;--dim:#8b95a6;
--ok:#3fb950;--bad:#f85149;--acc:#4c8dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:18px 16px 70px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
h1{font-size:18px;margin:0}
.sub{color:var(--dim);font-size:12px;margin-top:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px;margin-bottom:14px}
.card h2{font-size:12px;margin:0 0 12px;color:var(--dim);font-weight:600;
letter-spacing:.06em;text-transform:uppercase}
input,select{background:#0d1015;border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:8px 10px;font-size:13px;width:100%;font-family:inherit}
input:focus{outline:none;border-color:var(--acc)}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;
padding:9px 15px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover{filter:brightness(1.12)}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button.danger{background:transparent;border:1px solid #4a2528;color:var(--bad)}
button.sm{padding:5px 10px;font-size:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.row>div{flex:1;min-width:130px}
.row .fix{flex:0 0 auto;min-width:0}
label{display:block;color:var(--dim);font-size:11px;margin-bottom:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:11px;
padding:0 8px 8px;text-transform:uppercase;letter-spacing:.04em}
td{padding:9px 8px;border-top:1px solid var(--line);vertical-align:middle}
.tag{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600}
.t-ok{background:rgba(63,185,80,.14);color:var(--ok)}
.t-skip{background:rgba(139,149,166,.14);color:var(--dim)}
.t-bad{background:rgba(248,81,73,.14);color:var(--bad)}
.t-none{background:rgba(139,149,166,.1);color:var(--dim)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.dim{color:var(--dim)}
.logs{max-height:330px;overflow:auto}
.logs div{padding:6px 0;border-top:1px solid var(--line);font-size:12px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);
background:#22283a;border:1px solid var(--line);color:var(--fg);
padding:10px 18px;border-radius:10px;font-size:13px;opacity:0;
transition:opacity .25s;pointer-events:none;z-index:9;max-width:80vw}
.toast.on{opacity:1}
.chk{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--dim)}
.chk input{width:auto}
</style></head><body><div class="wrap">
<div class="top"><div><h1>🥕 emos 签到面板</h1>
<div class="sub" id="status">加载中…</div></div>
<button class="ghost sm" onclick="logout()">退出</button></div>

<div class="card"><h2>签到设置</h2>
<div class="row">
<div><label>签到时间</label><input id="c-time" placeholder="08:00"></div>
<div><label>时区</label><input id="c-tz" placeholder="Asia/Shanghai"></div>
<div><label>接口地址</label><input id="c-base" placeholder="https://emos.best"></div>
</div>
<div class="row">
<div><label>签到附言（随机多给萝卜）</label><input id="c-content" placeholder="留空也可以"></div>
<div class="fix"><label>号间隔(秒)</label><input id="c-gap" style="width:88px"></div>
</div>
<div class="row"><div class="fix"><button onclick="saveCfg()">保存</button></div></div>
</div>

<div class="card"><h2>通知</h2>
<div class="row">
<div><label>TG Bot Token</label><input id="c-tg" placeholder="123456:ABC..."></div>
<div><label>TG Chat ID</label><input id="c-chat" placeholder="8129903593"></div>
</div>
<div class="row">
<div><label>Bark Key（可选）</label><input id="c-bark" placeholder="留空不用"></div>
</div>
<div class="row" style="gap:18px">
<label class="chk"><input type="checkbox" id="n-ok"> 签到成功通知</label>
<label class="chk"><input type="checkbox" id="n-bad"> 失败通知</label>
<label class="chk"><input type="checkbox" id="n-skip"> 仅已签时也通知</label>
<label class="chk"><input type="checkbox" id="n-sum"> 合并成一条汇总</label>
</div>
<div class="row"><div class="fix"><button onclick="saveCfg()">保存</button></div>
<div class="fix"><button class="ghost" onclick="testTg()">测试通知</button></div></div>
</div>

<div class="card"><h2>添加账号</h2>
<div class="row">
<div class="fix" style="flex:0 0 130px"><input id="a-name" placeholder="备注名"></div>
<div><input id="a-token" placeholder="密钥 xx_xxx"></div>
<div class="fix"><button onclick="addAcc()">添加</button></div>
</div>
<div class="row" style="margin-top:10px">
<div><button class="ghost" style="width:100%" onclick="oauthLogin()">🔗 用 emos 账号授权登录（推荐）</button></div>
</div>
<div class="dim" style="font-size:12px;margin-top:8px;line-height:1.6">
跳转到 emos 官方授权页，同意后自动把密钥写进来，不用手动复制。
需要能公网访问本面板（或已配好反代），否则 emos 回调打不回来。
</div></div>

<div class="card"><h2>账号列表</h2>
<div style="overflow:auto"><table id="tbl">
<thead><tr><th>名称</th><th>状态</th><th>萝卜</th><th>最近</th><th></th></tr></thead>
<tbody></tbody></table></div></div>

<div class="card"><h2>最近日志</h2>
<div class="logs" id="logs"><div class="dim">—</div></div></div>
</div><div class="toast" id="toast"></div>
<script>
function api(p,o){o=o||{};o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});
o.credentials='same-origin';return fetch(p,o).then(r=>{if(r.status===401){location.href='/login';throw 0;}return r.json();});}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('on');
setTimeout(()=>t.classList.remove('on'),1900);}
function tag(s){if(s==='signed')return '<span class="tag t-ok">已签到</span>';
if(s==='already')return '<span class="tag t-skip">今日已签</span>';
if(s==='failed')return '<span class="tag t-bad">失败</span>';
return '<span class="tag t-none">未运行</span>';}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function loadCfg(){api('/api/config').then(c=>{
document.getElementById('c-time').value=c.sign_time||'';
document.getElementById('c-tz').value=c.timezone||'';
document.getElementById('c-base').value=c.base_url||'';
document.getElementById('c-content').value=c.content||'';
document.getElementById('c-gap').value=c.gap_seconds||8;
document.getElementById('c-tg').value=c.tg_bot_token||'';
document.getElementById('c-chat').value=c.tg_chat_id||'';
document.getElementById('c-bark').value=c.bark_key||'';
document.getElementById('n-ok').checked=c.notify_success==='1';
document.getElementById('n-bad').checked=c.notify_fail==='1';
document.getElementById('n-skip').checked=c.notify_already==='1';
document.getElementById('n-sum').checked=c.notify_summary==='1';
document.getElementById('status').textContent='签到时间 '+c.sign_time+' ('+c.timezone+') · '+c.base_url;
});}

function saveCfg(){
const b={sign_time:v('c-time'),timezone:v('c-tz'),base_url:v('c-base'),content:v('c-content'),
gap_seconds:parseInt(v('c-gap'))||8,tg_bot_token:v('c-tg'),tg_chat_id:v('c-chat'),bark_key:v('c-bark'),
notify_success:document.getElementById('n-ok').checked?'1':'0',
notify_fail:document.getElementById('n-bad').checked?'1':'0',
notify_already:document.getElementById('n-skip').checked?'1':'0',
notify_summary:document.getElementById('n-sum').checked?'1':'0'};
api('/api/config',{method:'POST',body:JSON.stringify(b)})
.then(r=>{toast(r.ok?'已保存':'失败: '+(r.error||''));loadCfg();});}
function v(id){return document.getElementById(id).value.trim();}

function testTg(){api('/api/notify-test',{method:'POST'})
.then(r=>toast(r.ok?'通知已发送':'发送失败: '+(r.error||'')));}

function oauthLogin(){location.href='/oauth/start';}
function addAcc(){const token=v('a-token');if(!token){toast('填密钥');return;}
api('/api/accounts',{method:'POST',body:JSON.stringify({name:v('a-name')||'未命名',token})})
.then(r=>{if(r.ok){document.getElementById('a-name').value='';
document.getElementById('a-token').value='';toast('已添加');load();}
else toast('失败: '+(r.error||''));});}

function delAcc(id){if(!confirm('删除这个账号？'))return;
api('/api/accounts?id='+id,{method:'DELETE'}).then(()=>{toast('已删除');load();});}
function toggleAcc(id,en){api('/api/accounts',{method:'PATCH',
body:JSON.stringify({id,enabled:en?0:1})}).then(()=>load());}
function runOne(id){toast('运行中…');api('/api/run',{method:'POST',
body:JSON.stringify({id})}).then(r=>{toast(r.ok?'完成':'失败');setTimeout(load,1200);});}
function runAll(){toast('全部运行中…');api('/api/run',{method:'POST',
body:JSON.stringify({})}).then(r=>{toast(r.ok?'完成':'失败');setTimeout(load,2500);});}
function logout(){api('/api/logout',{method:'POST'}).then(()=>location.href='/login');}

function load(){api('/api/accounts').then(list=>{
document.querySelector('#tbl tbody').innerHTML=list.map(a=>`<tr>
<td><b>${esc(a.name)}</b><div class="dim mono" style="font-size:11px">${esc((a.token||'').slice(0,6))}…</div></td>
<td>${tag(a.last_status)}<div class="dim" style="font-size:11px">${esc((a.last_msg||'').slice(0,42))}</div></td>
<td class="mono">${a.last_carrot==null?'—':a.last_carrot}</td>
<td class="dim" style="font-size:11px">${esc(a.last_run||'—')}</td>
<td style="white-space:nowrap">
<button class="sm ghost" onclick="runOne(${a.id})">签到</button>
<button class="sm ghost" onclick="toggleAcc(${a.id},${a.enabled})">${a.enabled?'停用':'启用'}</button>
<button class="sm danger" onclick="delAcc(${a.id})">删</button></td></tr>`).join('')
||'<tr><td colspan="5" class="dim">还没有账号</td></tr>';});
api('/api/logs').then(list=>{
document.getElementById('logs').innerHTML=list.map(l=>{
const c=l.status==='signed'?'var(--ok)':l.status==='failed'?'var(--bad)':'var(--dim)';
return `<div><span class="dim mono">${esc(l.ts)}</span> <b style="color:${c}">${esc(l.name)}</b>
<span class="dim">${esc(l.message||'')}</span>${l.carrot?` <span style="color:var(--ok)">+${l.carrot}</span>`:''}</div>`;
}).join('')||'<div class="dim">—</div>';});}

loadCfg();load();setInterval(load,15000);
</script></body></html>"""


# ================================================================== HTTP

def _cookie_sid(handler):
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, val = part.strip().partition("=")
        if k == "sid":
            return val
    return ""


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
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

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

        # ---------- emos 官方 OAuth ----------
        if path == "/oauth/start":
            # 跳 emos 官方授权页；回调回到本站 /oauth/callback
            state = secrets.token_urlsafe(18)
            set_setting("oauth_state_" + state, str(int(time.time())))
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            back = (q.get("back") or "/")[0]
            set_setting("oauth_back_" + state, back[:200])
            cfg = cfg_all()
            pub = public_base(self)
            app_name = cfg.get("oauth_app_name") or "emoskata"
            target = "%s/link?uuid=%s&name=%s&url=%s" % (
                (cfg.get("base_url") or "https://emos.best").rstrip("/"),
                urllib.parse.quote(state),
                urllib.parse.quote(app_name),
                urllib.parse.quote(pub + "/oauth/callback"),
            )
            return self._send(302, "", "text/plain", location=target)

        if path == "/oauth/callback":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            state = (q.get("uuid") or q.get("state") or [""])[0]
            token = (q.get("token") or [""])[0].strip()
            user = (q.get("username") or [""])[0]
            uid = (q.get("user_id") or [""])[0]

            if not state or not get_setting("oauth_state_" + state):
                return self._send(400, "授权状态无效，请重新发起登录。",
                                  "text/html; charset=utf-8")
            set_setting("oauth_state_" + state, "")   # 一次性

            if not token:
                return self._send(400, "授权被拒绝或未返回密钥。",
                                  "text/html; charset=utf-8")

            # 验证密钥真实有效
            try:
                prof = EmosClient(token, cfg_all().get("base_url")).profile()
                disp = prof.get("_display") or user or uid or "emos 账号"
            except EmosError as e:
                return self._send(400, "密钥验证失败：%s" % e.message,
                                  "text/html; charset=utf-8")

            with _write_lock, db() as conn:
                dup = conn.execute("SELECT id FROM accounts WHERE token=?",
                                   (token,)).fetchone()
                if dup:
                    acc_id, created = dup["id"], False
                else:
                    cur = conn.execute(
                        "INSERT INTO accounts(name,token,created) VALUES(?,?,?)",
                        (disp[:40], token,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    acc_id, created = cur.lastrowid, True

            back = get_setting("oauth_back_" + state) or "/"
            set_setting("oauth_back_" + state, "")
            msg = ("已添加账号 <b>%s</b>，正在返回…" % _esc(disp) if created
                   else "该账号已存在（#%d），正在返回…" % acc_id)
            return self._send(200, OAUTH_DONE_PAGE % (msg, back),
                              "text/html; charset=utf-8")

        if path == "/api/health":
            return self._send(200, {"ok": True, "time": datetime.now().isoformat()})

        if not self._authed():
            return self._deny()

        if path == "/api/accounts":
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM accounts ORDER BY id").fetchall()]
            return self._send(200, rows)

        if path == "/api/logs":
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM logs ORDER BY id DESC LIMIT 120").fetchall()]
            return self._send(200, rows)

        if path == "/api/config":
            cfg = cfg_all()
            cfg.pop("panel_pw_hash", None)
            return self._send(200, cfg)

        return self._send(404, {"ok": False, "error": "not found"})

    # ---------- POST ----------

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        if path == "/api/login":
            ip = self._ip()
            left = rl_blocked(ip)
            if left:
                return self._send(429, {"ok": False,
                                        "error": "尝试过多，%d 秒后再试" % left})
            if not verify_password(body.get("password") or ""):
                cnt = rl_fail(ip)
                rest = max(0, LOGIN_MAX_FAIL - cnt)
                return self._send(401, {"ok": False,
                                        "error": "密码错误" + ("，还剩 %d 次" % rest if rest else "，已锁定")})
            rl_ok(ip)
            tok = create_session(ip, self.headers.get("User-Agent"))
            cookie = "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (tok, SESSION_TTL)
            return self._send(200, {"ok": True}, cookie=cookie)

        if path == "/api/logout":
            drop_session(_cookie_sid(self))
            return self._send(200, {"ok": True}, cookie="sid=; Path=/; Max-Age=0")

        if not self._authed():
            return self._deny()

        if path == "/api/accounts":
            token = (body.get("token") or "").strip()
            if not token:
                return self._send(400, {"ok": False, "error": "缺少密钥"})
            name = (body.get("name") or "未命名").strip()[:40]
            with _write_lock, db() as conn:
                conn.execute(
                    "INSERT INTO accounts(name,token,created) VALUES(?,?,?)",
                    (name, token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            return self._send(200, {"ok": True})

        if path == "/api/run":
            cfg = cfg_all()
            only = body.get("id")
            if _run_lock.locked():
                return self._send(409, {"ok": False, "error": "正在运行中，稍等"})
            threading.Thread(target=run_all, args=(cfg, "manual", only),
                             daemon=True).start()
            return self._send(200, {"ok": True})

        if path == "/api/config":
            for k in ("base_url", "timezone", "tg_bot_token", "tg_chat_id",
                      "bark_key", "content"):
                if k in body:
                    set_setting(k, str(body[k]).strip())
            if "sign_time" in body:
                set_setting("sign_time", clamp_sign_time(body["sign_time"]))
            if "gap_seconds" in body:
                try:
                    set_setting("gap_seconds", max(2, int(body["gap_seconds"])))
                except Exception:
                    pass
            for k in ("notify_success", "notify_fail", "notify_already", "notify_summary"):
                if k in body:
                    set_setting(k, "1" if str(body[k]) == "1" else "0")
            if body.get("new_password"):
                pw = str(body["new_password"])
                if len(pw) >= 6:
                    set_setting("panel_pw_hash", hash_password(pw))
                    drop_all_sessions()
            return self._send(200, {"ok": True})

        if path == "/api/notify-test":
            ok, err = notify(cfg_all(), "🥕 emos 签到面板",
                             "这是一条测试消息。\n配置正常。")
            return self._send(200, {"ok": ok, "error": None if ok else err})

        return self._send(404, {"ok": False, "error": "not found"})

    # ---------- PATCH / DELETE ----------

    def do_PATCH(self):
        if not self._authed():
            return self._deny()
        body = self._body()
        if urllib.parse.urlparse(self.path).path == "/api/accounts":
            with _write_lock, db() as conn:
                conn.execute("UPDATE accounts SET enabled=? WHERE id=?",
                             (1 if body.get("enabled") else 0, body.get("id")))
            return self._send(200, {"ok": True})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        if not self._authed():
            return self._deny()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if urllib.parse.urlparse(self.path).path == "/api/accounts":
            try:
                aid = int(q.get("id", [0])[0])
            except Exception:
                aid = 0
            with _write_lock, db() as conn:
                conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
            return self._send(200, {"ok": True})
        return self._send(404, {"ok": False, "error": "not found"})


def bot_supervisor():
    """监听 tg_bot_token 变化，自动启停 Bot（改 token 不用重启容器）。"""
    cur_token, cur_bot = None, None
    while True:
        try:
            token = (cfg_all().get("tg_bot_token") or "").strip()
            if token != cur_token:
                if cur_bot:
                    cur_bot.stop()
                cur_token, cur_bot = token, None
                if token:
                    cur_bot = Bot(token, Store())
                    threading.Thread(target=cur_bot.run, daemon=True).start()
                    print("[bot] 已启动（token 已配置）")
                else:
                    print("[bot] 未配置 token，等待中")
        except Exception:
            traceback.print_exc()
        time.sleep(15)


def main():
    init_db()
    generated = init_password()
    cfg = cfg_all()

    port = int(os.environ.get("PORT") or 8080)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=bot_supervisor, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 60)
    print(" emos 签到面板  →  http://0.0.0.0:%d/" % port)
    if generated:
        print(" 面板密码（只显示这一次，请立刻记下）：")
        print("   %s" % generated)
    else:
        print(" 面板密码：已设置（改密码用面板或 PANEL_PASSWORD 环境变量）")
    print(" 签到时间：%s (%s)" % (cfg.get("sign_time"), cfg.get("timezone")))
    print(" TG Bot   ：%s" % ("已配置" if (cfg.get("tg_bot_token") or "").strip() else "未配置"))
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
