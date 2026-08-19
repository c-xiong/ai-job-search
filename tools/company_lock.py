"""Cross-runtime O_EXCL lock shared by Python and the Bun ATS registry writer."""

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_LOCAL = threading.RLock()
_DEPTH = 0


@contextmanager
def registry_lock(registry, timeout=30):
    global _DEPTH
    with _LOCAL:
        if _DEPTH:
            _DEPTH += 1
            try:
                yield
            finally:
                _DEPTH -= 1
            return
        lock = Path(registry).with_name(".companies.lock")
        deadline = time.monotonic() + timeout
        fd = None
        while fd is None:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("company registry is busy")
                time.sleep(0.025)
        _DEPTH = 1
        try:
            yield
        finally:
            _DEPTH = 0
            os.close(fd)
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
