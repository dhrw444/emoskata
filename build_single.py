"""从维护模块生成独立单文件，并保持模块命名空间隔离。

构建需要源码；运行生成入口不需要这些 .py 或 Node。内嵌源码在导入后释放。
Runtime: Python 3.10+ / 标准库。
Build: python3 -B build_single.py
Test: python3 -B build_single.py --check
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ('envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app')
PACKAGE = '_emoskata_embedded'


def embedded_source(name):
    source = (ROOT / (name + '.py')).read_text(encoding='utf-8')
    lines = source.splitlines(keepends=True)
    replacements = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in MODULES:
            original = ast.get_source_segment(source, node)
            transformed = ast.ImportFrom(module=PACKAGE + '.' + node.module,
                                         names=node.names, level=0)
            replacement = ast.unparse(transformed)
            start, end = node.lineno - 1, node.end_lineno - 1
            prefix = lines[start][:node.col_offset]
            suffix = lines[end][node.end_col_offset:]
            assert original and original.startswith('from '), (name, original)
            replacements.append((start, end + 1, prefix + replacement + suffix))
        elif isinstance(node, ast.Import) and any(item.name in MODULES for item in node.names):
            raise ValueError('Use explicit from-imports for embedded local modules: ' + name)
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    result = ''.join(lines)
    compile(result, name + '.py', 'exec')
    return result


def build(write=True):
    sources = {name: embedded_source(name) for name in MODULES}
    bootstrap = '''#!/usr/bin/env python3
"""emoskata 单文件入口，由 build_single.py 生成。

Run: python3 emoskata.py
Config: .env；Python 3.10+ 标准库。
稳态一个应用进程；重启前临时运行导入预检子进程。
只修改分模块源码，再重建并执行 --check；不要手改此文件。
维护契约与验收边界见 MAINTENANCE.md / VALIDATION.md。
"""
import sys as _sys
import types as _types
import importlib.util as _import_util

_SOURCES = __SOURCES__
_package_name = '_emoskata_embedded'
_package = _types.ModuleType(_package_name)
_package.__path__ = []
_package.__file__ = __file__
_sys.modules[_package_name] = _package
for _name, _source in _SOURCES.items():
    _qualified = _package_name + '.' + _name
    _module = _types.ModuleType(_qualified)
    _module.__file__ = __file__
    _module.__package__ = _package_name
    _module.__spec__ = _import_util.spec_from_loader(_qualified, loader=None)
    _sys.modules[_qualified] = _module
    setattr(_package, _name, _module)
    exec(compile(_source, __file__ + ':' + _name, 'exec'), _module.__dict__)

emos = _package.emos
accounts = _package.accounts
webui = _package.webui
bot = _package.bot
app = _package.app
main = app.main

if __name__ == '__main__':
    _SOURCES.clear()
    del _source
    if _sys.argv[1:] == ['--init-config']:
        print('唯一配置文件 .env 已准备，凭据不回显；数据库在实际启动时初始化。')
    else:
        main()
'''
    output = bootstrap.replace('__SOURCES__', repr(sources))
    compile(output, 'emoskata.py', 'exec')
    if write:
        (ROOT / 'emoskata.py').write_text(output, encoding='utf-8')
        print('Generated emoskata.py: %d bytes, %d isolated modules.' %
              (len(output.encode('utf-8')), len(sources)))
    return output


if __name__ == '__main__':
    if '--check' in __import__('sys').argv[1:]:
        # 部署使用生成入口，维护模块更新必须同步重建。
        output = build(write=False)
        current = (ROOT / 'emoskata.py').read_text(encoding='utf-8')
        if current != output:
            raise SystemExit('emoskata.py 落后于磁盘模块：请运行 python3 build_single.py 后提交。')
        print('emoskata.py 与磁盘模块一致。')
    else:
        build()
