"""Bounded, advisory Git reads for the currently selected launch target.

The App owns this cache. No worker owns a screen, launches a profile, or opens
a modal. A selection replaces queued work; two running reads may finish late.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from uxon.infra.worktrees import GitDiscovery

from .context import TuiContext

_WORKERS = 2
_CACHE_LIMIT = 32
DiscoveryKey = tuple[str, str, str]


@dataclass(frozen=True)
class DiscoveryState:
    status: Literal["pending", "ready", "nongit", "error"] = "pending"
    discovery: GitDiscovery | None = None
    error: str = ""


class WorktreeDiscovery:
    def __init__(self, changed: Callable[[], object]) -> None:
        self._changed = changed
        self._pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="uxon-git")
        self._lock = threading.RLock()
        self._closed = False
        self._active: dict[DiscoveryKey, Future[DiscoveryState]] = {}
        self._queued: OrderedDict[DiscoveryKey, Callable[[], GitDiscovery | None]] = OrderedDict()
        self._cache: OrderedDict[DiscoveryKey, DiscoveryState] = OrderedDict()

    def begin(self) -> None:
        """A new launch selection refreshes completed facts, retaining live reads."""
        with self._lock:
            self._cache.clear()
            self._queued.clear()

    @staticmethod
    def users(ctx: TuiContext) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                profile.launch_user or ctx.launch_user or ctx.current_user
                for pid, profile in ctx.launch_profiles.items()
                if pid in ctx.enabled_profiles
            )
        )

    @staticmethod
    def _key(ctx: TuiContext, path: str, user: str) -> DiscoveryKey:
        return (ctx.execution.backend_for_user(user).fingerprint, user, os.path.abspath(path))

    def _cached(self, key: DiscoveryKey) -> DiscoveryState | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        self._cache.move_to_end(key)
        return entry

    def prefetch(self, ctx: TuiContext, path: str | None) -> None:
        """Replace pending selection work, sharing active reads across screens."""
        with self._lock:
            if self._closed:
                return
            self._queued.clear()
            if path is not None:
                fetch = ctx.on_discover_worktrees
                for user in self.users(ctx):
                    key = self._key(ctx, path, user)
                    if key in self._active or self._cached(key) is not None:
                        continue
                    if len(self._queued) >= _CACHE_LIMIT:
                        break
                    self._queued[key] = lambda path=path, user=user: fetch(path, user)
            self._pump()

    def peek(self, ctx: TuiContext, path: str, user: str) -> DiscoveryState:
        with self._lock:
            return self._cached(self._key(ctx, path, user)) or DiscoveryState()

    def status(self, ctx: TuiContext, path: str, user: str | None = None) -> str:
        users = (user,) if user is not None else self.users(ctx)
        lines = []
        for target_user in users:
            state = self.peek(ctx, path, target_user)
            label = f"{target_user}: " if len(users) > 1 else ""
            if state.status == "error":
                detail = f"Git unavailable: {state.error}. Enter continues in this folder."
            elif state.status == "pending":
                detail = "Checking Git… Enter continues in this folder without waiting."
            elif state.status == "ready":
                detail = "Git workspaces available; current checkout is the default."
            else:
                detail = "Git not initialized; Enter continues in this folder."
            lines.append(label + detail)
        return "\n".join(lines)

    @staticmethod
    def _read(fetch: Callable[[], GitDiscovery | None]) -> DiscoveryState:
        try:
            discovery = fetch()
        except Exception as exc:
            return DiscoveryState("error", error=str(exc) or type(exc).__name__)
        return DiscoveryState("ready" if discovery is not None else "nongit", discovery)

    def _pump(self) -> None:
        # Only active reads are submitted to the executor: its internal queue
        # cannot accumulate one task for every transient cursor position.
        while not self._closed and self._queued and len(self._active) < _WORKERS:
            key, fetch = self._queued.popitem(last=False)
            future = self._pool.submit(self._read, fetch)
            self._active[key] = future
            future.add_done_callback(lambda done, key=key: self._completed(key, done))

    def _completed(self, key: DiscoveryKey, future: Future[DiscoveryState]) -> None:
        with self._lock:
            self._active.pop(key, None)
            if self._closed or future.cancelled():
                return
            self._cache[key] = future.result()
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_LIMIT:
                self._cache.popitem(last=False)
            self._pump()
        self._changed()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queued.clear()
            self._cache.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
