"""用本地 mock 验证 emos 客户端的全部分支。

不依赖真实站点 —— 真实站点会被 Cloudflare 拦（沙箱 IP 直连返回 403 HTML），
那样测出来的是 CF 的行为，不是 emos 的行为。

跑：python3 test_client.py
"""

import json
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emos import EmosClient, EmosError

fail = 0


def ck(name, got, want):
    global fail
    ok = got == want
    if not ok:
        fail += 1
    print("%s %-44s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


class S:
    """服务端可调状态。"""
    mode = "ok"          # ok | already409 | already200 | invalid | boom
    calls = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        S.calls.append(("GET", self.path))
        if self.path.startswith("/api/user"):
            if S.mode == "invalid":
                return self._j(401, {"message": "Unauthenticated."})
            return self._j(200, {"data": {"username": "tester", "user_id": 7}})
        if self.path.startswith("/api/sign/check"):
            # 无 token 也返回 200 —— 这是真实行为，不能当 token 有效性判据
            return self._j(200, {"is_sign": False, "user_id": None})
        self._j(404, {})

    def do_PUT(self):
        S.calls.append(("PUT", self.path))
        if S.mode == "invalid":
            return self._j(401, {"message": "Unauthenticated."})
        if S.mode == "already409":
            return self._j(409, {"message": "already"})
        if S.mode == "already200":
            return self._j(200, {"message": "您今天已经签到过了"})
        if S.mode == "boom":
            return self._j(500, {"message": "server error"})
        return self._j(200, {"earn_point": 3, "continuous_days": 5, "sign_index": 11})


def serve():
    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


srv, port = serve()
BASE = "http://127.0.0.1:%d" % port


def run(mode):
    S.mode = mode
    S.calls = []
    c = EmosClient("11_test", BASE)
    return c.sign_once(""), S.calls


# ---- 正常签到
S.mode = "ok"
c = EmosClient("11_test", BASE)
res, calls = run("ok")
ck("正常签到 status", res["status"], "signed")
ck("正常签到 萝卜", res["point"], 3)
ck("正常签到 连续天数", res["days"], 5)
ck("正常签到 名次", res["rank"], 11)
ck("正常签到 拿到账号名", res["name"], "tester")

# ---- 409 已签
res, _ = run("already409")
ck("409 判为已签", res["status"], "already")

# ---- 200 + 已签到文案（我新加的兜底）
res, _ = run("already200")
ck("200+已签到文案 判为已签", res["status"], "already")
ck("200+已签到文案 不记萝卜", res["point"], None)

# ---- token 失效
res, _ = run("invalid")
ck("401 判为失效", res["status"], "invalid")
ck("401 说明为 Token 失效", res["message"], "Token 失效")

# ---- 服务端 500
res, _ = run("boom")
ck("500 判为失败", res["status"], "failed")
ck("500 说明含状态码", "500" in res["message"], True)

# ---- 网络不可达：status 必须是 network，不是 failed
dead = EmosClient("11_test", "http://127.0.0.1:1")
res = dead.sign_once("")
ck("连不上 判为 network", res["status"], "network")
ck("连不上 不占重试配额", res["status"] != "failed", True)

# ---- 网络异常对象带 network 标记
try:
    dead.profile()
    ck("网络异常应抛出", "no raise", "raise")
except EmosError as e:
    ck("网络异常 network=True", e.network, True)
    ck("网络异常 status=None", e.status, None)

srv.shutdown()
print()
if fail:
    print("❌ %d 条失败" % fail)
    sys.exit(1)
print("✅ 全部通过")
