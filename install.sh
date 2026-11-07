#!/bin/sh
# Runtime: Python 3.10+ | Build: python3 build_single.py
# Run: sh install.sh /opt/emoskata | Deps: Python standard library, optional systemd
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
if [ ! -f "$DIR/.env" ]; then
    python3 -c 'import pathlib,secrets,sys; p=pathlib.Path(sys.argv[1])/".env"; p.write_text("PANEL_PASSWORD="+secrets.token_urlsafe(18)+"\nTG_BOT_TOKEN=\nEMOS_APP_USER_ID=\nPORT=8080\nHTTP_HOST=127.0.0.1\nSIGN_TIMEZONE=Asia/Shanghai\n");p.chmod(0o600)' "$DIR"
    echo "初始配置已写入 $DIR/.env（权限 600），凭据不打印。"
fi
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
    python3 -c 'import pathlib,sys; d=sys.argv[1]; p=pathlib.Path("/etc/systemd/system/emoskata.service"); p.write_text("[Unit]\nDescription=emoskata Telegram signing Bot\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nWorkingDirectory="+d+"\nEnvironmentFile="+d+"/.env\nExecStart=/usr/bin/env python3 "+d+"/emoskata.py\nRestart=always\nRestartSec=5\nTimeoutStopSec=60\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n")' "$DIR"
    systemctl daemon-reload
    systemctl enable emoskata
    systemctl restart emoskata
    echo '服务已启动；日志用 journalctl -u emoskata -f。'
else
    echo "手动运行：cd $DIR && set -a && . ./.env && set +a && python3 emoskata.py"
fi
echo '配置 Bot Token 与应用创建者的 emos 用户 ID，再去 TG Bot 登录。'
