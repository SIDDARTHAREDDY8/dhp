"""DHP storage backend interface.

The durability substrate is pluggable. ``SQLiteStore`` (dhp.store.Store)
is the default: zero-dependency, crash-safe, single-writer. For
multi-writer / multi-host deployments, implement ``StoreBackend``
against Postgres (advisory locks + FOR UPDATE SKIP LOCKED give you the
same CAS semantics) or any store that can provide:

- atomic compare-and-set on (holder, fence)
- monotonic fence increments
- ordered, deduplicated checkpoint log

The protocol logic (runner, supervisor, transport) only talks to this
interface — never to SQL directly.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class StoreBackend(abc.ABC):
    """Durability substrate for DHP."""

    # -- handoffs --
    @abc.abstractmethod
    def create_handoff(self, hid: str, envelope: Dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def get_envelope(self, hid: str) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def save_envelope(self, hid: str, envelope: Dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def all_hids(self) -> List[str]: ...

    # -- checkpoints --
    @abc.abstractmethod
    def add_checkpoint(self, hid: str, state: Dict[str, Any]) -> int: ...

    @abc.abstractmethod
    def last_checkpoint_state(self, hid: str) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def checkpoint_count(self, hid: str) -> int: ...

    @abc.abstractmethod
    def get_checkpoints(self, hid: str) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def import_checkpoints(self, hid: str,
                           items: List[Dict[str, Any]]) -> int: ...

    # -- ownership / leases / fencing --
    @abc.abstractmethod
    def acquire_ownership(self, hid: str, holder: str,
                          lease_s: float) -> int: ...

    @abc.abstractmethod
    def get_ownership(self, hid: str) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def refresh_lease(self, hid: str, holder: str, fence: int) -> float: ...

    @abc.abstractmethod
    def release_ownership(self, hid: str, holder: str,
                          fence: int) -> None: ...

    @abc.abstractmethod
    def force_orphan(self, hid: str, orphan_holder: str) -> bool: ...

    @abc.abstractmethod
    def expired_leases(self,
                       now: Optional[float] = None) -> List[str]: ...

    # -- supervisor leadership --
    @abc.abstractmethod
    def acquire_leadership(self, holder: str, ttl_s: float) -> bool: ...

    @abc.abstractmethod
    def renew_leadership(self, holder: str, ttl_s: float) -> bool: ...

    @abc.abstractmethod
    def get_leadership(self) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def close(self) -> None: ...
