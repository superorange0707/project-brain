"""Small, local coordination for workspace-mutating operations."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, TypeVar

if TYPE_CHECKING:
    from .core import Settings

try:  # POSIX covers the supported macOS and Linux standalone builds.
    import fcntl
except ImportError:  # pragma: no cover - exercised by the Windows fallback
    fcntl = None  # type: ignore[assignment]

try:  # Keep the wheel usable on Windows without an additional dependency.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX hosts do not provide msvcrt
    msvcrt = None  # type: ignore[assignment]


class WorkspaceOperationBusy(RuntimeError):
    """Raised when a second process tries to mutate one Brain workspace."""


_LOCAL = threading.local()
T = TypeVar("T")


def _held() -> dict[str, tuple[Any, int]]:
    value = getattr(_LOCAL, "held", None)
    if value is None:
        value = {}
        _LOCAL.held = value
    return value


def _acquire(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:  # pragma: no cover - Windows-only fallback
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    raise RuntimeError("workspace operation locking is unavailable on this platform")


def _release(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows-only fallback
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def workspace_operation(settings: Settings) -> Iterator[None]:
    """Fail closed when another process is publishing state for this workspace.

    The lock is advisory and owner-local, matching the existing private state
    directory. Nested calls in one thread are re-entrant so a refresh can call
    the semantic publisher without releasing its workspace boundary.
    """
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    path = settings.state_dir / "operations.lock"
    key = str(path.resolve())
    held = _held()
    existing = held.get(key)
    if existing is not None:
        handle, depth = existing
        held[key] = (handle, depth + 1)
        try:
            yield
        finally:
            handle, depth = held[key]
            held[key] = (handle, depth - 1)
        return

    handle = path.open("a+b")
    try:
        try:
            _acquire(handle)
        except BlockingIOError as error:
            raise WorkspaceOperationBusy(
                "another Project Brain workspace operation is already running; wait for it to finish and retry"
            ) from error
        except OSError as error:
            if error.errno in {11, 13, 35}:  # EAGAIN/EACCES on POSIX and Windows.
                raise WorkspaceOperationBusy(
                    "another Project Brain workspace operation is already running; wait for it to finish and retry"
                ) from error
            raise
        held[key] = (handle, 1)
        try:
            yield
        finally:
            held.pop(key, None)
            _release(handle)
    finally:
        handle.close()


def workspace_exclusive(function: Callable[..., T]) -> Callable[..., T]:
    """Decorate a state-mutating entry point with the workspace operation lock."""
    @wraps(function)
    def wrapped(settings: Settings, *args: Any, **kwargs: Any) -> T:
        with workspace_operation(settings):
            return function(settings, *args, **kwargs)

    return wrapped
