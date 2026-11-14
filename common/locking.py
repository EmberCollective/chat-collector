from pathlib import Path
import sys
import time


class InstanceLock:
    def __init__(self, path, timeout=0):
        self.path = Path(str(path) + '.lock')
        self.timeout = timeout

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.stream.tell() == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stream.close()
                    raise RuntimeError('Another collector is using this state file') from None
                time.sleep(min(.1, remaining))
        return self

    def __exit__(self, *args):
        self.stream.close()
