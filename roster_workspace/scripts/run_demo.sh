#!/usr/bin/env bash
# 可复跑端到端演示：初始化 -> 多批更正 -> 空变更 -> 撤回 -> 重送 -> 拒绝路径 -> 核验。
# 每次运行都在脚本目录下重建 demo_ws，不依赖任何网络或采集器服务调用。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPORTS="$(cd "$ROOT/../booth_collector/out" && pwd)"
WS="$ROOT/demo_ws"
PY=(python3 -I "$HERE/roster")

echo "### 0. 清理旧演示工作区"
rm -rf "$WS"

echo
echo "### 1. 初始化：按从早到晚的届次顺序接入三份已验收全目录报告"
echo "    （S1=8 展位, S2=16 展位, S6=8 展位；S3/S4/S5 等非完整报告被排除）"
"${PY[@]}" init "$WS" \
  --edition "2024春=$REPORTS/S1_complete.json" \
  --edition "2024秋=$REPORTS/S2_fill_truncated.json" \
  --edition "2025春=$REPORTS/S6_retry_cache.json"

echo
echo "### 2. c1：登记两条依据，把 1/B01 - 2/B01 - 3/B01 连成同一身份（传递关系）"
"${PY[@]}" correct "$WS" --commit c1 --base commit-0 \
  --register e1:1/B01:2/B01 --register e2:2/B01:3/B01

echo
echo "### 3. c2：为同一对补第二条依据；名册无变化，但仍交付带两个提交号的空变更"
"${PY[@]}" correct "$WS" --commit c2 --base c1 --register e3:2/B01:1/B01

echo
echo "### 4. c3：撤回 e2；2-3 连接消失，3/B01 回到独立身份（撤回不等于声明不同）"
"${PY[@]}" correct "$WS" --commit c3 --base c2 --withdraw e2:2/B01:3/B01

echo
echo "### 5. 重送 c1（展位对反方向、操作换序）：拿回原变更集，即使头部已到 c3"
"${PY[@]}" replay "$WS" --commit c1 --base commit-0 \
  --register e2:3/B01:2/B01 --register e1:2/B01:1/B01

echo
echo "### 6. 拒绝路径（均返回退出码 2，且不改变已发布结果）"
set +e
"${PY[@]}" correct "$WS" --commit cX --base c3 --register e9:1/B01:9/B01
echo "  -> 未知展位 exit=$?"
"${PY[@]}" correct "$WS" --commit cX --base commit-0 --register e9:1/B02:2/B02
echo "  -> 基于旧提交 exit=$?"
"${PY[@]}" correct "$WS" --commit c1 --base c3 --register e9:1/B02:2/B02
echo "  -> 提交号复用 exit=$?"
"${PY[@]}" replay "$WS" --commit c1 --base c3 --register e1:1/B01:2/B01
echo "  -> 同号不同内容重送 exit=$?"
set -e

echo
echo "### 7. 独立核验：全量重放所有提交并核对当前名册（不读取原始报告）"
"${PY[@]}" verify "$WS"

echo
echo "### 8. 当前名册与提交链"
"${PY[@]}" log "$WS"
echo
echo "演示工作区：$WS"
