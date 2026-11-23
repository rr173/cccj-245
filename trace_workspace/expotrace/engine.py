"""离线追溯主引擎：装载校验 → 交接重放 → 查询段构建（纯标准库、磁盘后备）。

输入
  * 已验收的全目录报告（JSON）：只取合法展位编号，另允许一个公共收货区，
    不重新采集；
  * 盘点时刻物资清册（JSONL）：固定的一批展品与周转箱，物件编号唯一，
    每件物品直接位于展位/收货区/某只箱，箱可装箱；
  * 交接记录（JSONL，行可乱序）：记录号、整数秒时刻、被搬物件、
    原直接去向、不同的新直接去向；
  * 查询参数：待查展品编号列表 + 非空左闭右开整数秒区间 [start, end)。

规则要点
  * 只把 ts < end（查询截止秒之前）的记录纳入台账；交接必须晚于盘点；
  * 同一秒记录属于同一次交接：原去向都须与交接前一致、每件物品至多移动
    一次、交接后每条装箱链落到合法地点且不成环（按整组终态判定，
    不按行序逐条搬动）；
  * 输出覆盖整个查询范围、互不重叠的连续段；路径或凭据不变不拆段，
    地点未变但装箱路径变化必须拆段；
  * 任何规则违例（含发生在未查询箱子里的问题）整次拒绝，不留半份导出、
    不覆盖上次成功结果。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any, Iterator, Optional

from . import extmem, storage
from .errors import (
    BadParameter,
    HandoverError,
    ManifestError,
    QueryError,
    ReportRejected,
    TraceError,
)

RECEIVING_LOCATION = "RECEIVING"  # 公共收货区的固定地点编号
_RUN_SIZE = extmem.DEFAULT_RUN_SIZE


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


# --------------------------------------------------------------------------- #
# 目录报告准入：只确认本届合法展位编号
# --------------------------------------------------------------------------- #
def load_catalog(path: str, receiving_id: str) -> dict[str, Any]:
    """读取已验收全目录报告，返回 {展位编号集合, 目录版本, 展位数}。

    兼容两种报告形态：本工作区 booth_collector 的完整报告
    （complete=true + booths[].id），以及精简形态
    {"accepted": true, "booth_ids": [...]}。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except FileNotFoundError:
        raise ReportRejected(f"目录报告不存在：{path}") from None
    except json.JSONDecodeError as exc:
        raise ReportRejected(f"目录报告不是合法 JSON：{path}（{exc}）") from None
    if not isinstance(report, dict):
        raise ReportRejected(f"{path}: 报告顶层必须是 JSON 对象")

    booth_ids: list[str]
    if "booth_ids" in report:
        if report.get("accepted") is not True:
            raise ReportRejected(f"{path}: 精简报告必须声明 accepted=true")
        booth_ids = report["booth_ids"]
        if not isinstance(booth_ids, list) or not booth_ids:
            raise ReportRejected(f"{path}: booth_ids 必须是非空列表")
    else:
        if report.get("complete") is not True:
            raise ReportRejected(
                f"{path}: 拒绝非完整报告（complete={report.get('complete')!r}）"
            )
        booths = report.get("booths")
        if not isinstance(booths, list) or not booths:
            raise ReportRejected(f"{path}: 完整报告必须含非空 booths 列表")
        booth_ids = []
        for row in booths:
            if not isinstance(row, dict) or not nonempty_str(row.get("id")):
                raise ReportRejected(f"{path}: 展位记录缺少字符串 id：{row!r}")
            booth_ids.append(row["id"])

    seen: set[str] = set()
    for bid in booth_ids:
        if not nonempty_str(bid):
            raise ReportRejected(f"{path}: 展位编号必须是非空字符串：{bid!r}")
        if bid in seen:
            raise ReportRejected(f"{path}: 报告内展位编号重复：{bid}")
        if bid == receiving_id:
            raise ReportRejected(
                f"{path}: 展位编号 {bid!r} 与公共收货区编号冲突"
            )
        seen.add(bid)
    return {
        "booth_ids": seen,
        "catalog_version": report.get("catalog_version"),
        "booth_count": len(seen),
    }


# --------------------------------------------------------------------------- #
# 清册装载（流式校验，行可大于内存总量）
# --------------------------------------------------------------------------- #
_MANIFEST_KEYS = {"obj_id", "kind", "parent_id", "parent_kind"}


def _normalize_manifest_line(obj: Any, lineno: int, valid_locations: set[str]) -> dict[str, Any]:
    where = f"清册第 {lineno} 行"
    if not isinstance(obj, dict):
        raise ManifestError(f"{where}：每行必须是 JSON 对象")
    extra = set(obj) - _MANIFEST_KEYS
    if extra:
        raise ManifestError(f"{where}：未知字段 {sorted(extra)}")
    obj_id, kind = obj.get("obj_id"), obj.get("kind")
    parent_id, parent_kind = obj.get("parent_id"), obj.get("parent_kind")
    if not nonempty_str(obj_id):
        raise ManifestError(f"{where}：obj_id 必须是非空字符串")
    if kind not in ("exhibit", "box"):
        raise ManifestError(f"{where}：kind 只能是 exhibit/box：{kind!r}")
    if not nonempty_str(parent_id):
        raise ManifestError(f"{where}：parent_id 必须是非空字符串")
    if parent_kind not in ("location", "box"):
        raise ManifestError(
            f"{where}：parent_kind 只能是 location/box：{parent_kind!r}"
        )
    if parent_kind == "location" and parent_id not in valid_locations:
        raise ManifestError(
            f"{where}：地点 {parent_id!r} 不是合法展位或公共收货区"
        )
    return {
        "obj_id": obj_id,
        "kind": kind,
        "parent_id": parent_id,
        "parent_kind": parent_kind,
    }


def load_manifest(
    conn,
    manifest_path: str,
    valid_locations: set[str],
    work_dir: str,
    run_size: int = _RUN_SIZE,
) -> dict[str, int]:
    """流式校验并入库清册。返回 {物件数, 展品类, 箱数}。

    单行规则逐行校验后批量入库；跨行规则（编号唯一、父箱存在且为箱、
    成环、悬挂地点）全部交给 SQLite 主键与索引查询做集合判定，
    全程不把清册整批载入内存，也不依赖行序。
    """
    if not os.path.isfile(manifest_path):
        raise BadParameter(f"清册不存在：{manifest_path}")
    total = 0
    exhibits = boxes = 0
    batch: list[tuple] = []

    def flush() -> None:
        nonlocal batch
        if batch:
            try:
                storage.bulk_load_objects(conn, batch)
            except storage.sqlite3.IntegrityError as exc:
                raise ManifestError(f"清册物件编号重复或主键冲突：{exc}") from None
            batch = []

    with open(manifest_path, "r", encoding="utf-8") as src:
        for lineno, line in enumerate(src, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ManifestError(f"清册第 {lineno} 行：不是合法 JSON（{exc}）") from None
            rec = _normalize_manifest_line(obj, lineno, valid_locations)
            if rec["kind"] == "exhibit":
                exhibits += 1
            else:
                boxes += 1
            batch.append(
                (
                    rec["obj_id"],
                    rec["kind"],
                    rec["parent_id"],
                    rec["parent_kind"],
                    rec["obj_id"],  # 初始凭据 = 初始清册物件号
                )
            )
            if len(batch) >= storage.BATCH_INSERT:
                flush()
    flush()
    conn.commit()
    if total == 0:
        raise ManifestError("清册为空：至少需要一件展品")
    if exhibits == 0:
        raise ManifestError("清册中没有任何展品")

    # 跨行集合校验：父箱必须存在且是箱（不能是展品）
    bad = conn.execute(
        """
        SELECT o.obj_id, o.parent_id, COALESCE(p.kind, 'MISSING')
          FROM objects o
          LEFT JOIN objects p ON p.obj_id = o.parent_id
         WHERE o.parent_kind = 'box'
           AND (p.obj_id IS NULL OR p.kind <> 'box')
         LIMIT 5
        """
    ).fetchall()
    if bad:
        obj_id, parent_id, pkind = bad[0]
        if pkind == "MISSING":
            raise ManifestError(f"清册物件 {obj_id} 的父箱不存在：{parent_id!r}")
        raise ManifestError(
            f"清册物件 {obj_id} 的直接去向是展品而非周转箱：{parent_id!r}"
        )
    # 直挂地点再兜底校验一次（行级已查，防规范化遗漏）
    bad = conn.execute(
        """
        SELECT o.obj_id, o.parent_id
          FROM objects o
         WHERE o.parent_kind = 'location'
           AND NOT EXISTS (SELECT 1 FROM locations l WHERE l.loc_id = o.parent_id)
         LIMIT 5
        """
    ).fetchall()
    if bad:
        raise ManifestError(
            "清册物件直挂不存在的地点：" + ", ".join(f"{o}->{p}" for o, p in bad)
        )
    storage.validate_manifest_forest(conn)
    return {"objects": total, "exhibits": exhibits, "boxes": boxes}


# --------------------------------------------------------------------------- #
# 交接日志装载（乱序输入，外排后入库）
# --------------------------------------------------------------------------- #
_HANDOVER_KEYS = {"record_id", "ts", "obj_id", "old_parent", "new_parent", "new_kind"}


def _normalize_handover_line(obj: Any, lineno: int) -> dict[str, Any]:
    where = f"交接记录第 {lineno} 行"
    if not isinstance(obj, dict):
        raise HandoverError(f"{where}：每行必须是 JSON 对象")
    extra = set(obj) - _HANDOVER_KEYS
    if extra:
        raise HandoverError(f"{where}：未知字段 {sorted(extra)}")
    rid, ts, oid = obj.get("record_id"), obj.get("ts"), obj.get("obj_id")
    old_parent, new_parent = obj.get("old_parent"), obj.get("new_parent")
    new_kind = obj.get("new_kind")
    if not nonempty_str(rid):
        raise HandoverError(f"{where}：record_id 必须是非空字符串")
    if not is_int(ts):
        raise HandoverError(f"{where}：ts 必须是整数秒")
    if not nonempty_str(oid):
        raise HandoverError(f"{where}：obj_id 必须是非空字符串")
    if not nonempty_str(old_parent):
        raise HandoverError(f"{where}：old_parent 必须是非空字符串")
    if not nonempty_str(new_parent):
        raise HandoverError(f"{where}：new_parent 必须是非空字符串")
    if new_kind not in ("location", "box"):
        raise HandoverError(f"{where}：new_kind 只能是 location/box：{new_kind!r}")
    return {
        "record_id": rid,
        "ts": ts,
        "obj_id": oid,
        "old_parent": old_parent,
        "new_parent": new_parent,
        "new_kind": new_kind,
    }


def load_handovers(
    conn,
    handover_path: str,
    inventory_ts: int,
    cutoff: int,
    work_dir: str,
    run_size: int = _RUN_SIZE,
) -> dict[str, int]:
    """流式规范化并批量入库；只装载 ts < cutoff 的记录。

    确定性次序由 SQLite 索引 (ts, record_id, obj_id) 保证，与输入行序
    无关，不需要额外外排。交接必须晚于盘点（ts > inventory_ts），
    等于或早于盘点时刻一律拒绝。
    """
    if not os.path.isfile(handover_path):
        raise BadParameter(f"交接记录不存在：{handover_path}")
    total = 0
    kept = 0
    batch: list[tuple] = []

    def flush() -> None:
        nonlocal batch, kept
        if batch:
            storage.bulk_load_handovers(conn, batch)
            kept += len(batch)
            batch = []

    with open(handover_path, "r", encoding="utf-8") as src:
        for lineno, line in enumerate(src, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HandoverError(
                    f"交接记录第 {lineno} 行：不是合法 JSON（{exc}）"
                ) from None
            rec = _normalize_handover_line(obj, lineno)
            if rec["ts"] <= inventory_ts:
                raise HandoverError(
                    f"交接记录 {rec['record_id']} 时刻 {rec['ts']} 不晚于盘点时刻 "
                    f"{inventory_ts}（交接必须晚于盘点）"
                )
            if rec["ts"] >= cutoff:
                continue  # 只把查询截止秒之前的记录纳入台账
            if not storage.object_exists(conn, rec["obj_id"]):
                raise HandoverError(
                    f"交接记录 {rec['record_id']} 搬动了清册中不存在的物件："
                    f"{rec['obj_id']!r}"
                )
            batch.append(
                (
                    rec["record_id"],
                    rec["ts"],
                    rec["obj_id"],
                    rec["old_parent"],
                    rec["new_parent"],
                    rec["new_kind"],
                )
            )
            if len(batch) >= storage.BATCH_INSERT:
                flush()
    flush()
    conn.commit()
    return {
        "handover_lines": total,
        "handovers_before_cutoff": kept,
        "handovers_loaded": kept,
    }


# --------------------------------------------------------------------------- #
# 交接重放：静态预检 + 逐同秒组合法性校验 + 原地更新
# --------------------------------------------------------------------------- #
def replay_handovers(conn, cutoff: int) -> dict[str, int]:
    dup_rid = storage.check_duplicate_record_ids(conn, cutoff)
    if dup_rid is not None:
        raise HandoverError(f"截止秒之前交接记录号重复：{dup_rid!r}")
    dup_move = storage.check_duplicate_move_in_group(conn, cutoff)
    if dup_move is not None:
        ts, obj_id, n = dup_move
        raise HandoverError(
            f"时刻 {ts} 的同一次交接中物件 {obj_id} 被移动 {n} 次（每件物品至多一次）"
        )

    groups = 0
    moved_total = 0
    for ts, rows in storage.handover_groups(conn, cutoff):
        groups += 1
        moved_total += len(rows)
        storage.prepare_group_table(conn, rows, ts)
        storage.check_group_static(conn, ts)
        storage.check_group_no_cycle(conn, ts)
        storage.apply_group(conn)
    return {"handover_groups": groups, "moves_applied": moved_total}


# --------------------------------------------------------------------------- #
# 查询参数
# --------------------------------------------------------------------------- #
def load_query(query_path: str, inventory_ts: int, cutoff: int) -> dict[str, Any]:
    try:
        with open(query_path, "r", encoding="utf-8") as fh:
            q = json.load(fh)
    except FileNotFoundError:
        raise BadParameter(f"查询参数文件不存在：{query_path}") from None
    except json.JSONDecodeError as exc:
        raise BadParameter(f"查询参数不是合法 JSON：{query_path}（{exc}）") from None
    if not isinstance(q, dict):
        raise BadParameter("查询参数顶层必须是 JSON 对象")
    start, end = q.get("start"), q.get("end")
    targets = q.get("targets")
    if not is_int(start) or not is_int(end):
        raise BadParameter("查询 start/end 必须是整数秒")
    if start >= end:
        raise QueryError(f"查询区间必须非空且左闭右开：[{start}, {end})")
    if start < inventory_ts:
        raise QueryError(
            f"查询起点 {start} 早于盘点时刻 {inventory_ts}"
        )
    if end > cutoff:
        # cutoff 来自调用方（查询截止秒）；正常调用 end==cutoff
        raise QueryError(f"查询终点 {end} 晚于截止秒 {cutoff}")
    if not isinstance(targets, list) or not targets:
        raise QueryError("targets 必须是非空展品编号列表")
    deduped: list[str] = []
    seen: set[str] = set()
    for t in targets:
        if not nonempty_str(t):
            raise QueryError(f"待查展品编号必须是非空字符串：{t!r}")
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return {"start": start, "end": end, "targets": deduped}


def validate_targets(conn, targets: list[str]) -> None:
    """待查编号必须存在且是展品；不认识的编号整次拒绝。"""
    for t in targets:
        row = conn.execute("SELECT kind FROM objects WHERE obj_id = ?", (t,)).fetchone()
        if row is None:
            raise QueryError(f"待查展品编号在清册中不存在：{t!r}")
        if row[0] != "exhibit":
            raise QueryError(f"待查编号 {t!r} 是周转箱而非展品")


# --------------------------------------------------------------------------- #
# 路径重建与段构建
#
# 历史路径不能靠「对每个节点查 ts 时刻的边」拼出来：某节点在 ts 之后
# 可能再次移动，单点查询会拿到错误的边。正确做法是对相关节点集合做
# 事件回放——每个节点维护一条按时间有序的边事件流（初始清册边在
# t=-inf，交接边在交接时刻），推进到目标时刻后沿当前父指针走链。
# --------------------------------------------------------------------------- #
def _node_edge_stream(conn, node: str):
    """产出节点 node 的有序边事件 (ts, parent_id, parent_kind, evidence)。

    初始清册边以 None 打头表示「盘点时刻即生效、无更早交接」；
    之后按 (ts, record_id) 有序追加该节点参与的全部交接。
    """
    initial = storage.initial_parent_of(conn, node)
    if initial is None:
        raise TraceError(f"物件 {node} 缺少清册记录，无法重建路径")
    parent_id, parent_kind, evidence = initial
    yield (None, parent_id, parent_kind, evidence)
    # 直接迭代服务端游标，不把该节点的全部历史物化进内存
    cur = conn.execute(
        "SELECT ts, new_parent, new_kind, record_id FROM handovers "
        "WHERE obj_id = ? ORDER BY ts, record_id",
        (node,),
    )
    for ts, new_parent, new_kind, record_id in cur:
        yield (ts, new_parent, new_kind, record_id)


class _NodeState:
    """单节点的边事件回放游标：advance_to 把当前边推进到不晚于 ts 的最后一条。

    调用方按时间升序推进；晚于当前目标时刻的事件保留在 _pending，
    下一次 advance_to 先消费它，事件不会丢失也不会倒流。
    """

    def __init__(self, conn, node: str):
        self.node = node
        self._events = iter(_node_edge_stream(conn, node))
        self._cur = next(self._events)  # 初始清册边一定存在
        self._pending: Optional[tuple] = None

    def advance_to(self, ts: int) -> None:
        while True:
            if self._pending is None:
                event = next(self._events, None)
            else:
                event = self._pending
                self._pending = None
            if event is None:
                return
            if event[0] > ts:
                self._pending = event  # 留给下一次推进
                return
            self._cur = event

    @property
    def edge(self):
        return self._cur[1], self._cur[2], self._cur[3]


def build_path_at(
    conn,
    obj_id: str,
    ts: int,
    states: dict | None = None,
    advance_ts: int | None = None,
) -> list[dict[str, str]]:
    """重建 obj_id 在时刻 ts（含该秒交接后的状态）的完整有序路径。

    返回从展品本身出发、经各层箱、直到最终地点的有序关系序列：
    [{"from": 子, "to": 父, "parent_kind": ..., "evidence": 记录号|初始物件号}, ...]
    states 给出相关节点的回放游标；走链遇到节点集之外的新祖先箱时
    （例如互换后新出现的父箱）惰性建游标，并用 advance_ts（默认 ts）
    推进到同一时刻，保证它拿到的也是该时刻的边。
    """
    target_ts = ts if advance_ts is None else advance_ts
    path: list[dict[str, str]] = []
    node = obj_id
    seen: set[str] = set()
    while True:
        if node in seen:  # 防御：重放期已保证无环
            raise TraceError(f"路径重建遇到环（数据可能损坏）：{node}")
        seen.add(node)
        if states is not None:
            state = states.get(node)
            if state is None:
                state = _NodeState(conn, node)
                state.advance_to(target_ts)
                states[node] = state
            parent_id, parent_kind, evidence = state.edge
        else:
            edge = storage.edge_at(conn, node, ts)
            if edge is None:
                raise TraceError(f"物件 {node} 缺少去向边，无法重建路径")
            parent_id, parent_kind, evidence = edge
        path.append(
            {
                "from": node,
                "to": parent_id,
                "parent_kind": parent_kind,
                "evidence": evidence,
            }
        )
        if parent_kind == "location":
            return path
        node = parent_id


def _path_signature(path: list[dict[str, str]]) -> list[tuple]:
    return [(e["from"], e["to"], e["evidence"]) for e in path]


def collect_relevant_nodes(conn, exhibit_id: str, cutoff: int) -> set[str]:
    """收集「在 cutoff 之前曾经出现在该展品装箱链上」的全部节点。

    从初始清册链出发，按时间顺序重放只与已知节点相关的事件；
    每遇到新祖先箱就纳入节点集并重扫，直到集合收敛。事件按
    (ts, record_id, obj_id) 全序，结果与输入行序无关。
    """
    nodes: set[str] = {exhibit_id}
    # 初始链（objects 表即盘点时刻状态）
    node = exhibit_id
    seen_init: set[str] = set()
    while True:
        row = storage.initial_parent_of(conn, node)
        if row is None:
            raise TraceError(f"物件 {node} 缺少清册记录")
        parent_id, parent_kind, _evidence = row
        if parent_kind == "location":
            break
        nodes.add(parent_id)
        if parent_id in seen_init:
            raise TraceError("初始清册成环（应已在装载期拒绝）")
        seen_init.add(parent_id)
        node = parent_id

    changed = True
    while changed:
        changed = False
        events = storage.affecting_events(conn, nodes)
        for ts, _record_id, obj_id in events:
            if ts >= cutoff:
                break
            if obj_id not in nodes:
                continue
            edge = storage.edge_at(conn, obj_id, ts)
            parent_id, parent_kind = edge[0], edge[1]
            if parent_kind == "box" and parent_id not in nodes:
                nodes.add(parent_id)
                changed = True
    return nodes


def build_segments(
    conn, exhibit_id: str, start: int, end: int, cutoff: int
) -> list[dict[str, Any]]:
    """产出覆盖 [start, end) 的互不重叠连续段。

    为全部相关节点各建一条边事件回放游标，扫描窗口内的候选交接时刻：
    先把所有游标推进到该时刻，再沿当前父指针走链得到终态路径；
    签名不变不拆段，地点未变但装箱路径或凭据变化（签名不同）一定拆段。
    窗口起点的状态自然包含起点之前交接的影响。
    """
    nodes = collect_relevant_nodes(conn, exhibit_id, cutoff)
    states = {node: _NodeState(conn, node) for node in nodes}

    boundary_ts: list[int] = []
    last_ts: Optional[int] = None
    for ts, _rid, _oid in storage.affecting_events(conn, nodes):
        if start <= ts < end and ts != last_ts:
            boundary_ts.append(ts)
            last_ts = ts

    # 起点状态：把所有相关游标推进到 start（含 start 秒的交接后状态）
    for st in states.values():
        st.advance_to(start)
    segments: list[dict[str, Any]] = []
    seg_start = start
    prev_path = build_path_at(conn, exhibit_id, start, states, advance_ts=start)
    prev_sig = _path_signature(prev_path)
    for ts in boundary_ts:
        for st in states.values():
            st.advance_to(ts)
        new_path = build_path_at(conn, exhibit_id, ts, states, advance_ts=ts)
        new_sig = _path_signature(new_path)
        if new_sig != prev_sig:
            segments.append(
                {
                    "exhibit_id": exhibit_id,
                    "start": seg_start,
                    "end": ts,
                    "path": prev_path,
                }
            )
            seg_start = ts
            prev_path, prev_sig = new_path, new_sig
    segments.append(
        {
            "exhibit_id": exhibit_id,
            "start": seg_start,
            "end": end,
            "path": prev_path,
        }
    )
    return segments


# --------------------------------------------------------------------------- #
# 顶层入口
# --------------------------------------------------------------------------- #
def run_trace(
    catalog_path: str,
    manifest_path: str,
    handover_path: str,
    query_path: str,
    inventory_ts: int,
    out_dir: str,
    work_dir: Optional[str] = None,
    receiving_id: str = RECEIVING_LOCATION,
    run_size: int = _RUN_SIZE,
) -> dict[str, Any]:
    """执行一次离线追溯。任何校验失败都抛 TraceError，不写交付目录。"""
    if not is_int(inventory_ts):
        raise BadParameter("盘点时刻必须是整数秒")

    catalog = load_catalog(catalog_path, receiving_id)
    valid_locations = set(catalog["booth_ids"]) | {receiving_id}

    # 查询参数先读，end 即截止秒，决定台账纳入哪些交接
    query = load_query(query_path, inventory_ts, cutoff=_query_end(query_path))
    cutoff = query["end"]

    os.makedirs(out_dir, exist_ok=True)
    _clean_stale(out_dir)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=out_dir)
    work = None
    try:
        if work_dir is None:
            work = tempfile.mkdtemp(prefix=".work-", dir=out_dir)
        else:
            os.makedirs(work_dir, exist_ok=True)
            work = tempfile.mkdtemp(prefix=".trace-work-", dir=work_dir)
        db_path = os.path.join(work, "ledger.db")
        conn = storage.open_store(db_path)
        try:
            storage.create_schema(conn)
            for loc_id in catalog["booth_ids"]:
                storage.add_location(conn, loc_id, "booth")
            storage.add_location(conn, receiving_id, "receiving")
            conn.commit()

            manifest_counts = load_manifest(
                conn, manifest_path, valid_locations, work, run_size
            )
            handover_counts = load_handovers(
                conn, handover_path, inventory_ts, cutoff, work, run_size
            )
            replay_counts = replay_handovers(conn, cutoff)
            validate_targets(conn, query["targets"])

            # 追溯段：逐展品流式写临时结果，再外排为交付顺序
            raw_path = os.path.join(work, "segments.raw.jsonl")
            segment_rows = 0
            with open(raw_path, "w", encoding="utf-8") as dst:
                for exhibit_id in query["targets"]:
                    for seg in build_segments(
                        conn, exhibit_id, query["start"], query["end"], cutoff
                    ):
                        dst.write(extmem.dump_record(seg))
                        dst.write("\n")
                        segment_rows += 1
        finally:
            storage.cleanup_temp(conn)
            conn.close()

        sorted_path = os.path.join(staging, "segments.jsonl")
        extmem.sort_jsonl(
            raw_path,
            sorted_path,
            key=lambda r: (r["exhibit_id"], r["start"]),
            tmpdir=os.path.join(work, "sort-segments"),
            run_size=run_size,
        )
        _verify_segment_coverage(sorted_path, query)

        manifest = {
            "schema_version": 1,
            "kind": "expo-offline-trace",
            "inventory_ts": inventory_ts,
            "query": {
                "start": query["start"],
                "end": query["end"],
                "targets": query["targets"],
            },
            "catalog_version": catalog["catalog_version"],
            "booth_ids": sorted(catalog["booth_ids"]),
            "booth_count": catalog["booth_count"],
            "receiving_location": receiving_id,
            "counts": {
                **manifest_counts,
                **handover_counts,
                **replay_counts,
                "targets": len(query["targets"]),
                "segment_rows": segment_rows,
            },
            "outputs": {"segments": "segments.jsonl"},
        }
        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")

        name = _next_trace_name(out_dir)
        result_dir = os.path.join(out_dir, name)
        os.replace(staging, result_dir)
        staging = None
        _atomic_write_text(os.path.join(out_dir, "CURRENT"), name + "\n")
        return {"trace": name, "trace_dir": os.path.abspath(result_dir), "manifest": manifest}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def _query_end(query_path: str) -> int:
    """轻量预读 end 作为截止秒（load_query 随后做完整校验）。"""
    try:
        with open(query_path, "r", encoding="utf-8") as fh:
            q = json.load(fh)
    except FileNotFoundError:
        raise BadParameter(f"查询参数文件不存在：{query_path}") from None
    except json.JSONDecodeError as exc:
        raise BadParameter(f"查询参数不是合法 JSON：{query_path}（{exc}）") from None
    end = q.get("end") if isinstance(q, dict) else None
    if not is_int(end):
        raise BadParameter("查询 end 必须是整数秒")
    return end


def _verify_segment_coverage(sorted_path: str, query: dict[str, Any]) -> None:
    """独立自检：每展品的段连续、互不重叠且恰好覆盖 [start, end)。"""
    expected_start = query["start"]
    expected_end = query["end"]
    current_id: Optional[str] = None
    cursor = expected_start
    for rec in extmem.iter_jsonl(sorted_path):
        if rec["exhibit_id"] != current_id:
            if current_id is not None and cursor != expected_end:
                raise QueryError(
                    f"展品 {current_id} 的段未覆盖到查询终点：缺口到 {expected_end}，实际到 {cursor}"
                )
            current_id = rec["exhibit_id"]
            cursor = expected_start
        if rec["start"] != cursor:
            raise QueryError(
                f"展品 {current_id} 的段不连续或重叠：期望起点 {cursor}，实际 {rec['start']}"
            )
        if rec["end"] <= rec["start"]:
            raise QueryError(f"展品 {current_id} 存在非正长度段：{rec['start']}->{rec['end']}")
        cursor = rec["end"]
    if current_id is not None and cursor != expected_end:
        raise QueryError(
            f"展品 {current_id} 的段未覆盖到查询终点：缺口到 {expected_end}，实际到 {cursor}"
        )
    found = {rec["exhibit_id"] for rec in extmem.iter_jsonl(sorted_path)}
    missing = set(query["targets"]) - found
    if missing:
        raise QueryError(f"以下展品没有任何追溯段：{sorted(missing)}")


def _next_trace_name(out_dir: str) -> str:
    import re

    seq = 0
    for name in os.listdir(out_dir):
        match = re.fullmatch(r"trace-(\d{6})", name)
        if match:
            seq = max(seq, int(match.group(1)))
    return f"trace-{seq + 1:06d}"


def _clean_stale(out_dir: str) -> None:
    for name in os.listdir(out_dir):
        if name.startswith((".staging-", ".work-")):
            shutil.rmtree(os.path.join(out_dir, name), ignore_errors=True)


def _atomic_write_text(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
