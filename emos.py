"""emos JSON client; redirect refusal and official-host-only failover."""
import json
import random
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

BASES = ('https://emos.best', 'https://api.emos.best')
UA = 'Mozilla/5.0 (compatible; emoskata/2.0)'
TIMEOUT = 20
CONTENTS = ('签到', '打卡', '今日签到', '来签到', '今天也在', '平安喜乐', '万事顺意')


class EmosError(Exception):
    def __init__(self, message, status=None, payload=None, network=False,
                 kind=None, retry_after=0):
        super().__init__(message)
        self.message, self.status, self.payload = message, status, payload
        self.network = network
        self.kind = kind or ('network' if network else 'failed')
        self.retry_after = retry_after

    def __str__(self):
        return self.message


def _status_of(error):
    return error.kind


def _safe_json(raw):
    try:
        body = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return None
    return body if isinstance(body, dict) else None


def pick_content(preferred=''):
    content = str(preferred or '').strip() or random.choice(CONTENTS)
    if len(content) > 10:
        raise EmosError('签到附言最多 10 个字。')
    return content


def clamp_sign_time(hhmm):
    try:
        h, minute = [int(x) for x in str(hhmm).split(':')]
    except (ValueError, TypeError):
        return '08:00'
    h, minute = min(max(h, 1), 23), min(max(minute, 0), 59)
    return '%02d:%02d' % (h, minute)


def valid_base(base):
    value = str(base or BASES[0]).strip().rstrip('/')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise EmosError('接口地址必须是完整的 HTTP/HTTPS 地址，不能包含凭据或查询参数。')
    return value


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise EmosError('接口发生重定向，已停止请求以保护密钥。', code, kind='blocked')


def _already_message(body):
    text = str(body.get('message') or '').lower()
    return body.get('is_sign') is True or any(
        word in text for word in ('已签到', '已经签到', '已签过', 'already signed',
                                  'already checked'))


class EmosClient:
    def __init__(self, token, base='', timeout=TIMEOUT):
        self.before_request = None
        self.expected_user_id = None
        self.token = str(token or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', self.token) or '_' not in self.token:
            raise EmosError('登录密钥格式错误，请重新通过官方 Bot 授权。')
        endpoint = valid_base(base)
        self.bases = [endpoint]
        if endpoint in BASES:
            self.bases += [other for other in BASES if other != endpoint]
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoCredentialRedirect())

    def _once(self, method, path, base, params=None):
        url = base + path
        if params:
            url += '?' + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        req = urllib.request.Request(url, data=b'' if method in ('PUT', 'POST') else None,
                                     method=method)
        req.add_header('Authorization', 'Bearer ' + self.token)
        req.add_header('Accept', 'application/json')
        req.add_header('User-Agent', UA)
        req.add_header('Referer', base + '/')
        if callable(self.before_request) and not self.before_request():
            raise EmosError('账号授权已撤销或切换，本次停止。', kind='revoked')
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status if hasattr(response, 'status') else response.code
                raw = response.read(1048577)
                retry = response.headers.get('Retry-After', '')
        except EmosError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, OSError):
            raise EmosError('网络连接失败，稍后自动重试。', network=True) from None
        except (ValueError, UnicodeError):
            raise EmosError('请求配置无效，请检查接口地址和登录密钥。') from None
        body = _safe_json(raw) if len(raw) <= 1048576 else None
        if body is None:
            kind = 'blocked' if status in (200, 403, 429, 503) else 'failed'
            raise EmosError('接口没有返回有效 JSON，可能被网关拦截。', status, kind=kind)
        if status == 401:
            raise EmosError('登录密钥失效，请重新授权。', status, kind='invalid')
        if status == 403:
            text = str(body.get('message') or '').lower()
            if any(x in text for x in ('unauthenticated', 'invalid token', 'token expired', '密钥失效')):
                raise EmosError('登录密钥失效，请重新授权。', status, kind='invalid')
            raise EmosError('接口拒绝访问，账号未被停用。', status, kind='blocked')
        if status == 429 and not _already_message(body):
            delay = min(86400, max(0, int(retry))) if str(retry).isdigit() else 0
            raise EmosError('接口请求过多，稍后重试。', status, kind='rate', retry_after=delay)
        return status, body

    def call(self, method, path, params=None):
        last = None
        for base in list(self.bases):
            try:
                result = self._once(method, path, base, params)
                self.bases = [base] + [other for other in self.bases if other != base]
                return result
            except EmosError as error:
                last = error
                if error.kind not in ('network', 'blocked'):
                    raise
        raise last or EmosError('所有接口均不可用。', network=True)

    def profile(self):
        status, body = self.call('GET', '/api/user')
        if status != 200:
            raise EmosError('读取用户信息失败。', status)
        profile = body.get('data') if isinstance(body.get('data'), dict) else body
        uid = str(profile.get('user_id') or '').strip()
        if not uid or len(uid) > 64 or not re.fullmatch(r'[A-Za-z0-9_-]+', uid):
            raise EmosError('官方用户信息缺少有效账号 ID，登录未保存。')
        result = dict(profile)
        result['_user_id'] = uid
        result['_display'] = str(profile.get('username') or profile.get('name') or uid)
        return result

    def check(self):
        status, body = self.call('GET', '/api/sign/check')
        if status != 200 or not isinstance(body.get('is_sign'), bool):
            raise EmosError('签到状态响应不完整。', status)
        return body['is_sign'], body

    def do_sign(self, content=''):
        text = pick_content(content)
        status, body = self.call('PUT', '/api/user/sign', {'content': text})
        if status == 400 and not content:
            choices = [s for s in CONTENTS if s != text]
            status, body = self.call('PUT', '/api/user/sign', {'content': random.choice(choices)})
        return status, body

    def reset_token(self):
        status, body = self.call('PUT', '/api/user/resetToken')
        token = body.get('token')
        if status != 200 or not isinstance(token, str) or not token:
            raise EmosError('重置密钥响应无效。', status)
        self.token = token
        return token

    def sign_once(self, content=''):
        result = {'status': 'failed', 'message': '', 'point': None, 'days': None,
                  'rank': None, 'name': '', 'retry_after': 0}
        try:
            profile = self.profile()
            result['name'] = profile['_display']
            if self.expected_user_id and profile['_user_id'] != str(self.expected_user_id):
                raise EmosError('当前密钥的官方账号 ID 与登记账号不一致，请重新授权。', kind='invalid')
            signed, check_body = self.check()
            checked_id = check_body.get('user_id')
            if checked_id is not None and str(checked_id) != profile['_user_id']:
                raise EmosError('签到状态返回了其他账号 ID，本次未执行。')
            if signed:
                result.update(status='already', message='今日已签到')
                return result
            status, body = self.do_sign(content)
            if status == 409 or (status in (200, 429) and _already_message(body)):
                result.update(status='already', message='今日已签到')
            elif status == 200:
                fields = ('earn_point', 'continuous_days', 'sign_index')
                if not all(type(body.get(k)) is int for k in fields):
                    raise EmosError('签到成功响应缺少奖励、连签或名次字段。')
                result.update(status='signed', message='签到成功', point=body['earn_point'],
                              days=body['continuous_days'], rank=body['sign_index'])
            else:
                raise EmosError('签到未成功（HTTP %d）。' % status, status)
        except EmosError as error:
            result.update(status=error.kind, message=error.message, retry_after=error.retry_after)
        return result
