"""把 emos.py + bot.py + app.py 合并成单文件 emoskata.py。

容器只能跑一个文件时用这个。合并顺序固定：emos → bot → app，
后定义的覆盖先定义的（app.py 的 _esc 会覆盖 bot.py 的）。

只做三件事：
1. 删掉模块间互相 import 的行
2. 去掉重复的 _esc 定义（保留 html.escape 版本，TG HTML 里 &quot; 合法）
3. 去掉各文件的 __main__ 块，只保留最后一份
"""

import re
import sys

ORDER = ["emos.py", "bot.py", "app.py"]
OUT = "emoskata.py"

DROP_IMPORT = re.compile(
    r"^\s*(from\s+(emos|bot)\s+import|import\s+(emos|bot)\s*$)"
)


def strip_main(src):
    """去掉 `if __name__ == "__main__":` 及其后全部内容。"""
    m = re.search(r'^if __name__ == ["\']__main__["\']:', src, re.M)
    return src[:m.start()].rstrip() + "\n" if m else src


def drop_esc(src):
    """删掉顶层 def _esc(...) 的整个函数体（到下一个顶层定义为止）。"""
    lines = src.split("\n")
    out, i, removed = [], 0, 0
    while i < len(lines):
        if lines[i].startswith("def _esc("):
            removed += 1
            i += 1
            while i < len(lines) and (
                lines[i].startswith((" ", "\t")) or lines[i] == ""
            ):
                i += 1
            # 回退掉尾随空行，交给外层统一控制
            while out and out[-1] == "":
                out.pop()
            out.append("")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), removed


def main():
    parts, seen_esc = [], False
    for name in ORDER:
        src = open(name, encoding="utf-8").read()
        src = strip_main(src)

        kept = []
        for line in src.split("\n"):
            if DROP_IMPORT.match(line):
                continue
            kept.append(line)
        src = "\n".join(kept)

        # 只保留一份 _esc：emos.py 没有，bot.py 第一份留着，app.py 那份删掉
        if name == "app.py":
            src, n = drop_esc(src)
            if n:
                seen_esc = True

        parts.append("# " + "=" * 66 + "\n# 来自 %s\n# %s\n" % (name, "=" * 66)
                     + src.strip())

    header = '''#!/usr/bin/env python3
"""emoskata —— emos 自动签到 · 网页面板 + Telegram Bot（单文件版）

由 emos.py + bot.py + app.py 合并生成，改动请改源文件后重跑 build_single.py。

运行：
    PANEL_PASSWORD='你的密码' PORT=8080 python3 emoskata.py

环境变量：
    PANEL_PASSWORD  面板密码（不填随机生成并打印）
    PORT            监听端口，默认 8080
    DATA_DIR        数据目录，默认 ./data
    PUBLIC_BASE     OAuth 回调用，反代后填 https://你的域名
"""

'''

    body = "\n\n\n".join(parts)
    tail = '\n\n\nif __name__ == "__main__":\n    main()\n'
    open(OUT, "w", encoding="utf-8").write(header + body + tail)
    print("生成 %s（%d 字节，_esc 去重=%s）"
          % (OUT, len((header + body + tail).encode()), seen_esc))


if __name__ == "__main__":
    main()
