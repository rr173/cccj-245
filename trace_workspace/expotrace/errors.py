"""可预期输入错误的类型体系：CLI 统一以退出码 2 呈现。"""
from __future__ import annotations


class TraceError(Exception):
    code = "trace_error"


class BadParameter(TraceError):
    code = "bad_parameter"


class ReportRejected(TraceError):
    code = "report_rejected"


class ManifestError(TraceError):
    code = "manifest_error"


class HandoverError(TraceError):
    code = "handover_error"


class QueryError(TraceError):
    code = "query_error"


class VerifyError(TraceError):
    code = "verify_failed"
