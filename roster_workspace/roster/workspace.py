"""名册工作区核心状态机。

设计要点（与需求逐条对应，见 README「需求对应」）：

* 初始化只读取已验收的全目录采集报告：报告必须 ``complete=true`` 且根筛选
  为空（带任何 include/exclude 的根筛选一律拒绝）；展位编号在届次内唯一。
* 展位身份是 ``(届次序号, 展位编号)``；同名、同标签、同编号都不会自动并组。
* 人工依据（evidence）登记/撤回「两个展位同属一个参展商」。依据编号全局
  不可复用；一条依据只在 active 时贡献一条无向连接，连接按传递关系成组。
* 每个身份组的主标识 = 最早出现届次中字典序最小的展位编号。
* 变更集以 (届次, 主标识) 为表键：初始化全量 upsert；之后每批只发删除的键
  和新增/改写的完整行；无变化不发；即使无名册变化也发带两个提交号的空变更。
* 更正只触碰本批展位批前/批后所在的身份组：邻接索引、组件表、行索引均为
  增量维护，沿受影响展位做局部 BFS/DSU，不遍历无关组的展位、连接或名册行。
* 同提交号重送：操作集合（归一化、去方向、忽略顺序）相同则原样返回原变更集；
  内容不同则拒绝。基于旧提交号的新批、未知展位、重复登记、撤回未生效依据
  一律拒绝且不改变任何已发布结果。
* 状态与变更集原子替换落盘；进程中断重开不会出现「身份已更正但取不回变更」。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from bisect import bisect_left, insort
from typing import Any

INIT_COMMIT = "commit-0"
STATE_FILENAME = "state.json"
CHANGE_DIRNAME = "changes"
SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# 异常类型
# --------------------------------------------------------------------------- #
class RosterError(Exception):
    """工作区可预期错误的基类，CLI 以退出码 2 呈现。"""

    code = "error"


class ReportRejected(RosterError):
    code = "report_rejected"


class WorkspaceExists(RosterError):
    code = "workspace_exists"


class StaleCommit(RosterError):
    code = "stale_commit"


class CommitConflict(RosterError):
    code = "commit_conflict"


class UnknownBooth(RosterError):
    code = "unknown_booth"


class BadOperation(RosterError):
    code = "bad_operation"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def booth_key(order: int, booth_id: str) -> str:
    """展位的全局唯一引用：``"届次序号/展位编号"``，如 ``"2/B07"``。"""
    return f"{order}/{booth_id}"


def parse_booth_ref(ref: str) -> tuple[int, str]:
    """解析 ``"order/BoothId"`` 形式的展位引用。"""
    if not isinstance(ref, str) or "/" not in ref:
        raise BadOperation(f"展位引用必须形如 '届次序号/展位编号'，收到 {ref!r}")
    left, _, right = ref.partition("/")
    try:
        order = int(left)
    except (TypeError, ValueError):
        raise BadOperation(f"展位引用的届次序号必须是整数：{ref!r}") from None
    if not right:
        raise BadOperation(f"展位引用缺少展位编号：{ref!r}")
    return order, right


def state_path_for(workdir: str) -> str:
    return os.path.join(workdir, STATE_FILENAME)


def changeset_path(workdir: str, commit: str) -> str:
    return os.path.join(workdir, CHANGE_DIRNAME, f"{commit}.json")


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    """同目录临时文件 + os.replace 原子替换；目录不存在则创建。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _canonical_digest(obj: Any) -> str:
    blob = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# 表键在内存中是 (order, primary_id)，落盘统一编码为 "order/primary_id"。
def _tkey_to_str(tkey: tuple[int, str]) -> str:
    return f"{tkey[0]}/{tkey[1]}"


def _str_to_tkey(text: str) -> tuple[int, str]:
    order, _, primary = text.partition("/")
    return int(order), primary


def _encode_state(state: dict[str, Any]) -> dict[str, Any]:
    """把内存状态转成 JSON 可序列化结构（tuple 键显式编码）。"""
    encoded = dict(state)
    encoded["component_of"] = dict(state["component_of"])
    encoded["components"] = {
        str(cid): list(members) for cid, members in state["components"].items()
    }
    encoded["component_rows"] = {
        str(cid): [_tkey_to_str(tuple(k)) for k in row_keys]
        for cid, row_keys in state["component_rows"].items()
    }
    encoded["row_index"] = {
        _tkey_to_str(k): row for k, row in state["row_index"].items()
    }
    return encoded


def _decode_state(raw: dict[str, Any]) -> dict[str, Any]:
    """还原内存状态：组件号转回 int，表键转回 tuple。"""
    raw["components"] = {
        int(cid): list(members) for cid, members in raw["components"].items()
    }
    raw["component_rows"] = {
        int(cid): [_str_to_tkey(k) for k in row_keys]
        for cid, row_keys in raw["component_rows"].items()
    }
    raw["row_index"] = {
        _str_to_tkey(k): row for k, row in raw["row_index"].items()
    }
    return raw


# --------------------------------------------------------------------------- #
# 报告准入
# --------------------------------------------------------------------------- #
def _load_accepted_report(path: str) -> dict[str, Any]:
    """加载并校验一份采集报告，返回带 booths 列表的报告字典。

    拒绝条件：无法解析、结构缺字段、complete 非真、根筛选非空（带根筛选）、
    展位编号缺失/重复。不重新调用任何服务，也不读取采集器之外的信息。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except FileNotFoundError:
        raise ReportRejected(f"报告不存在：{path}") from None
    except json.JSONDecodeError as exc:
        raise ReportRejected(f"报告不是合法 JSON：{path}（{exc}）") from None
    if not isinstance(report, dict):
        raise ReportRejected(f"报告顶层必须是对象：{path}")

    if report.get("complete") is not True:
        raise ReportRejected(
            f"{path}: 拒绝非完整报告（complete={report.get('complete')!r}，"
            f"终止类别={report.get('termination_class')!r}）"
        )
    root = report.get("root_filter")
    if not isinstance(root, dict):
        raise ReportRejected(f"{path}: 缺少 root_filter 字段")
    if root.get("include") or root.get("exclude"):
        raise ReportRejected(
            f"{path}: 拒绝带根筛选的报告 root_filter={root!r}（名册只接受全目录）"
        )
    booths = report.get("booths")
    if not isinstance(booths, list) or not booths:
        raise ReportRejected(f"{path}: 完整报告必须含非空 booths 列表")

    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for row in booths:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ReportRejected(f"{path}: 展位记录缺少字符串 id：{row!r}")
        bid = row["id"]
        if bid in seen:
            raise ReportRejected(f"{path}: 报告内展位编号重复：{bid}")
        seen.add(bid)
        clean.append(
            {
                "id": bid,
                "tags": sorted(row.get("tags", []) or []),
                "content": row.get("content", ""),
            }
        )
    report["booths"] = sorted(clean, key=lambda r: r["id"])
    return report


# --------------------------------------------------------------------------- #
# 纯函数：组件、主标识、名册行
# --------------------------------------------------------------------------- #
def _primary_for(member_keys: list[str], order_of: dict[str, int]) -> str:
    """主标识：最早出现届次、该届字典序最小的展位编号。"""
    min_order = min(order_of[k] for k in member_keys)
    return min(
        k.split("/", 1)[1]
        for k in member_keys
        if order_of[k] == min_order
    )


def _rows_for_component(
    cid: int,
    member_keys: list[str],
    order_of: dict[str, int],
    editions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """一个身份组的名册行：每个实际参展届次一行，缺席届次不补行。"""
    primary = _primary_for(member_keys, order_of)
    present = sorted({order_of[k] for k in member_keys})
    rows: list[dict[str, Any]] = []
    prev_seen: int | None = None
    for order in present:
        ids = sorted(
            k.split("/", 1)[1] for k in member_keys if order_of[k] == order
        )
        rows.append(
            {
                "edition": editions[order - 1]["edition"],
                "order": order,
                "primary_id": primary,
                "booth_ids": ids,
                "prev_edition": (
                    editions[prev_seen - 1]["edition"]
                    if prev_seen is not None
                    else None
                ),
            }
        )
        prev_seen = order
    return rows


def _row_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    return row["order"], row["primary_id"]


def _row_table_key(row: dict[str, Any]) -> tuple[int, str]:
    return row["order"], row["primary_id"]


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    """去掉表键后的完整行内容（upsert 载荷）。"""
    return {
        "edition": row["edition"],
        "booth_ids": list(row["booth_ids"]),
        "prev_edition": row["prev_edition"],
    }


def _diff_scoped(
    old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """只在作用域名册行之间求差异：删除的键 + 新增/改写的完整行。"""
    old_map = {_row_table_key(r): r for r in old_rows}
    new_map = {_row_table_key(r): r for r in new_rows}
    deletes = [
        {"key": {"order": order, "primary_id": primary}}
        for order, primary in sorted(set(old_map) - set(new_map))
    ]
    upserts: list[dict[str, Any]] = []
    for order, primary in sorted(new_map):
        entry = {
            "key": {"order": order, "primary_id": primary},
            "row": _row_payload(new_map[(order, primary)]),
        }
        old = old_map.get((order, primary))
        if old is None or _row_payload(old) != entry["row"]:
            upserts.append(entry)
    return {"deletes": deletes, "upserts": upserts}


# --------------------------------------------------------------------------- #
# 状态持久化
# --------------------------------------------------------------------------- #
def _initial_state(
    editions: list[dict[str, Any]], booths: list[dict[str, Any]]
) -> dict[str, Any]:
    """初始化状态：无依据、无邻接；每个展位自成组件、自成一行。"""
    order_of = {b["key"]: b["order"] for b in booths}
    component_of: dict[str, int] = {}
    components: dict[int, list[str]] = {}
    component_rows: dict[int, list[tuple[int, str]]] = {}
    row_index: dict[tuple[int, str], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for idx, b in enumerate(booths, start=1):
        cid = idx
        component_of[b["key"]] = cid
        components[cid] = [b["key"]]
        comp_rows = _rows_for_component(cid, [b["key"]], order_of, editions)
        components_rows_keys = [_row_table_key(r) for r in comp_rows]
        component_rows[cid] = components_rows_keys
        for r in comp_rows:
            row_index[_row_table_key(r)] = r
            insort(rows, r, key=_row_sort_key)
    return {
        "schema_version": SCHEMA_VERSION,
        "editions": editions,
        "booths": booths,
        "evidence": {},          # id -> {pair, active, created_commit, withdrawn_commit}
        "adjacency": {b["key"]: {} for b in booths},
        # key -> {neighbor_key: [evidence_id, ...]}（仅 active 依据）
        "component_of": component_of,
        "components": components,            # cid -> [booth_key, ...]
        "component_rows": component_rows,    # cid -> [table_key, ...]
        "row_index": row_index,              # table_key -> row
        "rows": rows,                        # 按 (order, primary_id) 排序
        "next_component_id": len(booths) + 1,
        "head_commit": None,
        "commits": [],
    }


def load_state(workdir: str) -> dict[str, Any]:
    """读取工作区状态并做头部一致性检查（中断重开不会留下悬空状态）。"""
    path = state_path_for(workdir)
    if not os.path.exists(path):
        raise RosterError(f"工作区尚未初始化：{workdir}")
    with open(path, "r", encoding="utf-8") as fh:
        state = _decode_state(json.load(fh))
    head = state.get("head_commit")
    if head is None or not state.get("commits"):
        raise RosterError(f"工作区状态缺少已发布提交：{path}")
    if state["commits"][-1]["commit"] != head:
        raise RosterError("工作区状态损坏：head_commit 与提交记录不一致")
    cs_path = changeset_path(workdir, head)
    if not os.path.exists(cs_path):
        raise RosterError(
            f"工作区状态损坏：头部提交 {head} 的变更集丢失，无法保证可重放"
        )
    with open(cs_path, "r", encoding="utf-8") as fh:
        changeset = json.load(fh)
    if changeset.get("commit") != head:
        raise RosterError(f"变更集 {cs_path} 的提交号与头部不一致")
    return state


def _publish(
    state: dict[str, Any],
    workdir: str,
    commit: str,
    base: str | None,
    kind: str,
    operations: list[dict[str, Any]],
    changes: dict[str, list[dict[str, Any]]],
    digest: str,
) -> dict[str, Any]:
    """写变更集并原子推进状态。先落变更集，再替换状态。"""
    changeset = {
        "schema_version": SCHEMA_VERSION,
        "commit": commit,
        "base_commit": base,
        "kind": kind,
        "operations": operations,
        "digest": digest,
        "changes": changes,
    }
    record = {
        "commit": commit,
        "base_commit": base,
        "kind": kind,
        "digest": digest,
        "changeset": os.path.relpath(changeset_path(workdir, commit), workdir),
    }
    # 先写新变更集文件（不触碰任何已有文件），再原子替换状态文件；
    # 中断只会停在「变更集已写、状态未推进」或之前，二者永不出现悬空组合。
    _atomic_write(changeset_path(workdir, commit), changeset)
    state["head_commit"] = commit
    state["commits"].append(record)
    _atomic_write(state_path_for(workdir), _encode_state(state))
    return changeset


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def init_workspace(
    workdir: str,
    edition_specs: list[tuple[str, str]],
    *,
    reset: bool = False,
) -> dict[str, Any]:
    """从多届已验收报告建立本地工作区并交付初始化变更集。

    ``edition_specs`` 为 ``(届次名, 报告路径)`` 列表，顺序即从早到晚；
    届次名不可重复。已存在的工作区除非 ``reset=True`` 否则拒绝覆盖。
    """
    if os.path.exists(state_path_for(workdir)) and not reset:
        raise WorkspaceExists(f"工作区已存在：{workdir}（如需重建请显式 reset）")
    if not edition_specs:
        raise RosterError("初始化至少需要一届报告")

    names = [name for name, _ in edition_specs]
    if len(names) != len(set(names)):
        raise RosterError(f"届次名重复：{names}")

    editions: list[dict[str, Any]] = []
    booths: list[dict[str, Any]] = []
    for order, (name, report_path) in enumerate(edition_specs, start=1):
        report = _load_accepted_report(report_path)
        editions.append(
            {
                "edition": name,
                "order": order,
                "report_path": os.path.abspath(report_path),
                "catalog_version": report.get("catalog_version"),
                "booth_count": len(report["booths"]),
            }
        )
        for row in report["booths"]:
            booths.append(
                {
                    "key": booth_key(order, row["id"]),
                    "order": order,
                    "booth_id": row["id"],
                    "tags": row["tags"],
                    "content": row["content"],
                }
            )

    state = _initial_state(editions, booths)
    digest = _canonical_digest({"base_commit": None, "operations": []})
    changes = {
        "deletes": [],
        "upserts": [
            {
                "key": {"order": r["order"], "primary_id": r["primary_id"]},
                "row": _row_payload(r),
            }
            for r in state["rows"]
        ],
    }
    os.makedirs(os.path.join(workdir, CHANGE_DIRNAME), exist_ok=True)
    changeset = _publish(
        state, workdir, INIT_COMMIT, None, "init", [], changes, digest
    )
    return {"state": state, "changeset": changeset}


# --------------------------------------------------------------------------- #
# 更正提交（作用域处理）
# --------------------------------------------------------------------------- #
def _normalized_ops(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化操作集合：展位对去方向、按稳定键排序（顺序不计入内容）。"""
    norm = [
        {
            "action": op["action"],
            "evidence_id": op["evidence_id"],
            "pair": sorted(op["pair"]),
        }
        for op in operations
    ]
    norm.sort(key=lambda o: (o["action"], o["evidence_id"], o["pair"]))
    return norm


def _reachable(start: set[str], neighbor_of) -> set[str]:
    """从起点沿给定邻接视图 BFS，只访问可达（同身份组）展位。

    ``neighbor_of(key)`` 返回该节点批后的邻居映射；调用方保证只暴露
    作用域内需要的边，从而不遍历无关组的连接。
    """
    seen: set[str] = set()
    stack = list(start)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for neighbor in neighbor_of(node):
            if neighbor not in seen:
                stack.append(neighbor)
    return seen


def correct(
    workdir: str,
    commit: str,
    base_commit: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """应用一批身份更正并交付相对上一批的变更集。

    全量校验通过后才做任何修改；任何非法情形都原样抛错且不触碰已发布结果。
    处理范围严格限定在本批展位批前/批后所属身份组内。
    """
    state = load_state(workdir)
    head = state["head_commit"]
    known_commits = {c["commit"] for c in state["commits"]}

    if not isinstance(commit, str) or not commit:
        raise BadOperation("提交号必须是非空字符串")
    if not isinstance(base_commit, str) or not base_commit:
        raise BadOperation("预期上一提交号必须是非空字符串")
    if commit in known_commits:
        raise CommitConflict(f"提交号 {commit} 已发布，不能重复使用（重送走 replay）")
    if base_commit != head:
        raise StaleCommit(
            f"预期上一提交号 {base_commit!r} 不是当前头部 {head!r}"
        )
    if not operations:
        raise BadOperation("更正批次至少包含一条登记或撤回操作")

    evidence = state["evidence"]
    adjacency = state["adjacency"]
    # 展位收录情况直接查组件表（初始化时每个展位都在其中），无需遍历全部展位。
    def _booth_known(key: str) -> bool:
        return key in state["component_of"]

    # -- 1. 批内结构校验：一批内同一依据最多操作一次，两个展位必须不同且已收录 --
    touched_ev: set[str] = set()
    parsed: list[dict[str, Any]] = []
    for op in operations:
        if not isinstance(op, dict):
            raise BadOperation(f"操作必须是对象：{op!r}")
        action = op.get("action")
        if action not in ("register", "withdraw"):
            raise BadOperation(f"action 只能是 register/withdraw：{op!r}")
        ev_id = op.get("evidence_id")
        if not isinstance(ev_id, str) or not ev_id:
            raise BadOperation(f"依据编号必须是非空字符串：{op!r}")
        if ev_id in touched_ev:
            raise BadOperation(f"一批内对同一依据 {ev_id} 操作了两次")
        touched_ev.add(ev_id)
        pair = op.get("pair")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise BadOperation(f"依据必须引用恰好两个展位：{op!r}")
        refs = []
        for ref in pair:
            order, bid = parse_booth_ref(ref)
            key = booth_key(order, bid)
            if not _booth_known(key):
                raise UnknownBooth(f"未知展位 {ref!r}（未在任何已收录报告中）")
            refs.append(key)
        if refs[0] == refs[1]:
            raise BadOperation(f"依据 {ev_id} 必须引用两个不同的展位：{pair!r}")
        parsed.append(
            {"action": action, "evidence_id": ev_id, "pair": sorted(refs)}
        )

    # -- 2. 登记/撤回准入（依据编号不可复用；撤回必须命中一条有效依据） --
    for op in parsed:
        ev_id, action = op["evidence_id"], op["action"]
        if action == "register":
            if ev_id in evidence:
                raise BadOperation(f"依据编号 {ev_id} 已存在，不可复用")
        else:
            if ev_id not in evidence:
                raise BadOperation(f"撤回失败：依据 {ev_id} 不存在")
            rec = evidence[ev_id]
            if not rec["active"]:
                raise BadOperation(f"撤回未生效：依据 {ev_id} 已处于撤回状态")
            if tuple(rec["pair"]) != tuple(op["pair"]):
                raise BadOperation(
                    f"撤回 {ev_id} 引用的展位对 {op['pair']} 与其登记的 "
                    f"{rec['pair']} 不一致"
                )

    # -- 3. 定位批前作用域（仅沿本批展位所在组件查找，不遍历无关组） --
    affected: set[str] = set()
    for op in parsed:
        affected.update(op["pair"])
    before_cids = {state["component_of"][k] for k in affected}
    scoped_before: set[str] = set()
    for cid in before_cids:
        scoped_before.update(state["components"][cid])

    # 届次序号直接从展位键 "order/id" 解析，只对作用域展位调用，不建全量索引。
    def _order_of(key: str) -> int:
        return int(key.split("/", 1)[0])

    old_row_keys: set[tuple[int, str]] = set()
    old_rows_scoped: list[dict[str, Any]] = []
    for cid in before_cids:
        for tkey in state["component_rows"][cid]:
            old_row_keys.add(tuple(tkey))
            old_rows_scoped.append(state["row_index"][tuple(tkey)])

    # -- 4. 只记录本批的依据/边增量，不深拷贝、不遍历全部连接 --
    added_edges: set[tuple[str, str]] = set()
    withdrawn_evidence: set[str] = set()
    for op in parsed:
        if op["action"] == "register":
            added_edges.add(tuple(op["pair"]))
        else:
            withdrawn_evidence.add(op["evidence_id"])

    def _post_neighbors(key: str) -> list[str]:
        """某节点批后的邻居（按依据粒度判定边是否仍存活）。

        一条旧边只要还剩任一未撤回的依据就保留；本批新登记的边追加进来。
        多条依据支持同一对时，撤回其中一条不会断开连接。
        """
        result: list[str] = []
        for neighbor, ev_ids in adjacency.get(key, {}).items():
            if any(eid not in withdrawn_evidence for eid in ev_ids):
                result.append(neighbor)
        for a, b in added_edges:
            if key == a:
                result.append(b)
            elif key == b:
                result.append(a)
        return result

    # -- 5. 批后作用域：从同一批起点沿批后邻接视图 BFS，仅触及可达节点 --
    scoped_after = _reachable(affected, _post_neighbors)
    scoped = scoped_before | scoped_after

    # 批后局部组件（DSU 只建在作用域展位与作用域邻边上）。
    parent = {k: k for k in scoped}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for k in scoped:  # 只遍历作用域展位的批后邻居
        for neighbor in _post_neighbors(k):
            if neighbor in scoped and k < neighbor:
                ra, rb = find(k), find(neighbor)
                if ra != rb:
                    parent[rb] = ra

    groups: dict[str, list[str]] = {}
    for k in scoped:
        groups.setdefault(find(k), []).append(k)

    # -- 6. 组件号分配：与批前完全同组则沿用旧号，否则分配新号，旧号退役 --
    next_cid = state["next_component_id"]
    scoped_order_of = {k: _order_of(k) for k in scoped}
    new_rows_scoped: list[dict[str, Any]] = []
    cid_updates: dict[str, int] = {}
    new_components: dict[int, list[str]] = {}
    new_component_rows: dict[int, list[tuple[int, str]]] = {}
    for members in groups.values():
        member_before_cids = {state["component_of"][k] for k in members}
        if len(member_before_cids) == 1 and len(members) == len(
            state["components"][next(iter(member_before_cids))]
        ):
            cid = next(iter(member_before_cids))
        else:
            cid = next_cid
            next_cid += 1
        comp_rows = _rows_for_component(
            cid, sorted(members), scoped_order_of, state["editions"],
        )
        new_components[cid] = sorted(members)
        new_component_rows[cid] = [_row_table_key(r) for r in comp_rows]
        for k in members:
            cid_updates[k] = cid
        new_rows_scoped.extend(comp_rows)

    # -- 7. 作用域内差异与表键唯一性校验（此刻尚未修改任何持久状态） --
    # 跨届合并会「接管」被并入身份在其参展届次的旧行键：若该键属于批前
    # 作用域组件（old_row_keys），或其既有行含作用域展位，都是合法改写。
    # 若新键命中一个与作用域展位完全无关的既有行，说明按「最早届次+字典序
    # 最小编号」选举出的主标识与另一个未涉及身份撞了表键，表键唯一性被破坏，
    # 必须拒绝整批（此时状态一字未改，已发布结果不受影响）。
    def _row_is_in_scope(tkey: tuple[int, str]) -> bool:
        if tkey in old_row_keys:
            return True
        row = state["row_index"].get(tkey)
        if row is None:
            return True  # 全新键
        return any(
            f"{row['order']}/{bid}" in scoped
            for bid in row["booth_ids"]
        )

    new_row_keys = {_row_table_key(r) for r in new_rows_scoped}
    if len(new_row_keys) != len(new_rows_scoped):
        raise BadOperation(
            "本批更正使两个不同身份组产生相同的 (届次, 主标识) 表键，拒绝该批"
        )
    for tkey in new_row_keys:
        if not _row_is_in_scope(tkey):
            raise BadOperation(
                f"本批更正会使两个不同身份共用表键 {tkey}（主标识选举冲突），"
                "为保证 (届次, 主标识) 唯一，拒绝该批"
            )
    changes = _diff_scoped(old_rows_scoped, new_rows_scoped)

    # -- 8. 全部校验通过，落地依据与邻接增量 --
    for op in parsed:
        a, b = op["pair"]
        ev_id = op["evidence_id"]
        if op["action"] == "register":
            evidence[ev_id] = {
                "evidence_id": ev_id,
                "pair": [a, b],
                "active": True,
                "created_commit": commit,
                "withdrawn_commit": None,
            }
            adjacency[a].setdefault(b, []).append(ev_id)
            adjacency[b].setdefault(a, []).append(ev_id)
        else:
            rec = evidence[ev_id]
            rec["active"] = False
            rec["withdrawn_commit"] = commit
            x, y = rec["pair"]
            adjacency[x][y].remove(ev_id)
            adjacency[y][x].remove(ev_id)
            if not adjacency[x][y]:
                del adjacency[x][y]
            if not adjacency[y][x]:
                del adjacency[y][x]

    # -- 9. 增量更新索引与有序名册（只删/插作用域条目） --
    for tkey in old_row_keys:
        pos = bisect_left(state["rows"], tkey, key=_row_sort_key)
        if pos < len(state["rows"]) and _row_table_key(state["rows"][pos]) == tkey:
            del state["rows"][pos]
        del state["row_index"][tkey]
    for cid in before_cids:
        state["components"].pop(cid, None)
        state["component_rows"].pop(cid, None)
    for r in new_rows_scoped:
        state["row_index"][_row_table_key(r)] = r
        insort(state["rows"], r, key=_row_sort_key)
    for k, cid in cid_updates.items():
        state["component_of"][k] = cid
    state["components"].update(new_components)
    state["component_rows"].update(new_component_rows)
    state["next_component_id"] = next_cid

    norm_ops = _normalized_ops(parsed)
    digest = _canonical_digest(
        {"base_commit": base_commit, "operations": norm_ops}
    )
    changeset = _publish(
        state, workdir, commit, base_commit, "correction",
        norm_ops, changes, digest,
    )
    return {
        "changeset": changeset,
        "scope": {
            "affected_booths": sorted(affected),
            "scoped_booth_count": len(scoped),
            "total_booth_count": len(state["booths"]),
            "before_components": sorted(before_cids),
        },
    }


def replay(workdir: str, commit: str, base_commit: str,
           operations: list[dict[str, Any]]) -> dict[str, Any]:
    """按提交号重送：同号同内容返回原变更集；同号不同内容拒绝。

    内容相等 = 上一提交号相同且规范化操作集合相同（不计操作顺序与展位对方向）。
    即使该提交之后已有更新的批次，也照样取回原始变更集。
    """
    state = load_state(workdir)
    record = next((c for c in state["commits"] if c["commit"] == commit), None)
    if record is None:
        raise CommitConflict(f"提交号 {commit} 从未发布，无法重送")
    path = changeset_path(workdir, commit)
    with open(path, "r", encoding="utf-8") as fh:
        changeset = json.load(fh)
    if changeset.get("base_commit") != base_commit:
        raise CommitConflict(
            f"重送 {commit} 内容不一致：base_commit 为 {base_commit!r}，"
            f"已发布的是 {changeset.get('base_commit')!r}"
        )
    if record["kind"] == "init":
        if operations:
            raise CommitConflict(f"初始化提交 {commit} 不携带操作")
    else:
        incoming = []
        for op in operations:
            refs = []
            for ref in op["pair"]:
                parse_booth_ref(ref)  # 形式校验
                refs.append(ref)
            incoming.append(
                {
                    "action": op.get("action"),
                    "evidence_id": op.get("evidence_id"),
                    "pair": sorted(refs),
                }
            )
        if _normalized_ops(incoming) != changeset.get("operations", []):
            raise CommitConflict(
                f"重送 {commit} 的操作集合与已发布内容不一致"
            )
    if changeset.get("digest") != record["digest"]:
        raise RosterError(f"变更集 {path} 摘要与提交记录不符（文件可能被改动）")
    return changeset


# --------------------------------------------------------------------------- #
# 独立核验（诊断命令允许全量重放；更正路径本身保持作用域处理）
# --------------------------------------------------------------------------- #
def verify_workspace(workdir: str) -> dict[str, Any]:
    """从工作区自身状态全量重放并核对所有已发布变更集与当前名册。

    不依赖任何原始采集报告仍在线（初始化时已快照展位）。
    """
    state = load_state(workdir)
    booths = state["booths"]
    editions = state["editions"]
    order_of = {b["key"]: b["order"] for b in booths}

    keys = [b["key"] for b in booths]
    if len(keys) != len(set(keys)):
        raise RosterError("核验失败：展位键重复")
    for b in booths:
        if not (1 <= b["order"] <= len(editions)):
            raise RosterError(f"核验失败：展位 {b['key']} 届次序号越界")

    evidence_sim: dict[str, dict[str, Any]] = {}
    adj: dict[str, dict[str, list[str]]] = {b["key"]: {} for b in booths}
    prev_rows: list[dict[str, Any]] = []
    checked = 0

    for record in state["commits"]:
        path = changeset_path(workdir, record["commit"])
        with open(path, "r", encoding="utf-8") as fh:
            cs = json.load(fh)
        if cs["commit"] != record["commit"] or cs["digest"] != record["digest"]:
            raise RosterError(f"核验失败：变更集 {record['commit']} 元数据不一致")

        if cs["kind"] == "init":
            if cs["base_commit"] is not None or cs["operations"]:
                raise RosterError("核验失败：初始化变更集结构非法")
            expected_rows = []
            for b in booths:
                expected_rows.extend(
                    _rows_for_component(b["key"], [b["key"]], order_of, editions)
                )
            expected_rows.sort(key=_row_sort_key)
            expected = {
                "deletes": [],
                "upserts": [
                    {
                        "key": {"order": r["order"], "primary_id": r["primary_id"]},
                        "row": _row_payload(r),
                    }
                    for r in expected_rows
                ],
            }
            prev_rows = expected_rows
        else:
            for op in cs["operations"]:
                ev_id = op["evidence_id"]
                a, b = op["pair"]
                if a not in adj or b not in adj:
                    raise RosterError(f"核验失败：依据 {ev_id} 引用未知展位")
                if op["action"] == "register":
                    if ev_id in evidence_sim:
                        raise RosterError(f"核验失败：依据 {ev_id} 被复用")
                    evidence_sim[ev_id] = {"active": True, "pair": (a, b)}
                    adj[a].setdefault(b, []).append(ev_id)
                    adj[b].setdefault(a, []).append(ev_id)
                else:
                    if ev_id not in evidence_sim or not evidence_sim[ev_id]["active"]:
                        raise RosterError(f"核验失败：依据 {ev_id} 的撤回不合法")
                    evidence_sim[ev_id]["active"] = False
                    x, y = evidence_sim[ev_id]["pair"]
                    adj[x][y].remove(ev_id)
                    adj[y][x].remove(ev_id)
                    if not adj[x][y]:
                        del adj[x][y]
                    if not adj[y][x]:
                        del adj[y][x]

            # 全量 DSU 重算当前名册。
            parent = {k: k for k in adj}

            def find(x: str) -> str:
                root = x
                while parent[root] != root:
                    root = parent[root]
                while parent[x] != root:
                    parent[x], x = root, parent[x]
                return root

            for k, neighbors in adj.items():
                for n in neighbors:
                    if k < n:
                        ra, rb = find(k), find(n)
                        if ra != rb:
                            parent[rb] = ra
            groups: dict[str, list[str]] = {}
            for k in adj:
                groups.setdefault(find(k), []).append(k)
            current_rows: list[dict[str, Any]] = []
            for members in groups.values():
                current_rows.extend(
                    _rows_for_component(0, sorted(members), order_of, editions)
                )
            current_rows.sort(key=_row_sort_key)
            expected = _diff_scoped(prev_rows, current_rows)
            prev_rows = current_rows

        if cs["changes"] != expected:
            raise RosterError(
                f"核验失败：提交 {record['commit']} 的变更集与重算结果不一致"
            )
        if cs["kind"] != "init":
            digest = _canonical_digest(
                {"base_commit": cs["base_commit"], "operations": cs["operations"]}
            )
            if digest != cs["digest"]:
                raise RosterError(f"核验失败：提交 {record['commit']} 摘要不符")
        checked += 1

    # 与持久化台账/名册交叉核对。
    live_ids = {i for i, e in state["evidence"].items() if e["active"]}
    if live_ids != {i for i, e in evidence_sim.items() if e["active"]}:
        raise RosterError("核验失败：有效依据集合与重放结果不符")
    if state["rows"] != prev_rows:
        raise RosterError("核验失败：持久化名册与全量重算结果不一致")
    if state["commits"][-1]["commit"] != state["head_commit"]:
        raise RosterError("核验失败：头部提交号不一致")
    for earlier, later in zip(state["commits"], state["commits"][1:]):
        if later["base_commit"] != earlier["commit"]:
            raise RosterError("核验失败：提交链 base_commit 断裂")

    return {
        "ok": True,
        "commits_checked": checked,
        "booths": len(booths),
        "editions": len(editions),
        "active_evidence": len(live_ids),
        "rows": len(prev_rows),
        "head_commit": state["head_commit"],
    }
