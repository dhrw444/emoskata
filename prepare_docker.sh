#!/bin/sh
# Runtime: Python 3.10+ for build, Docker Compose for deployment
# Build: python3 build_single.py | Run: sh prepare_docker.sh
# Deps: Python stdlib; does not start containers or overwrite runtime/config/data
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
EMOS_ENV_FILE="$HERE/config/.env" python3 "$HERE/runtime/emoskata.py" --init-config
echo '配置已准备。先编辑 config/.env，再运行 docker compose up -d --build。'
echo '更新只替换 runtime/emoskata.py。保留 config/ 与 data/；数据库在容器实际启动时创建。'
