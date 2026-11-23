"""离线追溯命令行入口。

用法示例：
  python3 -I -m expotrace.cli run \
      --catalog examples/catalog.json \
      --manifest examples/manifest.jsonl \
      --handovers examples/handovers.jsonl \
      --query examples/query.json --inventory-ts 1000 \
      --out ./out

  python3 -I -m expotrace.cli show --out ./out
  python3 -I -m expotrace.cli verify ./out            # 核验 CURRENT 指向的结果
  python3 -I -m expotrace.cli verify ./out/trace-000001
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:  # 同时支持「python -m expotrace.cli」与直接执行脚本路径
    from . import engine
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from expotrace import engine

from .errors import TraceError


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_run(args: argparse.Namespace) -> int:
    result = engine.run_trace(
        catalog_path=args.catalog,
        manifest_path=args.manifest,
        handover_path=args.handovers,
        query_path=args.query,
        inventory_ts=args.inventory_ts,
        out_dir=args.out,
        work_dir=args.work_dir,
        receiving_id=args.receiving_id,
    )
    _print_json(
        {
            "ok": True,
            "trace": result["trace"],
            "trace_dir": result["trace_dir"],
            "query": result["manifest"]["query"],
            "counts": result["manifest"]["counts"],
        }
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .verifier import resolve_trace_dir

    trace_dir = resolve_trace_dir(args.out)
    with open(os.path.join(trace_dir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    _print_json(
        {
            "ok": True,
            "trace": os.path.basename(trace_dir),
            "trace_dir": os.path.abspath(trace_dir),
            "manifest": manifest,
        }
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from .verifier import verify_trace

    _print_json(verify_trace(args.path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expotrace.cli",
        description="离线展品追溯：交接台账重放与去向凭据导出",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一次离线追溯")
    p_run.add_argument("--catalog", required=True, help="已验收的全目录报告（JSON）")
    p_run.add_argument("--manifest", required=True, help="盘点时刻物资清册（JSONL）")
    p_run.add_argument("--handovers", required=True, help="各扫码点交接记录（JSONL，可乱序）")
    p_run.add_argument("--query", required=True, help="查询参数（JSON：start/end/targets）")
    p_run.add_argument(
        "--inventory-ts", required=True, type=int, help="盘点时刻（整数秒）"
    )
    p_run.add_argument(
        "--receiving-id",
        default=engine.RECEIVING_LOCATION,
        help=f"公共收货区地点编号（默认 {engine.RECEIVING_LOCATION}）",
    )
    p_run.add_argument("--out", required=True, help="追溯结果输出目录（版本化存放）")
    p_run.add_argument("--work-dir", help="磁盘临时空间（默认在输出目录下自建自清）")
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="查看当前追溯结果（CURRENT 指向）")
    p_show.add_argument("--out", required=True, help="追溯结果输出目录")
    p_show.set_defaults(func=cmd_show)

    p_ver = sub.add_parser("verify", help="独立核验：段覆盖、路径凭据与台账一致性")
    p_ver.add_argument("path", help="追溯输出目录或某个 trace-NNNNNN 目录")
    p_ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TraceError as exc:
        json.dump(
            {"ok": False, "error": exc.code, "message": str(exc)},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
