#!/usr/bin/env python3
"""现场生成大输入并做全量参考实现对账（不提交生成的大文件）。

用法：
  python3 -I scripts/stress_test.py --objects 20000 --groups 50 --out /tmp/trace-stress
  python3 -I scripts/stress_test.py --small        # 小规模快速自检（默认）

生成器只产出合法数据：每条交接的原去向都由参考状态机实时取得，
同秒组按终态整体生效（含内外箱互换等场景），输入行故意乱序。
随后用独立的纯 Python 参考实现（dict 维护森林）逐展品逐段对账，
确认磁盘台账引擎的段边界、路径与凭据完全一致。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from expotrace import engine  # noqa: E402
from expotrace.extmem import dump_record  # noqa: E402


def generate_dataset(out_dir: str, n_objects: int, n_groups: int, seed: int) -> dict:
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    n_boxes = n_objects // 2
    n_exhibits = n_objects - n_boxes
    boxes = [f"BX{i:08d}" for i in range(n_boxes)]
    exhibits = [f"EX{i:08d}" for i in range(n_exhibits)]
    booths = [f"BOOTH{i:04d}" for i in range(max(4, n_objects // 500))]
    receiving = "RECEIVING"
    locations = booths + [receiving]

    catalog_path = os.path.join(out_dir, "catalog.json")
    with open(catalog_path, "w", encoding="utf-8") as fh:
        json.dump({"accepted": True, "catalog_version": f"stress-{seed}",
                   "booth_ids": booths}, fh, ensure_ascii=False)

    # 参考森林：parent[x] = (parent_id, kind)；初始随机布点/装箱
    parent: dict[str, tuple[str, str]] = {}
    manifest_rows = []
    for box in boxes:
        if rng.random() < 0.45 and boxes:
            target = rng.choice(boxes)
            if target != box:
                parent[box] = (target, "box")
                manifest_rows.append(
                    {"obj_id": box, "kind": "box", "parent_id": target, "parent_kind": "box"}
                )
                continue
        loc = rng.choice(locations)
        parent[box] = (loc, "location")
        manifest_rows.append(
            {"obj_id": box, "kind": "box", "parent_id": loc, "parent_kind": "location"}
        )
    for ex in exhibits:
        if rng.random() < 0.7:
            box = rng.choice(boxes)
            parent[ex] = (box, "box")
            manifest_rows.append(
                {"obj_id": ex, "kind": "exhibit", "parent_id": box, "parent_kind": "box"}
            )
        else:
            loc = rng.choice(locations)
            parent[ex] = (loc, "location")
            manifest_rows.append(
                {"obj_id": ex, "kind": "exhibit", "parent_id": loc, "parent_kind": "location"}
            )
    # 若初始意外成环（随机互指），重挂到地点修复
    def walk_chain(x):
        seen = set()
        node = x
        while parent[node][1] == "box":
            if node in seen:
                return None
            seen.add(node)
            node = parent[node][0]
        return node

    for x in list(parent):
        if walk_chain(x) is None:
            loc = rng.choice(locations)
            parent[x] = (loc, "location")
            for row in manifest_rows:
                if row["obj_id"] == x:
                    row["parent_id"], row["parent_kind"] = loc, "location"
                    break

    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    rng.shuffle(manifest_rows)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        for row in manifest_rows:
            fh.write(dump_record(row) + "\n")

    inventory_ts = 1000
    all_objs = boxes + exhibits
    handover_rows = []
    # 交接前的参考森林快照（= 盘点清册状态），供参考实现取初始边
    initial_parent = {x: parent[x] for x in all_objs}
    rid = 0
    ts = inventory_ts
    # 记录每个节点每次移动的 (ts, record_id)，供参考实现回放
    event_log: dict[str, list[tuple]] = {x: [] for x in all_objs}
    used_groups = 0
    attempts = 0
    while used_groups < n_groups and attempts < n_groups * 4:
        attempts += 1
        ts += rng.randint(50, 200)
        group_size = rng.randint(1, max(2, n_objects // 20))
        movers = rng.sample(all_objs, min(group_size, len(all_objs)))
        group_moves = []
        for obj in movers:
            old = parent[obj]
            # 选一个不同于旧父的新父：地点或箱子
            if rng.random() < 0.5:
                new = (rng.choice(locations), "location")
            else:
                new = (rng.choice(boxes), "box")
            if new == old:
                continue
            rid += 1
            record_id = f"RH{rid:09d}"
            group_moves.append((record_id, obj, old, new))
        if not group_moves:
            continue
        # 终态合法性检查：把本组移动一次性应用到参考森林，检测成环
        tentative = dict(parent)
        for _record_id, obj, _old, new in group_moves:
            tentative[obj] = new
        ok = True
        for _record_id, obj, _old, new in group_moves:
            seen = set()
            node = obj
            while tentative[node][1] == "box":
                if node in seen:
                    ok = False
                    break
                seen.add(node)
                node = tentative[node][0]
            if not ok:
                break
        if not ok:
            continue  # 整组废弃重抽（真实扫码点不会产生这种数据）
        # 提交：写入乱序交接行，更新参考状态与事件日志
        for record_id, obj, old, new in group_moves:
            parent[obj] = new
            event_log[obj].append((ts, record_id, new[0], new[1]))
            handover_rows.append({
                "record_id": record_id, "ts": ts, "obj_id": obj,
                "old_parent": old[0], "new_parent": new[0], "new_kind": new[1],
            })
        used_groups += 1

    handover_path = os.path.join(out_dir, "handovers.jsonl")
    rng.shuffle(handover_rows)
    with open(handover_path, "w", encoding="utf-8") as fh:
        for row in handover_rows:
            fh.write(dump_record(row) + "\n")

    end_ts = ts + 100
    targets = rng.sample(exhibits, min(20, len(exhibits)))
    query = {"start": inventory_ts, "end": end_ts, "targets": targets}
    query_path = os.path.join(out_dir, "query.json")
    with open(query_path, "w", encoding="utf-8") as fh:
        json.dump(query, fh, ensure_ascii=False)

    return {
        "catalog": catalog_path,
        "manifest": manifest_path,
        "handovers": handover_path,
        "query": query_path,
        "inventory_ts": inventory_ts,
        "end_ts": end_ts,
        "targets": targets,
        "event_log": event_log,
        "initial_parent": initial_parent,
    }


def reference_path_at(initial: dict, event_log: dict, obj: str, ts: int):
    """参考实现：逐节点取不晚于 ts 的最后一条边，沿父指针走完整链。"""
    def edge_of(node):
        cur = initial[node]
        evidence = node  # 初始凭据 = 清册物件号
        for ets, rid, new_parent, new_kind in event_log.get(node, []):
            if ets <= ts:
                cur = (new_parent, new_kind)
                evidence = rid
            else:
                break
        return cur[0], cur[1], evidence

    path = []
    node = obj
    seen = set()
    while True:
        if node in seen:
            raise AssertionError("reference detected cycle")
        seen.add(node)
        to, kind, evidence = edge_of(node)
        path.append({"from": node, "to": to, "parent_kind": kind, "evidence": evidence})
        if kind == "location":
            return path
        node = to


def _run_big_group_case(args) -> int:
    """极端场景：n 只箱顺序套成深链，同一秒整组改挂到展位。

    验证磁盘并查集对「同秒交接涉及大批物件」不依赖内存，以及深链
    路径重建的正确性。
    """
    n = max(100, args.objects)
    keep = args.out is not None
    base = args.out or tempfile.mkdtemp(prefix="trace-biggroup-")
    os.makedirs(base, exist_ok=True)
    try:
        booths = [f"B{i}" for i in range(10)]
        cat = os.path.join(base, "catalog.json")
        man = os.path.join(base, "manifest.jsonl")
        han = os.path.join(base, "handovers.jsonl")
        q = os.path.join(base, "query.json")
        with open(cat, "w", encoding="utf-8") as fh:
            json.dump({"accepted": True, "booth_ids": booths}, fh)
        boxes = [f"K{i:08d}" for i in range(n)]
        rows = [{"obj_id": "E1", "kind": "exhibit",
                 "parent_id": boxes[0], "parent_kind": "box"}]
        for i in range(n - 1):
            rows.append({"obj_id": boxes[i], "kind": "box",
                         "parent_id": boxes[i + 1], "parent_kind": "box"})
        rows.append({"obj_id": boxes[n - 1], "kind": "box",
                     "parent_id": "B0", "parent_kind": "location"})
        with open(man, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(dump_record(r) + "\n")
        with open(han, "w", encoding="utf-8") as fh:
            for i, box in enumerate(boxes):
                old = boxes[i + 1] if i < n - 1 else "B0"
                new_booth = booths[(i + 3) % 10]
                fh.write(dump_record({
                    "record_id": f"R{i:08d}", "ts": 2000, "obj_id": box,
                    "old_parent": old, "new_parent": new_booth, "new_kind": "location",
                }) + "\n")
        with open(q, "w", encoding="utf-8") as fh:
            json.dump({"start": 1000, "end": 3000, "targets": ["E1"]}, fh)
        result = engine.run_trace(
            cat, man, han, q, inventory_ts=1000,
            out_dir=os.path.join(base, "out"), work_dir=os.path.join(base, "work"),
        )
        seg_path = os.path.join(result["trace_dir"], "segments.jsonl")
        segments = [json.loads(line) for line in open(seg_path, encoding="utf-8")]
        assert len(segments) == 2
        assert segments[0]["end"] == 2000 and len(segments[0]["path"]) == n + 1
        assert segments[1]["path"] == [
            {"from": "E1", "to": boxes[0], "parent_kind": "box", "evidence": "E1"},
            {"from": boxes[0], "to": booths[3], "parent_kind": "location",
             "evidence": "R00000000"},
        ]
        print(json.dumps({"ok": True, "case": "big_group", "same_second_moves": n,
                          "deep_chain_edges": n + 1, "segments": 2}, ensure_ascii=False))
        return 0
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--objects", type=int, default=2000)
    ap.add_argument("--groups", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261123)
    ap.add_argument("--small", action="store_true", help="小规模快速自检")
    ap.add_argument("--big-group", action="store_true",
                    help="极端场景：单个同秒组移动全部箱子 + 超深装箱链")
    ap.add_argument("--out", help="生成数据与结果目录（默认临时目录，用完即删）")
    args = ap.parse_args()
    if args.small:
        args.objects, args.groups = 500, 8

    if args.big_group:
        return _run_big_group_case(args)

    keep = args.out is not None
    base = args.out or tempfile.mkdtemp(prefix="trace-stress-")
    work = os.path.join(base, "work")
    out_dir = os.path.join(base, "out")
    os.makedirs(base, exist_ok=True)
    try:
        info = generate_dataset(base, args.objects, args.groups, args.seed)
        result = engine.run_trace(
            info["catalog"], info["manifest"], info["handovers"], info["query"],
            inventory_ts=info["inventory_ts"], out_dir=out_dir, work_dir=work,
        )
        # 逐展品逐段对账
        produced = {}
        with open(os.path.join(result["trace_dir"], "segments.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                produced.setdefault(rec["exhibit_id"], []).append(rec)
        initial = info["initial_parent"]
        event_log = info["event_log"]
        q_start, q_end = info["inventory_ts"], info["end_ts"]
        checked_segments = 0
        for ex in info["targets"]:
            segs = produced[ex]
            assert segs[0]["start"] == q_start and segs[-1]["end"] == q_end, ex
            for prev, nxt in zip(segs, segs[1:]):
                assert prev["end"] == nxt["start"], f"gap in {ex}"
            for seg in segs:
                # 段内任一时刻路径都应等于段首路径；段首时刻的参考路径必须一致
                ref = reference_path_at(initial, event_log, ex, seg["start"])
                assert ref == seg["path"], (
                    f"path mismatch {ex} [{seg['start']},{seg['end']})\n"
                    f"engine={seg['path']}\nreference={ref}"
                )
                checked_segments += 1
            # 参考实现的「签名变化时刻」必须与引擎的段边界一致
            all_ts = sorted({ets for node in _chain_nodes(initial, event_log, ex, q_end)
                             for ets, *_ in event_log.get(node, [])
                             if q_start <= ets < q_end})
            ref_bounds = [q_start]
            prev_sig = _sig(reference_path_at(initial, event_log, ex, q_start))
            for ets in all_ts:
                sig = _sig(reference_path_at(initial, event_log, ex, ets))
                if sig != prev_sig:
                    ref_bounds.append(ets)
                    prev_sig = sig
            engine_bounds = [s["start"] for s in segs]
            assert engine_bounds == ref_bounds, (
                f"segment boundary mismatch {ex}: {engine_bounds} vs {ref_bounds}"
            )
        print(json.dumps({
            "ok": True, "objects": args.objects, "groups": args.groups,
            "targets": len(info["targets"]), "segments_checked": checked_segments,
            "trace": result["trace"],
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


def _sig(path):
    return [(e["from"], e["to"], e["evidence"]) for e in path]


def _chain_nodes(initial, event_log, ex, end_ts):
    """收集参考视角下 ex 链上出现过的全部节点（与引擎 collect 逻辑对应）。

    从初始链出发，按时间重放已知节点的事件；事件把某节点改挂到新箱时，
    新箱即使历史上与链无关也要纳入（中途加入的祖先）。
    """
    nodes = {ex}
    node = ex
    seen_init = set()
    while initial[node][1] == "box":
        nxt = initial[node][0]
        if nxt in seen_init:
            break
        seen_init.add(nxt)
        nodes.add(nxt)
        node = nxt
    changed = True
    while changed:
        changed = False
        for node in list(nodes):
            for ets, _rid, new_parent, new_kind in event_log.get(node, []):
                if ets >= end_ts:
                    break
                if new_kind == "box" and new_parent not in nodes:
                    nodes.add(new_parent)
                    changed = True
    return nodes


if __name__ == "__main__":
    raise SystemExit(main())
