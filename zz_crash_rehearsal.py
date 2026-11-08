#!/usr/bin/env python3
"""运行即崩演练：py 语法合法、但一导入就抛，护栏能不能把旧核心留住？

上一版是假通过：注入用了双引号锚点 `if __name__ == "__main__":`，
而那个字符串在 _SOURCES 里也出现一次，replace 打在了内嵌源码字符串上，
写坏了引号 —— 于是 400 是「真的不合法」，根本没测到「合法但启动即崩」。

这一版三处收紧：
1. 锚点用单引号真身，且取最后一次出现（rindex）。
2. 注入内容不含任何引号：`raise SystemExit(91)`。
3. 注入完先在独立进程里以 __name__='__probe__' 跑一遍，
   必须看到退出码 91 —— 证明「这段代码真的会在导入时执行」，
   判据本身先验一遍，再拿去验程序。
"""

import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = '/tmp/rcrash'
SRC = '/var/minis/workspace/emos-sign/emoskata.py'
PW = 'RestartFixture123'
PORT = 8080
ANCHOR = "if __name__ == '__main__':"
INJECT = 'raise SystemExit(91)'
PROBE = ("import sys\n"
         "p = sys.argv[1]\n"
         "s = open(p, 'rb').read()\n"
         "exec(compile(s, p, 'exec'), {'__name__': '__probe__', '__file__': p,\n"
         "                              '__package__': None, '__spec__': None})\n")


def call(path, body=None, cookie=''):
    headers = {'Content-Type': 'application/json'}
    if cookie:
        headers['Cookie'] = cookie
    req = urllib.request.Request(
        'http://127.0.0.1:%d%s' % (PORT, path),
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method='POST' if body is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), resp.headers.get('Set-Cookie', '')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode('utf-8', 'replace'), ''


def snapshot(path):
    db = sqlite3.connect(path)
    rows = db.execute('SELECT id,name,token,carrot FROM accounts ORDER BY id').fetchall()
    db.close()
    return rows


def main():
    # 端口干净：上个演练留下的进程会让「存活」判据全错
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/health' % PORT, timeout=2) as resp:
            if resp.status == 200:
                print('FAIL 端口 %d 已有人占用，先清理再跑' % PORT)
                return 1
    except Exception:
        pass

    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT)
    shutil.copy(SRC, os.path.join(ROOT, 'emoskata.py'))
    env = dict(os.environ, PANEL_PASSWORD=PW)
    for key in ('PORT', 'HTTP_ENABLED', 'EMOS_ENV_FILE', 'TG_BOT_TOKEN', 'TG_ALLOWED_USER_IDS'):
        env.pop(key, None)
    log = open(os.path.join(ROOT, 'run.log'), 'w')
    proc = subprocess.Popen([sys.executable, '-B', os.path.join(ROOT, 'emoskata.py')],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(0.5)
        if proc.poll() is not None:
            print('FAIL 起不来'); log.close(); return 1
        try:
            if call('/api/health')[0] == 200:
                break
        except Exception:
            continue
    code, _, cookie = call('/api/login', {'password': PW})
    if code != 200:
        print('FAIL 登录失败'); proc.kill(); log.close(); return 1
    sid = cookie.split(';')[0]

    db_path = os.path.join(ROOT, 'data', 'data.db')
    db = sqlite3.connect(db_path)
    with db:
        db.execute("INSERT INTO accounts (name, token, emos_id, enabled, auto_sign, sign_time,"
                   " sign_mode, owner_tg_id, created, carrot, carrot_at)"
                   " VALUES ('崩溃演练号', 'tokenFIXTURE8888', 'eCrash', 1, 0, '08:00',"
                   " 'normal', 101, 1, 999, 1700000000)")
    db.commit()
    db.close()
    before = snapshot(db_path)
    print('演练前账号：%s' % (before,))

    path = os.path.join(ROOT, 'emoskata.py')
    source = open(path, encoding='utf-8').read()
    at = source.rindex(ANCHOR)
    marker = source[at - 60:at]
    assert '_SOURCES' not in marker, '锚点落在内嵌源码里了'
    source = source[:at] + INJECT + '\n\n' + source[at:]
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(source)

    # 判据自检 1：注入后语法仍然合法（否则 400 说明的是「不合法」，不是「启动即崩」）
    ast.parse(open(path, encoding='utf-8').read())
    print('注入后语法合法 = True')

    # 判据自检 2：这段注入确实会在「导入」时执行 —— 用 __probe__ 跑，期望退出码 91
    probe = subprocess.run([sys.executable, '-B', '-c', PROBE, path],
                           cwd=ROOT, capture_output=True, timeout=120)
    print('探针退出码 = %s（期望 91）' % probe.returncode)
    if probe.returncode != 91:
        print('FAIL 注入没在导入期生效，这次演练没有判据效力')
        print(probe.stderr.decode('utf-8', 'replace')[-800:])
        proc.kill(); log.close(); return 1

    code, info, _ = call('/api/restart', {'confirm': True}, sid)
    print('重启请求：%s %s' % (code, info))
    # 这个场景的正确结果是 400：自检发现新 py 一导入就崩，拒绝切换，旧核心继续服务。
    # 202 说明门禁又漏了；401/409 说明请求根本没走到门禁，没有判据效力。
    # 注意 call() 在 4xx 时返回的是响应体字符串，不是 dict —— 判据别按 dict 写。
    body_text = info.get('error', '') if isinstance(info, dict) else str(info)
    guard_caught = code == 400 and '自检' in body_text
    print('门禁拦截=%s' % guard_caught)
    if code not in (400, 202):
        print('FAIL 重启请求未被处理，这次演练没有判据效力')
        if proc.poll() is None:
            proc.kill()
        log.close()
        return 1

    alive, waited = False, 0
    for _ in range(120):
        time.sleep(1)
        waited += 1
        try:
            if call('/api/health')[0] == 200:
                alive = True
                break
        except Exception:
            continue
    print('核心存活=%s 进程状态=%s' % (alive, proc.poll()))
    after = snapshot(db_path)
    print('演练后账号：%s' % (after,))
    print('数据一致=%s' % (after == before))
    # 文件没被切换（还是那份坏文件），说明旧核心读的是内存里的旧代码。
    still_broken = INJECT in open(path, encoding='utf-8').read()
    print('磁盘文件仍是演练注入的坏文件=%s' % still_broken)
    ok = guard_caught and alive and after == before
    print('RESULT: %s' % ('PASS' if ok else 'FAIL'))
    if proc.poll() is None:
        proc.kill()
    log.close()
    print('--- run.log 末尾 ---')
    print(open(os.path.join(ROOT, 'run.log'), encoding='utf-8').read()[-1500:])
    return 0


if __name__ == '__main__':
    sys.exit(main())
