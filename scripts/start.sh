#!/bin/zsh
# Siphon 启动: 默认 127.0.0.1:4100; 配置见 .env(从 .env.example 复制)
# 用法: ./scripts/start.sh   或   PYTHON=/path/to/python ./scripts/start.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env — 请修改 SIPHON_MASTER_KEY 并填 OPENCODE_KEYS"
fi
set -a; source .env; set +a
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m uvicorn siphon.main:app \
  --host "${SIPHON_HOST:-127.0.0.1}" --port "${SIPHON_PORT:-4100}"
