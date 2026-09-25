#!/usr/bin/env bash
# B站自动回复机器人 - macOS / Linux 启动脚本
set -euo pipefail

cd "$(dirname "$0")"

echo "========================================"
echo "  B站自动回复机器人启动中..."
echo "========================================"
echo

# Python 解释器
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "[错误] 未找到 Python，请先安装 Python 3.10+"
  exit 1
fi

# 虚拟环境：优先 .venv，其次 venv
if [[ -d ".venv" ]]; then
  VENV_DIR=".venv"
elif [[ -d "venv" ]]; then
  VENV_DIR="venv"
else
  echo "[提示] 首次运行，创建虚拟环境 .venv ..."
  "$PYTHON" -m venv .venv
  VENV_DIR=".venv"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[提示] 检查依赖包..."
if ! python -c "import yaml, openai, bilibili_api" >/dev/null 2>&1; then
  echo "[提示] 安装依赖包..."
  pip install -r requirements.txt
else
  echo "[信息] 依赖包已安装"
fi

# 配置文件
if [[ ! -f "config.yaml" ]]; then
  if [[ -f "config.example.yaml" ]]; then
    echo "[提示] 未找到 config.yaml，正在从模板复制..."
    cp config.example.yaml config.yaml
    echo "[重要] 请编辑 config.yaml，填写 B站 Cookie 与 AI API Key 后再启动"
    exit 1
  else
    echo "[错误] 缺少 config.yaml 与 config.example.yaml"
    exit 1
  fi
fi

# 运行时目录
mkdir -p logs data

echo "[信息] 启动时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "[信息] 按 Ctrl+C 停止程序"
echo

exec python bilibili_auto_reply.py
