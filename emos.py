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
    """status=None 且 network=True 表示本机网络问题，不是账号问题。

    区分这两者很重要：断网时 8 个账号会同时失败，如果都算作账号失败，
    退避重试配额（RETRY_DELAYS 只有 3 档）一次就被烧光，之后当天不再重试。
    """

    def __init__(self, message, status=None, payload=None, network=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload
        self.network = network

    def __str__(self):
        return "%s (HTTP %s)" % (self.message, self.status) if self.status else self.message


def _safe_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"_raw": raw[:600].decode("utf-8", "replace")}


def _status_of(e):
    """把异常映射成对外状态。网络问题单独一类，不占重试配额。"""
    if e.network:
        return "network"
    return "invalid" if e.status in (401, 403) else "failed"


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
        # 配了自定义站点就只用它，不追加 emos.best 降级。
        # 否则自建站一断，token 会被发去公共服务器 —— 那是凭据泄露，
        # 而且 CF 回 403 会被误判成 token 失效。
        self.bases = [base.rstrip("/")] if base else list(BASES)
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
            # 网络层失败（DNS、超时、TLS、连接被重置）—— 标出来，
            # 上层据此判断「这次失败不该消耗重试配额」
            raise EmosError("网络错误: %s" % e, network=True)

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
            res["status"] = _status_of(e)
            res["message"] = e.message
            return res

        # 1) 查今日状态
        try:
            is_sign, _ = self.check()
        except EmosError as e:
            res["status"] = _status_of(e)
            res["message"] = e.message
            return res

        if is_sign:
            res["status"] = "already"
            res["message"] = "今日已签到"
            return res

        # 2) 执行签到
        st, body = self.do_sign(content)

        if st == 200:
            msg = body.get("message") if isinstance(body, dict) else ""
            # 有的站点用 200 + 文案表示「已签到」。必须放在 200 分支里判断，
            # 放进 else 永远走不到。只做单向纠正，反向不动，避免把真成功吞掉。
            if msg and any(k in str(msg) for k in ("已签到", "已经签到", "已签过")):
                res["status"] = "already"
                res["message"] = "今日已签到"
                return res
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
