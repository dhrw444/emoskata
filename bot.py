"""Telegram-first emoskata entry point; Python 3.10+, standard library only.

Run: python3 app.py (Store.accounts and Store.run_user are supplied by app).
Test: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_bot
Updates are checkpointed BEFORE dispatch: a crash can lose an action, not replay it.
"""

import base64
from collections import OrderedDict, deque
import html
import json
import re
import threading
import time
import urllib.error
import urllib.request

from accounts import DomainError
from emos import EmosClient, EmosError, pick_content

POLL_TIMEOUT = 30
PAGE_SIZE = 8
HTML_LIMIT = 3500
_SECRET = re.compile(
    r"https?://api\.telegram\.org/bot[^\s<>&]+|emosLinkAgree-[^\s<>&]+|"
    r"\bBearer\s+[^\s<>&]+|\b(?:token|authorization)\s*[:=]\s*[^\s<>&]+|"
    r"\b\d+_[A-Za-z0-9_-]{6,}", re.I)


def _safe(value, limit=600):
    return _SECRET.sub("［密钥已隐藏］", str(value if value is not None else ""))[:limit]


def _esc(value, limit=600):
    return html.escape(_safe(value, limit), quote=True)


def _flag(value):
    return value is True or value == 1 or value == "1"


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
        raise DomainError("按钮参数无效，请刷新菜单。")
    number = int(value)
    if not _positive(number):
        raise DomainError("按钮参数无效，请刷新菜单。")
    return number


def _page(value):
    if not re.fullmatch(r"[0-9]{1,6}", str(value)):
        raise DomainError("页码无效，请刷新菜单。")
    return int(value)


def _html_pages(text, limit=HTML_LIMIT):
    """Keep tags/entities and UTF-16 characters intact; reserve closing tags."""
    stack, parts, pages = [], [], []
    size = 0
    for match in re.finditer(r"</?(?:b|i|code|pre|u|s)>|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);|.",
                             str(text), re.S):
        token = match.group()
        tag = re.fullmatch(r"<(/?)(b|i|code|pre|u|s)>", token)
        next_stack = list(stack)
        if tag:
            if tag[1]:
                if not stack or stack[-1] != tag[2]:
                    continue
                next_stack.pop()
            else:
                next_stack.append(tag[2])
        cost = len(token.encode("utf-16-le")) // 2
        closing = sum(len(name) + 3 for name in next_stack)
        if size + cost + closing > limit and parts:
            pages.append("".join(parts) + "".join("</%s>" % t for t in reversed(stack)))
            parts = ["<%s>" % t for t in stack]
            size = sum(map(len, parts))
        parts.append(token)
        size += cost
        stack = next_stack
    if parts:
        pages.append("".join(parts) + "".join("</%s>" % t for t in reversed(stack)))
    return pages or ["—"]


class TgError(Exception):
    """Public text never contains a request URL, token, or Telegram response body."""
    def __init__(self, message="Telegram 请求失败，请稍后再试。", code=None,
                 retry_after=0, not_modified=False):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.not_modified = not_modified


class TG:
    METHODS = {"getMe", "getUpdates", "sendMessage", "editMessageText",
               "answerCallbackQuery", "deleteMessage", "setMyCommands"}

    def __init__(self, token, store):
        self.token = (token or '').strip()
        self.store = store
        settings = store.cfg_all() if store is not None else {}
        self.base = settings.get('tg_api_base_url') or 'https://api.telegram.org'
        self.poll_timeout = int(settings.get('tg_poll_timeout') or POLL_TIMEOUT)

    def enabled(self):
        return bool(self.token)

    def _api(self, method, payload=None, timeout=POLL_TIMEOUT + 15):
        if not self.token or method not in self.METHODS:
            raise TgError("Telegram 未配置或请求方法无效。")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            '%s/bot%s/%s' % (self.base.rstrip('/'), self.token, method), data=data,
            method='POST' if data is not None else 'GET')
        if data is not None:
            req.add_header("Content-Type", "application/json")
        status = None
        try:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
            except urllib.error.HTTPError as error:
                status = error.code
                try:
                    raw = error.read(1024 * 1024 + 1)
                finally:
                    error.close()
            if len(raw) > 1024 * 1024:
                raise ValueError("oversize")
            result = json.loads(raw.decode("utf-8"))
        except Exception:
            raise TgError("Telegram 网络或响应异常，请稍后再试。", code=status) from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            result = result if isinstance(result, dict) else {}
            code = result.get("error_code", status)
            code = code if isinstance(code, int) and not isinstance(code, bool) else status
            params = result.get("parameters")
            retry = params.get("retry_after", 0) if isinstance(params, dict) else 0
            retry = min(60, max(0, retry)) if isinstance(retry, int) else 0
            unchanged = result.get("description") == "Bad Request: message is not modified"
            raise TgError(code=code, retry_after=retry, not_modified=unchanged) from None
        return result

    def send(self, chat_id, text, keyboard=None):
        pages = _html_pages(text)
        result = None
        for index, page in enumerate(pages):
            payload = {"chat_id": chat_id, "text": page, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if keyboard is not None and index == len(pages) - 1:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            result = self._api("sendMessage", payload, timeout=12)
        return result

    def edit(self, chat_id, msg_id, text, keyboard=None):
        pages = _html_pages(text)
        payload = {"chat_id": chat_id, "message_id": msg_id, "text": pages[0],
                   "parse_mode": "HTML", "disable_web_page_preview": True,
                   "reply_markup": {"inline_keyboard": keyboard or [] if len(pages) == 1 else []}}
        try:
            result = self._api("editMessageText", payload, timeout=12)
        except TgError as error:
            if not error.not_modified:
                raise
            result = {"ok": True, "result": None}
        if len(pages) > 1:
            result = self.send(chat_id, "\n".join(pages[1:]), keyboard)
        return result

    def answer(self, cb_id, text=""):
        return self._api("answerCallbackQuery", {"callback_query_id": cb_id,
                         "text": _safe(text, 100)}, timeout=5)

    def get_me(self):
        result = self._api("getMe", timeout=15).get("result")
        if not isinstance(result, dict) or result.get("is_bot") is not True:
            raise TgError("Telegram 机器人身份验证失败。")
        return result

    def delete(self, chat_id, msg_id):
        return self._api("deleteMessage", {"chat_id": chat_id, "message_id": msg_id}, timeout=5)


class _Jobs:
    """Fixed daemon workers, bounded running+queued work, optional keyed dedup."""
    def __init__(self, workers, capacity, synchronous=False):
        self.workers = max(1, int(workers))
        self.capacity = max(self.workers, int(capacity))
        self.synchronous = synchronous
        self.condition = threading.Condition()
        self.queue = deque()
        self.keys = set()
        self.threads = []
        self.closed = False
        self.accepting = True

    def submit(self, key, function, priority=False):
        key = object() if key is None else key
        with self.condition:
            if self.closed or not self.accepting or key in self.keys or len(self.keys) >= self.capacity:
                return False
            self.keys.add(key)
            if not self.synchronous:
                (self.queue.appendleft if priority else self.queue.append)((key, function))
                if not self.threads:
                    for index in range(self.workers):
                        thread = threading.Thread(target=self._worker, name="emos-bot-job-%d" % index,
                                                  daemon=True)
                        self.threads.append(thread)
                        thread.start()
                self.condition.notify()
                return True
        self._execute(key, function)
        return True

    def _execute(self, key, function):
        try:
            function()
        except Exception:
            print("[bot] 后台任务失败；敏感异常详情不记录。")
        finally:
            with self.condition:
                self.keys.discard(key)
                self.condition.notify_all()

    def _worker(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if not self.queue:
                    return
                key, function = self.queue.popleft()
            self._execute(key, function)

    def pause(self):
        with self.condition:
            self.accepting = False

    def stop(self, discard=True):
        with self.condition:
            self.closed = True
            self.accepting = False
            if discard:
                while self.queue:
                    key, _ = self.queue.popleft()
                    self.keys.discard(key)
            self.condition.notify_all()

    def resume(self):
        with self.condition:
            self.closed = False
            self.accepting = True
            self.threads = [thread for thread in self.threads if thread.is_alive()]
            self.condition.notify_all()

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        with self.condition:
            while self.keys:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True


def menu_kb(account=None):
    root = account["id"] if account else 0
    sign = "sign:%s" % root if account else "login:0"
    return [[{"text": "✅ 签到", "callback_data": sign},
             {"text": "👤 我的账号", "callback_data": "accounts:%d:0" % root}],
            [{"text": "🔗 关联账号", "callback_data": "links:%d:0" % root},
             {"text": "⚙️ 签到设置", "callback_data": "settings:%d" % root}],
            [{"text": "📜 签到记录", "callback_data": "logs:%d:0" % root},
             {"text": "🔐 登录或切换账号", "callback_data": "login:%d" % root}],
            [{"text": "🌐 网页配置", "callback_data": "web:%d" % root},
             {"text": "❓ 帮助", "callback_data": "help"}]]


HELP = """<b>🥕 emoskata · 每个人自己的签到 Bot</b>

/login — 通过 @emospg_bot 官方授权，10 分钟有效
/menu — 主菜单
/sign — 本人及直接关联各按自己的普通/随机模式签到
<code>/sign 任意附言</code> — 本人本次带附言签到，不改保存设置或目标设置
/account 或 /accounts — 我的账号与切换
/links — 当前账号的关联管理
/code — 为当前账号生成一次性授权码与关联链接
<code>/link 目标官方ID 一次性码</code> — 添加直接关联
/web — 打开本人专属网页，配置签到、关联与通知
/settings — 自动签到、时间及个人通知
<code>/time HH:MM</code> — 签到时间，不早于 01:00
/logs — 当前账号与直接关联的记录
/cancel — 取消授权或待填写输入

目标账号必须先登录本 Bot。ID 定位目标，一次性码创建申请，目标本人二次确认后才授权。
授权码最多 10 分钟、仅能使用一次。关联菜单可确认、拒绝、撤回；来源可暂停，双方可撤销。
不接收明文密钥，不提供全局账号管理。"""


class Bot:
    REQUIRED_METHODS = ("ensure_user", "user", "begin_login", "login_pending", "cancel_login",
                        "finish_login", "owned", "active", "switch", "set_account",
                        "set_preferences", "issue_code", "redeem", "links", "toggle_link",
                        "unlink", "pending_links", "review_link", "withdraw_link", "visible", "logs",
                        "set_input", "input_state", "clear_input")

    def __init__(self, token, store, *, tg=None, client_factory=None, workers=4,
                 max_jobs=32, synchronous=False, retry_base=1, retry_cap=30):
        self.store = store
        self.accounts = store.accounts
        missing = [name for name in self.REQUIRED_METHODS
                   if not callable(getattr(self.accounts, name, None))]
        if missing:
            raise TypeError("Store.accounts 缺少契约方法：" + ", ".join(missing))
        if not callable(getattr(store, "run_user", None)):
            raise TypeError("Store 缺少契约方法：run_user(tg_id, include_links=True)")
        self.tg = tg if tg is not None else TG(token, store)
        self.client_factory = client_factory if client_factory is not None else EmosClient
        self.stop_event = threading.Event()
        self.drain_event = threading.Event()
        self._jobs = _Jobs(workers, max_jobs, synchronous)
        self._control = _Jobs(2, max(8, max_jobs * 2), synchronous)
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._recent_callbacks = OrderedDict()
        self._guided = {}
        self.me = None
        self._offset_key = 'tg_update_offset'
        self._offset = self._load_offset()
        cfg = store.cfg_all()
        self.poll_timeout = int(cfg.get('tg_poll_timeout') or POLL_TIMEOUT)
        self._poll_thread = None
        self.retry_base = max(0.001, float(retry_base))
        self.retry_cap = max(self.retry_base, float(retry_cap))

    @property
    def running(self):
        return not self.stop_event.is_set()

    def begin_drain(self):
        self.drain_event.set()
        self._jobs.pause()

    def resume(self):
        self.drain_event.clear()
        self._jobs.resume()

    def stop(self, wait=False, timeout=None):
        self.stop_event.set()
        self._jobs.stop()
        self._control.stop()
        return self.join(timeout) if wait else None

    def join_jobs(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        for pool in (self._jobs, self._control):
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not pool.join(remaining):
                return False
        return True

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        if not self.join_jobs(timeout):
            return False
        thread = self._poll_thread
        if thread and thread is not threading.current_thread():
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            thread.join(remaining)
            return not thread.is_alive()
        return True

    def _load_offset(self):
        getter = getattr(self.store, 'get_setting', None)
        value = getter(self._offset_key, '0') if callable(getter) else self.store.cfg_all().get(self._offset_key, '0')
        try:
            return max(0, int(value))
        except (ValueError, TypeError, OverflowError):
            return 0

    def _accept_me(self, profile):
        if (not isinstance(profile, dict) or profile.get("is_bot") is not True
                or not _positive(profile.get("id"))
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", str(profile.get("username", "")))):
            raise TgError("Telegram 机器人身份验证失败。")
        with self._state_lock:
            same_bot = self.me is not None and self.me.get('id') == profile['id']
            self.me = {'id': profile['id'], 'is_bot': True, 'username': profile['username']}
            self._offset_key = 'tg_update_offset_%d' % profile['id']
            stored_offset = self._load_offset()
            self._offset = max(self._offset, stored_offset) if same_bot else stored_offset

    def _identity(self):
        if not self.me:
            self._accept_me(self.tg.get_me())
        return self.me["username"]

    def run(self):
        if not self.tg.enabled() or not self._run_lock.acquire(blocking=False):
            return
        self._poll_thread = threading.current_thread()
        try:
            delay = self.retry_base
            while not self.stop_event.is_set() and not self.drain_event.is_set():
                try:
                    self._accept_me(self.tg.get_me())
                    break
                except Exception:
                    print("[bot] Telegram 身份服务暂不可用，自动重试。")
                    if self.stop_event.wait(delay):
                        return
                    delay = min(self.retry_cap, delay * 2)
            if self.stop_event.is_set() or self.drain_event.is_set():
                return
            print("[bot] @%s 已上线" % self.me["username"])
            try:
                self.tg._api("setMyCommands", {"commands": [
                    {"command": command, "description": description} for command, description in
                    (("menu", "主菜单"), ("login", "官方登录或切换账号"), ("sign", "签到当前与直接关联"),
                     ("accounts", "我的账号"), ("links", "关联管理"), ("code", "生成一次性授权码"),
                     ("link", "用官方 ID 与一次性码关联"), ("settings", "签到与通知设置"),
                     ("time", "设置签到时间"), ("logs", "可见签到记录"), ("cancel", "取消输入或授权"),
                     ('web', '本人网页配置'), ('help', '帮助'))]}, timeout=12)
            except Exception:
                pass
            delay = self.retry_base
            empty_runs = 0
            while not self.stop_event.is_set() and not self.drain_event.is_set():
                try:
                    response = self.tg._api('getUpdates', {
                        'offset': self._offset, 'timeout': self.poll_timeout,
                        'allowed_updates': ['message', 'callback_query']}, timeout=self.poll_timeout + 15)
                    if not isinstance(response, dict) or response.get("ok") is not True:
                        raise TgError()
                    updates = response.get("result")
                    if not isinstance(updates, list):
                        raise TgError()
                    for update in updates:
                        if self.stop_event.is_set():
                            break
                        self._handle(update)
                    delay = self.retry_base
                    if not updates:
                        # 正常长轮询会阻塞 poll_timeout 才返回空；立刻返回说明服务端
                        # 或反代没挂住连接（本地 Bot API、提前收连接）。此时按 0.1 秒
                        # 重试会退化成 10 次/秒短轮询，既吃 CPU 又逼近官方频率限制。
                        empty_runs += 1
                        if empty_runs > 3:
                            self.stop_event.wait(min(3.0, 0.2 * empty_runs))
                        else:
                            self.stop_event.wait(0.2)
                    else:
                        empty_runs = 0
                except Exception:
                    print("[bot] 轮询或检查点暂时失败；未持久确认的动作不执行。")
                    if self.stop_event.wait(delay):
                        break
                    delay = min(self.retry_cap, delay * 2)
        finally:
            self._run_lock.release()

    def _control_call(self, function, priority=False):
        def guarded():
            try:
                function()
            except Exception:
                pass
        return self._control.submit(None, guarded, priority)

    def _handle(self, update):
        if self.stop_event.is_set() or self.drain_event.is_set() or not isinstance(update, dict):
            return
        uid = update.get("update_id")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            return
        with self._state_lock:
            if uid < self._offset:
                return
            self.store.set_setting(self._offset_key, str(uid + 1))
            self._offset = uid + 1
        callback = update.get("callback_query")
        callback = callback if isinstance(callback, dict) else None
        message = callback.get("message", {}) if callback else update.get("message", {})
        if not isinstance(message, dict):
            message = {}
        actor = (callback or message).get("from", {})
        chat = message.get("chat", {})
        actor = actor if isinstance(actor, dict) else {}
        chat = chat if isinstance(chat, dict) else {}
        tg_id, chat_id = actor.get("id"), chat.get("id")
        text = message.get("text", "") if not callback else ""
        text = text.strip() if isinstance(text, str) else ""
        mid = message.get("message_id")
        sensitive_text = ("emosLinkAgree-" in text or text.lower().startswith("/add ")
                          or bool(re.match(r"/link(?:@[A-Za-z0-9_]+)?\s", text, re.I))
                          or bool(re.match(r"/start(?:@[A-Za-z0-9_]+)?\s+associate_", text, re.I)))
        if sensitive_text and isinstance(mid, int):
            self._control_call(lambda: self.tg.delete(chat_id, mid), priority=True)
        permitted = (_positive(tg_id) and chat.get("type") == "private" and chat_id == tg_id
                     and actor.get("is_bot") is not True)
        if callback:
            cb_id = callback.get("id")
            if not isinstance(cb_id, str) or not cb_id or len(cb_id) > 256:
                return
            duplicate = False
            with self._state_lock:
                duplicate = cb_id in self._recent_callbacks
                self._recent_callbacks[cb_id] = None
                while len(self._recent_callbacks) > 2048:
                    self._recent_callbacks.popitem(last=False)
            answer = ("仅限本人私聊操作。" if not permitted else
                      "已处理，请刷新菜单。" if duplicate else "")
            self._control_call(lambda: self.tg.answer(cb_id, answer), priority=True)
            if not permitted or duplicate:
                return
        elif not permitted or not text:
            return
        data = callback.get("data", "") if callback else None
        if callback and (not isinstance(data, str) or len(data.encode("utf-8")) > 64):
            return
        # Snapshot DB-only state at arrival, before a worker can wait behind another user.
        user_at_arrival = self.accounts.user(tg_id) or {}
        root_at_arrival = user_at_arrival.get("active_account_id") or 0
        login_at_arrival = user_at_arrival.get("login_until", 0)
        login_revision = user_at_arrival.get('login_revision', 0)
        input_at_arrival = self._guided.get(tg_id)
        guided_link = not text.startswith('/') and (
            bool(input_at_arrival and input_at_arrival[0] == 'link')
            or (user_at_arrival.get('input_kind') == 'link'
                and user_at_arrival.get('input_until', 0) > int(time.time())))
        if guided_link and not sensitive_text and isinstance(mid, int):
            self._control_call(lambda: self.tg.delete(chat_id, mid), priority=True)
        is_cancel = (callback and data == 'cancel') or (
            not callback and bool(re.fullmatch(r'/cancel(?:@[A-Za-z0-9_]+)?', text, re.I)))
        if is_cancel:
            self.accounts.cancel_login(tg_id)
            self._clear_input(tg_id)
            self._control_call(lambda: self.tg.send(chat_id, '已取消待填写输入与登录授权。'))
            return
        def work():
            if self.stop_event.is_set():
                return
            try:
                self.accounts.ensure_user(tg_id, chat_id)
                if callback:
                    self._on_callback(tg_id, chat_id, mid, data, root_at_arrival)
                else:
                    self._on_command(tg_id, chat_id, text, root_at_arrival,
                                     login_at_arrival, input_at_arrival, login_revision)
            except DomainError as error:
                account = self._active(tg_id)
                self._reply(chat_id, "❌ " + _esc(str(error)), menu_kb(account))
                if (not account and not self.accounts.login_pending(tg_id)
                        and not self.accounts.input_state(tg_id)):
                    self._show_login(tg_id, chat_id)
            except Exception:
                print("[bot] 操作失败；原始输入、凭据及异常详情不记录。")
                self._reply(chat_id, "操作暂时失败，请刷新菜单后重试。", menu_kb())
        if not self._jobs.submit(tg_id, work):
            self._control_call(lambda: self.tg.send(chat_id, "已有操作在处理或队列已满。本次未执行，请稍后重试。"))

    def _reply(self, chat_id, text, keyboard=None, msg_id=None):
        if self.stop_event.is_set():
            return
        try:
            if msg_id is None:
                return self.tg.send(chat_id, text, keyboard)
            return self.tg.edit(chat_id, msg_id, text, keyboard)
        except Exception:
            print("[bot] 消息未送达；不重复执行原操作。")

    def _active(self, tg_id):
        try:
            return self.accounts.active(tg_id, required=False)
        except DomainError:
            return None

    def _root(self, tg_id, expected=None):
        account = self.accounts.active(tg_id)
        if expected is not None and account["id"] != expected:
            raise DomainError("当前账号已切换，请刷新菜单后重试。")
        return account

    def _selection(self, tg_id, expected):
        account = self._active(tg_id)
        if (account["id"] if account else 0) != expected:
            raise DomainError("当前账号已切换，旧按钮/输入已失效，请刷新菜单。")
        return account

    def _clear_input(self, tg_id):
        self.accounts.clear_input(tg_id)
        with self._state_lock:
            self._guided.pop(tg_id, None)

    def _show(self, chat_id, view, msg_id=None):
        return self._reply(chat_id, view[0], view[1], msg_id)

    def _show_login(self, tg_id, chat_id, msg_id=None, expected=None, official_id=None):
        account = self._active(tg_id)
        root = account['id'] if account else 0
        if expected is not None:
            self._selection(tg_id, expected)
            root = expected
        if official_id is None:
            self.accounts.cancel_login(tg_id)
            self._clear_input(tg_id)
            self.accounts.set_input(tg_id, 'login', ttl=600)
            user = self.accounts.user(tg_id)
            with self._state_lock:
                self._guided[tg_id] = ('login', root, user['input_until'])
            text = ('<b>🔐 本人官方登录</b>\n\n请发送你自己的 emos 用户 ID，或使用 '
                    '<code>/login 你的emosID</code>。不是 TG 数字 ID，也不是账号密钥。\n'
                    '在 emos 官网账号卡片点“欢迎”，再点“点击复制用户ID”。\n'
                    '填写后跳转 @emospg_bot 授权；回传资料必须与本次 ID 和本人 TG 一致。')
            keyboard = []
            if account:
                text += '\n当前本人账号：<code>%s</code>' % _esc(account['emos_id'])
                keyboard.append([{'text': '用当前本人 ID 重新授权', 'callback_data': 'relogin:%d' % root}])
            keyboard.extend([[{'text': '在官网查看本人 ID', 'url': 'https://emos.best/'}],
                             [{'text': '选择本人已登录账号', 'callback_data': 'accounts:%d:0' % root}],
                             [{'text': '取消输入', 'callback_data': 'cancel'}]])
            return self._reply(chat_id, text, keyboard, msg_id)
        if not isinstance(official_id, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,48}', official_id):
            raise DomainError('请输入本人 emos 用户 ID，只含字母、数字或下划线；不是密钥或 TG ID。')
        username = self._identity()
        self._selection(tg_id, root)
        payload = 'link_%s-%s' % (official_id, username)
        if len(payload) > 64:
            raise DomainError('本人 ID 与 Bot 用户名组成的官方登录参数超过 64 字符。')
        self.accounts.begin_login(tg_id, official_id, expected_root=root)
        self._clear_input(tg_id)
        text = ('<b>🔐 本人官方登录</b>\n\n本次填写的本人 emos ID：<code>%s</code>\n'
                '点击 @emospg_bot 同意授权后返回此 Bot。10 分钟有效。\n'
                '回传账号 ID 与本人 TG 必须匹配；其他用户和关联目标不能替代本人登录。\n'
                '已有账号和设置保留。密钥回传消息会尝试删除，不回显密钥。' % _esc(official_id))
        keyboard = [[{'text': '通过官方 Bot 授权本人账号', 'url': 'https://t.me/emospg_bot?start=' + payload}],
                    [{'text': '重新填写本人 ID', 'callback_data': 'login:%d' % root}],
                    [{'text': '取消授权', 'callback_data': 'cancel'}]]
        return self._reply(chat_id, text, keyboard, msg_id)

    def _show_web(self, tg_id, chat_id, expected):
        self._root(tg_id, expected)
        opener = getattr(self.store, 'web_login', None)
        if not callable(opener):
            raise DomainError('网页配置暂未启用，请联系运营者。')
        url = opener(tg_id)
        self._root(tg_id, expected)
        self._reply(chat_id, '<b>🌐 本人网页配置</b>\n\n'
                    '可在网页设置签到、关联和通知。此入口两分钟内有效且只能使用一次。'
                    '请勿转发；普通账号无法访问运营者管理页面。',
                    [[{'text': '打开我的配置网页', 'url': url}],
                     [{'text': '返回菜单', 'callback_data': 'menu'}]])

    def _show_menu(self, tg_id, chat_id, msg_id=None):
        account = self._active(tg_id)
        if not account:
            return self._show_login(tg_id, chat_id, msg_id)
        self._reply(chat_id, "<b>🥕 emoskata</b>\n\n当前：<b>%s</b>\n官方 ID：<code>%s</code>\n"
                    "签到仅包含当前账号及已启用的直接关联。" %
                    (_esc(account.get("name") or "未命名"), _esc(account.get("emos_id"))),
                    menu_kb(account), msg_id)

    def _pagination(self, rows, page, prefix):
        last = max(0, (len(rows) - 1) // PAGE_SIZE)
        page = min(page, last)
        nav = []
        if page:
            nav.append({"text": "⬅️ 上页", "callback_data": "%s:%d" % (prefix, page - 1)})
        if page < last:
            nav.append({"text": "下页 ➡️", "callback_data": "%s:%d" % (prefix, page + 1)})
        return rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, last, nav

    def _view_accounts(self, tg_id, page=0):
        rows = self.accounts.owned(tg_id)
        active = self._active(tg_id)
        root = active["id"] if active else 0
        items, page, last, nav = self._pagination(rows, page, "accounts:%d" % root)
        lines = ["<b>👤 我的账号</b> · %d/%d 页" % (page + 1, last + 1), ""]
        keyboard = []
        if not rows:
            lines.append("尚未登录账号，请通过官方 Bot 登录。")
        for account in items:
            selected = active and account["id"] == active["id"]
            name = account.get("name") or "未命名"
            lines.append("%s <b>%s</b> · ID <code>%s</code>\n%s · 自动签到%s · %s" % (
                "▶️" if selected else "👤", _esc(name), _esc(account.get("emos_id")),
                "有效" if _flag(account.get("enabled")) else "失效，请重新授权",
                "开" if _flag(account.get("auto_sign")) else "关",
                _esc(account.get("sign_time") or "默认时间")))
            keyboard.append([{"text": ("✓ " if selected else "切换至 ") + _safe(name, 30),
                              "callback_data": "switch:%d:%d" % (root, account["id"])}])
        if nav:
            keyboard.append(nav)
        keyboard.extend([[{"text": "登录另一个账号", "callback_data": "login:%d" % root}],
                         [{"text": "主菜单", "callback_data": "menu"}]])
        return "\n".join(lines), keyboard

    def _view_links(self, tg_id, page=0):
        account = self._root(tg_id)
        links = self.accounts.links(tg_id)
        rows = [("outgoing", link) for link in links.get("outgoing", [])]
        rows += [("incoming", link) for link in links.get("incoming", [])]
        pending = self.accounts.pending_links(tg_id)
        rows += [("pending_outgoing", link) for link in pending.get("outgoing", [])]
        rows += [("pending_incoming", link) for link in pending.get("incoming", [])]
        items, page, last, nav = self._pagination(rows, page, "links:%d" % account["id"])
        lines = ["<b>🔗 关联账号</b> · %d/%d 页" % (page + 1, last + 1),
                 "当前：<b>%s</b> · ID <code>%s</code>" %
                 (_esc(account.get("name")), _esc(account.get("emos_id"))),
                 "来源 → 目标，只授予直接签到权限；双方均能撤销。", ""]
        keyboard = [[{"text": "添加目标 · ID + 一次性码",
                      "callback_data": "guide:link:%d" % account["id"]}],
                    [{"text": "为当前账号生成码/链接", "callback_data": "code:%d" % account["id"]}]]
        if not rows:
            lines.append("暂无关联。目标先登录，再由目标生成一次性码。")
        for direction, link in items:
            source, target = link["source_id"], link["target_id"]
            if direction.startswith('pending_'):
                lines.append("⏳ %s <code>%s</code> → %s <code>%s</code> · 等待目标本人确认" % (
                    _esc(link.get('source_name')), _esc(link.get('source_emos_id')),
                    _esc(link.get('target_name')), _esc(link.get('target_emos_id'))))
                if direction == 'pending_incoming':
                    keyboard.append([
                        {'text': '确认直接代签', 'callback_data': 'review:%d:%d:%d:1' % (account['id'], source, target)},
                        {'text': '拒绝', 'callback_data': 'review:%d:%d:%d:0' % (account['id'], source, target)}])
                else:
                    keyboard.append([{'text': '撤回申请', 'callback_data': 'withdraw:%d:%d:%d' % (account['id'], source, target)}])
                continue
            enabled = _flag(link.get("enabled"))
            lines.append("%s %s <code>%s</code> → %s <code>%s</code> · %s" % (
                "➡️ 我代签" if direction == "outgoing" else "⬅️ 代签我",
                _esc(link.get("source_name")), _esc(link.get("source_emos_id")),
                _esc(link.get("target_name")), _esc(link.get("target_emos_id")),
                "启用" if enabled else "暂停"))
            row = []
            if direction == "outgoing":
                row.append({"text": ("暂停 " if enabled else "启用 ") + _safe(link.get("target_name"), 20),
                            "callback_data": "toggle:%d:%d" % (source, target)})
            row.append({"text": "撤销 " + _safe(link.get("target_name") if direction == "outgoing"
                                              else link.get("source_name"), 20),
                        "callback_data": "unlink:%d:%d:%d" % (account["id"], source, target)})
            keyboard.append(row)
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "主菜单", "callback_data": "menu"}])
        return "\n".join(lines), keyboard

    def _view_settings(self, tg_id):
        account = self._root(tg_id)
        user = self.accounts.user(tg_id) or {}
        def mark(value):
            return "开" if _flag(value) else "关"
        text = ("<b>⚙️ 签到设置</b>\n\n当前：<b>%s</b> · ID <code>%s</code>\n"
                "账号状态：%s\n自动签到：%s\n签到时间：%s（不早于 01:00）\n签到模式：%s\n\n"
                "<b>我的通知</b>\n成功：%s · 失败：%s · 已签：%s\n"
                "自动签到开关不会恢复失效密钥；失效时请重新官方登录。" % (
                    _esc(account.get("name")), _esc(account.get("emos_id")),
                    "有效" if _flag(account.get("enabled")) else "失效",
                    mark(account.get("auto_sign")), _esc(account.get("sign_time") or "默认时间"),
                    '随机字签到' if account.get('sign_mode') == 'random' else '普通签到（不带附言）',
                    mark(user.get("notify_success")), mark(user.get("notify_fail")),
                    mark(user.get("notify_already"))))
        keyboard = [[{'text': '✓ 普通签到' if account.get('sign_mode') != 'random' else '普通签到',
                      'callback_data': 'mode:%d:normal' % account['id']},
                     {'text': '✓ 随机字签到' if account.get('sign_mode') == 'random' else '随机字签到',
                      'callback_data': 'mode:%d:random' % account['id']}],
                    [{"text": "自动签到 " + mark(account.get("auto_sign")),
                      "callback_data": "auto:%d" % account["id"]},
                     {"text": "修改签到时间", "callback_data": "guide:time:%d" % account["id"]}],
                    [{"text": "成功通知 " + mark(user.get("notify_success")),
                      "callback_data": "pref:notify_success"},
                     {"text": "失败通知 " + mark(user.get("notify_fail")),
                      "callback_data": "pref:notify_fail"}],
                    [{"text": "已签通知 " + mark(user.get("notify_already")),
                      "callback_data": "pref:notify_already"}],
                    [{"text": "主菜单", "callback_data": "menu"}]]
        return text, keyboard

    def _view_logs(self, tg_id, page=0):
        account = self._root(tg_id)
        rows = self.accounts.logs(tg_id, limit=40)
        items, page, last, nav = self._pagination(rows, page, "logs:%d" % account["id"])
        lines = ["<b>📜 签到记录</b> · %d/%d 页" % (page + 1, last + 1),
                 "只显示当前账号和当前直接授权目标；撤销后立即不可见。", ""]
        if not rows:
            lines.append("暂无可见记录。")
        for row in items:
            icon = {"signed": "✅", "already": "⚪", "network": "🌐"}.get(row.get("status"), "❌")
            carrot = " +%s" % _esc(row["carrot"], 40) if row.get("carrot") is not None else ""
            lines.append("%s <code>%s</code> <b>%s</b>%s\n%s" % (
                icon, _esc(row.get("ts") or row.get("last_run") or "—", 40),
                _esc(row.get("name") or row.get("emos_id") or "未命名", 100), carrot,
                _esc(row.get("message") or "—", 200)))
        keyboard = [nav] if nav else []
        keyboard.append([{"text": "刷新", "callback_data": "logs:%d:%d" % (account["id"], page)},
                         {"text": "主菜单", "callback_data": "menu"}])
        return "\n".join(lines), keyboard

    @staticmethod
    def _link_args(target, code):
        if (isinstance(target, bool) or not isinstance(target, (str, int))
                or not str(target) or len(str(target)) > 256
                or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                       for character in str(target))
                or not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", code)):
            raise DomainError("请输入目标官方 ID 和正确的一次性码。")
        return str(target), code

    @staticmethod
    def _association_payload(target, code):
        """Compact v1 payload, no Emos tokens; long IDs fall back to /link text."""
        target, code = Bot._link_args(target, code)
        packed = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
        # First '-' separates encoded ID from the URL-safe one-use secret.
        payload = "associate_%s-%s" % (packed, code) if "-" not in packed else ""
        return payload if payload and len(payload) <= 64 else ""

    @staticmethod
    def _association_args(payload):
        if not isinstance(payload, str) or len(payload) > 64 or not payload.startswith("associate_"):
            raise DomainError("关联链接无效，请使用 ID 与一次性码。")
        packed, separator, code = payload[len("associate_"):].partition("-")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_]+", packed):
            raise DomainError("关联链接无效，请使用 ID 与一次性码。")
        try:
            target = base64.b64decode(packed + "=" * (-len(packed) % 4), altchars=b"-_", validate=True).decode("utf-8")
        except Exception:
            raise DomainError("关联链接无效，请使用 ID 与一次性码。") from None
        return Bot._link_args(target, code)

    def _show_code(self, tg_id, chat_id, expected):
        self._root(tg_id, expected)
        username = self._identity()
        self._root(tg_id, expected)
        issued = self.accounts.issue_code(tg_id, expected_root=expected)
        target, code = self._link_args(issued["target_emos_id"], issued["code"])
        expires = int(issued["expires_at"])
        ttl = min(600, max(0, expires - int(time.time())))
        payload = self._association_payload(target, code)
        # Authorization code is intentionally visible ONLY to its private issuer.
        text = ("<b>🔑 当前账号的一次性代签授权</b>\n\n目标官方 ID：<code>%s</code>\n"
                "一次性码：<code>%s</code>\n剩余最多 %d 秒（不超过 10 分钟），只能使用一次。\n"
                "重新生成会替换旧码。仅把本条交给你同意的代签方。\n\n"
                "代签方先登录并选择来源，再输入：\n<code>/link %s %s</code>" %
                (html.escape(target), html.escape(code), ttl, html.escape(target), html.escape(code)))
        keyboard = []
        if payload:
            keyboard.append([{"text": "使用一次性关联链接", "url": "https://t.me/%s?start=%s" % (username, payload)}])
        else:
            text += "\n官方 ID 较长，使用上面的命令关联。"
        keyboard.append([{"text": "返回关联管理", "callback_data": "links:%d:0" % expected},
                         {"text": "隐藏本条授权码", "callback_data": "hide"}])
        self._reply(chat_id, text, keyboard)

    def _redeem(self, tg_id, chat_id, expected, target, code):
        self._root(tg_id, expected)
        target, code = self._link_args(target, code)
        linked = self.accounts.redeem(tg_id, target, code, expected_root=expected)
        self._clear_input(tg_id)
        self._reply(chat_id, "⏳ 已提交关联申请：<b>%s</b> · 官方 ID <code>%s</code>\n"
                    "目标本人需打开关联菜单确认。确认前不能代签；不创建反向或递归权限。" %
                    (_esc(linked.get("name")), _esc(linked.get("emos_id"))), menu_kb(self._root(tg_id, expected)))

    def _guide(self, tg_id, chat_id, kind, expected, msg_id=None):
        self._root(tg_id, expected)
        self.accounts.set_input(tg_id, kind, ttl=600)
        user = self.accounts.user(tg_id)
        with self._state_lock:
            self._guided[tg_id] = (kind, expected, user["input_until"])
        text = ("请发送：<code>目标官方ID 一次性码</code>\n目标必须已登录本 Bot；不接收账号密钥。"
                if kind == "link" else "请发送 <code>HH:MM</code>（00:xx 调整为 01:xx）。")
        self._reply(chat_id, text + "\n输入 10 分钟有效，切换账号后失效。/cancel 取消。",
                    [[{"text": "取消输入", "callback_data": "cancel"}]], msg_id)

    def _time(self, tg_id, chat_id, expected, value):
        self._root(tg_id, expected)
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", str(value)):
            raise DomainError("时间格式必须为 HH:MM，范围 00:00–23:59。")
        value = "01" + value[2:] if value.startswith("00:") else value
        self.accounts.set_account(tg_id, expected_root=expected, sign_time=value)
        self._clear_input(tg_id)
        self._reply(chat_id, "✅ 签到时间已设为 <code>%s</code>。" % value)
        self._show(chat_id, self._view_settings(tg_id))

    def _guided_text(self, tg_id, chat_id, text, expected, state):
        kind = self.accounts.input_state(tg_id)
        user = self.accounts.user(tg_id) or {}
        if (not kind or not state or state != self._guided.get(tg_id)
                or state[0] != kind or state[2] != user.get("input_until")
                or state[2] <= int(time.time())):
            self._clear_input(tg_id)
            raise DomainError("输入会话已过期或重启后失效，请重新点击填写按钮。")
        if kind == 'login':
            self._selection(tg_id, expected)
            self._selection(tg_id, state[1])
            return self._show_login(tg_id, chat_id, expected=expected, official_id=text.strip())
        self._root(tg_id, expected)
        self._root(tg_id, state[1])
        if kind == "link":
            args = text.split()
            if len(args) != 2:
                raise DomainError("请发送：目标官方ID 一次性码；/cancel 取消。")
            return self._redeem(tg_id, chat_id, expected, args[0], args[1])
        return self._time(tg_id, chat_id, expected, text)

    def _finish_login(self, tg_id, chat_id, token, expected, login_until, login_revision):
        self._selection(tg_id, expected)
        user = self.accounts.user(tg_id) or {}
        requested_id = user.get('login_emos_id')
        if (not login_until or not requested_id or user.get("login_until") != login_until
                or user.get('login_revision') != login_revision or not self.accounts.login_pending(tg_id)):
            raise DomainError("授权请求未发起或已过期，请重新 /login。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", token):
            raise DomainError("官方回传格式无效，请重新授权。")
        base = self.store.cfg_all().get('base_url') or ''
        timeout = int(self.store.cfg_all().get('emos_timeout') or 20)
        self._reply(chat_id, '⏳ 正在验证官方账号…')
        try:
            client = self.client_factory(token, base)
            if hasattr(client, 'timeout'):
                client.timeout = timeout
            profile = client.profile()
        except EmosError:
            raise DomainError("官方账号验证失败，请重新授权或稍后再试。") from None
        except Exception:
            raise DomainError("官方账号验证暂不可用，请稍后再试。") from None
        if not isinstance(profile, dict):
            raise DomainError("官方资料无效，请重新授权。")
        official = profile.get("user_id")
        if official is None or (isinstance(official, str) and not official.strip()):
            official = profile.get("_user_id")
        if official is None or isinstance(official, bool) or not str(official).strip():
            raise DomainError("官方资料缺少账号 ID，授权未保存。")
        if str(official).strip() != requested_id:
            raise DomainError('官方回传账号 ID 与本人本次填写的 ID 不一致，账号未保存。')
        linked_tg = profile.get("telegram_user_id")
        if linked_tg in (None, ""):
            self._reply(chat_id, '官方账号尚未绑定 Telegram，当前凭证不能证明是你的本人账号。\n'
                        '请在 emos 官网完成本人 TG 绑定后重新 /login。账号未保存。',
                        [[{'text': '在 emos 官网完成本人绑定', 'url': 'https://emos.best/'}]])
            return
        if isinstance(linked_tg, str) and re.fullmatch(r"[0-9]{1,19}", linked_tg):
            linked_tg = int(linked_tg)
        if not _positive(linked_tg) or linked_tg != tg_id:
            raise DomainError("官方授权与当前 Telegram 用户不匹配。")
        self._selection(tg_id, expected)
        current = self.accounts.user(tg_id) or {}
        if (self.stop_event.is_set() or current.get("login_until") != login_until
                or current.get('login_revision') != login_revision or not self.accounts.login_pending(tg_id)):
            raise DomainError("本次授权已取消或过期，请重新登录。")
        # Domain service atomically consumes pending authorization and verifies ownership.
        account = self.accounts.finish_login(tg_id, token, profile, base_url=base,
                                             expected_root=expected, expected_login_until=login_until,
                                             expected_login_revision=login_revision)
        self._clear_input(tg_id)
        self._reply(chat_id, "✅ 已登录 <b>%s</b> · 官方 ID <code>%s</code>。" %
                    (_esc(account.get("name")), _esc(account.get("emos_id"))), menu_kb(account))

    def _run_and_report(self, tg_id, chat_id, expected, content=None):
        root = self._root(tg_id, expected)
        if not _flag(root.get("enabled")) or root.get("last_status") == "invalid":
            raise DomainError("当前账号已失效，请重新官方登录。")
        if self.stop_event.is_set():
            return
        self._reply(chat_id, "⏳ 正在签到当前账号与已启用的直接关联…")
        # Recheck AFTER message HTTP: never silently sign a newly selected root.
        self._root(tg_id, expected)
        if self.stop_event.is_set():
            return
        options = {'content': content} if content is not None else {}
        result = self.store.run_user(tg_id, include_links=True, expected_root=expected, **options)
        if result is None:
            self._reply(chat_id, "已有签到任务在运行。本次未执行，请稍后重试。", menu_kb(root))
            return
        rows = result
        if isinstance(result, dict):
            if result.get("root_id", expected) != expected:
                raise DomainError("签到时当前账号发生变化，请刷新记录。")
            rows = result.get("rows")
        if not isinstance(rows, list):
            raise TypeError("Store.run_user 返回必须为结果行列表或 None")
        if self.stop_event.is_set():
            return
        self._root(tg_id, expected)
        # A grant revoked mid-flight must no longer expose the former target's result.
        visible = {account["id"] for account in self.accounts.visible(tg_id)}
        rows = [row for row in rows if isinstance(row, dict) and row.get("id") in visible]
        if not rows:
            self._reply(chat_id, "没有可报告的签到结果。授权撤销的目标不会继续展示。", menu_kb(root))
            return
        lines = ["<b>🥕 本次签到</b>", "当前：<b>%s</b> · ID <code>%s</code>" %
                 (_esc(root.get("name")), _esc(root.get("emos_id"))), ""]
        for row in rows:
            icon = {"signed": "✅", "already": "⚪", "network": "🌐"}.get(row.get("status"), "❌")
            carrot = " +%s" % _esc(row["carrot"], 40) if row.get("carrot") is not None else ""
            lines.append("%s <b>%s</b> · <code>%s</code>%s\n%s" % (
                icon, _esc(row.get("name") or "未命名", 120), _esc(row.get("emos_id"), 128), carrot,
                _esc(row.get("message") or "—", 400)))
        self._reply(chat_id, "\n".join(lines), menu_kb(root))

    def _on_command(self, tg_id, chat_id, text, expected, login_until=0, input_state=None,
                    login_revision=0):
        if not text.startswith("/"):
            return self._guided_text(tg_id, chat_id, text, expected, input_state)
        parts = text.split()
        command = parts[0].split("@", 1)
        if len(command) == 2 and (not self.me or command[1].lower() != self.me["username"].lower()):
            return
        cmd, args = command[0].lower(), parts[1:]
        if cmd == "/start" and args:
            payload = args[0]
            if len(args) != 1:
                raise DomainError("授权或关联回传格式无效，请重新操作。")
            if payload.startswith("emosLinkAgree-"):
                return self._finish_login(tg_id, chat_id, payload[len("emosLinkAgree-"):], expected,
                                          login_until, login_revision)
            if payload.startswith("emosLinkRefuse-"):
                refused = _number(payload[len("emosLinkRefuse-"):])
                if refused != tg_id:
                    raise DomainError("拒绝回传的 Telegram ID 与本人不匹配。")
                if (not login_until or not self.accounts.login_pending(tg_id)
                        or (self.accounts.user(tg_id) or {}).get("login_until") != login_until
                        or (self.accounts.user(tg_id) or {}).get('login_revision') != login_revision):
                    raise DomainError("本次登录请求已过期或未发起。")
                self.accounts.cancel_login(tg_id)
                self._reply(chat_id, "已取消本次官方授权。", menu_kb(self._active(tg_id)))
                return
            if payload.startswith("associate_"):
                target, code = self._association_args(payload)
                return self._redeem(tg_id, chat_id, expected, target, code)
            raise DomainError("未知的启动参数，请使用菜单。")
        if cmd == "/help":
            return self._reply(chat_id, HELP, menu_kb(self._active(tg_id)))
        if cmd == "/cancel":
            self.accounts.cancel_login(tg_id)
            self._clear_input(tg_id)
            return self._reply(chat_id, "已取消待填写输入与登录授权。", menu_kb(self._active(tg_id)))
        if cmd not in {"/start", "/login", "/menu", "/sign", "/account", "/accounts", "/links",
                       '/code', '/link', '/settings', '/time', '/logs', '/web'}:
            return self._reply(chat_id, "不支持此命令。请通过 /login 官方授权，使用 /help 查看命令。", menu_kb(self._active(tg_id)))
        self._selection(tg_id, expected)
        if cmd in ("/start", "/menu"):
            if args:
                raise DomainError("请直接使用 /menu。")
            return self._show_menu(tg_id, chat_id)
        if cmd == "/login":
            if len(args) > 1:
                raise DomainError('用法：/login 本人emos用户ID；不能填写账号密钥。')
            return self._show_login(tg_id, chat_id, expected=expected,
                                    official_id=args[0] if args else None)
        if cmd in ("/account", "/accounts"):
            return self._show(chat_id, self._view_accounts(tg_id))
        self._root(tg_id, expected)
        if cmd == '/web':
            if args:
                raise DomainError('请直接发送 /web 打开本人配置。')
            return self._show_web(tg_id, chat_id, expected)
        if cmd == "/sign":
            content = text.split(None, 1)[1] if args else None
            if content is not None:
                try:
                    content = pick_content(content)
                except EmosError as error:
                    raise DomainError(str(error)) from None
            return self._run_and_report(tg_id, chat_id, expected, content)
        if cmd == "/links":
            return self._show(chat_id, self._view_links(tg_id))
        if cmd == "/code":
            if args:
                raise DomainError("生成码请直接使用 /code。")
            return self._show_code(tg_id, chat_id, expected)
        if cmd == "/link":
            if not args:
                return self._guide(tg_id, chat_id, "link", expected)
            if len(args) != 2:
                raise DomainError("用法：/link 目标官方ID 一次性码。仅有 ID 无法授权。")
            return self._redeem(tg_id, chat_id, expected, args[0], args[1])
        if cmd == "/settings":
            return self._show(chat_id, self._view_settings(tg_id))
        if cmd == "/time":
            if not args:
                return self._guide(tg_id, chat_id, "time", expected)
            if len(args) != 1:
                raise DomainError("用法：/time HH:MM。")
            return self._time(tg_id, chat_id, expected, args[0])
        if cmd == "/logs":
            return self._show(chat_id, self._view_logs(tg_id))

    def _on_callback(self, tg_id, chat_id, msg_id, data, arrival_root):
        parts = data.split(":")
        action = parts[0]
        if data == "help":
            return self._reply(chat_id, HELP, menu_kb(self._active(tg_id)), msg_id)
        if data == "cancel":
            self.accounts.cancel_login(tg_id)
            self._clear_input(tg_id)
            return self._reply(chat_id, "已取消输入和登录授权。", menu_kb(self._active(tg_id)), msg_id)
        if data == "hide":
            self._control_call(lambda: self.tg.delete(chat_id, msg_id), priority=True)
            return
        if data == "menu":
            return self._show_menu(tg_id, chat_id, msg_id)
        if action == "pref" and len(parts) == 2:
            key = parts[1]
            if key not in {"notify_success", "notify_fail", "notify_already"}:
                raise DomainError("通知设置字段无效。")
            user = self.accounts.user(tg_id) or {}
            self.accounts.set_preferences(tg_id, **{key: not _flag(user.get(key))})
            return self._show(chat_id, self._view_settings(tg_id), msg_id)
        if action in {'login', 'relogin', 'sign', 'code', 'settings', 'auto', 'web'} and len(parts) == 2:
            expected = _page(parts[1]) if parts[1] == "0" else _number(parts[1])
            self._selection(tg_id, expected)
            self._selection(tg_id, arrival_root)
            if action == "login":
                return self._show_login(tg_id, chat_id, msg_id, expected)
            account = self._root(tg_id, expected)
            if action == 'relogin':
                return self._show_login(tg_id, chat_id, msg_id, expected, official_id=account['emos_id'])
            if action == 'web':
                return self._show_web(tg_id, chat_id, expected)
            if action == "sign":
                return self._run_and_report(tg_id, chat_id, expected)
            if action == "code":
                return self._show_code(tg_id, chat_id, expected)
            if action == "auto":
                self.accounts.set_account(tg_id, expected_root=expected, auto_sign=not _flag(account.get('auto_sign')))
            return self._show(chat_id, self._view_settings(tg_id), msg_id)
        if action == 'mode' and len(parts) == 3:
            expected = _number(parts[1])
            self._root(tg_id, expected)
            self._root(tg_id, arrival_root)
            self.accounts.set_account(tg_id, expected_root=expected, sign_mode=parts[2])
            return self._show(chat_id, self._view_settings(tg_id), msg_id)
        if action in {"accounts", "links", "logs"} and len(parts) == 3:
            expected = 0 if parts[1] == "0" else _number(parts[1])
            self._selection(tg_id, expected)
            self._selection(tg_id, arrival_root)
            page = _page(parts[2])
            if action == "accounts":
                view = self._view_accounts(tg_id, page)
            elif action == "links":
                view = self._view_links(tg_id, page)
            else:
                view = self._view_logs(tg_id, page)
            return self._show(chat_id, view, msg_id)
        if action == "switch" and len(parts) == 3:
            expected = 0 if parts[1] == "0" else _number(parts[1])
            self._selection(tg_id, expected)
            self._selection(tg_id, arrival_root)
            target = _number(parts[2])
            if target not in {account["id"] for account in self.accounts.owned(tg_id)}:
                raise DomainError("只能选择本人已登录的账号。")
            self.accounts.switch(tg_id, target, expected_root=expected)
            self._clear_input(tg_id)
            self.accounts.cancel_login(tg_id)
            return self._show_menu(tg_id, chat_id, msg_id)
        if action == "guide" and len(parts) == 3 and parts[1] in {"link", "time"}:
            expected = _number(parts[2])
            self._root(tg_id, expected)
            self._root(tg_id, arrival_root)
            return self._guide(tg_id, chat_id, parts[1], expected, msg_id)
        if action in ('review', 'withdraw') and len(parts) in (4, 5):
            if (action == 'review' and (len(parts) != 5 or parts[4] not in ('0', '1'))
                    or action == 'withdraw' and len(parts) != 4):
                raise DomainError('关联审核按钮参数无效。')
            expected, source, target = map(_number, parts[1:4])
            self._root(tg_id, expected)
            self._root(tg_id, arrival_root)
            pending = self.accounts.pending_links(tg_id)
            visible = pending['outgoing'] + pending['incoming']
            if not any(link['source_id'] == source and link['target_id'] == target for link in visible):
                raise DomainError('申请已过期、已处理或不属于当前账号。')
            if action == 'review':
                if expected != target:
                    raise DomainError('只有目标本人可以确认或拒绝。')
                self.accounts.review_link(tg_id, source, target, parts[4] == '1', expected_root=expected)
            else:
                self.accounts.withdraw_link(tg_id, source, target, expected_root=expected)
            return self._show(chat_id, self._view_links(tg_id), msg_id)
        if action == "toggle" and len(parts) == 3:
            source, target = _number(parts[1]), _number(parts[2])
            self._root(tg_id, source)
            self._root(tg_id, arrival_root)
            links = self.accounts.links(tg_id)["outgoing"]
            if not any(link["source_id"] == source and link["target_id"] == target for link in links):
                raise DomainError("该直接关联已不存在，请刷新菜单。")
            self.accounts.toggle_link(tg_id, source, target, expected_root=source)
            return self._show(chat_id, self._view_links(tg_id), msg_id)
        if action == "unlink" and len(parts) == 4:
            expected, source, target = map(_number, parts[1:])
            self._root(tg_id, expected)
            self._root(tg_id, arrival_root)
            links = self.accounts.links(tg_id)
            visible = links["outgoing"] + links["incoming"]
            if not any(link["source_id"] == source and link["target_id"] == target for link in visible):
                raise DomainError("该关联已不存在或不属于当前账号，请刷新菜单。")
            self.accounts.unlink(tg_id, source, target, expected_root=expected)
            return self._show(chat_id, self._view_links(tg_id), msg_id)
        raise DomainError("按钮已失效或参数无效，请刷新 /menu。")


def start(token, store, **options):
    """Start production polling thread; returned Bot supports stop()/join()."""
    if not (token or "").strip():
        return None
    bot = Bot(token, store, **options)
    thread = threading.Thread(target=bot.run, name="emos-bot-poll", daemon=True)
    bot._poll_thread = thread
    thread.start()
    return bot
