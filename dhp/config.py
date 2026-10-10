"""DHP configuration. Every operational knob via env vars (DHP_* prefix).

Production rule: no magic numbers in the hot path. All defaults match
the chaos-proven v0.4 values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class DHPConfig:
    # leases / supervision
    lease_interval_s: float = field(
        default_factory=lambda: _env_float("DHP_LEASE_INTERVAL_S", 2.0))
    lease_max_missed: int = field(
        default_factory=lambda: _env_int("DHP_LEASE_MAX_MISSED", 3))
    supervisor_scan_s: float = field(
        default_factory=lambda: _env_float("DHP_SUPERVISOR_SCAN_S", 1.0))
    leader_ttl_s: float = field(
        default_factory=lambda: _env_float("DHP_LEADER_TTL_S", 5.0))
    # safety bounds
    max_checkpoint_bytes: int = field(
        default_factory=lambda: _env_int("DHP_MAX_CHECKPOINT_BYTES",
                                         1_000_000))  # 1 MB
    max_envelope_bytes: int = field(
        default_factory=lambda: _env_int("DHP_MAX_ENVELOPE_BYTES",
                                         10_000_000))  # 10 MB
    max_result_bytes: int = field(
        default_factory=lambda: _env_int("DHP_MAX_RESULT_BYTES",
                                         10_000_000))  # 10 MB
    max_checkpoints_per_handoff: int = field(
        default_factory=lambda: _env_int("DHP_MAX_CHECKPOINTS", 1_000_000))
    # transport
    transport_max_line_bytes: int = field(
        default_factory=lambda: _env_int("DHP_TRANSPORT_MAX_LINE_BYTES",
                                         2_000_000))  # 2 MB
    # observability
    log_level: str = field(
        default_factory=lambda: os.environ.get("DHP_LOG_LEVEL", "INFO"))

    @property
    def lease_ttl_s(self) -> float:
        return self.lease_interval_s * self.lease_max_missed

    def validate(self) -> None:
        assert self.lease_interval_s > 0, "lease_interval_s must be > 0"
        assert self.lease_max_missed >= 2, "lease_max_missed must be >= 2"
        assert self.leader_ttl_s > self.supervisor_scan_s, \
            "leader_ttl_s must exceed supervisor_scan_s"
        for f in ("max_checkpoint_bytes", "max_envelope_bytes",
                  "max_result_bytes", "transport_max_line_bytes",
                  "max_checkpoints_per_handoff"):
            assert getattr(self, f) > 0, f"{f} must be > 0"


DEFAULT = DHPConfig()
DEFAULT.validate()
