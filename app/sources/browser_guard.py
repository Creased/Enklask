"""Process hygiene shared by the Camoufox brokers.

A failed or hung Camoufox launch can leave its Playwright driver, Xvfb display
and half-started browser running. Relaunching on every poll then piles them up
until the host runs out of PIDs. These helpers record what a launch spawned so
it can be killed on reset, and space relaunches out after failures.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# Held for the whole launch so the processes that appear meanwhile belong to it.
LAUNCH_LOCK = threading.Lock()
LAUNCH_LOCK_TIMEOUT_SECONDS = 120.0


def _children_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read()
        except OSError:
            continue
        # comm may contain spaces or parentheses; ppid is the 2nd field after it.
        fields = stat[stat.rfind(b")") + 2 :].split()
        if len(fields) < 2:
            continue
        children.setdefault(int(fields[1]), []).append(int(entry))
    return children


def child_pids() -> set[int]:
    """Direct children of this process (empty where /proc is unavailable)."""
    return set(_children_map().get(os.getpid(), []))


def kill_tree(roots: Iterable[int]) -> int:
    """SIGKILL ``roots`` and all their descendants; return how many were signalled.

    Only roots that are still direct children of this process are followed, so a
    recycled PID that now belongs to an unrelated process is never touched.
    """
    if os.name != "posix":
        return 0
    children = _children_map()
    own = set(children.get(os.getpid(), []))
    stack = [pid for pid in roots if pid in own]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    killed = 0
    for pid in seen:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    if killed:
        logger.info("Killed %d leftover browser process(es)", killed)
    return killed


class LaunchBackoff:
    """Exponential pause between browser launches after consecutive failures."""

    def __init__(
        self,
        base_seconds: float = 60.0,
        max_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_seconds
        self._max = max_seconds
        self._clock = clock
        self._failures = 0
        self._until = 0.0

    def remaining(self) -> float:
        return max(0.0, self._until - self._clock())

    def failed(self) -> float:
        self._failures += 1
        delay = min(self._base * 2 ** (self._failures - 1), self._max)
        self._until = self._clock() + delay
        return delay

    def succeeded(self) -> None:
        self._failures = 0
        self._until = 0.0
