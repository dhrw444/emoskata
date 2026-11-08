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
    def __init__(self, message, status=None, network=False, kind=None, retry_after=0):
        super().__init__(message)
        self.message, self.status = message, status
        self.network = network
        self.kind = kind or ('network' if network else 'failed')
        self.retry_after = retry_after

    def __str__(self):
        return self.message


def _safe_json(raw):
    try:
        body = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return None
    return body if isinstance(body, dict) else None


def pick_content(preferred='', mode='normal'):
    if mode not in ('normal', 'random'):
        raise EmosError('签到模式必须为普通或随机字。', kind='config')
    if not isinstance(preferred, str):
        raise EmosError('签到附言必须为文字。', kind='config')
    content = preferred.strip()
    if len(content) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in content):
        raise EmosError('签到附言最多 256 字，且不能含控制字符（本程序限制）。', kind='config')
    return content or (random.choice(CONTENTS) if mode == 'random' else '')


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


def _carrot_of(profile):
    """官方 /api/user 的 carrot 字段是可用萝卜余额。

    官方只说它是 int。缺失或类型不对时返回 None 而不是 0 —— 把「读不到」
    显示成「0 个萝卜」会让人误以为账号被清了。
    """
    value = profile.get('carrot')
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _already_message(body):
    text = str(body.get('message') or '').strip().lower().rstrip('。.!！')
    return text in ('重复签到', '已签到', '已经签到', '已签过', '今日已签到',
                    '今天已签到', '您今天已经签到过了', 'already signed', 'already checked')


class EmosClient:
    def __init__(self, token, base='', timeout=TIMEOUT):
        self.before_request = None
        self.expected_user_id = None
        self.expected_telegram_id = None
        self.token = str(token or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', self.token):
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
        if status == 429 and not (path == '/api/user/sign' and _already_message(body)):
            delay = min(86400, max(0, int(retry))) if str(retry).isdigit() else 0
            raise EmosError('接口请求过多，稍后重试。', status, kind='rate', retry_after=delay)
        return status, body

    def call(self, method, path, params=None):
        """GET 可以在备用域名上重试；PUT /api/user/sign 不是幂等请求。

        签到换域名重发等于向官方多提交一次签到尝试（官方明确禁止接口轮询）。
        网络中断时请求可能已经到达服务器并签到成功，所以只报 network，
        由本地退避稍后再查；网关拦截（blocked，403/HTML）确定没有执行，可以换域名。
        """
        bases = list(self.bases)
        non_idempotent = method in ('PUT', 'POST', 'DELETE')
        last = None
        for index, base in enumerate(bases):
            try:
                result = self._once(method, path, base, params)
                self.bases = [base] + [other for other in self.bases if other != base]
                return result
            except EmosError as error:
                last = error
                if error.kind not in ('network', 'blocked'):
                    raise
                if non_idempotent and error.kind == 'network':
                    raise
                if index + 1 >= len(bases):
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
        result['_carrot'] = _carrot_of(profile)
        return result

    def check(self):
        """Official '判断是否登录': is_sign is logged-in state, not today's sign-in."""
        status, body = self.call('GET', '/api/sign/check')
        if status != 200 or not isinstance(body.get('is_sign'), bool):
            raise EmosError('登录状态响应不完整。', status)
        if body['is_sign'] and not body.get('user_id'):
            raise EmosError('登录状态缺少官方账号 ID。', status)
        return body['is_sign'], body

    def do_sign(self, content=''):
        """content 已由 sign_once 校验并选定；空字符串表示不带附言的普通签到。"""
        return self.call('PUT', '/api/user/sign', {'content': content} if content else None)

    def sign_once(self, content='', mode='normal'):
        result = {'status': 'failed', 'message': '', 'point': None, 'days': None,
                  'rank': None, 'name': '', 'retry_after': 0, 'carrot': None}
        try:
            content = pick_content(content, mode)
            profile = self.profile()
            result['name'] = profile['_display']
            result['carrot'] = profile.get('_carrot')
            if self.expected_user_id and profile['_user_id'] != str(self.expected_user_id):
                raise EmosError('当前密钥的官方账号 ID 与登记账号不一致，请重新授权。', kind='invalid')
            if self.expected_telegram_id is not None:
                linked = profile.get('telegram_user_id')
                if isinstance(linked, str) and re.fullmatch(r'[0-9]{1,19}', linked):
                    linked = int(linked)
                if type(linked) is not int or linked != self.expected_telegram_id:
                    raise EmosError('官方 TG 绑定已取消或改变，请本人重新授权。', kind='invalid')
            if 'sign' not in profile:
                raise EmosError('官方用户资料缺少当天签到信息，本次未执行。')
            today_sign = profile['sign']
            if today_sign is not None and not isinstance(today_sign, dict):
                raise EmosError('官方当天签到信息结构不完整，本次未执行。')
            # 官方原文只说「当天未签到时返回 null」。空对象不是已签证据，
            # 当成已签会白丢一天萝卜；宁可多一次 PUT，官方会用 429 重复签到告诉我们。
            if today_sign:
                result.update(status='already', message='今日已签到')
                return result
            status, body = self.do_sign(content)
            if status in (200, 429) and _already_message(body):
                result.update(status='already', message='今日已签到')
            elif status == 200:
                fields = ('earn_point', 'continuous_days', 'sign_index')
                if not all(type(body.get(k)) is int for k in fields):
                    raise EmosError('签到成功响应缺少奖励、连签或名次字段。')
                result.update(status='signed', message='签到成功', point=body['earn_point'],
                              days=body['continuous_days'], rank=body['sign_index'])
            else:
                raise EmosError('签到被官方拒绝（HTTP %d），请核对附言后手动重试。' % status,
                                status, kind='rejected' if status in (400, 422) else 'failed')
        except EmosError as error:
            result.update(status=error.kind, message=error.message, retry_after=error.retry_after)
        return result
