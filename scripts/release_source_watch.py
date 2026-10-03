"""Linux source-write monitoring, including transient untracked/ignored files."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import struct

from release_source_guard import DEPENDENCY_DIRS, OUTPUT_DIRS, OUTPUT_FILES, require

# Writes, permission changes, rename/create/delete and watched-directory loss.
# Reads/atime updates are intentionally not source-write events.
WRITE_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080
WRITE_MASK |= 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800
QUEUE_OVERFLOW = 0x00004000
WATCH_LOST = 0x00008000


def excluded(path: str) -> bool:
    return (path == ".git" or path.startswith(".git/") or path in OUTPUT_FILES
            or any(path == base or path.startswith(base + "/")
                   for base in DEPENDENCY_DIRS | OUTPUT_DIRS))


class SourceWatch:
    """No optional fallback: unavailable monitoring or lost events abort a build."""
    def __init__(self, root: Path):
        self.root = root
        self.fd = None
        self.directories = {}

    def __enter__(self):
        libc = ctypes.CDLL(None, use_errno=True)
        require(hasattr(libc, "inotify_init1") and hasattr(libc, "inotify_add_watch"),
                "required Linux source-write monitoring unavailable")
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        require(self.fd >= 0, "unable to initialize source-write monitoring")
        try:
            for directory, children, _ in os.walk(self.root, followlinks=False):
                relative = Path(directory).relative_to(self.root).as_posix()
                prefix = "" if relative == "." else relative + "/"
                # Include existing empty untracked directories as well; their
                # temporary files must not escape observation.
                children[:] = [name for name in children if not excluded(prefix + name)]
                wd = libc.inotify_add_watch(self.fd, os.fsencode(directory), WRITE_MASK)
                require(wd >= 0, "unable to watch every source directory")
                self.directories[wd] = prefix
            self.check()
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def check(self) -> None:
        require(self.fd is not None, "source-write monitoring not active")
        while True:
            try:
                events = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            require(bool(events), "source-write event stream lost")
            offset = 0
            while offset < len(events):
                require(len(events) - offset >= 16, "truncated source-write event")
                wd, mask, _, length = struct.unpack_from("iIII", events, offset)
                offset += 16
                require(length <= len(events) - offset, "truncated source-write event name")
                name = os.fsdecode(events[offset:offset + length].split(b"\0", 1)[0])
                offset += length
                require(not mask & (QUEUE_OVERFLOW | WATCH_LOST),
                        "source-write monitoring overflow/lost watch")
                require(wd in self.directories, "unknown source-write watch")
                path = self.directories[wd] + name
                require(excluded(path), "source filesystem changed during build")

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None