"""按需资源统计、独立公网 IP 探测与托管清单准备。

CPU 由进程时间增量计算；不可读指标保持未知，不设置监控轮询。
公网地址就绪后探测一次且缓存，CDN 入口与源站 IP 分开；不创建 .env。"""
import ipaddress
import os
from pathlib import Path
import socket
import stat
import threading
import time
import urllib.error
import urllib.request

_STARTED = time.monotonic()
_LOCK = threading.Lock()
_LAST = None
_PUBLIC_LOCK = threading.Lock()
_PUBLIC_THREAD = None
_PUBLIC_IP = None
_PUBLIC_STATUS = 'pending'
_PUBLIC_IP_URLS = ('https://api.ipify.org', 'https://checkip.amazonaws.com')
_REQUIREMENTS_TEXT = '# emoskata uses only the Python standard library. No pip packages are required.\n'


def prepare_requirements(entry_file):
    """补齐入口目录和当前工作目录的空依赖清单；已有普通文件不覆盖，不处理 .env。

    托管器在 Python 启动前检查文件，首次上传需一并上传或先执行 --prepare-host。"""
    directories = dict.fromkeys((Path(entry_file).absolute().parent, Path.cwd()))
    results = []
    for directory in directories:
        path = directory / 'requirements.txt'
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError('requirements.txt 必须是普通文件，拒绝覆盖链接或目录')
            results.append((str(path), False))
            continue
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(_REQUIREMENTS_TEXT)
        results.append((str(path), True))
    return results


def _global_ipv4(value):
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    return str(address) if (isinstance(address, ipaddress.IPv4Address)
                            and address.is_global and not address.is_multicast
                            and not address.is_reserved) else None


def detect_public_ip(local_ip, stopped=None):
    """返回本机公网 IPv4 或通过无凭据 HTTPS 查询出口 IPv4；失败返回 None。

    两处固定 HTTPS 服务，每次两秒 I/O 超时且只读 96 字节；DNS 和重定向不保证同一墙钟上限。
    不读 .env、不发送凭据；CDN 域名不参与探测，DNS 结果不能冒充源站 IP。"""
    direct = _global_ipv4(local_ip)
    if direct:
        return direct
    for url in _PUBLIC_IP_URLS:
        if stopped is not None and stopped.is_set():
            return None
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'emoskata-address/1.0'})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status != 200:
                    continue
                raw = response.read(97)
            if len(raw) > 96:
                continue
            address = _global_ipv4(raw.decode('ascii').strip())
            if address:
                return address
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            continue
    return None


def start_public_ip_probe(local_ip, stopped, on_result=None):
    """每进程一次后台探测，成功或失败均缓存；配置了 CDN 也执行，不设轮询。"""
    global _PUBLIC_THREAD, _PUBLIC_STATUS
    with _PUBLIC_LOCK:
        if _PUBLIC_THREAD is not None:
            return _PUBLIC_THREAD
        def work():
            global _PUBLIC_IP, _PUBLIC_STATUS
            address = detect_public_ip(local_ip, stopped)
            with _PUBLIC_LOCK:
                _PUBLIC_IP, _PUBLIC_STATUS = address, 'ready' if address else 'unavailable'
            if on_result is not None and not stopped.is_set():
                on_result(address)
        _PUBLIC_THREAD = threading.Thread(target=work, name='emos-address-probe', daemon=True)
        try:
            _PUBLIC_THREAD.start()
        except RuntimeError:
            _PUBLIC_STATUS = 'unavailable'
        return _PUBLIC_THREAD


def public_ip_status():
    """只取缓存，管理员访问不会重新访问公网服务；内网地址不冒充公网 IP。"""
    with _PUBLIC_LOCK:
        return {'server_public_ip': _PUBLIC_IP, 'public_ip_status': _PUBLIC_STATUS}


def management_url(public_base, public_ip, port):
    """CDN/公开配置决定访问入口，源站公网 IP 独立显示；无可用公网地址返回 None。"""
    if public_base:
        return str(public_base).rstrip('/') + '/admin'
    address = _global_ipv4(public_ip)
    if not address:
        return None
    suffix = '' if int(port) == 80 else ':' + str(int(port))
    return 'http://' + address + suffix + '/admin'


def container_ip():
    """通过 UDP 路由选择查询本地地址，不向探测地址发送数据；失败回落本机地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('198.51.100.1', 9))
            address = sock.getsockname()[0]
            if address and not address.startswith('127.'):
                return address
    except OSError:
        pass
    try:
        address = socket.gethostbyname(socket.gethostname())
        if address and not address.startswith('127.'):
            return address
    except OSError:
        pass
    return '127.0.0.1'


def snapshot():
    """按需读取进程 CPU 和 /proc 内存，返回当前 Python 线程数。

    首个 CPU 比例未知；峰值包含启动和 scrypt 认证，不等于稳态 RSS。"""
    global _LAST
    now, process = time.monotonic(), time.process_time()
    with _LOCK:
        percent = None
        if _LAST is not None and now > _LAST[0]:
            percent = max(0.0, (process - _LAST[1]) / (now - _LAST[0]) * 100.0)
        _LAST = (now, process)
    rss, maximum = None, None
    try:
        values = {}
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith(('VmRSS:', 'VmHWM:')):
                key, value = line.split(':', 1)
                values[key] = int(value.split()[0]) * 1024
        rss, maximum = values.get('VmRSS'), values.get('VmHWM')
    except (OSError, ValueError):
        pass
    return {'pid': os.getpid(), 'uptime_seconds': max(0, now - _STARTED),
            'cpu_percent': round(percent, 2) if percent is not None else None,
            'cpu_seconds': round(process, 3), 'rss_bytes': rss, 'max_rss_bytes': maximum,
            'threads': threading.active_count(), 'cpu_basis': '100% = one CPU core'}
