#!/bin/sh
# 准备 Compose 挂载目录，仅补缺失的运行文件与配置模板；不创建 .env，不启动容器。
# Run: sh prepare_docker.sh
# Build: python3 build_single.py
# Runtime: 构建用 Python 3.10+ / 标准库；实际部署另需 Docker Compose。
set -eu
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE"
command -v python3 >/dev/null 2>&1 || { echo '需要 Python 3.10+。'; exit 1; }
if [ ! -f emoskata.py ]; then
    python3 build_single.py
fi
mkdir -p runtime config data
if [ ! -f runtime/emoskata.py ]; then
    cp emoskata.py runtime/emoskata.py
    echo '首次运行文件已放入 runtime/emoskata.py。'
else
    echo 'runtime/emoskata.py 已存在，未覆盖；更新时只删除并重新上传这一个 py。'
fi
if [ ! -f runtime/requirements.txt ]; then
    if [ -f requirements.txt ]; then
        cp requirements.txt runtime/requirements.txt
    else
        (cd "$HERE/runtime" && python3 "$HERE/emoskata.py" --prepare-host)
    fi
fi
if [ ! -f config/.env.example ]; then
    cp .env.example config/.env.example
fi
if [ ! -f config/.env ]; then
    echo '缺少 config/.env。请复制 config/.env.example 为 config/.env，填写 PANEL_PASSWORD（至少 8 字符）、' >&2
    echo 'TG_BOT_TOKEN、TG_ALLOWED_USER_IDS 后再运行 docker compose up -d --build。' >&2
    echo '程序不会生成配置；只校验已上传的文件。' >&2
    exit 1
fi
chmod 600 config/.env
# 只读校验：不创建、不修改任何文件。
EMOS_ENV_FILE="$HERE/config/.env" python3 "$HERE/runtime/emoskata.py" --check-config
echo '配置已通过校验。运行 docker compose up -d --build。'
echo '更新只替换 runtime/emoskata.py。保留 config/ 与 data/；数据库在容器实际启动时创建。'
