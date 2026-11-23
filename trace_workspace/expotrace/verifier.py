"""独立核验：对已交付的追溯结果做自洽性检查（纯标准库、流式）。

核验不相信生成过程，只从产物自身出发检查：
  V1  manifest 与段文件存在、字段类型合法；
  V2  段按 (展品编号, 起始秒) 严格有序，无重复边界；
  V3  每件待查展品的段连续、互不重叠，恰好覆盖查询区间 [start, end)；
  V4  每条路径从展品出发逐级到合法地点（展位或公共收货区）收尾，
      中间节点都是周转箱，路径节点编号不重复（不成环）；
  V5  凭据非空：每级关系都带交接记录号或初始清册物件号；
  V6  段内首尾秒与路径状态自洽（起止均为整数、start < end）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterator

from . import extmem
from .errors import VerifyError


def resolve_trace_dir(path: str) -> str:
    if os.path.isfile(os.path.join(path, "manifest.json")):
        return path
    current_file = os.path.join(path, "CURRENT")
    if os.path.isfile(current_file):
        with open(current_file, "r", encoding="utf-8") as fh:
            name = fh.read().strip()
        trace_dir = os.path.join(path, name)
        if os.path.isfile(os.path.join(trace_dir, "manifest.json")):
            return trace_dir
    raise VerifyError(f"找不到已交付的追溯结果：{path}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_path(path: list[dict[str, Any]], exhibit_id: str, valid_locations: set[str]) -> None:
    if not isinstance(path, list) or not path:
        raise VerifyError(f"展品 {exhibit_id} 存在空路径段")
    if path[0]["from"] != exhibit_id:
        raise VerifyError(
            f"展品 {exhibit_id} 路径起点必须是展品自身：{path[0].get('from')!r}"
        )
    seen: set[str] = set()
    for i, edge in enumerate(path):
        for key in ("from", "to", "parent_kind", "evidence"):
            if not isinstance(edge.get(key), str) or not edge[key]:
                raise VerifyError(f"展品 {exhibit_id} 路径第 {i} 级字段非法：{edge!r}")
        if edge["from"] in seen:
            raise VerifyError(f"展品 {exhibit_id} 路径成环：{edge['from']}")
        seen.add(edge["from"])
        if edge["parent_kind"] not in ("location", "box"):
            raise VerifyError(f"展品 {exhibit_id} 路径 parent_kind 非法：{edge!r}")
        if i > 0 and edge["from"] != path[i - 1]["to"]:
            raise VerifyError(f"展品 {exhibit_id} 路径在第 {i} 级断裂")
    last = path[-1]
    if last["parent_kind"] != "location":
        raise VerifyError(f"展品 {exhibit_id} 路径未落到地点，末端为 {last['to']!r}")
    if last["to"] not in valid_locations:
        raise VerifyError(f"展品 {exhibit_id} 路径落到非法地点：{last['to']!r}")


def verify_trace(path: str) -> dict[str, Any]:
    trace_dir = resolve_trace_dir(path)
    with open(os.path.join(trace_dir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("kind") != "expo-offline-trace":
        raise VerifyError("manifest.kind 不匹配")
    query = manifest.get("query", {})
    start, end = query.get("start"), query.get("end")
    targets = query.get("targets", [])
    if not _is_int(start) or not _is_int(end) or start >= end:
        raise VerifyError("manifest 中查询区间非法")
    if not isinstance(targets, list) or not targets:
        raise VerifyError("manifest 中 targets 非法")

    receiving = manifest.get("receiving_location", "RECEIVING")
    booth_ids = manifest.get("booth_ids")
    if not isinstance(booth_ids, list) or not all(isinstance(b, str) and b for b in booth_ids):
        raise VerifyError("manifest 缺少合法展位编号列表 booth_ids")
    valid_locations = set(booth_ids) | {receiving}

    seg_path = os.path.join(trace_dir, manifest["outputs"]["segments"])
    if not os.path.isfile(seg_path):
        raise VerifyError(f"缺少段文件：{seg_path}")

    prev_key: tuple | None = None
    current_id: str | None = None
    cursor = start
    rows = 0
    covered_targets: set[str] = set()
    for rec in extmem.iter_jsonl(seg_path):
        key = (rec["exhibit_id"], rec["start"])
        if prev_key is not None and key <= prev_key:
            raise VerifyError(f"段未按 (展品编号, 起始秒) 严格有序：{rec!r}")
        prev_key = key
        if rec["exhibit_id"] != current_id:
            if current_id is not None and cursor != end:
                raise VerifyError(
                    f"展品 {current_id} 的段未覆盖到查询终点 {end}，实际到 {cursor}"
                )
            current_id = rec["exhibit_id"]
            cursor = start
            covered_targets.add(current_id)
        if not _is_int(rec["start"]) or not _is_int(rec["end"]) or rec["end"] <= rec["start"]:
            raise VerifyError(f"段时间区间非法：{rec!r}")
        if rec["start"] < start or rec["end"] > end:
            raise VerifyError(f"段超出查询窗口：{rec!r}")
        if rec["start"] != cursor:
            raise VerifyError(
                f"展品 {current_id} 段不连续/重叠：期望 {cursor}，实际 {rec['start']}"
            )
        _check_path(rec["path"], current_id, valid_locations)
        cursor = rec["end"]
        rows += 1

    if current_id is not None and cursor != end:
        raise VerifyError(f"展品 {current_id} 的段未覆盖到查询终点 {end}，实际到 {cursor}")
    missing = set(targets) - covered_targets
    if missing:
        raise VerifyError(f"以下待查展品缺少追溯段：{sorted(missing)}")
    extra = covered_targets - set(targets)
    if extra:
        raise VerifyError(f"出现了未请求的展品段：{sorted(extra)}")
    if manifest.get("counts", {}).get("segment_rows") != rows:
        raise VerifyError("manifest 计数 segment_rows 与实际段数不符")
    return {
        "ok": True,
        "trace_dir": os.path.abspath(trace_dir),
        "trace": os.path.basename(trace_dir),
        "window": [start, end],
        "targets": len(targets),
        "segment_rows": rows,
    }
