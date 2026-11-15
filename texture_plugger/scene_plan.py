# SPDX-License-Identifier: GPL-3.0-or-later
"""Sizing for whole-scene texturing: memory budget, texture size and parts.

Pure Python (no ``bpy``) so it can be unit-tested with a plain interpreter.

A scene with hundreds of objects cannot have one 2K texture set per object:
Blender keeps every image it shows in memory (an 8-bit RGBA copy in RAM and
another on the GPU with mipmaps), and Painter slows to a crawl with hundreds
of texture sets.  So the add-on sizes the job to the computer:

* ``choose_resolution`` lowers the texture size until every set fits in the
  memory budget.
* ``split_parts`` cuts the scene into parts Painter can handle; *Edit Scene in
  Painter* opens the part holding the selected object.
* Placeholders are flat colours, so they are written small (``PLACEHOLDER_MAX``)
  whatever the final size: they cost almost no memory until Painter exports.
"""

from __future__ import annotations

import math
import os
import sys

PLACEHOLDER_MAX = 64
MIN_RESOLUTION = 256
FALLBACK_MEMORY = 8 * 1024 ** 3
# RAM copy (RGBA8) + GPU copy with mipmaps (x 4/3)
BYTES_PER_PIXEL = 4 * (1.0 + 4.0 / 3.0)


def system_memory_bytes():
    """Physical memory of this computer in bytes (FALLBACK_MEMORY if unknown)."""
    if sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
        except Exception:
            pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return FALLBACK_MEMORY


def texture_bytes(size, maps):
    """Memory one texture set of ``maps`` square maps of ``size`` px takes when shown."""
    return int(size * size * BYTES_PER_PIXEL * max(0, maps))


def choose_resolution(n_sets, n_maps, wanted, budget_bytes, minimum=MIN_RESOLUTION):
    """Largest power of two <= ``wanted`` (and >= ``minimum``) whose sets fit the budget.

    Never goes below ``minimum``: at that size even ~1000 sets of 4 maps stay
    under ~2.5 GB.
    """
    wanted = max(1, int(wanted))
    res = 1 << (wanted.bit_length() - 1)              # round down to a power of two
    floor = min(res, max(1, int(minimum)))
    while res > floor and n_sets * texture_bytes(res, n_maps) > budget_bytes:
        res //= 2
    return max(res, floor)


def placeholder_size(resolution):
    return max(1, min(int(resolution), PLACEHOLDER_MAX))


def split_parts(keys, max_per_part):
    """Cut ``keys`` (already in a meaningful order) into near-equal consecutive parts."""
    keys = list(keys)
    if not keys:
        return []
    max_per_part = max(1, int(max_per_part))
    n = math.ceil(len(keys) / max_per_part)
    base, extra = divmod(len(keys), n)
    out, i = [], 0
    for p in range(n):
        size = base + (1 if p < extra else 0)
        out.append(keys[i:i + size])
        i += size
    return out


def pack_parts(weights, max_weight):
    """Group consecutive items into parts of total weight <= ``max_weight``.

    ``weights`` is a list of positive ints (texture sets per object).  Returns a
    list of index lists.  An item heavier than the limit gets a part of its own.
    The parts are then evened out so the last one is not a tiny leftover.
    """
    weights = [max(1, int(w)) for w in weights]
    if not weights:
        return []
    max_weight = max(1, int(max_weight))
    total = sum(weights)
    n = max(1, math.ceil(total / max_weight))
    # Even split by running total: item i goes to the part its start falls in.
    target = total / n
    even = [[] for _ in range(n)]
    cum = 0
    for i, w in enumerate(weights):
        even[min(n - 1, int(cum / target + 1e-9))].append(i)
        cum += w
    even = [p for p in even if p]
    if all(sum(weights[i] for i in p) <= max_weight for p in even):
        return even
    # Heavy items broke the even split: plain greedy (fewest consecutive parts).
    parts, cur, cur_w = [], [], 0
    for i, w in enumerate(weights):
        if cur and cur_w + w > max_weight:
            parts.append(cur)
            cur, cur_w = [], 0
        cur.append(i)
        cur_w += w
    if cur:
        parts.append(cur)
    return parts


def part_name(scene_name, index, count):
    """File stem for part ``index`` (0-based) of ``count``; the plain scene name for one part."""
    if count <= 1:
        return scene_name
    width = max(2, len(str(count)))
    return f"{scene_name}_part{index + 1:0{width}d}"


def describe_bytes(n):
    gb = n / 1024 ** 3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n / 1024 ** 2:.0f} MB"
