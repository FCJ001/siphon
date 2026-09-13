#!/bin/zsh
# Siphon 启动: 默认 127.0.0.1:4100; 配置见 .env(从 .env.example 复制)
cd "$(dirname "$0")"
if [ ! -f .env ]; then cp .env.example .env && echo "已生成 .env — 请改 SIPHON_MASTER_KEY 并填 OPENCODE_KEYS"; fi
set -a; source .env 2>/dev/null; set +a
exec "${PYTHON:-python3}" -m uvicorn siphon.main:app --host "${SIPHON_HOST:-127.0.0.1}" --port "${SIPHON_PORT:-4100}"
