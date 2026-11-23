#!/usr/bin/env bash
# 可复跑验证：机制级单测 + 演示脚本端到端 + 工作区独立核验。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

echo "### 1/3 机制级单元测试（python3 -I，标准库 unittest）"
python3 -I "$ROOT/tests/test_roster.py"

echo
echo "### 2/3 端到端演示脚本"
bash "$HERE/run_demo.sh"

echo
echo "### 3/3 演示工作区最终核验"
python3 -I "$HERE/roster" verify "$ROOT/demo_ws"

echo
echo "全部验证通过。"
