#!/usr/bin/env bash
# 一键复跑：小样例 -> 端到端追溯+独立核验 -> 28 项单测 -> 小/大两档压力对账。
# 大输入由 stress_test.py 现场生成到临时目录，跑完即删，仓库不携带大文件。
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 重建固定小样例 =="
python3 -I scripts/build_examples.py

echo "== 2/4 端到端追溯并独立核验 =="
rm -rf out
python3 -I scripts/run_trace.py run \
  --catalog examples/catalog.json \
  --manifest examples/manifest.jsonl \
  --handovers examples/handovers.jsonl \
  --query examples/query.json \
  --inventory-ts 1000 --out ./out
python3 -I scripts/run_trace.py verify ./out

echo "== 3/4 机制单元测试（28 项，含全部拒绝情形与原子性） =="
python3 -I tests/test_core.py

echo "== 4/4 压力对账（独立参考实现逐段比对） =="
python3 -I scripts/stress_test.py --small
python3 -I scripts/stress_test.py --objects 20000 --groups 60 --seed 7
python3 -I scripts/stress_test.py --big-group --objects 3000

echo
echo "ALL REPRODUCTION STEPS PASSED"
