"""磁盘后备的外部排序与 JSONL 流工具（纯标准库）。

清册、交接日志、追溯结果都可能大于内存。需要全局有序的中间流统一走
「分块内排 + 多路归并」外排：任意时刻内存里只持有一个分块，归并时
每路各持一条记录。临时分块写在调用方给定的工作目录，归并完成即删。
"""
from __future__ import annotations

import heapq
import itertools
import json
import os
import tempfile
from typing import Any, Callable, Iterator

DEFAULT_RUN_SIZE = 100_000
MERGE_FANIN = 16


def dump_record(record: Any) -> str:
    """标准单行序列化（键排序、紧凑分隔），保证字节级确定性。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def iter_jsonl(path: str) -> Iterator[Any]:
    """逐行产出 JSONL 记录，不整文件载入内存。"""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = strip = line.strip()
            if strip:
                yield json.loads(strip)


def write_jsonl(path: str, records) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(dump_record(rec))
            fh.write("\n")
            count += 1
    return count


def _write_run(records: list, key: Callable, tmpdir: str) -> str:
    records.sort(key=key)
    fd, path = tempfile.mkstemp(prefix="run-", suffix=".jsonl", dir=tmpdir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(dump_record(rec))
            fh.write("\n")
    return path


def _merge_runs(paths: list[str], out_path: str, key: Callable) -> None:
    streams = [iter_jsonl(p) for p in paths]
    with open(out_path, "w", encoding="utf-8") as out:
        for rec in heapq.merge(*streams, key=key):
            out.write(dump_record(rec))
            out.write("\n")


def sort_jsonl(
    src: str,
    dst: str,
    key: Callable,
    tmpdir: str,
    run_size: int = DEFAULT_RUN_SIZE,
    fanin: int = MERGE_FANIN,
) -> int:
    """把 src（JSONL）按 key 外部排序到 dst，返回记录数。

    key 必须是全序；输出字节与输入行序无关。分块数超过 fanin 时自动
    多轮归并，打开的句柄数始终不超过 fanin。
    """
    os.makedirs(tmpdir, exist_ok=True)
    runs: list[str] = []
    buf: list = []
    total = 0
    with open(src, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            buf.append(json.loads(line))
            total += 1
            if len(buf) >= run_size:
                runs.append(_write_run(buf, key, tmpdir))
                buf = []
    if buf:
        runs.append(_write_run(buf, key, tmpdir))
    if not runs:
        open(dst, "w", encoding="utf-8").close()
        return 0
    try:
        while len(runs) > fanin:
            nxt: list[str] = []
            for i in range(0, len(runs), fanin):
                group = runs[i:i + fanin]
                if len(group) == 1:
                    nxt.append(group[0])
                    continue
                fd, out_path = tempfile.mkstemp(prefix="merge-", suffix=".jsonl", dir=tmpdir)
                os.close(fd)
                _merge_runs(group, out_path, key)
                for p in group:
                    os.unlink(p)
                nxt.append(out_path)
            runs = nxt
        _merge_runs(runs, dst, key)
        for p in runs:
            os.unlink(p)
    except BaseException:
        for p in runs:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        raise
    return total


def group_jsonl(path: str, key: Callable):
    """对有序 JSONL 按键分组，逐组产出 (键, 记录迭代器)，与 groupby 同约束。"""
    return itertools.groupby(iter_jsonl(path), key)
