#!/bin/sh
# 安装单文件程序与配置模板；不创建 .env，由部署者自己填写后上传。
# Run: sh install.sh /opt/emoskata
# Build: python3 build_single.py
# Runtime: Python 3.10+ / 标准库；systemd 可用时注册服务。
set -eu
DIR="${1:-/opt/emoskata}"
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || { echo '先安装 Python 3.10 或更新版本。'; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
    echo '需要 Python 3.10 或更新版本。'; exit 1;
}
mkdir -p "$DIR/data"
if [ "$HERE" != "$DIR" ]; then
    cp "$HERE/emoskata.py" "$DIR/emoskata.py"
fi
chmod 700 "$DIR/data"
# 程序不再生成配置：只补一份模板，缺 .env 就明确停下。
if [ ! -f "$DIR/.env.example" ] && [ -f "$HERE/.env.example" ]; then
    cp "$HERE/.env.example" "$DIR/.env.example"
fi
if [ ! -f "$DIR/.env" ]; then
    echo "缺少 $DIR/.env。请复制 .env.example 为 .env，填写 PANEL_PASSWORD（至少 8 字符）、" >&2
    echo 'TG_BOT_TOKEN、TG_ALLOWED_USER_IDS 后重新运行本脚本。程序不会替你生成配置。' >&2
    exit 1
fi
chmod 600 "$DIR/.env"
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
    python3 -c 'import pathlib,sys; d=sys.argv[1]; p=pathlib.Path("/etc/systemd/system/emoskata.service"); p.write_text("[Unit]\nDescription=emoskata Telegram signing Bot\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nWorkingDirectory="+d+"\nEnvironment=EMOS_ENV_FILE="+d+"/.env\nExecStart=/usr/bin/env python3 "+d+"/emoskata.py\nRestart=always\nRestartSec=5\nTimeoutStopSec=120\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n")' "$DIR"
    systemctl daemon-reload
    systemctl enable emoskata
    systemctl restart emoskata
    echo '服务已启动；日志用 journalctl -u emoskata -f。'
else
    echo "手动运行：EMOS_ENV_FILE=$DIR/.env python3 $DIR/emoskata.py"
fi
echo 'Bot 的 /login 直接给出官方授权链接；配置也可在管理员网页保存，保存时才写回 .env。'
