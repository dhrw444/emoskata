#!/bin/sh
# emoskata 一键安装（Debian/Ubuntu/Alpine，需要 root）
# 用法：sh install.sh [安装目录]
set -e

DIR="${1:-/opt/emoskata}"
PORT="${PORT:-8080}"

if [ "$(id -u)" != "0" ]; then
    echo "请用 root 运行：sudo sh install.sh"
    exit 1
fi

# 1) 检查 python
if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，请先安装（apt install python3 / apk add python3）"
    exit 1
fi
echo "python3: $(python3 -V 2>&1)"

# 2) 检查版本（需要 3.8+）
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "Python 版本过低，需要 3.8 及以上"
    exit 1
fi

# 3) 检查 tzdata（缺了也不致命，程序有固定偏移兜底）
if ! python3 -c 'from zoneinfo import ZoneInfo; ZoneInfo("Asia/Shanghai")' 2>/dev/null; then
    echo "提示：系统缺少 tzdata，程序会用固定 +08:00 偏移兜底。"
    echo "      想精确处理夏令时的话装一下：apt install tzdata / apk add tzdata"
fi

# 4) 拷文件
mkdir -p "$DIR"
for f in app.py bot.py emos.py README.md; do
    if [ -f "./$f" ]; then
        cp "./$f" "$DIR/"
    elif [ ! -f "$DIR/$f" ]; then
        echo "缺少 $f，请在仓库目录里运行本脚本"
        exit 1
    fi
done
mkdir -p "$DIR/data"

# 5) 生成密码（如果还没有）
if [ ! -f "$DIR/.env" ]; then
    PW=$(python3 -c 'import secrets;print(secrets.token_urlsafe(9))')
    cat > "$DIR/.env" <<EOF
PANEL_PASSWORD=$PW
PORT=$PORT
# 反代后改成你的公网地址，OAuth 回调要用
PUBLIC_BASE=http://localhost:$PORT
EOF
    chmod 600 "$DIR/.env"
    echo
    echo "面板密码：$PW"
    echo "（已写入 $DIR/.env，权限 600。忘了可以看这个文件）"
else
    echo "已存在 $DIR/.env，沿用原配置"
fi

# 6) systemd
if command -v systemctl >/dev/null 2>&1; then
    cat > /etc/systemd/system/emoskata.service <<EOF
[Unit]
Description=emoskata - emos 自动签到面板
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=/usr/bin/env python3 $DIR/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable emoskata >/dev/null 2>&1 || true
    systemctl restart emoskata
    sleep 2
    echo
    echo "已启动 systemd 服务：emoskata"
    echo "  查看状态：systemctl status emoskata"
    echo "  查看日志：journalctl -u emoskata -f"
else
    echo
    echo "没有 systemd，手动启动："
    echo "  cd $DIR && set -a && . ./.env && set +a && python3 app.py"
fi

echo
echo "打开：http://<服务器IP>:$PORT"
