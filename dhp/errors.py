"""DHP exception hierarchy. Every failure mode has a type; callers
(MCP server, bridge, CLI) map these to clean errors, never tracebacks."""
from __future__ import annotations


class DHPError(Exception):
    """Base for all DHP errors."""


# ---- validation ----
class ValidationError(DHPError):
    """Bad input: malformed ID, oversize payload, schema violation."""


class UnknownHandoff(ValidationError):
    pass


class OversizePayload(ValidationError):
    pass


# ---- ownership / fencing ----
class OwnershipError(DHPError):
    """Base for lease/ownership failures."""


class OwnershipLost(OwnershipError):
    """Lost a CAS race: someone else owns this handoff now."""


class LeaseLost(OwnershipError):
    """Not the current lease holder (or lease expired)."""


class FencingError(OwnershipError):
    """Stale fencing token: this worker is not the current owner."""


# ---- lifecycle ----
class StateError(DHPError):
    """Operation not allowed in the handoff's current state."""


class BudgetExceeded(DHPError):
    pass


class TamperError(DHPError):
    """Content-address verification failed (tampered or corrupt)."""


# ---- approvals ----
class ApprovalRequired(DHPError):
    """Destructive/financial action needs approval first (fail-closed)."""


class ApprovalDenied(DHPError):
    """Approval was denied or timed out (fail-closed)."""


# ---- stall detection ----
class StallDetected(DHPError):
    """Worker is alive but making no progress (livelocked)."""


# ---- capabilities ----
class CapabilityDenied(DHPError):
    """Action outside the handoff's declared capability scope."""


# ---- transport ----
class TransportError(DHPError):
    pass
