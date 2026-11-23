#!/usr/bin/env python3
"""生成可人工审计的固定小样例（examples/）。

场景：盘点时刻 1000，展位 B1/B2 与公共收货区 RECEIVING；
周转箱 C1（含内箱 C2，C2 内有展品 E2）、C3，展品 E1/E2/E3。
交接日志故意乱序，覆盖：外箱整体搬动、内箱改挂、同秒合法的
「内外箱互换」终态、收货区落地。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")


def write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def write_jsonl(path: str, rows) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def main() -> None:
    os.makedirs(EXAMPLES, exist_ok=True)

    # 已验收全目录报告（精简形态；booth_collector 的完整报告形态同样可读）
    write_json(
        os.path.join(EXAMPLES, "catalog.json"),
        {
            "accepted": True,
            "catalog_version": "demo-v1",
            "booth_ids": ["B1", "B2", "B3"],
        },
    )

    # 盘点清册（ts=1000 时的直接去向；箱可装箱）
    write_jsonl(
        os.path.join(EXAMPLES, "manifest.jsonl"),
        [
            {"obj_id": "E1", "kind": "exhibit", "parent_id": "B1", "parent_kind": "location"},
            {"obj_id": "E2", "kind": "exhibit", "parent_id": "C2", "parent_kind": "box"},
            {"obj_id": "E3", "kind": "exhibit", "parent_id": "C3", "parent_kind": "box"},
            {"obj_id": "C1", "kind": "box", "parent_id": "B1", "parent_kind": "location"},
            {"obj_id": "C2", "kind": "box", "parent_id": "C1", "parent_kind": "box"},
            {"obj_id": "C3", "kind": "box", "parent_id": "B2", "parent_kind": "location"},
        ],
    )

    # 交接记录：行序故意打乱，处理顺序只由 (ts, record_id, obj_id) 决定
    write_jsonl(
        os.path.join(EXAMPLES, "handovers.jsonl"),
        [
            # ts=1100：外箱 C1 整体从 B1 搬到 B2（E2/C2 随箱移动，自身无记录）
            {"record_id": "R3", "ts": 1100, "obj_id": "C1",
             "old_parent": "B1", "new_parent": "B2", "new_kind": "location"},
            # ts=1050：E1 从 B1 地面装进 C3（C3 在 B2）
            {"record_id": "R1", "ts": 1050, "obj_id": "E1",
             "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
            # ts=1200 同秒互换：C2 先从 C1 取出放 B2 地面，再把 C1 装进 C2。
            # 逐条按行序搬会临时成环，但整组终态合法：C2@B2 -> C1 in C2。
            {"record_id": "R5", "ts": 1200, "obj_id": "C1",
             "old_parent": "B2", "new_parent": "C2", "new_kind": "box"},
            {"record_id": "R4", "ts": 1200, "obj_id": "C2",
             "old_parent": "C1", "new_parent": "B2", "new_kind": "location"},
            # ts=1300：C3 从 B2 搬到公共收货区
            {"record_id": "R6", "ts": 1300, "obj_id": "C3",
             "old_parent": "B2", "new_parent": "RECEIVING", "new_kind": "location"},
            # ts=1400（落在查询截止秒 1400 之外，必须被台账忽略）
            {"record_id": "R7", "ts": 1400, "obj_id": "E1",
             "old_parent": "C3", "new_parent": "RECEIVING", "new_kind": "location"},
        ],
    )

    # 查询：窗口 [1000, 1400)，待查 E1/E2/E3
    write_json(
        os.path.join(EXAMPLES, "query.json"),
        {"start": 1000, "end": 1400, "targets": ["E2", "E1", "E3"]},
    )
    print("examples written to", EXAMPLES)


if __name__ == "__main__":
    main()
