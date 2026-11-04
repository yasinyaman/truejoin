"""Audit sinks."""

from trio.adapters.outbound.audit.log_sink import LoggingAuditSink

__all__ = ["LoggingAuditSink"]
