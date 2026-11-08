"""按需读取进程资源和展示地址。

CPU 比例来自两次调用之间的时间增量；不可读指标返回未知，不伪造零值。
Runtime: Python 3.10+ / 标准库；由管理页面调用。
"""
import os
from pathlib import Path
import socket
import threading
import time

_STARTED = time.monotonic()
_LOCK = threading.Lock()
_LAST = None


def container_ip():
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
    """按需采样当前进程；不可获取的指标保留未知值，不创建监控循环。

    """
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
