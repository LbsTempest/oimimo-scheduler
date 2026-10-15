"""跨进程排他锁，确保一个 Playwright Profile 同时只被一个任务使用。"""

import os
from pathlib import Path


class ProfileLock:
    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(str(self.path), 'a+b')
        try:
            os.chmod(str(self.path), 0o600)
        except OSError:
            pass
        if os.name == 'nt':
            import msvcrt
            self._file.seek(0)
            if not self._file.read(1):
                self._file.write(b'0')
                self._file.flush()
            self._file.seek(0)
            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self._file.close()
                self._file = None
                return False
        else:
            import fcntl
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._file.close()
                self._file = None
                return False
        return True

    def release(self):
        if self._file is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError('mihuashi profile is already locked by another process')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
