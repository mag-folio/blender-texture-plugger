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


GPU_BYTES_PER_PIXEL = 4 * 4.0 / 3.0       # Blender viewport texture: RGBA8 + mipmaps
BLENDER_GPU_SHARE = 0.30                   # of the graphics card, for Blender's textures
PAINTER_GPU_SHARE = 0.45                   # of the graphics card, for one Painter project
PAINTER_RAM_SHARE = 0.40                   # of the computer's memory, for one Painter project
# Measured in Painter 12.2 (964 texture sets, 512 px, one fill layer each):
# ~5.8 MB of RAM and ~5.6 MB of graphics memory per set per layer = ~22 bytes/px.
PAINTER_BYTES_PER_PIXEL_LAYER = 22.0
PAINTER_LAYERS = 1.5                       # headroom: on average a set and a half of layers
PAINTER_BASE_RAM = int(3.5 * 1024 ** 3)    # Painter itself with a big scene loaded
PAINTER_BASE_GPU = int(1.0 * 1024 ** 3)


def gpu_memory_bytes():
    """Dedicated memory of the biggest graphics card in bytes, or None if unknown.

    Read from the display adapters' registry keys on Windows (the 64-bit value the
    driver publishes; the WMI value stops at 4 GB).
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    best = 0
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return None
    try:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    for name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                        try:
                            val, typ = winreg.QueryValueEx(k, name)
                        except OSError:
                            continue
                        if isinstance(val, bytes):
                            val = int.from_bytes(val[:8], "little")
                        best = max(best, int(val))
            except OSError:
                continue
    finally:
        winreg.CloseKey(root)
    return best or None


def blender_fits(n_sets, n_maps, size, ram_budget, gpu_budget):
    """Can Blender show every texture of the scene at ``size`` px?"""
    pixels = n_sets * max(0, n_maps) * size * size
    if pixels * BYTES_PER_PIXEL > ram_budget:
        return False
    return gpu_budget is None or pixels * GPU_BYTES_PER_PIXEL <= gpu_budget


def painter_set_bytes(size):
    return size * size * PAINTER_BYTES_PER_PIXEL_LAYER * PAINTER_LAYERS


def painter_capacity(size, ram_bytes, gpu_bytes=None):
    """Texture sets one Painter project can hold at ``size`` px on this computer."""
    per = painter_set_bytes(size)
    room = ram_bytes * PAINTER_RAM_SHARE - PAINTER_BASE_RAM
    if gpu_bytes:
        room = min(room, gpu_bytes * PAINTER_GPU_SHARE - PAINTER_BASE_GPU)
    return max(1, int(room // per)) if room > 0 else 1


def plan_scene(weights, n_maps, wanted, ram_bytes, gpu_bytes=None, ram_share=0.40,
               max_sets=None, minimum=MIN_RESOLUTION):
    """Texture size and Painter parts for a whole scene.

    ``weights`` are texture sets per object, in the order parts should follow.
    The size is the largest power of two <= ``wanted`` (and >= ``minimum``) at which
    Blender can show every texture; it goes further down (not below ``minimum``)
    while that lets the whole scene open in Painter as one project. Returns
    ``(size, parts)`` with ``parts`` a list of index lists.
    """
    n_sets = sum(max(1, int(w)) for w in weights)
    gpu_budget = gpu_bytes * BLENDER_GPU_SHARE if gpu_bytes else None
    wanted = max(1, int(wanted))
    size = 1 << (wanted.bit_length() - 1)
    floor = min(size, max(1, int(minimum)))
    while size > floor and not blender_fits(n_sets, n_maps, size, ram_bytes * ram_share, gpu_budget):
        size //= 2
    cap = painter_capacity(size, ram_bytes, gpu_bytes)
    while size > floor and n_sets > cap:
        size //= 2
        cap = painter_capacity(size, ram_bytes, gpu_bytes)
    if max_sets:
        cap = min(cap, int(max_sets))
    return size, pack_parts(weights, cap)


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
