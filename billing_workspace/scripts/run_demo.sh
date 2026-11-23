#!/usr/bin/env bash
# 可复跑端到端演示：两日合并出账 -> 独立核验 -> 单日/多日一致性 ->
# 冲突日志失败原子性 -> 最终状态。每次运行都重建 demo_out，不依赖网络。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPORT="$(cd "$ROOT/../booth_collector/out" && pwd)/S1_complete.json"
OUT="$ROOT/demo_out"
PY=(python3 -I "$HERE/bill")

echo "### 0. 清理旧演示输出"
rm -rf "$OUT"

echo
echo "### 1. 两日合并出账（2025-03-01 ~ 2025-03-02，批次上界 5，最大间隔 14400 秒）"
"${PY[@]}" run --report "$REPORT" \
  --meter-log "$ROOT/examples/meter_log.jsonl" \
  --attribution "$ROOT/examples/attribution.jsonl" \
  --batch-bound 5 --from 2025-03-01 --to 2025-03-02 \
  --max-gap-seconds 14400 --out "$OUT"

echo
echo "### 2. 逐日汇总（日期+展位，未归属为 null）"
cat "$OUT/bill-000001/summary.jsonl"

echo
echo "### 3. 独立核验：明细逐项对回汇总"
"${PY[@]}" verify "$OUT"

echo
echo "### 4. 单日单独出账，与两日合并的同日记录必须完全一致"
"${PY[@]}" run --report "$REPORT" \
  --meter-log "$ROOT/examples/meter_log.jsonl" \
  --attribution "$ROOT/examples/attribution.jsonl" \
  --batch-bound 5 --from 2025-03-01 --to 2025-03-01 \
  --max-gap-seconds 14400 --out "$OUT" >/dev/null
"${PY[@]}" run --report "$REPORT" \
  --meter-log "$ROOT/examples/meter_log.jsonl" \
  --attribution "$ROOT/examples/attribution.jsonl" \
  --batch-bound 5 --from 2025-03-02 --to 2025-03-02 \
  --max-gap-seconds 14400 --out "$OUT" >/dev/null
python3 -I - "$OUT" <<'PYEOF'
import sys
out = sys.argv[1]
for day, bill in (("2025-03-01", "bill-000002"), ("2025-03-02", "bill-000003")):
    for name in ("summary.jsonl", "known_details.jsonl", "missing_details.jsonl"):
        multi = [l for l in open(f"{out}/bill-000001/{name}").read().splitlines()
                 if f'"date":"{day}"' in l]
        single = open(f"{out}/{bill}/{name}").read().splitlines()
        assert multi == single, (day, name)
        print(f"  {day} {name}: 一致（{len(multi)} 行）")
PYEOF

echo
echo "### 5. 冲突日志整次拒绝（退出码 2），上次成功结果原样保留"
set +e
"${PY[@]}" run --report "$REPORT" \
  --meter-log "$ROOT/examples/meter_log_conflict.jsonl" \
  --attribution "$ROOT/examples/attribution.jsonl" \
  --batch-bound 5 --from 2025-03-01 --to 2025-03-02 \
  --max-gap-seconds 14400 --out "$OUT"
echo "  -> 冲突日志 exit=$?"
set -e
echo "  CURRENT 仍指向：$(cat "$OUT/CURRENT")"
ls "$OUT"

echo
echo "### 6. 查看当前账单（CURRENT 指向）"
"${PY[@]}" show --out "$OUT" | head -30

echo
echo "演示输出目录：$OUT"
