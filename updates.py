"""同 PID 的更新事务、成功源码快照与启动失败恢复。

配置读取前 recover_entry，再 capture_image；应用就绪后 mark_ready。
exec_update 只在主线程排空旧任务后调用；没有常驻 watchdog。"""

# 公共实现同时内嵌进 -c bootstrap；恢复绝不导入磁盘上的 updates.py。
_COMMON_SOURCE = r'''
import builtins
import json
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import quote

_IMAGE_LIMIT = 2 * 1024 * 1024
_STATE_LIMIT = 128 * 1024


def _message(text):
    try:
        sys.stderr.write('[update] ' + text + '\n')
        sys.stderr.flush()
    except Exception:
        pass


def _absolute(path):
    path = os.fsdecode(os.fspath(path))
    if not path or '\x00' in path:
        raise RuntimeError('更新文件路径无效。')
    return os.path.abspath(path)


def _config_for(entry):
    selected = os.environ.get('EMOS_ENV_FILE', '.env')
    if not selected.strip():
        raise RuntimeError('更新配置路径无效。')
    return _absolute(selected if os.path.isabs(selected) else
                     os.path.join(os.path.dirname(entry), selected))


def _locations(entry, config):
    parent = os.path.dirname(config)
    base = '.' + os.path.basename(entry)
    return (os.path.join(parent, base + '.update-state.json'),
            os.path.join(parent, base + '.last-good'))


def _private(path, directory=False):
    info = os.lstat(path)
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)):
        raise RuntimeError('更新事务文件权限或类型无效。')


def _regular_if_present(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError('更新目标不是普通文件。')
    return True


def _read_image(path):
    with open(path, 'rb') as handle:
        image = handle.read(_IMAGE_LIMIT + 1)
    if not image or len(image) > _IMAGE_LIMIT:
        raise RuntimeError('更新源码为空或超过 2 MiB。')
    return image


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path, value):
    _regular_if_present(path)
    parent = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.update-write-', dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _sync_directory(parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _atomic_copy(source, destination):
    _private(source)
    _regular_if_present(destination)
    parent = os.path.dirname(destination)
    descriptor, temporary = tempfile.mkstemp(prefix='.update-write-', dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as output, open(source, 'rb') as original:
            os.fchmod(output.fileno(), 0o600)
            shutil.copyfileobj(original, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _sync_directory(parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _write_state(path, state):
    value = json.dumps(state, ensure_ascii=True, sort_keys=True,
                       separators=(',', ':')).encode('ascii')
    if len(value) > _STATE_LIMIT:
        raise RuntimeError('更新事务记录过大。')
    _atomic_bytes(path, value)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _valid_absolute(path):
    return isinstance(path, str) and bool(path) and '\x00' not in path and path == _absolute(path)


def _validate_state(state, entry, config, state_path):
    required = {'version', 'status', 'nonce', 'entry', 'config_path', 'state_path',
                'last_good', 'stage', 'source_copy', 'config', 'databases'}
    expected_state, expected_good = _locations(entry, config)
    if (not isinstance(state, dict) or set(state) != required or
            type(state['version']) is not int or state['version'] != 1 or
            state['status'] not in ('pending', 'committed', 'rolledback') or
            state['entry'] != entry or state['config_path'] != config or
            state['state_path'] != state_path or state_path != expected_state or
            state['last_good'] != expected_good):
        raise RuntimeError('更新事务身份不匹配。')
    nonce = state['nonce']
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise RuntimeError('更新事务标识无效。')
    stage = state['stage']
    prefix = '.' + os.path.basename(entry) + '.update-stage-'
    if (not _valid_absolute(stage) or os.path.dirname(stage) != os.path.dirname(config) or
            not os.path.basename(stage).startswith(prefix) or
            len(os.path.basename(stage)) <= len(prefix)):
        raise RuntimeError('更新临时目录不匹配。')
    if state['source_copy'] != os.path.join(stage, 'last-good'):
        raise RuntimeError('更新源码快照路径无效。')
    cfg = state['config']
    if (not isinstance(cfg, dict) or set(cfg) != {'path', 'existed', 'copy'} or
            cfg['path'] != config or type(cfg['existed']) is not bool or
            cfg['copy'] != (os.path.join(stage, 'config.raw') if cfg['existed'] else None)):
        raise RuntimeError('更新配置快照路径无效。')
    records = state['databases']
    if not isinstance(records, list) or len(records) > 128:
        raise RuntimeError('更新数据库清单无效。')
    protected = {entry, config, expected_state, expected_good}
    seen = set(protected)
    for index, record in enumerate(records):
        if (not isinstance(record, dict) or set(record) != {'path', 'existed', 'copy'} or
                not _valid_absolute(record['path']) or type(record['existed']) is not bool or
                record['copy'] != (os.path.join(stage, 'db-%04d.sqlite' % index)
                                   if record['existed'] else None)):
            raise RuntimeError('更新数据库快照路径无效。')
        path = record['path']
        collateral = {path, path + '-wal', path + '-shm'}
        if (collateral & seen or path == stage or path.startswith(stage + os.sep) or
                os.path.isdir(path)):
            raise RuntimeError('更新数据库目标冲突。')
        seen.update(collateral)
    if os.path.lexists(stage):
        _private(stage, directory=True)
    elif state['status'] == 'pending':
        raise RuntimeError('更新事务快照缺失。')
    if state['status'] == 'pending':
        copies = [state['source_copy']]
        if cfg['existed']:
            copies.append(cfg['copy'])
        copies.extend(record['copy'] for record in records if record['existed'])
        for path in copies:
            _private(path)
    return state


def _load_state(entry, config, state_path):
    try:
        _private(state_path)
    except FileNotFoundError:
        return None
    try:
        with open(state_path, 'rb') as handle:
            raw = handle.read(_STATE_LIMIT + 1)
        if len(raw) > _STATE_LIMIT:
            raise ValueError('oversize')
        state = json.loads(raw, object_pairs_hook=_json_object)
        return _validate_state(state, entry, config, state_path)
    except Exception:
        raise RuntimeError('更新事务记录无效；已停止恢复。') from None


def _cleanup_stage(state):
    stage = state['stage']
    if os.path.lexists(stage):
        _private(stage, directory=True)
        shutil.rmtree(stage)
        _sync_directory(os.path.dirname(stage))


def _cleanup_quiet(state):
    try:
        _cleanup_stage(state)
    except Exception:
        _message('更新临时文件清理延后。')


def _snapshot_db(path, copy, timeout=30):
    """用 SQLite backup 复制包含已提交 WAL 的一致数据；目标须不存在。

    进度回调控制等待预算；任何异常都关闭已创建的源和副本连接。
    """
    descriptor = os.open(copy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    original = backup = None
    deadline = time.monotonic() + max(0, timeout)
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise RuntimeError('更新数据库快照超时。')
    try:
        original = sqlite3.connect('file:' + quote(path, safe='/') + '?mode=ro',
                                   uri=True, timeout=5)
        backup = sqlite3.connect(copy, timeout=5)
        original.backup(backup, pages=256, progress=progress, sleep=0.01)
        backup.execute('PRAGMA journal_mode=DELETE')
    finally:
        try:
            if backup is not None:
                backup.close()
        finally:
            if original is not None:
                original.close()
    os.chmod(copy, 0o600)
    with open(copy, 'rb') as handle:
        os.fsync(handle.fileno())


def _restore_absent(path, stage, name):
    # 仅处理事务清单中原先不存在、由候选创建的目标；不 unlink 用户目标。
    if _regular_if_present(path):
        os.replace(path, os.path.join(stage, name))
        _sync_directory(os.path.dirname(path))


def _restore_transaction(state):
    # 只由 exec 后的干净 Python 调用：候选的线程和 SQLite 连接已经消失。
    old_image = _read_image(state['source_copy'])
    _atomic_bytes(state['entry'], old_image)
    _atomic_bytes(state['last_good'], old_image)
    config = state['config']
    if config['existed']:
        _atomic_copy(config['copy'], config['path'])
    else:
        _restore_absent(config['path'], state['stage'], 'created-config')
    for index, record in enumerate(state['databases']):
        path = record['path']
        for suffix in ('-wal', '-shm'):
            auxiliary = path + suffix
            if _regular_if_present(auxiliary):
                os.unlink(auxiliary)
        if record['existed']:
            _atomic_copy(record['copy'], path)
        else:
            _restore_absent(path, state['stage'], 'created-db-%04d' % index)
            if os.path.isdir(os.path.dirname(path)):
                _sync_directory(os.path.dirname(path))
    state['status'] = 'rolledback'
    _write_state(state['state_path'], state)
    _cleanup_quiet(state)
    return old_image


def _exit_code(error):
    if isinstance(error, SystemExit):
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return error.code
    return 1
'''

# 同一个命名空间保留 helper 的 globals，同时显式绑定供静态检查识别。
_runtime = globals()
exec(compile(_COMMON_SOURCE, '<emos-update-runtime>', 'exec'), _runtime)
builtins = _runtime['builtins']
os = _runtime['os']
sys = _runtime['sys']
secrets = _runtime['secrets']
shutil = _runtime['shutil']
tempfile = _runtime['tempfile']
_IMAGE_LIMIT = _runtime['_IMAGE_LIMIT']
_absolute = _runtime['_absolute']
_config_for = _runtime['_config_for']
_locations = _runtime['_locations']
_load_state = _runtime['_load_state']
_cleanup_quiet = _runtime['_cleanup_quiet']
_cleanup_stage = _runtime['_cleanup_stage']
_message = _runtime['_message']
_read_image = _runtime['_read_image']
_atomic_bytes = _runtime['_atomic_bytes']
_write_state = _runtime['_write_state']
_private = _runtime['_private']
_regular_if_present = _runtime['_regular_if_present']
_validate_state = _runtime['_validate_state']
_snapshot_db = _runtime['_snapshot_db']
snapshot_database = _snapshot_db
_sync_directory = _runtime['_sync_directory']
del _runtime
import threading

# 回退镜像也通过 fd 启动，不再读可能被重传的入口，也不会导入新 updates。
_SOURCE_BOOTSTRAP = r'''
import os, sys
try:
    entry, config, descriptor = sys.argv[1:4]
    with os.fdopen(int(descriptor), 'rb') as handle:
        source = handle.read(2097153)
    if not source or len(source) > 2097152:
        raise RuntimeError()
    sys.argv = [entry]
    sys.path[0] = os.path.dirname(entry)
    os.environ['EMOS_ENV_FILE'] = config
    os.environ.pop('EMOS_UPDATE_ACTIVE', None)
    namespace = {'__name__': '__main__', '__file__': entry, '__package__': None,
                 '__spec__': None, '__emos_image__': source}
    exec(compile(source, entry, 'exec'), namespace)
except BaseException as error:
    code = (error.code if isinstance(error, SystemExit) and isinstance(error.code, int)
            else 0 if isinstance(error, SystemExit) and error.code is None else 1)
    if code:
        sys.stderr.write('[update] 回退应用启动已停止。\n')
    os._exit(code)
'''

ROLLBACK_BOOTSTRAP = (_COMMON_SOURCE + '\n_SOURCE_BOOTSTRAP = ' +
                      repr(_SOURCE_BOOTSTRAP) + r'''
try:
    entry, config, state_path, nonce = sys.argv[1:5]
    state = _load_state(entry, config, state_path)
    if state is None or state['nonce'] != nonce or state['status'] != 'pending':
        raise RuntimeError()
    source = _restore_transaction(state)
    environment = dict(os.environ)
    environment.pop('EMOS_UPDATE_ACTIVE', None)
    environment['EMOS_ENV_FILE'] = config
    with tempfile.TemporaryFile() as image:
        image.write(source)
        image.flush()
        image.seek(0)
        os.set_inheritable(image.fileno(), True)
        _message('启动未就绪，已恢复上一就绪版本。')
        os.execve(sys.executable, [sys.executable, '-u', '-c', _SOURCE_BOOTSTRAP,
                                  entry, config, str(image.fileno())], environment)
except BaseException:
    _message('更新恢复未完成；已停止启动。')
    os._exit(1)
''')

UPDATER_BOOTSTRAP = (_COMMON_SOURCE + '\n_ROLLBACK_BOOTSTRAP = ' +
                     repr(ROLLBACK_BOOTSTRAP) + r'''
try:
    entry, config, state_path, nonce, descriptor = sys.argv[1:6]
    state = _load_state(entry, config, state_path)
    if state is None or state['status'] != 'pending' or state['nonce'] != nonce:
        raise RuntimeError()
    if os.environ.get('EMOS_UPDATE_ACTIVE') != nonce:
        raise RuntimeError()
    with os.fdopen(int(descriptor), 'rb') as handle:
        source = handle.read(_IMAGE_LIMIT + 1)
    if not source or len(source) > _IMAGE_LIMIT:
        raise RuntimeError()
    ready = {'value': False}
    def ready_hook(hook_entry, hook_nonce):
        if hook_entry == entry and hook_nonce == nonce:
            ready['value'] = True
    builtins._emos_update_ready_hook = ready_hook
    sys.argv = [entry]
    sys.path[0] = os.path.dirname(entry)
    namespace = {'__name__': '__main__', '__file__': entry, '__package__': None,
                 '__spec__': None, '__emos_image__': source}
    failure = None
    try:
        exec(compile(source, entry, 'exec'), namespace)
    except BaseException as error:
        failure = error
    if not ready['value']:
        current = _load_state(entry, config, state_path)
        ready['value'] = bool(current and current['nonce'] == nonce and
                              current['status'] == 'committed')
    if not ready['value']:
        environment = dict(os.environ)
        environment['EMOS_ENV_FILE'] = config
        os.execve(sys.executable, [sys.executable, '-u', '-c', _ROLLBACK_BOOTSTRAP,
                                  entry, config, state_path, nonce], environment)
    if failure is not None:
        code = _exit_code(failure)
        if code:
            _message('已就绪的新版本退出；不执行回滚。')
        os._exit(code)
except BaseException:
    # bootstrap 自身失败也保留 pending，下一次普通启动继续恢复；不泄漏异常内容。
    _message('更新启动器未完成；待下一次启动恢复。')
    os._exit(1)
''')

_IMAGES = {}
_READY_ENTRIES = set()


def recover_entry(entry):
    """在任何配置导入和线程启动前调用；pending 恢复先 exec 清理旧句柄。

    无事务/当前 active nonce 返回 False；committed/rolledback 只清临时目录。
    普通启动发现 pending 时不返回：同 PID exec 恢复器并执行 last-good。
    """
    try:
        entry = _absolute(entry)
        config = _config_for(entry)
        state_path, unused = _locations(entry, config)
        state = _load_state(entry, config, state_path)
        if state is None:
            return False
        if state['status'] != 'pending':
            _cleanup_quiet(state)
            os.environ.pop('EMOS_UPDATE_ACTIVE', None)
            return False
        if os.environ.get('EMOS_UPDATE_ACTIVE') == state['nonce']:
            return False
        environment = dict(os.environ)
        environment['EMOS_ENV_FILE'] = config
        environment.pop('EMOS_UPDATE_ACTIVE', None)
        _message('检测到未完成更新，开始恢复。')
        os.execve(sys.executable, [sys.executable, '-u', '-c', ROLLBACK_BOOTSTRAP,
                                  entry, config, state_path, state['nonce']], environment)
        raise RuntimeError('更新恢复进程未能替换。')
    except Exception:
        raise RuntimeError('更新事务恢复失败；已停止启动。') from None


def capture_image(entry, source=None):
    """固定正在启动的源码 bytes；显式 source 优先，已捕获且无 source 不读磁盘。

    普通首次启动读入口最多 2 MiB + 1 字节，不自动运行 main 或 mark_ready。
    """
    try:
        entry = _absolute(entry)
        if source is None and entry in _IMAGES:
            return _IMAGES[entry]
        if source is None:
            source = _read_image(entry)
        if not isinstance(source, bytes) or not source or len(source) > _IMAGE_LIMIT:
            raise RuntimeError('更新源码镜像无效。')
        if _IMAGES.get(entry) != source:
            _READY_ENTRIES.discard(entry)
        _IMAGES[entry] = source
        return source
    except Exception:
        raise RuntimeError('启动源码捕获失败。') from None


def mark_ready(entry, config_path):
    """仅在实际初始化、bind 和核心线程启动成功后原子提交当前内存源码。"""
    try:
        entry, config = _absolute(entry), _absolute(config_path)
        if entry not in _IMAGES:
            raise RuntimeError('未捕获启动源码。')
        state_path, last_good = _locations(entry, config)
        state = _load_state(entry, config, state_path)
        active = os.environ.get('EMOS_UPDATE_ACTIVE')
        if state is not None and state['status'] == 'pending':
            if active != state['nonce']:
                raise RuntimeError('未恢复的更新事务。')
        elif active:
            raise RuntimeError('更新事务标识不匹配。')
        unchanged = False
        try:
            _private(last_good)
            unchanged = _read_image(last_good) == _IMAGES[entry]
        except (FileNotFoundError, RuntimeError):
            pass
        if not unchanged:
            _atomic_bytes(last_good, _IMAGES[entry])
        if state is not None and state['status'] == 'pending':
            state['status'] = 'committed'
            _write_state(state_path, state)
            # 先持久提交，再通知 bootstrap；清理失败不会撤销已经 ready 的候选。
            hook = getattr(builtins, '_emos_update_ready_hook', None)
            if callable(hook):
                hook(entry, state['nonce'])
            os.environ.pop('EMOS_UPDATE_ACTIVE', None)
        _READY_ENTRIES.add(entry)
        if state is not None:
            _cleanup_quiet(state)
        return last_good
    except Exception:
        raise RuntimeError('更新就绪提交失败。') from None


def exec_update(image, entry, config_path, db_paths):
    """消费可信候选 TemporaryFile，在当前 PID 启动候选；成功调用不返回。

    调用方必须主线程排空任务并关闭数据库连接。失败抛泛型 OSError/RuntimeError，
    删除本次 pending/stage，不回写当前源码、配置或数据库，候选句柄始终关闭。
    """
    state = None
    prepared = None
    published = False
    stage = None
    state_path = None
    try:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('更新只允许主线程执行。')
        entry, config = _absolute(entry), _absolute(config_path)
        state_path, last_good = _locations(entry, config)
        if entry not in _IMAGES or entry not in _READY_ENTRIES:
            raise RuntimeError('当前版本未捕获并就绪。')
        _private(last_good)
        old_image = _read_image(last_good)
        if old_image != _IMAGES[entry]:
            raise RuntimeError('上一就绪源码与当前镜像不一致。')
        previous = _load_state(entry, config, state_path)
        if previous is not None:
            if previous['status'] == 'pending':
                raise RuntimeError('已有未完成更新。')
            _cleanup_stage(previous)
        if os.environ.get('EMOS_UPDATE_ACTIVE'):
            raise RuntimeError('已有更新启动标识。')
        if image.tell() != 0:
            raise RuntimeError('候选镜像游标必须为零。')
        candidate = image.read(_IMAGE_LIMIT + 1)
        if not isinstance(candidate, bytes) or not candidate or len(candidate) > _IMAGE_LIMIT:
            raise RuntimeError('候选镜像无效。')
        # 不预编译：SyntaxError 也交给隔离 bootstrap 的同 PID 回滚流程。
        paths = []
        if isinstance(db_paths, (str, bytes, os.PathLike)):
            raise RuntimeError('数据库路径必须是清单。')
        for path in db_paths:
            path = _absolute(path)
            if path not in paths:
                paths.append(path)
            if len(paths) > 128:
                raise RuntimeError('数据库清单过大。')
        stage = tempfile.mkdtemp(prefix='.' + os.path.basename(entry) + '.update-stage-',
                                 dir=os.path.dirname(config))
        os.chmod(stage, 0o700)
        config_existed = _regular_if_present(config)
        records = []
        for index, path in enumerate(paths):
            existed = _regular_if_present(path)
            records.append({'path': path, 'existed': existed,
                            'copy': os.path.join(stage, 'db-%04d.sqlite' % index)
                                    if existed else None})
        state = {'version': 1, 'status': 'pending', 'nonce': secrets.token_hex(16),
                 'entry': entry, 'config_path': config, 'state_path': state_path,
                 'last_good': last_good, 'stage': stage,
                 'source_copy': os.path.join(stage, 'last-good'),
                 'config': {'path': config, 'existed': config_existed,
                            'copy': os.path.join(stage, 'config.raw')
                                    if config_existed else None},
                 'databases': records}
        # 在写任何目标前校验冲突/同源约束；这里尚未建立全部副本。
        verification = dict(state, status='committed')
        _validate_state(verification, entry, config, state_path)
        _atomic_bytes(state['source_copy'], old_image)
        if config_existed:
            _copy_config_raw(config, state['config']['copy'])
        for record in records:
            if record['existed']:
                _snapshot_db(record['path'], record['copy'])
        prepared = tempfile.TemporaryFile(dir=stage)
        os.fchmod(prepared.fileno(), 0o600)
        prepared.write(candidate)
        prepared.flush()
        os.fsync(prepared.fileno())
        prepared.seek(0)
        _validate_state(state, entry, config, state_path)
        # _write_state 的 replace 后 fsync 也可能失败：从开始发布就检查本 nonce。
        published = True
        _write_state(state_path, state)
        environment = dict(os.environ)
        environment['EMOS_ENV_FILE'] = config
        environment['EMOS_UPDATE_ACTIVE'] = state['nonce']
        os.set_inheritable(prepared.fileno(), True)
        _message('启动更新候选。')
        os.execve(sys.executable, [sys.executable, '-u', '-c', UPDATER_BOOTSTRAP,
                                  entry, config, state_path, state['nonce'],
                                  str(prepared.fileno())], environment)
        raise RuntimeError('更新进程未能替换。')
    except BaseException as error:
        cleanup_failed = False
        if prepared is not None:
            prepared.close()
            prepared = None
        if published:
            try:
                current = _load_state(entry, config, state_path)
                if current is not None and current['nonce'] == state['nonce'] and current['status'] == 'pending':
                    os.unlink(state_path)
                    _sync_directory(os.path.dirname(state_path))
                elif current is not None and current['status'] == 'pending':
                    raise RuntimeError()
            except Exception:
                cleanup_failed = True
        if stage is not None and not cleanup_failed:
            try:
                if os.path.lexists(stage):
                    _private(stage, directory=True)
                    shutil.rmtree(stage)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise RuntimeError('更新准备取消未完成；已停止更新。') from None
        if isinstance(error, OSError):
            raise OSError('更新准备失败，当前版本未替换。') from None
        raise RuntimeError('更新准备失败，当前版本未替换。') from None
    finally:
        if prepared is not None:
            prepared.close()
        try:
            image.close()
        except Exception:
            pass


def _copy_config_raw(source, destination):
    # 旧配置可能由现有应用以宽权限写出；只限制新备份为 0600，不改旧配置。
    _regular_if_present(source)
    with open(source, 'rb') as handle:
        raw = handle.read()
    _atomic_bytes(destination, raw)


def restore_missing_entry(entry, config_path):
    """只在入口缺失时恢复成功快照；并发出现的上传文件不会被覆盖。

    快照与事务都位于配置目录。返回恢复路径；无可信快照或读写失败返回 None。
    """
    temporary = None
    try:
        entry, config = _absolute(entry), _absolute(config_path)
        if os.path.lexists(entry):
            return None
        _, last_good = _locations(entry, config)
        _private(last_good)
        source = _read_image(last_good)
        compile(source, entry, 'exec')
        parent = os.path.dirname(entry)
        descriptor, temporary = tempfile.mkstemp(prefix='.restore-', dir=parent)
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(source)
            output.flush()
            os.fsync(output.fileno())
        # link 是排他发布：若上传在检查后完成，不能用旧快照替换它。
        os.link(temporary, entry)
        _sync_directory(parent)
        return entry
    except (OSError, RuntimeError, SyntaxError, ValueError, TypeError):
        return None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
