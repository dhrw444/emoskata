"""提取最终网页中的四段真实 JavaScript，交给 Node 官方解析器。

不模拟语法规则；Node 仅用于开发和 CI，部署入口不需要 Node。
Run: python3 -B check_web_js.py"""
import ast
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.current = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.inside, self.current = True, []

    def handle_data(self, text):
        if self.inside:
            self.current.append(text)

    def handle_endtag(self, tag):
        if tag == 'script' and self.inside:
            self.scripts.append(''.join(self.current))
            self.inside = False


def script_sources():
    """按 AST 取得实际模板和脚本，并替换生产同款状态占位符。

    必须得到全部四段；少一段或留下占位符直接失败，不把局部解析当验收。"""
    result = {}
    import json as _json
    for filename, wanted in (('app.py', {'PAGE', 'LOGIN_PAGE'}),
                             ('webui.py', {'_AUTH_JS', '_DASHBOARD_JS'})):
        tree = ast.parse((ROOT / filename).read_text())
        for item in tree.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    text = ast.literal_eval(item.value)
                    if target.id.endswith('_JS'):
                        result[target.id] = text
                    else:
                        html = Scripts()
                        html.feed(text)
                        for index, script in enumerate(html.scripts):
                            result[target.id + '_' + str(index)] = script
    if len(result) != 4:
        raise ValueError('未取得全部四段真实网页脚本，检查页面结构。')
    # 必须检查渲染后的值；语法合法的占位符也可能在浏览器运行时报错。
    if '__STATUS_JS__' in result.get('_DASHBOARD_JS', ''):
        sys.path.insert(0, str(ROOT))
        import webui
        result['_DASHBOARD_JS'] = result['_DASHBOARD_JS'].replace(
            '__STATUS_JS__',
            _json.dumps(webui._STATUS_TEXT, ensure_ascii=False, sort_keys=True))
    if '__STATUS_JS__' in result.get('_DASHBOARD_JS', ''):
        raise ValueError('状态文案占位符未被替换，页面脚本会引用未定义变量。')
    return result


def check_scripts():
    """对四段真实脚本运行 node --check，任何解析错误中止检查。"""
    node = shutil.which('node')
    if not node:
        raise RuntimeError('网页脚本验收需要 Node 18+，应用运行不需要 Node。')
    with tempfile.TemporaryDirectory(prefix='emos-js-check-') as directory:
        for name, source in script_sources().items():
            path = Path(directory) / (name + '.js')
            path.write_text(source)
            result = subprocess.run([node, '--check', str(path)], text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError(name + ': ' + result.stderr)
    return list(script_sources())


if __name__ == '__main__':
    print('JavaScript 官方语法检查通过：' + ', '.join(check_scripts()))
