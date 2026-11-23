#!/usr/bin/env python3
"""命令行包装器：在 -I 隔离模式下把项目根目录加入 sys.path 后转发给 CLI。

等价于：PYTHONPATH=<项目根> python3 -I -m expotrace.cli "$@"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from expotrace.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
