"""Small, local coordination for workspace-mutating operations."""

from __future__ import annotations

import os
import hashlib
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


class TicketOperationBusy(RuntimeError):
    """Raised when the same ticket is already being mutated elsewhere."""


_LOCAL = threading.local()
MODEL_LANE = threading.Lock()
T = TypeVar("T")


def _held() -> dict[str, tuple[Any, int]]:
    value = getattr(_LOCAL, "held", None)
    if value is None:
        value = {}
        _LOCAL.held = value
    return value


def _acquire(handle: Any, *, shared: bool = False) -> None:
    if fcntl is not None:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
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


@contextmanager
def workspace_retrieval(settings: Settings) -> Iterator[None]:
    """Take a shared workspace lease so retrievals coexist but mutations do not."""
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
            _acquire(handle, shared=True)
        except (BlockingIOError, OSError) as error:
            raise WorkspaceOperationBusy(
                "another Project Brain workspace mutation is running; wait for it to finish and retry"
            ) from error
        held[key] = (handle, 1)
        try:
            yield
        finally:
            held.pop(key, None)
            _release(handle)
    finally:
        handle.close()


@contextmanager
def ticket_operation(settings: Settings, ticket: str) -> Iterator[None]:
    """Serialize one session across UI/CLI processes without blocking other tickets."""
    root = settings.state_dir / "ticket-locks"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(ticket.encode("utf-8", errors="replace")).hexdigest()
    path = root / f"{digest}.lock"
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
        except (BlockingIOError, OSError) as error:
            raise TicketOperationBusy(
                "another Project Brain request for this ticket is already running; wait for it to finish and retry"
            ) from error
        held[key] = (handle, 1)
        try:
            yield
        finally:
            held.pop(key, None)
            _release(handle)
    finally:
        handle.close()


@contextmanager
def retrieval_capacity(settings: Settings) -> Iterator[None]:
    """Bound cross-process retrieval concurrency with a small set of slot locks."""
    root = settings.state_dir / "retrieval-slots"
    root.mkdir(parents=True, exist_ok=True)
    group_key = f"slots:{root.resolve()}"
    held = _held()
    existing = held.get(group_key)
    if existing is not None:
        handle, depth = existing
        held[group_key] = (handle, depth + 1)
        try:
            yield
        finally:
            handle, depth = held[group_key]
            held[group_key] = (handle, depth - 1)
        return

    selected = None
    for index in range(settings.max_concurrent_investigations):
        handle = (root / f"slot-{index + 1}.lock").open("a+b")
        try:
            _acquire(handle)
        except (BlockingIOError, OSError):
            handle.close()
            continue
        selected = handle
        break
    if selected is None:
        raise WorkspaceOperationBusy(
            "the maximum number of Project Brain ticket retrievals is already running; wait for one to finish and retry"
        )
    held[group_key] = (selected, 1)
    try:
        yield
    finally:
        held.pop(group_key, None)
        _release(selected)
        selected.close()


@contextmanager
def retrieval_session(settings: Settings, ticket: str) -> Iterator[None]:
    with workspace_retrieval(settings), ticket_operation(settings, ticket), retrieval_capacity(settings):
        yield


def workspace_exclusive(function: Callable[..., T]) -> Callable[..., T]:
    """Decorate a state-mutating entry point with the workspace operation lock."""
    @wraps(function)
    def wrapped(settings: Settings, *args: Any, **kwargs: Any) -> T:
        with workspace_operation(settings):
            return function(settings, *args, **kwargs)

    return wrapped


def ticket_retrieval_exclusive(function: Callable[..., T]) -> Callable[..., T]:
    """Decorate an entry point whose second positional argument is a ticket id."""
    @wraps(function)
    def wrapped(settings: Settings, ticket: str, *args: Any, **kwargs: Any) -> T:
        with retrieval_session(settings, ticket):
            return function(settings, ticket, *args, **kwargs)

    return wrapped


def ticket_snapshot_exclusive(function: Callable[..., T]) -> Callable[..., T]:
    """Serialize a ticket operation that reads the current workspace snapshot."""
    @wraps(function)
    def wrapped(settings: Settings, ticket: str, *args: Any, **kwargs: Any) -> T:
        with workspace_retrieval(settings), ticket_operation(settings, ticket):
            return function(settings, ticket, *args, **kwargs)

    return wrapped


def ticket_exclusive(function: Callable[..., T]) -> Callable[..., T]:
    """Serialize a session-only mutation without consuming a retrieval slot."""
    @wraps(function)
    def wrapped(settings: Settings, ticket: str, *args: Any, **kwargs: Any) -> T:
        with ticket_operation(settings, ticket):
            return function(settings, ticket, *args, **kwargs)

    return wrapped
