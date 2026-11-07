"""离线自测：不联网的部分全部走一遍，联网部分只测错误分支。"""
import os, sys, json, tempfile, shutil

os.environ.setdefault("PANEL_PASSWORD", "test1234")

tmp = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import emos
from emos import EmosClient, EmosError, clamp_sign_time, pick_content

fail = 0
def ck(name, got, want):
    global fail
    ok = got == want
    if not ok:
        fail += 1
    print("%s %-46s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))

# ---- clamp_sign_time：官方要求不能 0 点
ck("clamp 00:00 -> 01:00", clamp_sign_time("00:00"), "01:00")
ck("clamp 0:5   -> 01:05", clamp_sign_time("0:5"), "01:05")
ck("clamp 08:00 保持", clamp_sign_time("08:00"), "08:00")
ck("clamp 23:59 保持", clamp_sign_time("23:59"), "23:59")
ck("clamp 垃圾 -> 08:00", clamp_sign_time("abc"), "08:00")
ck("clamp 99:99 -> 23:59", clamp_sign_time("99:99"), "23:59")

# ---- pick_content：指定优先，否则随机非空
ck("pick 指定值", pick_content("自定义"), "自定义")
ck("pick 留空随机非空", bool(pick_content("")), True)

# ---- 客户端构造与域名降级顺序
c = EmosClient("11_abc")
ck("bases 两个域名", len(c.bases), 2)
ck("bases 首位 emos.best", c.bases[0], "https://emos.best")
ck("bases 次位 api", c.bases[1], "https://api.emos.best")

c2 = EmosClient("11_abc", "https://custom.example")
ck("自定义 base 排首位", c2.bases[0], "https://custom.example")
ck("自定义 base 不降级（防 token 泄露）", len(c2.bases), 1)

# 联网分支见 test_client.py（本地 mock），这里只测纯函数，
# 避免把 Cloudflare 的 403 当成 emos 的 401。

shutil.rmtree(tmp, ignore_errors=True)
print()
if fail:
    print("❌ %d 条失败" % fail)
    sys.exit(1)
print("✅ 全部通过")
