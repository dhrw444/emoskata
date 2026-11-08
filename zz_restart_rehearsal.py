# !/usr/bin/env python3
"""删除旧入口、上传候选并验证重启及数据的历史手工演练。

固定端口和临时目录，只能串行手动运行；不进入 CI。使用假账号，不动真实配置。
Runtime: Python 3.10+ / 标准库。
Run: python3 zz_restart_rehearsal.py（先核对脚本路径和端口）
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

SRC = '/var/minis/workspace/emos-sign'
ROOT = '/tmp/rr'
PW = 'RestartFixture123'
MARKER = 'RESTART_MARKER_EVIDENCE.txt'


def env_port(root):
    """从已生成的 .env 读取端口；该文件存在后进程 PORT 不覆盖它。"""
    path = os.path.join(root, '.env')
    if not os.path.exists(path):
        return 8080
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line.startswith('PORT='):
            return int(line.split('=', 1)[1].strip().strip('"'))
    return 8080


def call(path, body=None, cookie=''):
    url = 'http://127.0.0.1:%d%s' % (env_port(ROOT), path)
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if cookie:
        headers['Cookie'] = cookie
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method='POST' if body is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), resp.headers.get('Set-Cookie', '')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400].decode('utf-8', 'replace'), ''
    except Exception as exc:        # 演练要把任何失败都记下来，不能中断
        return 0, str(exc), ''


def pid_of():
    out = subprocess.run(['ps', '-eo', 'pid,args'], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if 'emoskata.py' in line and 'zz_restart' not in line and 'ps -eo' not in line:
            return int(line.split()[0])
    return None


def wait_health(seconds=90):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.5)
        if call('/api/health')[0] == 200:
            return True
    return False


def snapshot_db(root):
    """快照账号身份、余额和用户选择；日志条数仅供观察，不作为等值判据。"""
    conn = sqlite3.connect(os.path.join(root, 'data', 'data.db'))
    conn.row_factory = sqlite3.Row
    try:
        rows = [tuple(r) for r in conn.execute(
            'SELECT id,name,emos_id,token,carrot,sign_mode,owner_tg_id FROM accounts ORDER BY id')]
        logs = conn.execute('SELECT COUNT(*) FROM logs').fetchone()[0]
        users = [tuple(r) for r in conn.execute(
            'SELECT tg_id,active_account_id FROM tg_users ORDER BY tg_id')]
        return {'accounts': rows, 'logs': logs, 'users': users}
    finally:
        conn.close()


def main():
    ok = True
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT)
    shutil.copy(os.path.join(SRC, 'emoskata.py'), os.path.join(ROOT, 'emoskata.py'))

    # PANEL_PASSWORD 要留着：首次生成 .env 时会继承它，登录才用同一个密码。
    env = {k: v for k, v in os.environ.items()
           if k not in ('PORT', 'HTTP_ENABLED', 'EMOS_ENV_FILE', 'TG_BOT_TOKEN',
                        'TG_ALLOWED_USER_IDS')}
    env.update(PANEL_PASSWORD=PW, TG_ALLOWED_USER_IDS='101',
               TG_BOT_TOKEN='', HTTP_ENABLED='1')

    log = open(os.path.join(ROOT, 'run.log'), 'w')
    proc = subprocess.Popen([sys.executable, '-B', os.path.join(ROOT, 'emoskata.py')],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if not wait_health():
        print('FAIL 首次启动没起来')
        print(open(os.path.join(ROOT, 'run.log'), encoding='utf-8').read()[:1500])
        proc.kill()
        return 1

    first_pid = pid_of()
    code, _, cookie = call('/api/login', {'password': PW})
    sid = cookie.split(';')[0]
    print('首次启动 PID=%s 登录=%s' % (first_pid, code))
    if code != 200:
        print('FAIL 首次登录失败'); proc.kill(); return 1

    # 写入虚构账号、日志及已登录用户夹具，逐字段验证数据保留。
    import sqlite3
    db = sqlite3.connect(os.path.join(ROOT, 'data', 'data.db'))
    with db:
        db.execute("INSERT INTO accounts (name, token, emos_id, enabled, auto_sign, sign_time,"
                   " sign_mode, owner_tg_id, created, carrot, carrot_at)"
                   " VALUES ('演练号', 'tokenFIXTURE1234', 'eRestart', 1, 1, '08:00',"
                   " 'random', 101, 1, 321, 1700000000)")
        aid = db.execute('SELECT id FROM accounts ORDER BY id DESC LIMIT 1').fetchone()[0]
        db.execute("INSERT INTO logs (account_id, name, ts, status, message, carrot)"
                   " VALUES (?, '演练号', 1700000000, 'signed', '演练日志', 3)", (aid,))
        db.execute("INSERT OR REPLACE INTO tg_users (tg_id, chat_id, active_account_id,"
                   " login_until, login_emos_id, login_revision, created)"
                   " VALUES (101, 101, ?, 0, '', 0, 1700000000)", (aid,))
    db.close()

    accounts_before = snapshot_db(ROOT)
    env_before = open(os.path.join(ROOT, '.env'), 'rb').read()
    print('重启前：账号=%d 用户=%d 日志=%d .env=%d 字节'
          % (len(accounts_before['accounts']), len(accounts_before['users']),
             accounts_before['logs'], len(env_before)))

    # 删除旧入口后上传完整候选，模拟部署更新。
    source = open(os.path.join(ROOT, 'emoskata.py'), encoding='utf-8').read()
    os.remove(os.path.join(ROOT, 'emoskata.py'))
    # 锚点用文件里唯一确定的那行：单文件头是 'import sys as _sys'，不是 'import sys'。
    # 注入代码必须在任何 import 之前也能跑：用 __import__ 而不是 os/pathlib。
    inject = (
        "\n# 演练注入：证明这个文件被当新核心加载了\n"
        "__import__('pathlib').Path(__file__).with_name('RESTART_MARKER_EVIDENCE.txt')"
        ".write_text(str(__import__('time').time()))\n"
    )
    anchor = '#!/usr/bin/env python3'
    assert anchor in source, '单文件缺 shebang，注入判据不成立'
    new_source = source.replace(anchor, anchor + inject, 1)
    assert inject in new_source, '注入点没找到，演练判据不成立'
    with open(os.path.join(ROOT, 'emoskata.py'), 'w', encoding='utf-8') as fh:
        fh.write(new_source)
    print('已删旧 py 并放入新 py（%d 字节）' % os.path.getsize(os.path.join(ROOT, 'emoskata.py')))

    code, body, _ = call('/api/restart', {'confirm': True}, sid)
    print('请求重启：', code, body)
    if code != 202:
        print('FAIL 重启没被接受'); proc.kill(); return 1

    # 预检也执行顶层标记；此处仍需后续 health、登录和数据判据。
    marker_path = os.path.join(ROOT, MARKER)
    statuses = []
    deadline = time.time() + 120
    reloaded = False
    while time.time() < deadline:
        time.sleep(0.5)
        sc, info, _ = call('/api/runtime', cookie=sid)
        if sc == 200:
            state = info.get('restart_status')
            if not statuses or statuses[-1] != state:
                statuses.append(state)
        if os.path.exists(marker_path):
            reloaded = True
            break
    print('restart_status 变化：%s' % statuses)

    if not reloaded:
        print('FAIL 新代码从未被加载（标记文件不存在），重启没有真的发生')
        ok = False
    else:
        print('新代码已加载：%s' % open(marker_path, encoding='utf-8').read())

    if not wait_health(60):
        print('FAIL 重启后健康检查不通过')
        ok = False
    print('重启后 PID=%s（exec 天然不变）' % pid_of())

    # 校验配置与业务字段，不把过程中的新增日志当作丢数据。
    accounts_after = snapshot_db(ROOT)
    env_after = open(os.path.join(ROOT, '.env'), 'rb').read()
    print('重启后：账号=%d 用户=%d 日志=%d .env=%d 字节'
          % (len(accounts_after['accounts']), len(accounts_after['users']),
             accounts_after['logs'], len(env_after)))
    # 日志行数不能拿来做等值判据：调度器在重启后可能真的跑了一次签到，多一条是正常的。
    # 账号身份/密钥/余额/归属 和 用户当前账号 必须逐字段一致，这些才是「数据不丢」。
    if accounts_after['accounts'] != accounts_before['accounts']:
        print('FAIL 账号数据变了：%s -> %s'
              % (accounts_before['accounts'], accounts_after['accounts'])); ok = False
    if accounts_after['users'] != accounts_before['users']:
        print('FAIL 用户绑定变了：%s -> %s'
              % (accounts_before['users'], accounts_after['users'])); ok = False
    if accounts_after['logs'] < accounts_before['logs']:
        print('FAIL 日志丢了：%d -> %d'
              % (accounts_before['logs'], accounts_after['logs'])); ok = False
    if env_after != env_before:
        print('FAIL .env 被改动了'); ok = False

    # 原管理员密码仍可认证。
    code2, _, cookie2 = call('/api/login', {'password': PW})
    print('重启后原密码登录=%s' % code2)
    if code2 != 200:
        print('FAIL 重启后登不进去'); ok = False

    # 本人权限配置保持不变。
    if b'TG_ALLOWED_USER_IDS="101"' not in env_after:
        print('FAIL 白名单在重启后丢了'); ok = False

    proc.kill()
    log.close()
    print('\nRESULT: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
