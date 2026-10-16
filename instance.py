"""OS 文件锁防止同一数据目录重复运行后台任务；退出自动释放。"""

import os
from pathlib import Path

from .cache import BusyError


class InstanceLock:
    def __init__(self, path: Path):
        self.path, self.stream = path, None

    def acquire(self):
        if self.stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, 2) == 0:
                    stream.write(b"1")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise BusyError("该数据目录已有运行实例，请勿重复启动。") from error
        self.stream = stream

    def release(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
