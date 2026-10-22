"""Cooperative local process exclusion for backend/operations and offline maintenance."""

import os
import threading
from functools import wraps
from hashlib import sha256
from pathlib import Path

_guard = threading.RLock()
_leases = {}


class DataBusyError(Exception):
    pass


def _lock(file, release=False):
    file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(
            file.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1
        )
    else:
        import fcntl

        fcntl.flock(
            file.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
        )


class DataLease:
    def __init__(self, root, role="operation"):
        if role not in ("operation", "backend", "maintenance"):
            raise ValueError("Invalid data lease role")
        self.key = os.path.normcase(str(Path(root).resolve()))
        self.role = role

    def __enter__(self):
        with _guard:
            held = _leases.get(self.key)
            if held:
                if (
                    self.role == "maintenance"
                    or held["roles"].get("maintenance", 0)
                    or self.role == "backend"
                    and held["roles"].get("backend", 0)
                ):
                    raise DataBusyError(
                        "Data directory is in use; stop the backend first"
                    )
                held["roles"][self.role] = held["roles"].get(self.role, 0) + 1
                return self
            directory = Path(self.key).parent
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or directory.resolve() != directory.absolute():
                raise DataBusyError("Unsafe local lock directory")
            path = directory / (
                ".daa-data-" + sha256(self.key.encode()).hexdigest() + ".lock"
            )
            if path.is_symlink() or (
                path.exists() and (not path.is_file() or path.stat().st_nlink != 1)
            ):
                raise DataBusyError("Unsafe local lock file")
            file = path.open("a+b")
            try:
                if not file.seek(0, os.SEEK_END):
                    file.write(b"0")
                    file.flush()
                _lock(file)
            except OSError:
                file.close()
                raise DataBusyError(
                    "Data directory is in use; stop the backend first"
                ) from None
            _leases[self.key] = {"file": file, "roles": {self.role: 1}}
        return self

    def __exit__(self, *exc):
        with _guard:
            held = _leases[self.key]
            held["roles"][self.role] -= 1
            if not any(held["roles"].values()):
                try:
                    _lock(held["file"], release=True)
                finally:
                    held["file"].close()
                    del _leases[self.key]


def data_operation(function):
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        with DataLease(self.root):
            return await function(self, *args, **kwargs)

    return guarded
