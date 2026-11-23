"""磁盘后备台账：SQLite 承载可能大于内存的清册与交接历史。

设计约束（对应题目要求）：
  * 清册、交接日志、追溯结果都可能大于内存——物件表与交接表全部落盘，
    Python 侧任何时刻只持有单条/单组记录；
  * 同秒交接可能涉及大批物件——每个同秒组的并查集也放在临时表里，
    不按组规模占用内存；
  * 不为每次交接扫描整场库存、不复制全量快照——预检与重放的每条
    SQL 都走索引，工作量只与「本组被搬物件及其装箱链」相关。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Iterator, Optional

from .errors import HandoverError, ManifestError

BATCH_INSERT = 50_000


def open_store(db_path: str) -> sqlite3.Connection:
    """建库/打开台账。WAL 不用于临时库（连接关闭即删），只用普通回滚日志。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA foreign_keys = OFF")  # 引用合法性由显式校验给出中文错误
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE locations (
            loc_id  TEXT PRIMARY KEY,
            kind    TEXT NOT NULL CHECK (kind IN ('booth', 'receiving'))
        );

        CREATE TABLE objects (
            obj_id      TEXT PRIMARY KEY,
            kind        TEXT NOT NULL CHECK (kind IN ('exhibit', 'box')),
            parent_id   TEXT NOT NULL,           -- 直接去向：位置或另一只箱
            parent_kind TEXT NOT NULL CHECK (parent_kind IN ('location', 'box')),
            evidence    TEXT NOT NULL            -- 初始凭据 = 该物件自己的编号
        );

        CREATE TABLE handovers (
            record_id   TEXT NOT NULL,
            ts          INTEGER NOT NULL,
            obj_id      TEXT NOT NULL,
            old_parent  TEXT NOT NULL,
            new_parent  TEXT NOT NULL,
            new_kind    TEXT NOT NULL CHECK (new_kind IN ('location', 'box'))
        );
        CREATE INDEX idx_hand_ts ON handovers (ts, record_id, obj_id);
        CREATE INDEX idx_hand_obj_ts ON handovers (obj_id, ts, record_id);
        CREATE INDEX idx_hand_rec ON handovers (record_id);

        -- 重放过程中的当前父指针（初始化为清册父指针，逐交接原地更新）
        CREATE TABLE current_parent (
            obj_id      TEXT PRIMARY KEY,
            parent_id   TEXT NOT NULL,
            parent_kind TEXT NOT NULL
        );
        """
    )


# --------------------------------------------------------------------------- #
# 装载
# --------------------------------------------------------------------------- #
def add_location(conn: sqlite3.Connection, loc_id: str, kind: str) -> None:
    conn.execute("INSERT INTO locations (loc_id, kind) VALUES (?, ?)", (loc_id, kind))


def bulk_load_objects(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (obj_id, kind, parent_id, parent_kind, evidence)，批量提交。"""
    conn.executemany(
        "INSERT INTO objects (obj_id, kind, parent_id, parent_kind, evidence) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT INTO current_parent (obj_id, parent_id, parent_kind) "
        "SELECT obj_id, parent_id, parent_kind FROM objects"
    )


def bulk_load_handovers(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (record_id, ts, obj_id, new_parent, new_kind, old_parent)。"""
    conn.executemany(
        "INSERT INTO handovers (record_id, ts, obj_id, old_parent, new_parent, new_kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


# --------------------------------------------------------------------------- #
# 清册合法性（流式三色深搜，整体 O(物件数)，额外内存仅与装箱深度成正比）
# --------------------------------------------------------------------------- #
def validate_manifest_forest(conn: sqlite3.Connection) -> None:
    """校验 current_parent（= 初始清册）构成森林，且每条链落到合法地点。

    引用（父箱/父地点）与对象类型在装载阶段已逐条校验，这里只查
    成环与悬挂链。对每只箱沿父指针走到地点或已完成节点：
      * 走到本次行走栈上的节点 => 成环；
      * 走到地点 => 该地点必须存在；
      * 走到此前已完成的节点 => 复用结论。
    每个节点只在栈中出现一次，总体 O(边数)，额外内存与装箱深度成正比。
    """
    done: set[str] = set()
    box_ids = [r[0] for r in conn.execute("SELECT obj_id FROM objects WHERE kind = 'box'")]
    for start in box_ids:
        if start in done:
            continue
        stack: list[str] = []
        on_stack: set[str] = set()
        node = start
        while True:
            if node in on_stack:
                idx = stack.index(node)
                chain = " -> ".join(stack[idx:] + [node])
                raise ManifestError(f"清册装箱关系成环：{chain}")
            if node in done:
                break
            row = conn.execute(
                "SELECT parent_id, parent_kind FROM current_parent WHERE obj_id = ?",
                (node,),
            ).fetchone()
            if row is None:  # 父引用在装载期已校验，这里只是防御
                raise ManifestError(f"清册物件缺少去向记录：{node}")
            parent_id, parent_kind = row
            if parent_kind == "location":
                loc = conn.execute(
                    "SELECT 1 FROM locations WHERE loc_id = ?", (parent_id,)
                ).fetchone()
                if loc is None:
                    raise ManifestError(f"清册装箱链落到不存在的地点：{parent_id}")
                break
            stack.append(node)
            on_stack.add(node)
            node = parent_id
        done.update(stack)
    # 展品直挂地点兜底再查一次（直挂箱的情况随上面的箱链一起验证过）
    bad = conn.execute(
        """
        SELECT o.obj_id, o.parent_id
          FROM objects o
         WHERE o.parent_kind = 'location'
           AND NOT EXISTS (SELECT 1 FROM locations l WHERE l.loc_id = o.parent_id)
        """
    ).fetchall()
    if bad:
        raise ManifestError(
            "清册物件直挂不存在的地点：" + ", ".join(f"{o}->{p}" for o, p in bad[:5])
        )


# --------------------------------------------------------------------------- #
# 交接日志静态预检（全部走索引/分组聚合，不重放、不扫整场）
# --------------------------------------------------------------------------- #
def check_duplicate_record_ids(conn: sqlite3.Connection, cutoff: int) -> Optional[str]:
    row = conn.execute(
        "SELECT record_id FROM handovers WHERE ts < ? GROUP BY record_id HAVING COUNT(*) > 1 "
        "LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row[0] if row else None


def check_duplicate_move_in_group(conn: sqlite3.Connection, cutoff: int) -> Optional[tuple]:
    """同一次交接（同一秒）内每件物品至多移动一次。"""
    row = conn.execute(
        "SELECT ts, obj_id, COUNT(*) FROM handovers WHERE ts < ? "
        "GROUP BY ts, obj_id HAVING COUNT(*) > 1 LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row if row else None


def handover_groups(conn: sqlite3.Connection, cutoff: int) -> Iterator[tuple[int, list[tuple]]]:
    """按 (ts, record_id, obj_id) 有序产出每个同秒组的全部行。

    同一秒的记录属于同一次交接；输出顺序只由数据内容决定，与输入行序
    无关。整组行先物化成列表再交付，调用方处理期间不持有流式游标。
    """
    cur = conn.execute(
        "SELECT ts, record_id, obj_id, old_parent, new_parent, new_kind "
        "FROM handovers WHERE ts < ? ORDER BY ts, record_id, obj_id",
        (cutoff,),
    )
    current_ts: Optional[int] = None
    buf: list[tuple] = []
    for ts, record_id, obj_id, old_parent, new_parent, new_kind in cur.fetchall():
        if current_ts is not None and ts != current_ts:
            yield current_ts, buf
            buf = []
        current_ts = ts
        buf.append((record_id, obj_id, old_parent, new_parent, new_kind))
    cur.close()
    if buf:
        assert current_ts is not None
        yield current_ts, buf


# --------------------------------------------------------------------------- #
# 单次交接：静态一致性检查
# --------------------------------------------------------------------------- #
def prepare_group_table(conn: sqlite3.Connection, rows: list[tuple], ts: int) -> None:
    """把同秒组落到临时表，供后续全部检查与更新以集合方式执行。"""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_group ( "
                 "record_id TEXT, obj_id TEXT PRIMARY KEY, old_parent TEXT, "
                 "new_parent TEXT, new_kind TEXT)")
    conn.execute("DELETE FROM tmp_group")
    conn.executemany(
        "INSERT INTO tmp_group (record_id, obj_id, old_parent, new_parent, new_kind) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def check_group_static(conn: sqlite3.Connection, ts: int) -> None:
    """原去向一致、新去向存在且类型合法、new != old。"""
    row = conn.execute(
        """
        SELECT g.obj_id, g.old_parent, c.parent_id
          FROM tmp_group g
          JOIN current_parent c ON c.obj_id = g.obj_id
         WHERE g.old_parent <> c.parent_id
         LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise HandoverError(
            f"时刻 {ts} 的交接中原去向不符：物件 {row[0]} 记录原去向={row[1]!r}，"
            f"交接前实际去向={row[2]!r}"
        )
    row = conn.execute(
        "SELECT obj_id, new_parent FROM tmp_group WHERE new_parent = old_parent LIMIT 1"
    ).fetchone()
    if row is not None:
        raise HandoverError(
            f"时刻 {ts} 的交接中新旧去向相同（不构成移动）：物件 {row[0]} -> {row[1]!r}"
        )
    # 新去向为箱：必须存在且是箱，不能是展品
    row = conn.execute(
        """
        SELECT g.obj_id, g.new_parent
          FROM tmp_group g
         WHERE g.new_kind = 'box'
           AND NOT EXISTS (
               SELECT 1 FROM objects o
                WHERE o.obj_id = g.new_parent AND o.kind = 'box')
         LIMIT 1
        """
    ).fetchone()
    if row is not None:
        exists = conn.execute(
            "SELECT kind FROM objects WHERE obj_id = ?", (row[1],)
        ).fetchone()
        if exists is None:
            raise HandoverError(
                f"时刻 {ts} 的交接中新去向不存在：物件 {row[0]} -> {row[1]!r}"
            )
        raise HandoverError(
            f"时刻 {ts} 的交接中新去向是展品而非周转箱，不能装入："
            f"物件 {row[0]} -> {row[1]!r}"
        )
    # 新去向为地点：必须在合法地点表内
    row = conn.execute(
        """
        SELECT g.obj_id, g.new_parent
          FROM tmp_group g
         WHERE g.new_kind = 'location'
           AND NOT EXISTS (SELECT 1 FROM locations l WHERE l.loc_id = g.new_parent)
         LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise HandoverError(
            f"时刻 {ts} 的交接中新去向不是合法展位或公共收货区："
            f"物件 {row[0]} -> {row[1]!r}"
        )


# --------------------------------------------------------------------------- #
# 单次交接：磁盘并查集成环检测
#
# 设 F0 为「交接前森林删掉本组被搬边」后的无环图。把每个 F0 连通分量
# 收缩成一个并查集节点，再逐条加入新边 (u -> p_u)：新边两端落在同一
# 分量，或并查集内已连通，则交接后必然成环；否则合并。并查集只有
# 「触及节点」，全部存在 SQLite 临时表里，组大小不受内存限制。
# --------------------------------------------------------------------------- #
def init_union_find(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS tmp_uf "
        "(node TEXT PRIMARY KEY, parent TEXT, rank INTEGER NOT NULL)"
    )
    conn.execute("DELETE FROM tmp_uf")


def _uf_ensure(conn: sqlite3.Connection, node: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO tmp_uf (node, parent, rank) VALUES (?, ?, 0)", (node, node)
    )


def _uf_root(conn: sqlite3.Connection, node: str) -> str:
    """带路径压缩的查根；压缩路径上的节点在临时表内原地更新。"""
    path = []
    cur = node
    while True:
        _uf_ensure(conn, cur)
        parent = conn.execute("SELECT parent FROM tmp_uf WHERE node = ?", (cur,)).fetchone()[0]
        if parent == cur:
            root = cur
            break
        path.append(cur)
        cur = parent
    for n in path:
        conn.execute("UPDATE tmp_uf SET parent = ? WHERE node = ?", (root, n))
    return root


def _is_moved(conn: sqlite3.Connection, node: str) -> bool:
    """节点是否属于本次同秒组（成员判定走临时表索引，不把整组载入内存）。"""
    return conn.execute(
        "SELECT 1 FROM tmp_group WHERE obj_id = ? LIMIT 1", (node,)
    ).fetchone() is not None


def _f0_component_id(conn: sqlite3.Connection, start: str) -> str:
    """沿「未被搬动」的父指针走到 F0 分量代表（地点 id 或某个被搬节点 id）。

    被搬节点的旧边在 F0 中已删除，因此它本身就是分量边界；成员身份直接
    查 tmp_group，调用方无需持有整组集合。路径上的触及节点登记进并查集表，
    后续直接命中。
    """
    node = start
    trail: list[str] = []
    while True:
        _uf_ensure(conn, node)
        if _is_moved(conn, node):
            boundary = node
            break
        row = conn.execute(
            "SELECT parent_id, parent_kind FROM current_parent WHERE obj_id = ?", (node,)
        ).fetchone()
        # 链顶为地点（装载/清册校验已保证合法）
        if row is None or row[1] == "location":
            boundary = row[0] if row else f"loc:{node}"
            _uf_ensure(conn, boundary)
            break
        trail.append(node)
        node = row[0]
    for n in trail:
        conn.execute("UPDATE tmp_uf SET parent = ? WHERE node = ?", (boundary, n))
    return boundary


def check_group_no_cycle(conn: sqlite3.Connection, ts: int) -> None:
    init_union_find(conn)
    # 自环：把自己装进自己
    row = conn.execute(
        "SELECT obj_id FROM tmp_group WHERE new_kind = 'box' AND new_parent = obj_id LIMIT 1"
    ).fetchone()
    if row is not None:
        raise HandoverError(f"时刻 {ts} 的交接后装箱成环：物件 {row[0]} 自包含")

    # 边按确定性次序分块读取：同秒组可以很大，不整组物化进内存。
    cur = conn.execute(
        "SELECT obj_id, new_parent, new_kind FROM tmp_group ORDER BY record_id, obj_id"
    )
    while True:
        chunk = cur.fetchmany(50_000)
        if not chunk:
            break
        for obj_id, new_parent, new_kind in chunk:
            if new_kind == "location":
                comp_u = _f0_component_id(conn, obj_id)
                # 地点是汇点：u 分量挂到地点分量，不可能反向，只会合并不会成环
                _uf_ensure(conn, new_parent)
                ru, rp = _uf_root(conn, comp_u), _uf_root(conn, new_parent)
                if ru != rp:
                    conn.execute("UPDATE tmp_uf SET parent = ? WHERE node = ?", (rp, ru))
                continue
            comp_u = _f0_component_id(conn, obj_id)
            comp_p = _f0_component_id(conn, new_parent)
            ru, rp = _uf_root(conn, comp_u), _uf_root(conn, comp_p)
            if ru == rp:
                cur.close()
                raise HandoverError(
                    f"时刻 {ts} 的交接后装箱成环：边 {obj_id} -> {new_parent} "
                    "在收缩后的分量图上闭合了环"
                )
            # 按秩合并
            rank_u = conn.execute(
                "SELECT rank FROM tmp_uf WHERE node = ?", (ru,)
            ).fetchone()[0]
            rank_p = conn.execute(
                "SELECT rank FROM tmp_uf WHERE node = ?", (rp,)
            ).fetchone()[0]
            if rank_u > rank_p:
                ru, rp = rp, ru
            conn.execute("UPDATE tmp_uf SET parent = ? WHERE node = ?", (rp, ru))
            if rank_u == rank_p:
                conn.execute("UPDATE tmp_uf SET rank = rank + 1 WHERE node = ?", (rp,))
    cur.close()


def apply_group(conn: sqlite3.Connection) -> None:
    """校验通过后原地更新当前父指针——只触碰本组被搬物件，不做快照。"""
    conn.execute(
        """
        UPDATE current_parent
           SET parent_id = (SELECT new_parent FROM tmp_group
                             WHERE tmp_group.obj_id = current_parent.obj_id),
               parent_kind = (SELECT new_kind FROM tmp_group
                                WHERE tmp_group.obj_id = current_parent.obj_id)
         WHERE obj_id IN (SELECT obj_id FROM tmp_group)
        """
    )
    conn.execute("DELETE FROM tmp_group")
    conn.execute("DELETE FROM tmp_uf")
    conn.commit()


# --------------------------------------------------------------------------- #
# 查询支持
# --------------------------------------------------------------------------- #
def current_parent_of(conn: sqlite3.Connection, obj_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT parent_id, parent_kind FROM current_parent WHERE obj_id = ?", (obj_id,)
    ).fetchone()
    return row if row is not None else (None, None)


def initial_parent_of(conn: sqlite3.Connection, obj_id: str) -> tuple[str, str, str]:
    """返回 (parent_id, parent_kind, evidence=初始清册物件号)。"""
    return conn.execute(
        "SELECT parent_id, parent_kind, evidence FROM objects WHERE obj_id = ?", (obj_id,)
    ).fetchone()


def edge_at(conn: sqlite3.Connection, obj_id: str, ts: int) -> Optional[tuple]:
    """取物件 obj_id 在时刻 ts 生效的那条边（ts 为交接时刻时取该次交接后的状态）。

    优先交接表（按 (obj, ts) 索引），没有则回落到初始清册。
    返回 (parent_id, parent_kind, evidence)，evidence 为交接记录号或初始物件号。
    """
    row = conn.execute(
        "SELECT new_parent, new_kind, record_id FROM handovers "
        "WHERE obj_id = ? AND ts <= ? ORDER BY ts DESC, record_id DESC LIMIT 1",
        (obj_id, ts),
    ).fetchone()
    if row is not None:
        return row
    return initial_parent_of(conn, obj_id)


def affecting_events(conn: sqlite3.Connection, obj_ids: set[str]) -> list[tuple]:
    """取与一组物件相关的全部交接事件（去重后按时间、记录号、物件有序）。"""
    if not obj_ids:
        return []
    placeholders = ",".join("?" * len(obj_ids))
    return conn.execute(
        f"SELECT DISTINCT ts, record_id, obj_id FROM handovers "
        f"WHERE obj_id IN ({placeholders}) ORDER BY ts, record_id, obj_id",
        tuple(sorted(obj_ids)),
    ).fetchall()


def object_exists(conn: sqlite3.Connection, obj_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM objects WHERE obj_id = ?", (obj_id,)
    ).fetchone() is not None


def is_box(conn: sqlite3.Connection, obj_id: str) -> bool:
    row = conn.execute("SELECT kind FROM objects WHERE obj_id = ?", (obj_id,)).fetchone()
    return row is not None and row[0] == "box"


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def cleanup_temp(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS tmp_group")
    conn.execute("DROP TABLE IF EXISTS tmp_uf")
    conn.commit()


def remove_db(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass
