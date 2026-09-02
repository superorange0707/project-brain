"""Small native-platform helpers shared by subprocess and identity hot paths."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


_TEST_SOURCE_SET_PATH = re.compile(
    r"(^|/)(?:test|tests)(?:/|$)|"
    r"(^|/)src/(?:test|integrationtest|functionaltest|componenttest|contracttest|"
    r"performancetest|acceptancetest|smoketest|e2e|it|testfixtures)(?:/|$)",
    re.I,
)
_TEST_BASENAME = re.compile(
    r"(?:^|/)(?:[^/]*(?:Test|Tests|IT|Spec)|test_[^/]+|[^/]+_(?:test|tests|spec)|"
    r"[^/]+\.(?:test|spec))\.[^/]+$"
)


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    """Remove a managed tree, retrying read-only files produced by Windows snapshots."""
    def writable_retry(function: Any, name: str, error: tuple[Any, BaseException, Any]) -> None:
        try:
            os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
            function(name)
        except OSError:
            if not ignore_errors:
                raise error[1]

    shutil.rmtree(path, onerror=writable_retry)


def _managed_parent(root: Path, path: Path, *, create: bool) -> Path:
    """Validate a managed path without following writable child directories."""
    if root.is_symlink():
        raise ValueError("managed state root must not be a symbolic link")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("managed state path escapes its root") from error
    if not relative.parts:
        raise ValueError("managed state path is invalid")
    resolved_root = root.resolve()
    parent = root
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("managed state parent must not be a symbolic link")
        if parent.exists() and not parent.is_dir():
            raise ValueError("managed state parent is not a directory")
        if create:
            parent.mkdir(exist_ok=True)
    if parent.resolve() != resolved_root and not parent.resolve().is_relative_to(resolved_root):
        raise ValueError("managed state parent escapes its root")
    return parent


def read_direct_file_bytes(path: Path, *, max_bytes: int) -> tuple[bytes, bool]:
    """Read a bounded direct regular file without accepting a leaf substitution."""
    if max_bytes < 0:
        raise ValueError("file byte limit must not be negative")
    try:
        expected = path.lstat()
    except OSError as error:
        raise ValueError("file is unavailable or symbolic") from error
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError("file is unavailable or symbolic")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("file identity changed while opening")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("file identity changed while reading")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    return payload, len(payload) > max_bytes


def _managed_relative(root: Path, path: Path) -> tuple[Path, Path, Path]:
    # Preserve the concrete path flavour when tests exercise the Windows branch
    # on POSIX by patching os.name; asking Path() to select a new flavour there
    # would manufacture an unsupported WindowsPath before any branch can run.
    root_path = type(root)(os.path.abspath(root))
    path_value = type(path)(os.path.abspath(path))
    try:
        relative = path_value.relative_to(root_path)
    except ValueError as error:
        raise ValueError("managed state path escapes its root") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("managed state path is invalid")
    return root_path, path_value, relative


def _open_managed_parent(root: Path, path: Path, *, create: bool) -> tuple[int, list[int], str, os.stat_result]:
    """Open a managed parent through no-follow directory descriptors on POSIX."""
    root_path, _, relative = _managed_relative(root, path)
    if create:
        root_path.mkdir(parents=True, exist_ok=True)
    expected = root_path.lstat()
    if not stat.S_ISDIR(expected.st_mode):
        raise ValueError("managed state root must be a direct directory")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(root_path, directory_flags)
    descriptors = [root_descriptor]
    try:
        opened = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("managed state root identity changed while opening")
        parent_descriptor = root_descriptor
        for part in relative.parts[:-1]:
            try:
                descriptor = os.open(part, directory_flags, dir_fd=parent_descriptor)
            except FileNotFoundError:
                if not create:
                    raise ValueError("managed state parent is unavailable") from None
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_descriptor)
                except FileExistsError:
                    pass
                descriptor = os.open(part, directory_flags, dir_fd=parent_descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(descriptor)
                raise ValueError("managed state parent is not a direct directory")
            descriptors.append(descriptor)
            parent_descriptor = descriptor
        return parent_descriptor, descriptors, relative.parts[-1], opened
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _lock_windows_managed_directories(
    root: Path, parent: Path, root_expected: os.stat_result,
) -> tuple[list[Any], list[tuple[Path, tuple[int, int]]]]:
    """Hold Windows directory handles that deny rename/delete during publication."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, ImportError, OSError) as error:
        raise ValueError("Windows managed directory locking is unavailable") from error

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    root_path = type(root)(os.path.abspath(root))
    parent_path = type(parent)(os.path.abspath(parent))
    relative = parent_path.relative_to(root_path)
    directories = [root_path]
    current = root_path
    for part in relative.parts:
        current /= part
        directories.append(current)

    handles: list[Any] = []
    identities: list[tuple[Path, tuple[int, int]]] = []
    try:
        for index, directory in enumerate(directories):
            expected = root_expected if index == 0 else directory.lstat()
            if not stat.S_ISDIR(expected.st_mode):
                raise ValueError("managed state directory changed before publication")
            handle = create_file(
                str(directory),
                0x80,  # FILE_READ_ATTRIBUTES
                0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately no FILE_SHARE_DELETE
                None, 3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error(), "could not lock managed state directory")
            handles.append(handle)
            information = FileInformation()
            if not get_information(handle, ctypes.byref(information)):
                raise OSError(ctypes.get_last_error(), "could not identify managed state directory")
            if not information.attributes & 0x10 or information.attributes & 0x400:
                raise ValueError("managed state directory is unavailable or a reparse point")
            after = directory.lstat()
            identity = (expected.st_dev, expected.st_ino)
            if (
                not stat.S_ISDIR(after.st_mode)
                or (after.st_dev, after.st_ino) != identity
            ):
                raise ValueError("managed state directory identity changed while locking")
            identities.append((directory, identity))
        return handles, identities
    except Exception:
        for handle in reversed(handles):
            close_handle(handle)
        raise


def _close_windows_handles(handles: Iterable[Any]) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    for handle in reversed(list(handles)):
        close_handle(handle)


class _ManagedLockHandle:
    """Keep Windows directory rename guards alive for the lock lease."""

    def __init__(self, handle: Any, directory_handles: Iterable[Any]) -> None:
        self._handle = handle
        self._directory_handles = list(directory_handles)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __enter__(self) -> _ManagedLockHandle:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._handle.close()
        finally:
            if self._directory_handles:
                _close_windows_handles(self._directory_handles)
                self._directory_handles.clear()


def atomic_managed_bytes_write(root: Path, path: Path, payload: bytes) -> None:
    """Atomically replace one managed file without following a target symlink."""
    if sys.platform != "win32":
        parent_descriptor, descriptors, leaf, root_identity = _open_managed_parent(
            root, path, create=True,
        )
        temporary = f".{leaf}.{os.urandom(8).hex()}.writing"
        published = False
        try:
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("managed state write made no progress")
                    view = view[written:]
                temporary_identity = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary, leaf,
                src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
            )
            published = True
            current = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            root_after = _managed_relative(root, path)[0].lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (temporary_identity.st_dev, temporary_identity.st_ino)
                or not stat.S_ISDIR(root_after.st_mode)
                or (root_after.st_dev, root_after.st_ino)
                != (root_identity.st_dev, root_identity.st_ino)
            ):
                raise ValueError("managed state identity changed during publication")
        finally:
            if not published:
                try:
                    os.unlink(temporary, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return
    root_path, path_value, _ = _managed_relative(root, path)
    root_path.mkdir(parents=True, exist_ok=True)
    root_expected = root_path.lstat()
    if not stat.S_ISDIR(root_expected.st_mode):
        raise ValueError("managed state root must be a direct directory")
    parent = _managed_parent(root_path, path_value, create=True)
    handles, identities = _lock_windows_managed_directories(
        root_path, parent, root_expected,
    )
    if path_value.exists() and path_value.is_dir() and not path_value.is_symlink():
        _close_windows_handles(handles)
        raise ValueError("managed state path is a directory")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=parent, prefix=f".{path_value.name}.", suffix=".writing", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
        for directory, identity in identities:
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
                raise ValueError("managed state directory changed during publication")
        temporary.replace(path_value)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _close_windows_handles(handles)


def atomic_managed_text_write(root: Path, path: Path, content: str) -> None:
    atomic_managed_bytes_write(root, path, content.encode("utf-8"))


def read_managed_bytes(root: Path, path: Path, *, max_bytes: int) -> bytes:
    """Read one bounded managed file while rejecting symlink substitutions."""
    if max_bytes < 0:
        raise ValueError("managed state file byte limit must not be negative")
    if sys.platform != "win32":
        parent_descriptor, descriptors, leaf, root_identity = _open_managed_parent(
            root, path, create=False,
        )
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ValueError("managed state file is unavailable or symbolic")
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(descriptor)
            after = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            root_after = _managed_relative(root, path)[0].lstat()
            if (
                not stat.S_ISREG(after.st_mode)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISDIR(root_after.st_mode)
                or (root_after.st_dev, root_after.st_ino)
                != (root_identity.st_dev, root_identity.st_ino)
            ):
                raise ValueError("managed state file identity changed while reading")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ValueError("managed state file exceeds its byte limit")
        return payload
    root_path, path_value, _ = _managed_relative(root, path)
    try:
        root_expected = root_path.lstat()
    except OSError as error:
        raise ValueError("managed state root is unavailable") from error
    if not stat.S_ISDIR(root_expected.st_mode):
        raise ValueError("managed state root must be a direct directory")
    parent = _managed_parent(root_path, path_value, create=False)
    handles, identities = _lock_windows_managed_directories(
        root_path, parent, root_expected,
    )
    descriptor: int | None = None
    try:
        try:
            metadata = path_value.lstat()
        except OSError as error:
            raise ValueError("managed state file is unavailable or symbolic") from error
        if path_value.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed state file is unavailable or symbolic")
        descriptor = os.open(path_value, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("managed state file identity changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read(max_bytes + 1)
        after = path_value.lstat()
        if (
            path_value.is_symlink()
            or not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("managed state file identity changed while reading")
        for directory, identity in identities:
            directory_metadata = directory.lstat()
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or (directory_metadata.st_dev, directory_metadata.st_ino) != identity
            ):
                raise ValueError("managed state directory changed while reading")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _close_windows_handles(handles)
    if len(payload) > max_bytes:
        raise ValueError("managed state file exceeds its byte limit")
    return payload


def read_managed_text(root: Path, path: Path, *, max_bytes: int) -> str:
    return read_managed_bytes(root, path, max_bytes=max_bytes).decode("utf-8")


def _sqlite_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=rw"


def connect_managed_sqlite(root: Path, path: Path, *, timeout: float = 30) -> sqlite3.Connection:
    """Open SQLite state without allowing its path open to create outside the anchored root."""
    root_path, path_value, _ = _managed_relative(root, path)
    if sys.platform != "win32":
        parent_descriptor, descriptors, leaf, root_identity = _open_managed_parent(
            root_path, path_value, create=True,
        )
        connection: sqlite3.Connection | None = None
        try:
            try:
                existing = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ValueError("managed SQLite database must be a direct regular file")
            flags = (
                os.O_RDWR | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_descriptor)
            except FileExistsError:
                try:
                    descriptor = os.open(
                        leaf,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_descriptor,
                    )
                except OSError as error:
                    raise ValueError(
                        "managed SQLite database must be a direct regular file"
                    ) from error
            try:
                database_identity = os.fstat(descriptor)
                if not stat.S_ISREG(database_identity.st_mode):
                    raise ValueError("managed SQLite database must be a direct regular file")
            finally:
                os.close(descriptor)
            before = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            root_before = root_path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino)
                != (database_identity.st_dev, database_identity.st_ino)
                or not stat.S_ISDIR(root_before.st_mode)
                or (root_before.st_dev, root_before.st_ino)
                != (root_identity.st_dev, root_identity.st_ino)
            ):
                raise ValueError("managed SQLite database identity changed before opening")
            # mode=rw is deliberate: a substituted path can be observed and rejected,
            # but SQLite must never create a database at that substituted location.
            connection = sqlite3.connect(_sqlite_uri(path_value), uri=True, timeout=timeout)
            after = path_value.lstat()
            root_after = root_path.lstat()
            if (
                path_value.is_symlink()
                or not stat.S_ISREG(after.st_mode)
                or (after.st_dev, after.st_ino)
                != (database_identity.st_dev, database_identity.st_ino)
                or not stat.S_ISDIR(root_after.st_mode)
                or (root_after.st_dev, root_after.st_ino)
                != (root_identity.st_dev, root_identity.st_ino)
            ):
                raise ValueError("managed SQLite database identity changed while opening")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    root_path.mkdir(parents=True, exist_ok=True)
    root_expected = root_path.lstat()
    if not stat.S_ISDIR(root_expected.st_mode):
        raise ValueError("managed state root must be a direct directory")
    parent = _managed_parent(root_path, path_value, create=True)
    handles, identities = _lock_windows_managed_directories(root_path, parent, root_expected)
    connection = None
    try:
        if path_value.exists() or path_value.is_symlink():
            existing = path_value.lstat()
            if path_value.is_symlink() or not stat.S_ISREG(existing.st_mode):
                raise ValueError("managed SQLite database must be a direct regular file")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path_value, flags, 0o600)
        except FileExistsError:
            descriptor = os.open(path_value, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            database_identity = os.fstat(descriptor)
            if not stat.S_ISREG(database_identity.st_mode):
                raise ValueError("managed SQLite database must be a direct regular file")
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(_sqlite_uri(path_value), uri=True, timeout=timeout)
        after = path_value.lstat()
        if (
            path_value.is_symlink()
            or not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino)
            != (database_identity.st_dev, database_identity.st_ino)
        ):
            raise ValueError("managed SQLite database identity changed while opening")
        for directory, identity in identities:
            directory_metadata = directory.lstat()
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or (directory_metadata.st_dev, directory_metadata.st_ino) != identity
            ):
                raise ValueError("managed SQLite directory changed while opening")
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise
    finally:
        _close_windows_handles(handles)


def open_managed_lock(root: Path, path: Path) -> Any:
    """Open one direct regular lock file without following a leaf symlink."""
    root_path, path_value, _ = _managed_relative(root, path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if sys.platform != "win32":
        parent_descriptor, descriptors, leaf, root_identity = _open_managed_parent(
            root_path, path_value, create=True,
        )
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_descriptor)
            except FileNotFoundError:
                # Darwin can transiently report ENOENT when two threads perform
                # the first O_NOFOLLOW|O_CREAT open of the same anchored leaf.
                descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_descriptor)
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            root_after = root_path.lstat()
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or not stat.S_ISREG(descriptor_metadata.st_mode)
                or (path_metadata.st_dev, path_metadata.st_ino)
                != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                or not stat.S_ISDIR(root_after.st_mode)
                or (root_after.st_dev, root_after.st_ino)
                != (root_identity.st_dev, root_identity.st_ino)
            ):
                raise ValueError("managed lock identity changed while opening")
            handle = os.fdopen(descriptor, "r+b")
            descriptor = None
            return handle
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for directory_descriptor in reversed(descriptors):
                os.close(directory_descriptor)

    root_path.mkdir(parents=True, exist_ok=True)
    root_expected = root_path.lstat()
    if not stat.S_ISDIR(root_expected.st_mode):
        raise ValueError("managed state root must be a direct directory")
    parent = _managed_parent(root_path, path_value, create=True)
    directory_handles, identities = _lock_windows_managed_directories(
        root_path, parent, root_expected,
    )
    descriptor = None
    try:
        descriptor = os.open(path_value, flags, 0o600)
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = path_value.lstat()
        if (
            path_value.is_symlink()
            or not stat.S_ISREG(path_metadata.st_mode)
            or not stat.S_ISREG(descriptor_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        ):
            raise ValueError("managed lock identity changed while opening")
        for directory, identity in identities:
            directory_metadata = directory.lstat()
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or (directory_metadata.st_dev, directory_metadata.st_ino) != identity
            ):
                raise ValueError("managed lock directory changed while opening")
        handle = os.fdopen(descriptor, "r+b")
        descriptor = None
        return _ManagedLockHandle(handle, directory_handles)
    except Exception:
        _close_windows_handles(directory_handles)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def platform_id(system_name: str | None = None, machine_name: str | None = None) -> str:
    """Return the canonical OS/architecture identity used by release artifacts."""
    raw_system = (system_name or platform.system()).lower()
    raw_machine = (machine_name or platform.machine()).lower()
    systems = {"windows": "windows", "win32": "windows", "darwin": "darwin", "linux": "linux"}
    machines = {"amd64": "amd64", "x86_64": "amd64", "x64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
    return f"{systems.get(raw_system, raw_system)}-{machines.get(raw_machine, raw_machine)}"


def normalize_platform_id(value: str) -> str:
    system_name, separator, machine_name = value.lower().partition("-")
    return platform_id(system_name, machine_name) if separator else value.lower()


def logical_path(value: os.PathLike[str] | str) -> str:
    """Return a stable repository-relative path independent of host separators."""
    raw = os.fspath(value).replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return PurePosixPath(raw).as_posix()


def filesystem_component(value: str, *, max_prefix: int = 80) -> str:
    """Encode a logical identity as one collision-resistant managed path component.

    Common lower-case identifiers retain their historical on-disk spelling;
    legacy arbitrary identities receive a readable prefix plus a raw-value hash.
    """
    reserved = {"con", "prn", "aux", "nul", *(f"com{number}" for number in range(1, 10)), *(f"lpt{number}" for number in range(1, 10))}
    stem = value.split(".", 1)[0].casefold()
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value)
        and value not in {".", ".."}
        and value.rstrip(" .") == value
        and stem not in reserved
    ):
        return value
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")[:max_prefix] or "identity"
    if prefix.split(".", 1)[0].casefold() in reserved:
        prefix = "id-" + prefix
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def is_test_path(value: os.PathLike[str] | str) -> bool:
    """Classify conventional unit and non-unit test source sets segment-wise."""
    path = logical_path(value)
    return bool(_TEST_SOURCE_SET_PATH.search(path) or _TEST_BASENAME.search(path))


def executable_filename(name: str, *, windows: bool | None = None) -> str:
    is_windows = os.name == "nt" if windows is None else windows
    return name if not is_windows or name.lower().endswith(".exe") else f"{name}.exe"


def adjacent_executable(name: str, *, executable: str | None = None, windows: bool | None = None) -> Path | None:
    """Find a helper shipped beside a standalone executable."""
    candidate = Path(executable or sys.executable).resolve().parent / executable_filename(name, windows=windows)
    return candidate if candidate.is_file() else None


def trusted_path_executable(
    name: str,
    *,
    environment: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> Path | None:
    """Resolve only explicit absolute PATH entries, never the working directory."""
    values = dict(environment) if environment is not None else os.environ
    is_windows = os.name == "nt" if windows is None else windows
    suffixes = [""]
    if is_windows and not Path(name).suffix:
        suffixes = [value.lower() for value in values.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if value]
        if ".exe" not in suffixes:
            suffixes.insert(0, ".exe")
    try:
        current = Path.cwd().resolve()
    except OSError:
        current = None
    for raw_directory in os.get_exec_path(values):
        if not raw_directory:
            continue
        directory = Path(raw_directory).expanduser()
        if not directory.is_absolute():
            continue
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if current is not None and directory == current:
            continue
        for suffix in suffixes:
            candidate = directory / (name if not suffix or name.lower().endswith(suffix) else name + suffix)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def native_command(name: str) -> str:
    """Return an absolute trusted executable path on every platform."""
    executable = trusted_path_executable(name, windows=os.name == "nt")
    if executable is None:
        raise FileNotFoundError(f"required executable is unavailable on the trusted PATH: {name}")
    return str(executable)


def _windows_system_directory() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32_768)
        length = int(ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)))
        if 0 < length < len(buffer):
            directory = Path(buffer.value)
            return directory if directory.is_absolute() else None
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def windows_system_executable(name: str, *subdirectories: str) -> Path | None:
    """Resolve a Windows-owned command without CreateProcess/PATH cwd search."""
    directory = _windows_system_directory()
    if directory is None:
        return None
    candidate = directory.joinpath(*subdirectories, executable_filename(name, windows=True))
    return candidate if candidate.is_file() else None


def process_group_kwargs(*, windows: bool | None = None) -> dict[str, Any]:
    """Create an independently terminable native process group without a shell."""
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def _assign_windows_kill_job(process: subprocess.Popen[Any]) -> int | None:
    """Own the complete Windows process tree even after its leader exits."""
    if os.name != "nt":
        return None
    try:  # pragma: no cover - exercised by the Windows CI matrix
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ) or not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return None


def _terminate_windows_job(process: subprocess.Popen[Any]) -> bool:
    handle = getattr(process, "_brain_job_handle", None)
    if not isinstance(handle, int) or handle <= 0 or os.name != "nt":
        return False
    process._brain_job_handle = None  # type: ignore[attr-defined]
    try:  # pragma: no cover - exercised by the Windows CI matrix
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1)
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False
    return True


def _resume_windows_process(process: subprocess.Popen[Any]) -> bool:
    """Resume a just-created suspended Windows process in one bounded call."""
    if os.name != "nt":
        return False
    handle = getattr(process, "_handle", None)
    if handle is None:
        return False
    try:  # pragma: no cover - exercised by the Windows CI matrix
        import ctypes
        from ctypes import wintypes

        # subprocess closes the primary thread handle before returning from
        # Popen. NtResumeProcess resumes the suspended process by its retained
        # process handle without enumerating unrelated system threads.
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = wintypes.LONG
        return int(ntdll.NtResumeProcess(wintypes.HANDLE(handle))) == 0
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False


def terminate_process_tree(process: subprocess.Popen[Any], *, graceful_timeout: float = 1.0) -> None:
    """Stop a process and its children using only native platform facilities."""
    leader_exited = process.poll() is not None
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        if leader_exited:
            return
        try:
            process.terminate()
            process.wait(timeout=max(graceful_timeout, 1.0))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
        if _terminate_windows_job(process):
            try:
                process.wait(timeout=max(3.0, graceful_timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return
        try:
            taskkill = windows_system_executable("taskkill")
            if taskkill is None:
                raise OSError("Windows taskkill.exe is unavailable")
            subprocess.run(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        try:
            process.wait(timeout=max(3.0, graceful_timeout))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return

    try:
        os.killpg(pid, signal.SIGTERM if graceful_timeout > 0 else signal.SIGKILL)
        if not leader_exited:
            process.wait(timeout=max(graceful_timeout, 1.0))
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        if not leader_exited:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def start_managed_process(args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    """Start a process in a tree Project Brain can reap after its leader exits."""
    group_kwargs = process_group_kwargs()
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
        group_kwargs["creationflags"] = int(group_kwargs["creationflags"]) | 0x00000004
    process = subprocess.Popen(args, **kwargs, **group_kwargs)
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
        job_handle = _assign_windows_kill_job(process)
        if job_handle is None:
            terminate_process_tree(process, graceful_timeout=0)
            raise OSError("could not establish a bounded Windows process job")
        process._brain_job_handle = job_handle  # type: ignore[attr-defined]
        if not _resume_windows_process(process):
            terminate_process_tree(process, graceful_timeout=0)
            raise OSError("could not resume the bounded Windows process job")
    return process


def run_bounded_process(
    args: list[str],
    cwd: Path,
    *,
    max_stdout_bytes: int,
    max_stderr_bytes: int = 64 * 1024,
    timeout: float = 30.0,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
    binary_output: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Run a shell-free command with hard output, time, and process-tree bounds."""
    command = [native_command(args[0]), *args[1:]] if Path(args[0]).parent == Path(".") else args
    process = start_managed_process(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    exceeded = threading.Event()
    timed_out = threading.Event()
    input_failed = threading.Event()
    output = bytearray()
    errors = bytearray()

    def drain(stream: Any, destination: bytearray, limit: int) -> None:
        try:
            while True:
                remaining = limit - len(destination)
                block = stream.read(min(64 * 1024, max(1, remaining + 1)))
                if not block:
                    return
                destination.extend(block[:remaining])
                if len(block) > remaining:
                    exceeded.set()
                    return
        finally:
            stream.close()

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=drain, args=(process.stdout, output, max(1, max_stdout_bytes)), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, errors, max(1, max_stderr_bytes)), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + max(0.1, timeout)
    writer: threading.Thread | None = None
    if input_bytes is not None and process.stdin is not None:
        def write_input() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(input_bytes)
            except (BrokenPipeError, OSError):
                input_failed.set()
            finally:
                try:
                    assert process.stdin is not None
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=write_input, name="brain-process-input", daemon=True)
        writer.start()
    while True:
        if exceeded.wait(0.01):
            terminate_process_tree(process, graceful_timeout=0)
            break
        if time.monotonic() >= deadline:
            timed_out.set()
            terminate_process_tree(process, graceful_timeout=0)
            break
        leader_exited = process.poll() is not None
        readers_done = all(not reader.is_alive() for reader in readers)
        writer_done = writer is None or not writer.is_alive()
        if leader_exited and readers_done and writer_done:
            # A descendant may have detached from the inherited pipes. Reap
            # the saved group/job before returning even though its leader is gone.
            terminate_process_tree(process, graceful_timeout=0)
            break
    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=2)
    if writer is not None:
        writer.join(timeout=2)
    if (exceeded.is_set() or timed_out.is_set()) and returncode == 0:
        returncode = 1
    result = subprocess.CompletedProcess(
        command,
        returncode,
        bytes(output) if binary_output else output.decode("utf-8", errors="replace"),
        bytes(errors) if binary_output else errors.decode("utf-8", errors="replace"),
    )
    # CompletedProcess intentionally permits diagnostic attributes. Callers
    # need to distinguish a bounded omission from an ordinary command error.
    result.output_truncated = exceeded.is_set()  # type: ignore[attr-defined]
    result.timed_out = timed_out.is_set()  # type: ignore[attr-defined]
    result.stdout_bytes = len(output)  # type: ignore[attr-defined]
    result.stderr_bytes = len(errors)  # type: ignore[attr-defined]
    return result
