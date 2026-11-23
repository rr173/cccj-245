"""跨届复展名册工作区（只读采集报告，离线维护身份更正）。

包内模块：
  workspace : 纯本地状态机——报告准入、身份组、依据台账、变更集与原子落盘
  cli       : 命令行入口（init / correct / changes / show / log / verify）
"""
from .workspace import (
    RosterError,
    ReportRejected,
    StaleCommit,
    CommitConflict,
    UnknownBooth,
    BadOperation,
    WorkspaceExists,
    INIT_COMMIT,
    booth_key,
    init_workspace,
    correct,
    load_state,
    state_path_for,
    changeset_path,
    verify_workspace,
)

__all__ = [
    "RosterError",
    "ReportRejected",
    "StaleCommit",
    "CommitConflict",
    "UnknownBooth",
    "BadOperation",
    "WorkspaceExists",
    "INIT_COMMIT",
    "booth_key",
    "init_workspace",
    "correct",
    "load_state",
    "state_path_for",
    "changeset_path",
    "verify_workspace",
]
