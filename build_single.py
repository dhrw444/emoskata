"""从维护模块生成独立入口，并保持模块命名空间隔离。

生成内容兼容旧版的明文 _SOURCES 校验；运行不需要旁边的模块或 Node。
Build: python3 -B build_single.py；Test: python3 -B build_single.py --check。"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ('updates', 'envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app')
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
"""emoskata 生成入口；只修改维护模块，再运行 build_single.py。

Run: python3 emoskata.py；Python 3.10+，仅标准库。
配置和 SQLite 独立保存；稳态单进程，更新预检临时使用子进程。"""
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
    if _name == 'updates':
        # 在配置与数据库模块导入前处理上次未完成的更新。
        _module.recover_entry(__file__)
        _module.capture_image(__file__, globals().get('__emos_image__'))

emos = _package.emos
accounts = _package.accounts
webui = _package.webui
bot = _package.bot
app = _package.app
main = app.main

# 作为模块导入时也释放字典与最后一段源码；不要求启动 main 才清理。
_SOURCES.clear()
del _source

if __name__ == '__main__':
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
