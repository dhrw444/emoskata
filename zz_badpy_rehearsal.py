# !/usr/bin/env python3
"""语法错误候选被拒绝而旧核心继续服务的手工演练。

夹具只使用临时或演练数据；本地通过不代表真实官方授权。
Test: python3 -B zz_badpy_rehearsal.py；固定演练目录必须串行使用。。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = '/tmp/rbad'
SRC = '/var/minis/workspace/emos-sign/emoskata.py'
PW = 'RestartFixture123'
PORT = 8080


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


def main():
    ok = True
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT)
    shutil.copy(SRC, os.path.join(ROOT, 'emoskata.py'))
    env = dict(os.environ, PANEL_PASSWORD=PW)
    for key in ('PORT', 'HTTP_ENABLED', 'EMOS_ENV_FILE', 'TG_BOT_TOKEN', 'TG_ALLOWED_USER_IDS'):
        env.pop(key, None)
    log = open(os.path.join(ROOT, 'run.log'), 'w')
    proc = subprocess.Popen([sys.executable, '-B', os.path.join(ROOT, 'emoskata.py')],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    for _ in range(90):
        time.sleep(0.5)
        if proc.poll() is not None:
            print('FAIL 起不来'); return 1
        try:
            if call('/api/health')[0] == 200:
                break
        except Exception:
            continue

    code, _, cookie = call('/api/login', {'password': PW})
    print('登录=%s' % code)
    if code != 200:
        print('FAIL 登录失败'); proc.kill(); return 1
    sid = cookie.split(';')[0]

    # 数据存证
    db = sqlite3.connect(os.path.join(ROOT, 'data', 'data.db'))
    with db:
        db.execute("INSERT INTO accounts (name, token, emos_id, enabled, auto_sign, sign_time,"
                   " sign_mode, owner_tg_id, created, carrot, carrot_at)"
                   " VALUES ('坏py演练号', 'tokenFIXTURE9999', 'eBadPy', 1, 1, '08:00',"
                   " 'normal', 101, 1, 654, 1700000000)")
    before = db.execute('SELECT id,name,token,carrot FROM accounts ORDER BY id').fetchall()
    db.close()
    print('存证账号：%s' % (before,))

    # 写入语法错误的 py
    bad_path = os.path.join(ROOT, 'emoskata.py')
    with open(bad_path, 'a', encoding='utf-8') as handle:
        handle.write('\n\ndef broken(:\n    pass\n')
    print('已写入语法错误的 py')

    code, info, _ = call('/api/restart', {'confirm': True}, sid)
    print('重启请求返回：%s %s' % (code, info))
    if code == 202:
        print('FAIL 坏 py 竟然被接受了'); ok = False
    else:
        # 必须明确拒绝，并且旧核心继续服务
        if '语法' not in json.dumps(info, ensure_ascii=False) and '无效' not in json.dumps(info, ensure_ascii=False):
            print('FAIL 拒绝原因不是「语法无效」：%s' % info); ok = False
        if call('/api/health')[0] != 200:
            print('FAIL 拒绝重启后旧核心也不健康了'); ok = False
        else:
            print('旧核心仍在服务（health=200）')

    # 数据必须没变
    db = sqlite3.connect(os.path.join(ROOT, 'data', 'data.db'))
    after = db.execute('SELECT id,name,token,carrot FROM accounts ORDER BY id').fetchall()
    db.close()
    if after != before:
        print('FAIL 坏 py 拒绝对数据有副作用：%s -> %s' % (before, after)); ok = False

    proc.terminate()
    try:
        proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    print('--- run.log ---')
    print(open(os.path.join(ROOT, 'run.log'), encoding='utf-8').read()[-800:])
    print('RESULT: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
