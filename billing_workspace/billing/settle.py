"""离线出账核心：临时电表逐日电量结算（纯标准库、全程流式）。

输入
  * 一届已验收的全目录采集报告（只用来确认本届展位编号，不重新采集，
    也不触碰跨届身份名册）；
  * 表计日志（逐行 JSON：每行是某表某读数编号的一个修订或撤销，含非负
    整数批次号；修订可改抄表时间；行可乱序）；
  * 归属表（逐行 JSON：表号 + 展位编号 + 左闭右开整数秒区间）；
  * 出账批次上界、连续 UTC 自然日范围、财务给定的最大可结算间隔秒数。

输出（先写 <out>/.staging-*，全部成功后原子改名为 <out>/bill-NNNNNN/
并切换 CURRENT；任何输入错误都不会留下半份账单，也不覆盖上次成功结果）
  * summary.jsonl         逐日汇总：日期+展位（含未归属 null）→ 已知瓦时、缺测表秒
  * known_details.jsonl   已知明细：表/日期/归属/区间/左右读数与批次/占用秒/所分瓦时
  * missing_details.jsonl 缺测明细：表/日期/归属/区间（相邻同键缺测已合并）
  * manifest.json         参数、输入摘要、计数

内存约束：日志、单表历史、明细均可超内存。除「本届展位编号集合」与
「单个可结算区间内的 (日期,归属) 秒数聚合」（其上界受最大间隔秒数
限制）外，不整批或整只表载入内存；所有需要全局有序的中间流都走
billing.extmem 的磁盘外排。
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import heapq
import itertools
import json
import os
import re
import shutil
import tempfile
from typing import Any, Iterator

from . import extmem

SCHEMA_VERSION = 1
BILL_KIND = "expo-meter-billing"
DAY_SECONDS = 86_400
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# 异常类型：所有可预期输入错误，CLI 以退出码 2 呈现
# --------------------------------------------------------------------------- #
class BillingError(Exception):
    code = "billing_error"


class ReportRejected(BillingError):
    code = "report_rejected"


class BadParameter(BillingError):
    code = "bad_parameter"


class LogFormatError(BillingError):
    code = "log_format_error"


class LogConflict(BillingError):
    code = "log_conflict"


class AttributionFormatError(BillingError):
    code = "attribution_format_error"


class UnknownBooth(BillingError):
    code = "unknown_booth"


class AttributionOverlap(BillingError):
    code = "attribution_overlap"


class ReadingSameSecond(BillingError):
    code = "reading_same_second"


class ReadingRegression(BillingError):
    code = "reading_regression"


class VerifyError(BillingError):
    code = "verify_failed"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def date_of(ts: int) -> str:
    """整数 Unix 秒 → UTC 日期串（平台无关，支持 1970 之前）。"""
    return (_EPOCH + datetime.timedelta(seconds=ts)).date().isoformat()


def parse_date(text: Any) -> int:
    """'YYYY-MM-DD' → 当日 00:00:00 UTC 的整数秒。"""
    if not isinstance(text, str) or not _DATE_RE.match(text):
        raise BadParameter(f"日期必须形如 YYYY-MM-DD：{text!r}")
    try:
        parts = datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise BadParameter(f"无效日期：{text!r}") from None
    return calendar.timegm(parts.timetuple())


def _booth_sort(booth: Any) -> tuple:
    """归属排序键：未归属（None）排在实际展位之后。"""
    return (1, "") if booth is None else (0, booth)


def _day_split(start: int, end: int) -> Iterator[tuple[int, int, str]]:
    """把左闭右开区间按 UTC 日界切开，产出 (起, 止, 日期串)。"""
    while start < end:
        boundary = (start // DAY_SECONDS + 1) * DAY_SECONDS
        cut = min(end, boundary)
        yield start, cut, date_of(start)
        start = cut


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


# --------------------------------------------------------------------------- #
# 报告准入：只确认本届展位编号（与名册工作区同一套验收标准）
# --------------------------------------------------------------------------- #
def load_accepted_report(path: str) -> dict[str, Any]:
    """加载并校验一份已验收的全目录采集报告，返回展位编号集合等信息。"""
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
            f"{path}: 拒绝带根筛选的报告 root_filter={root!r}（出账只接受全目录）"
        )
    booths = report.get("booths")
    if not isinstance(booths, list) or not booths:
        raise ReportRejected(f"{path}: 完整报告必须含非空 booths 列表")
    seen: set[str] = set()
    for row in booths:
        if not isinstance(row, dict) or not _is_nonempty_str(row.get("id")):
            raise ReportRejected(f"{path}: 展位记录缺少字符串 id：{row!r}")
        if row["id"] in seen:
            raise ReportRejected(f"{path}: 报告内展位编号重复：{row['id']}")
        seen.add(row["id"])
    return {
        "booth_ids": seen,
        "catalog_version": report.get("catalog_version"),
        "booth_count": len(seen),
    }


# --------------------------------------------------------------------------- #
# 表计日志：逐行校验 + 规范化
# --------------------------------------------------------------------------- #
_LOG_COMMON_KEYS = {"meter", "reading", "batch"}
_LOG_UPSERT_KEYS = _LOG_COMMON_KEYS | {"time", "period", "wh", "revoke"}
_LOG_REVOKE_KEYS = _LOG_COMMON_KEYS | {"revoke"}


def _normalize_log_line(obj: Any, lineno: int) -> dict[str, Any]:
    where = f"表计日志第 {lineno} 行"
    if not isinstance(obj, dict):
        raise LogFormatError(f"{where}：每行必须是 JSON 对象")
    meter, reading, batch = obj.get("meter"), obj.get("reading"), obj.get("batch")
    if not _is_nonempty_str(meter):
        raise LogFormatError(f"{where}：meter 必须是非空字符串")
    if not _is_nonempty_str(reading):
        raise LogFormatError(f"{where}：reading 必须是非空字符串")
    if not _is_int(batch) or batch < 0:
        raise LogFormatError(f"{where}：batch 必须是非负整数")
    if obj.get("revoke"):
        extra = set(obj) - _LOG_REVOKE_KEYS
        if extra:
            raise LogFormatError(
                f"{where}：撤销记录不得携带读数字段或未知字段 {sorted(extra)}"
            )
        return {"meter": meter, "reading": reading, "batch": batch, "revoke": True}
    extra = set(obj) - _LOG_UPSERT_KEYS
    if extra:
        raise LogFormatError(f"{where}：未知字段 {sorted(extra)}")
    time, period, wh = obj.get("time"), obj.get("period"), obj.get("wh")
    if not _is_int(time):
        raise LogFormatError(f"{where}：time 必须是整数 Unix 秒")
    if not _is_nonempty_str(period):
        raise LogFormatError(f"{where}：period 必须是非空字符串")
    if not _is_int(wh) or wh < 0:
        raise LogFormatError(f"{where}：wh 必须是非负整数")
    return {
        "meter": meter,
        "reading": reading,
        "batch": batch,
        "time": time,
        "period": period,
        "wh": wh,
    }


def _normalize_log(log_path: str, batch_bound: int, out_path: str) -> dict[str, int]:
    """逐行校验日志；只把不超过批次上界的可见修订写入规范化流。"""
    total = 0
    visible = 0
    with open(log_path, "r", encoding="utf-8") as src, open(
        out_path, "w", encoding="utf-8"
    ) as dst:
        for lineno, line in enumerate(src, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LogFormatError(
                    f"表计日志第 {lineno} 行：不是合法 JSON（{exc}）"
                ) from None
            rec = _normalize_log_line(obj, lineno)
            if rec["batch"] <= batch_bound:
                dst.write(extmem.dump_record(rec))
                dst.write("\n")
                visible += 1
    return {"log_lines": total, "visible_revisions": visible}


def _log_sort_key(rec: dict[str, Any]):
    # canonical 收尾保证全序：输出与输入行序无关。
    return (
        rec["meter"],
        rec["reading"],
        rec["batch"],
        extmem.dump_record(rec),
    )


def _resolve_readings(sorted_log_path: str, out_path: str) -> int:
    """按 (表, 读数编号) 解析可见修订：同批次冲突整次拒绝，取最新批次。"""
    count = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for (meter, reading), group in extmem.group_jsonl(
            sorted_log_path, key=lambda r: (r["meter"], r["reading"])
        ):
            selected = None
            for batch, batch_group in itertools.groupby(group, key=lambda r: r["batch"]):
                canonical = None
                for rec in batch_group:
                    if canonical is None:
                        canonical = rec
                    elif rec != canonical:
                        raise LogConflict(
                            f"表 {meter} 读数 {reading} 批次 {batch} 存在内容冲突的修订"
                        )
                selected = canonical  # 批次升序，最后即最新可见修订
            if selected is not None and not selected.get("revoke"):
                out.write(extmem.dump_record(selected))
                out.write("\n")
                count += 1
    return count


# --------------------------------------------------------------------------- #
# 归属表：逐行校验 + 同表不重叠检查
# --------------------------------------------------------------------------- #
_ATTR_KEYS = {"meter", "booth", "start", "end"}


def _normalize_attr_line(obj: Any, lineno: int, booth_ids: set[str]) -> dict[str, Any]:
    where = f"归属表第 {lineno} 行"
    if not isinstance(obj, dict):
        raise AttributionFormatError(f"{where}：每行必须是 JSON 对象")
    extra = set(obj) - _ATTR_KEYS
    if extra:
        raise AttributionFormatError(f"{where}：未知字段 {sorted(extra)}")
    meter, booth = obj.get("meter"), obj.get("booth")
    start, end = obj.get("start"), obj.get("end")
    if not _is_nonempty_str(meter):
        raise AttributionFormatError(f"{where}：meter 必须是非空字符串")
    if not _is_nonempty_str(booth):
        raise AttributionFormatError(f"{where}：booth 必须是非空字符串")
    if booth not in booth_ids:
        raise UnknownBooth(f"{where}：展位 {booth!r} 不在本届已验收目录中")
    if not _is_int(start) or not _is_int(end):
        raise AttributionFormatError(f"{where}：start/end 必须是整数 Unix 秒")
    if start >= end:
        raise AttributionFormatError(
            f"{where}：无效区间 [{start}, {end})（必须 start < end）"
        )
    return {"meter": meter, "booth": booth, "start": start, "end": end}


def _normalize_attr(attr_path: str, booth_ids: set[str], out_path: str) -> int:
    count = 0
    with open(attr_path, "r", encoding="utf-8") as src, open(
        out_path, "w", encoding="utf-8"
    ) as dst:
        for lineno, line in enumerate(src, start=1):
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AttributionFormatError(
                    f"归属表第 {lineno} 行：不是合法 JSON（{exc}）"
                ) from None
            dst.write(extmem.dump_record(_normalize_attr_line(obj, lineno, booth_ids)))
            dst.write("\n")
    return count


def _attr_sort_key(rec: dict[str, Any]):
    return (
        rec["meter"],
        rec["start"],
        rec["end"],
        rec["booth"],
        extmem.dump_record(rec),
    )


def _check_attr_overlap(sorted_path: str, out_path: str) -> None:
    """同一只表的归属区间不得重叠（相邻相接允许）；空档即未归属。"""
    prev = None
    with open(out_path, "w", encoding="utf-8") as out:
        for rec in extmem.iter_jsonl(sorted_path):
            if (
                prev is not None
                and rec["meter"] == prev["meter"]
                and rec["start"] < prev["end"]
            ):
                raise AttributionOverlap(
                    f"表 {rec['meter']} 的归属区间重叠："
                    f"[{prev['start']}, {prev['end']}) 与 [{rec['start']}, {rec['end']})"
                )
            out.write(extmem.dump_record(rec))
            out.write("\n")
            prev = rec


# --------------------------------------------------------------------------- #
# 归属游标：单表归属区间有序且不重叠，随结算时间轴单向前进
# --------------------------------------------------------------------------- #
class _AttrCursor:
    def __init__(self, attr_iter):
        self._it = iter(attr_iter)
        self._cur = None
        self._exhausted = False

    def _advance(self) -> None:
        try:
            nxt = next(self._it)
            self._cur = (nxt["start"], nxt["end"], nxt["booth"])
        except StopIteration:
            self._cur = None
            self._exhausted = True

    def chunks(self, start: int, end: int) -> Iterator[tuple[int, int, Any]]:
        """产出覆盖 [start, end) 的 (起, 止, 展位|None) 片段，要求调用按时间递增。"""
        pos = start
        while pos < end:
            if self._cur is None and not self._exhausted:
                self._advance()
            cur = self._cur
            if cur is None:
                yield pos, end, None
                pos = end
            else:
                c_start, c_end, booth = cur
                if c_end <= pos:
                    self._cur = None
                    continue
                if c_start > pos:
                    cut = min(c_start, end)
                    yield pos, cut, None
                    pos = cut
                else:
                    cut = min(c_end, end)
                    yield pos, cut, booth
                    pos = cut
                    if cut == c_end:
                        self._cur = None


# --------------------------------------------------------------------------- #
# 整数瓦时分配：按秒数比例，下限 + 小数余量从大到小补一
# --------------------------------------------------------------------------- #
def _allocate(energy: int, span_seconds: int, combos: dict) -> dict:
    """combos: {(日期, 展位|None): 秒数}（合计必须等于 span_seconds）。

    返回 {(日期, 展位|None): 瓦时}。余量相同先给日期较早者；同日按展位
    编号字典序，未归属排在实际展位之后。
    """
    floors: dict = {}
    rems: dict = {}
    floor_sum = 0
    for key, seconds in combos.items():
        q = (energy * seconds) // span_seconds
        floors[key] = q
        rems[key] = energy * seconds - q * span_seconds
        floor_sum += q
    leftover = energy - floor_sum
    order = sorted(combos, key=lambda k: (-rems[k], k[0], _booth_sort(k[1])))
    for i in range(leftover):
        floors[order[i]] += 1
    return floors


# --------------------------------------------------------------------------- #
# 主结算流程
# --------------------------------------------------------------------------- #
class _Sinks:
    """三类输出流的临时文件写入器（明细可能大于内存，先落盘后外排）。"""

    def __init__(self, work_dir: str):
        self.known_path = os.path.join(work_dir, "known.emit.jsonl")
        self.missing_path = os.path.join(work_dir, "missing.emit.jsonl")
        self.contrib_path = os.path.join(work_dir, "contrib.emit.jsonl")
        self._known = open(self.known_path, "w", encoding="utf-8")
        self._missing = open(self.missing_path, "w", encoding="utf-8")
        self._contrib = open(self.contrib_path, "w", encoding="utf-8")
        self.known_rows = 0
        self.missing_rows = 0

    def _put(self, fh, rec: dict) -> None:
        fh.write(extmem.dump_record(rec))
        fh.write("\n")

    def emit_known(self, rec: dict) -> None:
        self._put(self._known, rec)
        self.known_rows += 1

    def emit_missing(self, rec: dict) -> None:
        self._put(self._missing, rec)
        self.missing_rows += 1

    def emit_contrib(self, rec: dict) -> None:
        self._put(self._contrib, rec)

    def close(self) -> None:
        for fh in (self._known, self._missing, self._contrib):
            fh.close()


class _Context:
    def __init__(self, date_from: str, date_to: str, max_gap_seconds: int):
        self.date_from = date_from
        self.date_to = date_to
        self.window_start = parse_date(date_from)
        self.window_end = parse_date(date_to) + DAY_SECONDS
        self.max_gap_seconds = max_gap_seconds
        self.settleable_intervals = 0

    def date_in_window(self, date_str: str) -> bool:
        # ISO 日期串的字典序即时间序
        return self.date_from <= date_str <= self.date_to


def _emit_known_interval(
    meter: str, left: dict, right: dict, ctx: _Context, cursor: _AttrCursor, sinks: _Sinks
) -> None:
    """一段可结算区间：完整区间上按 (日期,归属) 秒数比例分配整数瓦时，
    只发布落在出账窗口内的日期。"""
    t1, t2 = left["time"], right["time"]
    energy = right["wh"] - left["wh"]
    span = t2 - t1
    combos: dict = {}
    for c_start, c_end, booth in cursor.chunks(t1, t2):
        for d_start, d_end, date_str in _day_split(c_start, c_end):
            key = (date_str, booth)
            combos[key] = combos.get(key, 0) + (d_end - d_start)
    alloc = _allocate(energy, span, combos)
    for date_str, booth in sorted(combos, key=lambda k: (k[0], _booth_sort(k[1]))):
        if not ctx.date_in_window(date_str):
            continue
        sinks.emit_known(
            {
                "meter": meter,
                "date": date_str,
                "booth": booth,
                "interval_start": t1,
                "interval_end": t2,
                "reading_left": left["reading"],
                "reading_right": right["reading"],
                "batch_left": left["batch"],
                "batch_right": right["batch"],
                "seconds": combos[(date_str, booth)],
                "wh": alloc[(date_str, booth)],
            }
        )
        sinks.emit_contrib(
            {
                "date": date_str,
                "booth": booth,
                "kind": "known",
                "value": alloc[(date_str, booth)],
            }
        )
    ctx.settleable_intervals += 1


def _emit_missing_range(
    meter: str, start: int, end: int, ctx: _Context, cursor: _AttrCursor, sinks: _Sinks
) -> None:
    """一段窗口内的缺测时间：按日界与归属切开，同表同日同归属且相邻的
    缺测合并为一段。"""
    pending = None  # [date, booth, start, end]

    def flush():
        nonlocal pending
        if pending is not None:
            date_str, booth, p_start, p_end = pending
            sinks.emit_missing(
                {
                    "meter": meter,
                    "date": date_str,
                    "booth": booth,
                    "start": p_start,
                    "end": p_end,
                    "seconds": p_end - p_start,
                }
            )
            sinks.emit_contrib(
                {
                    "date": date_str,
                    "booth": booth,
                    "kind": "missing",
                    "value": p_end - p_start,
                }
            )
            pending = None

    for c_start, c_end, booth in cursor.chunks(start, end):
        for d_start, d_end, date_str in _day_split(c_start, c_end):
            if (
                pending is not None
                and pending[0] == date_str
                and pending[1] == booth
                and pending[3] == d_start
            ):
                pending[3] = d_end
            else:
                flush()
                pending = [date_str, booth, d_start, d_end]
    flush()


def _settle_meter_known(meter: str, readings, ctx: _Context,
                        cursor: _AttrCursor, sinks: _Sinks) -> None:
    """已知趟：读数按时间递增逐条流过（内存里只保留上一条），
    校验同秒/倒退，并对每段可结算区间按完整区间分配整数瓦时。
    已知区间严格按时间递增发出，归属游标单向前进。"""
    prev = None
    for rec in readings:
        if prev is not None:
            if rec["time"] == prev["time"]:
                raise ReadingSameSecond(
                    f"表 {meter} 有两条读数占用同一秒 {rec['time']}"
                    f"（{prev['reading']} 与 {rec['reading']}）"
                )
            if rec["period"] == prev["period"]:
                if rec["wh"] < prev["wh"]:
                    raise ReadingRegression(
                        f"表 {meter} 计数周期 {rec['period']!r} 内相邻读数倒退："
                        f"{prev['reading']}={prev['wh']} → {rec['reading']}={rec['wh']}"
                    )
                if rec["time"] - prev["time"] <= ctx.max_gap_seconds:
                    _emit_known_interval(meter, prev, rec, ctx, cursor, sinks)
        prev = rec


def _settle_meter_missing(meter: str, readings, ctx: _Context,
                          cursor: _AttrCursor, sinks: _Sinks) -> None:
    """缺测趟：重放同一只有序读数流，只跟踪窗口内的覆盖推进，
    窗口内未被可结算区间覆盖的时间全部计为缺测。缺测区间同样
    严格按时间递增发出（covered_until 单调），归属游标单向前进。

    与已知趟分离的原因：完全落在窗口之后的可结算区间也要参与分配
    （不发布），若与缺测共用一个归属游标，会把游标推过窗口末尾，
    使窗口内的缺测归属被错误掏空。
    """
    win_start, win_end = ctx.window_start, ctx.window_end
    covered_until = win_start
    prev = None
    for rec in readings:
        if prev is not None and rec["period"] == prev["period"] \
                and rec["time"] - prev["time"] <= ctx.max_gap_seconds:
            lo = max(prev["time"], win_start)
            hi = min(rec["time"], win_end)
            if lo < hi:
                # 只有与窗口真正相交的区间才参与覆盖推进；
                # 完全在窗口外的区间不得移动 covered_until。
                if lo > covered_until:
                    _emit_missing_range(meter, covered_until, lo, ctx, cursor, sinks)
                if hi > covered_until:
                    covered_until = hi
        prev = rec
    if covered_until < win_end:
        _emit_missing_range(meter, covered_until, win_end, ctx, cursor, sinks)


def _co_group_meters(readings_path: str, attr_path: str):
    """按表号归并两条有序流，逐表产出 (表号, 读数迭代器|None, 归属迭代器|None)。"""
    readings_it = extmem.group_jsonl(readings_path, key=lambda r: r["meter"])
    attrs_it = extmem.group_jsonl(attr_path, key=lambda r: r["meter"])

    def nxt(it):
        try:
            return next(it)
        except StopIteration:
            return None

    r_group = nxt(readings_it)
    a_group = nxt(attrs_it)
    while r_group is not None or a_group is not None:
        if a_group is None or (r_group is not None and r_group[0] < a_group[0]):
            yield r_group[0], r_group[1], None
            r_group = nxt(readings_it)
        elif r_group is None or a_group[0] < r_group[0]:
            yield a_group[0], None, a_group[1]
            a_group = nxt(attrs_it)
        else:
            yield r_group[0], r_group[1], a_group[1]
            r_group = nxt(readings_it)
            a_group = nxt(attrs_it)


def _readings_sort_key(rec: dict[str, Any]):
    return (rec["meter"], rec["time"], rec["reading"], extmem.dump_record(rec))


def _known_sort_key(rec: dict[str, Any]):
    return (
        rec["date"],
        _booth_sort(rec["booth"]),
        rec["meter"],
        rec["interval_start"],
        rec["interval_end"],
        rec["reading_left"],
        rec["reading_right"],
    )


def _missing_sort_key(rec: dict[str, Any]):
    return (
        rec["date"],
        _booth_sort(rec["booth"]),
        rec["meter"],
        rec["start"],
        rec["end"],
    )


def _contrib_sort_key(rec: dict[str, Any]):
    return (
        rec["date"],
        _booth_sort(rec["booth"]),
        rec["kind"],
        extmem.dump_record(rec),
    )


def _build_summary(contrib_sorted_path: str, out_path: str) -> int:
    """把 (日期, 归属) 粒度的贡献流归并成逐日汇总行。"""
    rows = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for (date_str, _), group in extmem.group_jsonl(
            contrib_sorted_path, key=lambda r: (r["date"], _booth_sort(r["booth"]))
        ):
            known_wh = 0
            missing_seconds = 0
            booth = None
            for rec in group:
                booth = rec["booth"]
                if rec["kind"] == "known":
                    known_wh += rec["value"]
                else:
                    missing_seconds += rec["value"]
            out.write(
                extmem.dump_record(
                    {
                        "date": date_str,
                        "booth": booth,
                        "known_wh": known_wh,
                        "missing_meter_seconds": missing_seconds,
                    }
                )
            )
            out.write("\n")
            rows += 1
    return rows


# --------------------------------------------------------------------------- #
# 出账入口
# --------------------------------------------------------------------------- #
def _validate_parameters(
    batch_bound: Any, date_from: Any, date_to: Any, max_gap_seconds: Any
) -> None:
    if not _is_int(batch_bound) or batch_bound < 0:
        raise BadParameter(f"出账批次上界必须是非负整数：{batch_bound!r}")
    if not _is_int(max_gap_seconds) or max_gap_seconds <= 0:
        raise BadParameter(f"最大可结算间隔必须是正整数秒：{max_gap_seconds!r}")
    if parse_date(date_from) > parse_date(date_to):
        raise BadParameter(f"日期范围为空：{date_from!r} 晚于 {date_to!r}")


def _next_bill_name(out_dir: str) -> str:
    seq = 0
    for name in os.listdir(out_dir):
        match = re.fullmatch(r"bill-(\d{6})", name)
        if match:
            seq = max(seq, int(match.group(1)))
    return f"bill-{seq + 1:06d}"


def _clean_stale(out_dir: str) -> None:
    """清理上次崩溃留下的暂存目录（只认我们自己的前缀）。"""
    for name in os.listdir(out_dir):
        if name.startswith((".staging-", ".work-")):
            shutil.rmtree(os.path.join(out_dir, name), ignore_errors=True)


def run_billing(
    report_path: str,
    log_path: str,
    attr_path: str,
    batch_bound: int,
    date_from: str,
    date_to: str,
    max_gap_seconds: int,
    out_dir: str,
    work_dir: str | None = None,
    run_size: int = extmem.DEFAULT_RUN_SIZE,
) -> dict[str, Any]:
    """执行一次离线出账，返回交付信息。

    全部中间结果先落在工作目录，最终产物先写进暂存目录；只有全流程
    成功才把暂存目录原子改名为 bill-NNNNNN 并切换 CURRENT。任何输入
    错误都抛出 BillingError，且不影响上次成功结果。
    """
    _validate_parameters(batch_bound, date_from, date_to, max_gap_seconds)
    report = load_accepted_report(report_path)
    for label, path in (("表计日志", log_path), ("归属表", attr_path)):
        if not os.path.isfile(path):
            raise BadParameter(f"{label}不存在：{path}")

    os.makedirs(out_dir, exist_ok=True)
    _clean_stale(out_dir)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=out_dir)
    work = None
    try:
        # 工作目录总是本次运行自建的子目录，结束时整体删除；
        # --work-dir 只指定它落在哪块磁盘上。
        if work_dir is None:
            work = tempfile.mkdtemp(prefix=".work-", dir=out_dir)
        else:
            os.makedirs(work_dir, exist_ok=True)
            work = tempfile.mkdtemp(prefix=".billing-work-", dir=work_dir)
        input_meta = {
            "report": {"path": os.path.abspath(report_path), "sha256": _sha256_of(report_path)},
            "meter_log": {"path": os.path.abspath(log_path), "sha256": _sha256_of(log_path)},
            "attribution": {"path": os.path.abspath(attr_path), "sha256": _sha256_of(attr_path)},
        }

        # 1) 表计日志：规范化 → 外排 → 版本解析（冲突整次拒绝）
        counts = _normalize_log(log_path, batch_bound, os.path.join(work, "log.norm.jsonl"))
        extmem.sort_jsonl(
            os.path.join(work, "log.norm.jsonl"),
            os.path.join(work, "log.sorted.jsonl"),
            key=_log_sort_key,
            tmpdir=os.path.join(work, "sort-log"),
            run_size=run_size,
        )
        counts["selected_readings"] = _resolve_readings(
            os.path.join(work, "log.sorted.jsonl"),
            os.path.join(work, "readings.norm.jsonl"),
        )
        extmem.sort_jsonl(
            os.path.join(work, "readings.norm.jsonl"),
            os.path.join(work, "readings.sorted.jsonl"),
            key=_readings_sort_key,
            tmpdir=os.path.join(work, "sort-readings"),
            run_size=run_size,
        )

        # 2) 归属表：规范化 → 外排 → 同表重叠检查
        counts["attribution_lines"] = _normalize_attr(
            attr_path, report["booth_ids"], os.path.join(work, "attr.norm.jsonl")
        )
        extmem.sort_jsonl(
            os.path.join(work, "attr.norm.jsonl"),
            os.path.join(work, "attr.sorted.jsonl"),
            key=_attr_sort_key,
            tmpdir=os.path.join(work, "sort-attr"),
            run_size=run_size,
        )
        _check_attr_overlap(
            os.path.join(work, "attr.sorted.jsonl"),
            os.path.join(work, "attr.clean.jsonl"),
        )

        # 3) 主结算：已知趟与缺测趟各自逐表流式（两趟各自重开有序流，
        #    归属游标互不干扰；任何时刻内存里只有上一条读数/当前归属段）
        ctx = _Context(date_from, date_to, max_gap_seconds)
        sinks = _Sinks(work)
        meters = 0
        try:
            for meter, readings, attrs in _co_group_meters(
                os.path.join(work, "readings.sorted.jsonl"),
                os.path.join(work, "attr.clean.jsonl"),
            ):
                meters += 1
                _settle_meter_known(
                    meter, readings or iter(()), ctx,
                    _AttrCursor(attrs or iter(())), sinks,
                )
            for meter, readings, attrs in _co_group_meters(
                os.path.join(work, "readings.sorted.jsonl"),
                os.path.join(work, "attr.clean.jsonl"),
            ):
                _settle_meter_missing(
                    meter, readings or iter(()), ctx,
                    _AttrCursor(attrs or iter(())), sinks,
                )
        finally:
            sinks.close()
        counts["meters"] = meters
        counts["settleable_intervals"] = ctx.settleable_intervals
        counts["known_rows"] = sinks.known_rows
        counts["missing_rows"] = sinks.missing_rows

        # 4) 明细外排为交付顺序；汇总贡献外排后归并成逐日汇总
        extmem.sort_jsonl(
            sinks.known_path,
            os.path.join(staging, "known_details.jsonl"),
            key=_known_sort_key,
            tmpdir=os.path.join(work, "sort-known"),
            run_size=run_size,
        )
        extmem.sort_jsonl(
            sinks.missing_path,
            os.path.join(staging, "missing_details.jsonl"),
            key=_missing_sort_key,
            tmpdir=os.path.join(work, "sort-missing"),
            run_size=run_size,
        )
        extmem.sort_jsonl(
            sinks.contrib_path,
            os.path.join(work, "contrib.sorted.jsonl"),
            key=_contrib_sort_key,
            tmpdir=os.path.join(work, "sort-contrib"),
            run_size=run_size,
        )
        counts["summary_rows"] = _build_summary(
            os.path.join(work, "contrib.sorted.jsonl"),
            os.path.join(staging, "summary.jsonl"),
        )

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": BILL_KIND,
            "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "parameters": {
                "batch_bound": batch_bound,
                "date_from": date_from,
                "date_to": date_to,
                "max_gap_seconds": max_gap_seconds,
            },
            "catalog_version": report["catalog_version"],
            "booth_count": report["booth_count"],
            "inputs": input_meta,
            "counts": counts,
            "outputs": {
                "summary": "summary.jsonl",
                "known_details": "known_details.jsonl",
                "missing_details": "missing_details.jsonl",
            },
        }
        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")

        # 5) 原子交付：暂存目录改名 + CURRENT 切换；失败则上面任何一步
        #    抛出都会走 except 清理，上次成功结果原样保留。
        bill_name = _next_bill_name(out_dir)
        bill_dir = os.path.join(out_dir, bill_name)
        os.replace(staging, bill_dir)
        staging = None
        _atomic_write_text(os.path.join(out_dir, "CURRENT"), bill_name + "\n")
        return {
            "bill": bill_name,
            "bill_dir": os.path.abspath(bill_dir),
            "manifest": manifest,
        }
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 独立核验：明细逐项对回汇总（全程流式，不把明细载入内存）
# --------------------------------------------------------------------------- #
def _resolve_bill_dir(path: str) -> str:
    if os.path.isfile(os.path.join(path, "manifest.json")):
        return path
    current_file = os.path.join(path, "CURRENT")
    if os.path.isfile(current_file):
        with open(current_file, "r", encoding="utf-8") as fh:
            name = fh.read().strip()
        bill_dir = os.path.join(path, name)
        if os.path.isfile(os.path.join(bill_dir, "manifest.json")):
            return bill_dir
    raise VerifyError(f"找不到已出账结果：{path}")


def _check_sorted_unique(rows, sort_key, what: str) -> None:
    prev = None
    for rec in rows:
        key = sort_key(rec)
        if prev is not None and key <= prev:
            raise VerifyError(f"{what} 未按规范顺序或键重复：{rec!r}")
        prev = key


def _summary_key(rec: dict):
    return (rec["date"], _booth_sort(rec["booth"]))


def _aggregate_by_key(path: str, value_of, validate):
    """把有序的明细流按 (日期, 归属) 聚合成 ((date, booth), 合计) 有序流。"""
    for (date_str, _), group in extmem.group_jsonl(path, key=_summary_key):
        total = 0
        booth = None
        for rec in group:
            validate(rec)
            booth = rec["booth"]
            total += value_of(rec)
        yield {"date": date_str, "booth": booth}, total


def verify_bill(path: str) -> dict[str, Any]:
    """独立核验一份已出账结果：排序、键唯一、日期在窗口内、明细逐项对回汇总。"""
    bill_dir = _resolve_bill_dir(path)
    with open(os.path.join(bill_dir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    params = manifest["parameters"]
    date_from, date_to = params["date_from"], params["date_to"]
    summary_path = os.path.join(bill_dir, "summary.jsonl")
    known_path = os.path.join(bill_dir, "known_details.jsonl")
    missing_path = os.path.join(bill_dir, "missing_details.jsonl")

    def in_window(date_str: Any) -> bool:
        return isinstance(date_str, str) and date_from <= date_str <= date_to

    _check_sorted_unique(extmem.iter_jsonl(summary_path), _summary_key, "汇总")
    _check_sorted_unique(extmem.iter_jsonl(known_path), _known_sort_key, "已知明细")
    _check_sorted_unique(extmem.iter_jsonl(missing_path), _missing_sort_key, "缺测明细")

    def check_summary(rec: dict) -> None:
        if not in_window(rec.get("date")):
            raise VerifyError(f"汇总日期超出出账窗口：{rec!r}")
        if not _is_int(rec.get("known_wh")) or rec["known_wh"] < 0:
            raise VerifyError(f"汇总已知瓦时非法：{rec!r}")
        if not _is_int(rec.get("missing_meter_seconds")) or rec["missing_meter_seconds"] < 0:
            raise VerifyError(f"汇总缺测表秒非法：{rec!r}")

    def check_known(rec: dict) -> None:
        if not in_window(rec.get("date")):
            raise VerifyError(f"已知明细日期超出出账窗口：{rec!r}")
        if not _is_int(rec.get("interval_end")) or not _is_int(rec.get("interval_start")) \
                or rec["interval_end"] <= rec["interval_start"]:
            raise VerifyError(f"已知明细区间非法：{rec!r}")
        if not _is_int(rec.get("seconds")) or rec["seconds"] <= 0:
            raise VerifyError(f"已知明细占用秒非法：{rec!r}")
        if not _is_int(rec.get("wh")) or rec["wh"] < 0:
            raise VerifyError(f"已知明细瓦时非法：{rec!r}")

    last_by_meter: dict = {}

    def check_missing(rec: dict) -> None:
        if not in_window(rec.get("date")):
            raise VerifyError(f"缺测明细日期超出出账窗口：{rec!r}")
        if not _is_int(rec.get("seconds")) or not _is_int(rec.get("start")) \
                or not _is_int(rec.get("end")) \
                or rec["seconds"] != rec["end"] - rec["start"] or rec["seconds"] <= 0:
            raise VerifyError(f"缺测明细区间非法：{rec!r}")
        mkey = (rec["meter"], rec["date"], rec["booth"])
        prev_end = last_by_meter.get(mkey)
        if prev_end is not None and rec["start"] <= prev_end:
            raise VerifyError(f"缺测明细存在可合并/重叠段：{rec!r}")
        last_by_meter[mkey] = rec["end"]

    # 三条流都按 (日期, 归属) 有序，单趟归并比对：
    # 每个汇总键必须恰好由明细合计出来；没有明细的键不得出现。
    known_agg = _aggregate_by_key(known_path, lambda r: r["wh"], check_known)
    missing_agg = _aggregate_by_key(missing_path, lambda r: r["seconds"], check_missing)
    merged = heapq.merge(
        (("summary", rec, 0) for rec in extmem.iter_jsonl(summary_path)),
        (("known", rec, total) for rec, total in known_agg),
        (("missing", rec, total) for rec, total in missing_agg),
        key=lambda item: _summary_key(item[1]),
    )
    counts = {"summary_rows": 0, "known_rows": 0, "missing_rows": 0}
    for key, group in itertools.groupby(merged, key=lambda item: _summary_key(item[1])):
        summary = None
        known_wh = 0
        missing_seconds = 0
        has_detail = False
        for kind, rec, total in group:
            if kind == "summary":
                if summary is not None:
                    raise VerifyError(f"汇总键重复：{rec!r}")
                check_summary(rec)
                summary = rec
            elif kind == "known":
                known_wh += total
                has_detail = True
            else:
                missing_seconds += total
                has_detail = True
        if summary is None:
            raise VerifyError(f"明细键 {key} 缺少对应汇总行")
        if not has_detail:
            raise VerifyError(f"汇总键 {key} 没有任何明细支撑")
        if summary["known_wh"] != known_wh \
                or summary["missing_meter_seconds"] != missing_seconds:
            raise VerifyError(
                f"汇总与明细不符：{key} 汇总=({summary['known_wh']}, "
                f"{summary['missing_meter_seconds']}) 明细合计=({known_wh}, {missing_seconds})"
            )
        counts["summary_rows"] += 1

    counts["known_rows"] = sum(1 for _ in extmem.iter_jsonl(known_path))
    counts["missing_rows"] = sum(1 for _ in extmem.iter_jsonl(missing_path))
    manifest_counts = manifest.get("counts", {})
    for key, value in counts.items():
        if manifest_counts.get(key) != value:
            raise VerifyError(
                f"manifest 计数 {key}={manifest_counts.get(key)} 与实际 {value} 不符"
            )
    return {
        "ok": True,
        "bill_dir": os.path.abspath(bill_dir),
        "bill": os.path.basename(bill_dir),
        "window": [date_from, date_to],
        "rows": counts,
    }
