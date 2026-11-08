"""Compile real inline page scripts with Node's JavaScript parser, never regex rules.
Runtime: Python 3.10+ stdlib; Node 18+ (development/CI only, not application runtime)
Build/Run: python3 check_web_js.py
"""
import ast
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
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
    result = {}
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
    return result


def check_scripts():
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
