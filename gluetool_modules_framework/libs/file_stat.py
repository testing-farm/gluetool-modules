# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import time

from typing import Optional, Tuple

# How long a file has to stay untouched before `settled_file_stat` describes it. Well over the
# kernel's clock tick, which timestamps of files have the resolution of.
CACHE_SETTLE_TIME = 2

# Device, inode, size, modification time and change time of a file. Nobody can set the change
# time, `utime()` included, so a file whose `FileStat` still matches holds the same bytes. The
# exception is a write through a shared writable `mmap`, the kernel updates the timestamps on the
# first write to a clean page only, and the page stays dirty until writeback.
FileStat = Tuple[int, int, int, int, int]


def settled_file_stat(path: str, follow_symlinks: bool = True) -> Optional[FileStat]:
    """
    Describe `path`, or return ``None`` when it changed less than `CACHE_SETTLE_TIME` seconds
    ago. A write landing in the same clock tick as the previous one keeps the change time, so
    such a file could still change without changing what we return.
    """
    now = time.time_ns()
    stat_result = os.stat(path, follow_symlinks=follow_symlinks)

    if stat_result.st_ctime_ns > now - CACHE_SETTLE_TIME * 1000000000:
        return None

    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns
    )
