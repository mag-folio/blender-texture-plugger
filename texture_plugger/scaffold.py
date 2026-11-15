# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture set scaffolding for Substance 3D Painter round trips.

Pure Python: this module must never import ``bpy`` so it can be unit-tested
with a plain Python interpreter.

Painter's bundled export preset "Blender (Principled BSDF)" writes PNG files
named ``$textureSet_<Suffix>.png`` where ``$textureSet`` is the material name on
the imported mesh.  The placeholders created here use exactly those names, so
an export from Painter overwrites them and Blender picks up the new pixels.
"""

from __future__ import annotations

import glob
import os
import re
import struct
import zlib
from dataclasses import dataclass


@dataclass(frozen=True)
class MapSpec:
    id: str             # settings toggle id (create_<id>)
    map_type: str       # detection map type id
    suffix: str         # Painter export suffix
    label: str
    rgb: tuple          # 8-bit placeholder colour
    default: bool       # enabled by default


MAPS = (
    MapSpec("base_color", "base_color", "BaseColor", "Base Color", (204, 204, 204), True),
    MapSpec("normal", "normal_gl", "Normal", "Normal (OpenGL)", (128, 128, 255), True),
    MapSpec("metallic", "metallic", "Metallic", "Metallic", (0, 0, 0), True),
    MapSpec("roughness", "roughness", "Roughness", "Roughness", (128, 128, 128), True),
    MapSpec("emission", "emission", "Emission", "Emission", (0, 0, 0), False),
    MapSpec("alpha", "opacity", "Alpha", "Alpha", (255, 255, 255), False),
    MapSpec("displacement", "height", "Displacement", "Displacement", (128, 128, 128), False),
)

MAPS_BY_ID = {spec.id: spec for spec in MAPS}

_BAD_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_name(text, fallback="TextureSet"):
    """Keep ``[A-Za-z0-9_-]``; every other run of characters becomes one ``_``."""
    out = _BAD_CHARS_RE.sub("_", str(text or "")).strip("_")
    return out or fallback


def texture_filename(set_name, spec):
    return f"{set_name}_{spec.suffix}.png"


@dataclass(frozen=True)
class SetPaths:
    folder: str
    textures: str
    mesh_dir: str
    mesh: str


def set_paths(root, set_name):
    folder = os.path.join(root, set_name)
    mesh_dir = os.path.join(folder, "mesh")
    return SetPaths(
        folder=folder,
        textures=os.path.join(folder, "textures"),
        mesh_dir=mesh_dir,
        mesh=os.path.join(mesh_dir, set_name + ".fbx"),
    )


# --------------------------------------------------------------------------
# PNG writer
# --------------------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CHUNK_TARGET = 1 << 16


def _chunk(kind, data):
    crc = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


_PNG_CACHE = {}          # (width, height, rgb) -> encoded PNG bytes
_PNG_CACHE_MAX = 32
_BLOCK_BYTES = 1 << 20


def _encode_png(width, height, rgb):
    """Bytes of a solid colour PNG.

    Cached: placeholders repeat the same few colours, so a whole scene costs one
    encode per map type and size instead of one per file, and rows are fed to
    zlib in ~1 MB blocks instead of one call per row.
    """
    key = (width, height, rgb)
    data = _PNG_CACHE.get(key)
    if data is not None:
        return data
    row = b"\x00" + bytes(rgb) * width            # filter type 0 + pixels
    rows_per_block = max(1, _BLOCK_BYTES // len(row))
    comp = zlib.compressobj(9)
    parts = []
    full, rest = divmod(height, rows_per_block)
    if full:
        block = row * rows_per_block
        for _ in range(full):
            parts.append(comp.compress(block))
    if rest:
        parts.append(comp.compress(row * rest))
    parts.append(comp.flush())
    idat = b"".join(parts)
    out = [_PNG_SIG, _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))]
    for i in range(0, len(idat), _CHUNK_TARGET):
        out.append(_chunk(b"IDAT", idat[i:i + _CHUNK_TARGET]))
    out.append(_chunk(b"IEND", b""))
    data = b"".join(out)
    if len(_PNG_CACHE) >= _PNG_CACHE_MAX:
        _PNG_CACHE.clear()
    _PNG_CACHE[key] = data
    return data


def write_png(path, width, height, rgb):
    """Write a solid colour 8-bit RGB PNG. Never overwrites (FileExistsError)."""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError("PNG size must be positive")
    rgb = tuple(max(0, min(255, int(c))) for c in rgb)
    data = _encode_png(width, height, rgb)
    fh = open(path, "xb")
    try:
        fh.write(data)
    except BaseException:
        fh.close()
        try:
            os.remove(path)    # do not leave a broken placeholder behind
        except OSError:
            pass
        raise
    fh.close()
    return path


def _encode_png_rowwise(width, height, rgb):
    """Bytes the 1.0-1.2 placeholder writer produced (one zlib call per row)."""
    row = b"\x00" + bytes(rgb) * width
    comp = zlib.compressobj(9)
    buf = bytearray()
    idat_chunks = []
    for _ in range(height):
        buf += comp.compress(row)
        if len(buf) >= _CHUNK_TARGET:
            idat_chunks.append(bytes(buf))
            buf.clear()
    buf += comp.flush()
    if buf:
        idat_chunks.append(bytes(buf))
    out = [_PNG_SIG, _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))]
    out += [_chunk(b"IDAT", c) for c in idat_chunks]
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


_OLD_CACHE = {}


def png_size(path):
    """(width, height) from a PNG header, or None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != _PNG_SIG or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def is_untouched_placeholder(path, rgb):
    """True if ``path`` is exactly a placeholder this add-on wrote (never painted).

    Compared byte for byte with what the current and the earlier writer produce
    for that size and colour, so a painted or edited texture never matches.
    """
    dims = png_size(path)
    if dims is None:
        return False
    w, h = dims
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            return False
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    rgb = tuple(rgb)
    if data == _encode_png(w, h, rgb):
        return True
    key = (w, h, rgb)
    old = _OLD_CACHE.get(key)
    if old is None:
        if len(_OLD_CACHE) >= _PNG_CACHE_MAX:
            _OLD_CACHE.clear()
        old = _OLD_CACHE[key] = _encode_png_rowwise(w, h, rgb)
    return data == old


def create_files(root, set_name, map_ids, size, textures_dir=None, shrink=False):
    """Create the folders and missing placeholders.

    Returns ``[(spec, path, created)]`` in ``MAPS`` order.  Existing files are
    never overwritten.  ``textures_dir`` puts the files in one shared folder
    (whole-scene projects, where Painter exports every set to a single path).
    """
    paths = set_paths(root, set_name)
    tex_dir = textures_dir or paths.textures
    os.makedirs(tex_dir, exist_ok=True)
    if not textures_dir:
        os.makedirs(paths.mesh_dir, exist_ok=True)
    wanted = set(map_ids)
    out = []
    for spec in MAPS:
        if spec.id not in wanted:
            continue
        path = os.path.join(tex_dir, texture_filename(set_name, spec))
        created = False
        if shrink and os.path.exists(path):
            # A bigger, never-painted placeholder from an earlier run would cost
            # memory for nothing: replace it with a small one. Painted files are
            # never touched (they don't match a placeholder byte for byte).
            dims = png_size(path)
            if dims and max(dims) > size and is_untouched_placeholder(path, spec.rgb):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if not os.path.exists(path):
            try:
                write_png(path, size, size, spec.rgb)
                created = True
            except FileExistsError:
                created = False
        out.append((spec, path, created))
    return out


def find_project(folder):
    """Newest ``*.spp`` directly inside ``folder``, or None."""
    if not folder or not os.path.isdir(folder):
        return None
    best, best_t = None, None
    for p in glob.glob(os.path.join(glob.escape(folder), "*.spp")):
        try:
            t = os.path.getmtime(p)
        except OSError:
            continue
        if os.path.isfile(p) and (best_t is None or t > best_t):
            best, best_t = p, t
    return best


# --------------------------------------------------------------------------
# Painter executable / command line
# --------------------------------------------------------------------------

_EXE_NAME = "Adobe Substance 3D Painter.exe"


def painter_candidates():
    cands = []
    for var, default in (("ProgramFiles", r"C:\Program Files"),
                         ("ProgramW6432", r"C:\Program Files")):
        base = os.environ.get(var) or default
        p = os.path.join(base, "Adobe", "Adobe Substance 3D Painter", _EXE_NAME)
        if p not in cands:
            cands.append(p)
    x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    steam = os.path.join(glob.escape(os.path.join(x86, "Steam", "steamapps", "common")),
                         "Substance 3D Painter*", _EXE_NAME)
    cands.extend(sorted(glob.glob(steam), reverse=True))
    cands.append("/Applications/Adobe Substance 3D Painter.app/Contents/MacOS/"
                 "Adobe Substance 3D Painter")
    cands.append("/opt/Adobe/Adobe_Substance_3D_Painter/Adobe Substance 3D Painter")
    return cands


def find_painter(explicit=""):
    """Path of the Painter executable, or "" when not found."""
    if explicit and os.path.isfile(explicit):
        return explicit
    for p in painter_candidates():
        if os.path.isfile(p):
            return p
    return ""


def painter_command(exe, export_path, mesh="", project=""):
    """argv to open ``project`` (wins) or start a new project from ``mesh``."""
    if project:
        return [exe, "--export-path", export_path, project]
    return [exe, "--mesh", mesh, "--export-path", export_path]
