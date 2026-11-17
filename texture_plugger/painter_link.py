# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender -> Substance 3D Painter scene link: lighting, object bounds and view as data.

Pure Python: this module must never import ``bpy`` (or numpy) so it can be
unit-tested with a plain Python interpreter.

Painter has no lights of its own: everything is lit by one environment map,
and its Python API can only *choose* that map (no rotation, no exposure).  So
Blender bakes its lighting into an equirectangular HDR seen from the object,
already rotated into Painter's frame and already exposed, and writes a JSON
"link" file next to it.  The Painter plugin (``painter_plugin/``) watches that
file: it loads the HDR as the environment, solos and frames texture sets using
the object bounds, and can match the Blender view.

Axes: the FBX export uses Blender's defaults (forward -Z, up Y), so a Blender
vector ``(x, y, z)`` is ``(x, z, -y)`` in Painter.  Positions here stay in
Blender units; the plugin fits them onto Painter's scene bounding box (Painter
works in centimetres, and the user may have exported with any unit scale).
"""

from __future__ import annotations

import json
import math
import os
import time

LINK_DIR_NAME = "texture_plugger_link"   # inside Painter's user folder
LINK_FILE = "blender_link.json"
FORMAT_VERSION = 1

# Painter displays the panorama Blender renders (camera looking along Blender
# +Y, +X to the right, +Z up) turned by 90 degrees about the up axis.  Rolling
# the image a quarter turn to the right lands it on Painter's convention.
# Measured in Painter 12.2 with a direction-coded test map (no mirroring).
ENV_YAW_DEG = 90.0
ENV_MIRROR = False

# Blender's 3D viewport behaves like a camera with a 72 mm sensor.
VIEWPORT_SENSOR = 72.0


# --------------------------------------------------------------------------
# Small vector helpers (tuples, no numpy)
# --------------------------------------------------------------------------

def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def normalize(a):
    n = math.sqrt(_dot(a, a))
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def to_painter(v):
    """Blender vector -> Painter axes (FBX forward -Z, up Y)."""
    return (float(v[0]), float(v[2]), -float(v[1]))


def painter_aabb(points):
    """Axis-aligned box (min, max) in Painter axes of Blender-space points."""
    pts = [to_painter(p) for p in points]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def merge_aabb(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return ([min(a[0][i], b[0][i]) for i in range(3)],
            [max(a[1][i], b[1][i]) for i in range(3)])


# --------------------------------------------------------------------------
# Equirectangular mapping (of the panorama as Blender renders it)
# --------------------------------------------------------------------------

def dir_to_uv(d):
    """Blender world direction -> (u, v) in [0, 1); v is measured from the bottom.

    Matches a Cycles equirectangular camera rotated (90, 0, 0): it looks along
    +Y, +X is to the right (u = 0.75) and +Z is at the top (measured).
    """
    x, y, z = normalize(d)
    u = 0.5 + math.atan2(x, y) / (2.0 * math.pi)
    v = 0.5 + math.asin(max(-1.0, min(1.0, z))) / math.pi
    return u % 1.0, v


def uv_to_dir(u, v):
    """Inverse of ``dir_to_uv`` (v from the bottom)."""
    lon = (u - 0.5) * 2.0 * math.pi
    lat = (v - 0.5) * math.pi
    c = math.cos(lat)
    return (c * math.sin(lon), c * math.cos(lon), math.sin(lat))


def pixel_solid_angle(row, height, width):
    """Solid angle of one pixel in row ``row`` (counted from the bottom)."""
    lat0 = (row / height - 0.5) * math.pi
    lat1 = ((row + 1) / height - 0.5) * math.pi
    return (2.0 * math.pi / width) * (math.sin(lat1) - math.sin(lat0))


def disc_pixels(width, height, direction, radius):
    """Pixels whose centre lies within ``radius`` radians of ``direction``.

    Returns ``[(row_from_bottom, col, solid_angle)]``.  Always returns at least
    the pixel that contains the direction, so a tiny light is never lost.
    """
    d = normalize(direction)
    cos_r = math.cos(radius)
    u, v = dir_to_uv(d)
    row_c = min(height - 1, int(v * height))
    lat_c = (v - 0.5) * math.pi
    lat_lo, lat_hi = lat_c - radius, lat_c + radius
    r0 = max(0, int(math.floor((lat_lo / math.pi + 0.5) * height)) - 1)
    r1 = min(height - 1, int(math.ceil((lat_hi / math.pi + 0.5) * height)) + 1)
    out = []
    for row in range(r0, r1 + 1):
        lat = ((row + 0.5) / height - 0.5) * math.pi
        cl = math.cos(lat)
        if lat_lo <= -math.pi / 2 + 1e-9 or lat_hi >= math.pi / 2 - 1e-9 or cl < 1e-6:
            cols = range(width)
        else:
            span = radius / cl + 2.0 * math.pi / width
            if span >= math.pi:
                cols = range(width)
            else:
                c0 = int(math.floor((u - span / (2 * math.pi)) * width)) - 1
                c1 = int(math.ceil((u + span / (2 * math.pi)) * width)) + 1
                cols = sorted({c % width for c in range(c0, c1 + 1)})
        omega = pixel_solid_angle(row, height, width)
        for col in cols:
            pd = uv_to_dir((col + 0.5) / width, (row + 0.5) / height)
            if _dot(pd, d) >= cos_r:
                out.append((row, col, omega))
    if not out:
        col_c = min(width - 1, int(u * width))
        out.append((row_c, col_c, pixel_solid_angle(row_c, height, width)))
    return out


def light_radiance(irradiance, pixels):
    """Radiance for each disc pixel so the disc delivers ``irradiance`` head on."""
    omega = sum(p[2] for p in pixels)
    if omega <= 0:
        return (0.0, 0.0, 0.0)
    return tuple(c / omega for c in irradiance)


def angular_radius(kind, distance, size, sun_angle, minimum):
    """Apparent angular radius (radians) of a light, clamped to [minimum, 25 deg]."""
    if kind == "SUN":
        r = sun_angle / 2.0
    elif distance > 1e-9:
        r = math.atan2(max(size, 0.0) / 2.0, distance)
    else:
        r = minimum
    return max(minimum, min(math.radians(25.0), r))


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------

def fov_from_lens(lens, sensor):
    """Full field of view (degrees) across ``sensor`` mm for a focal length in mm."""
    return math.degrees(2.0 * math.atan(sensor / (2.0 * max(lens, 1e-6))))


def split_fov(fov_wide, aspect):
    """(horizontal, vertical) FOV in degrees; ``fov_wide`` spans the longer side."""
    t = math.tan(math.radians(fov_wide) / 2.0)
    if aspect >= 1.0:
        return fov_wide, math.degrees(2.0 * math.atan(t / aspect))
    return math.degrees(2.0 * math.atan(t * aspect)), fov_wide


def view_payload(cam_pos, forward, up, fov_h, fov_v, ortho=False, ortho_height=0.0):
    """Camera in Painter axes, Blender units (Blender world-space inputs)."""
    return {
        "position": list(to_painter(cam_pos)),
        "forward": list(normalize(to_painter(forward))),
        "up": list(normalize(to_painter(up))),
        "fov_h": float(fov_h),
        "fov_v": float(fov_v),
        "ortho": bool(ortho),
        "ortho_height": float(ortho_height),
    }


# --------------------------------------------------------------------------
# Link file
# --------------------------------------------------------------------------

def tone_mapping_for(view_transform):
    """Painter tone mapping closest to a Blender view transform.

    Painter has "Linear" (clips above 1, like Blender's Standard) and "ACES" (rolls
    bright light off smoothly, like AgX, Filmic or Khronos PBR Neutral). With Linear,
    a room lit by strong lamps looks blown out in Painter although it looks right in
    Blender.
    """
    return "Linear" if str(view_transform or "") in {"Standard", "Raw", ""} else "ACES"


def link_file(link_dir):
    return os.path.join(link_dir, LINK_FILE)


def read_link(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_link(link_dir, sets, active="", environment=None, exposure=0.0, view=None,
               blend_name="", replace_sets=True, scene=None, tone_mapping=None,
               lighting_key=None, preview=None):
    """Merge new data into the link file (atomic write). Returns its path.

    ``sets`` maps a texture set (material) name to
    ``{"min": [...], "max": [...], "objects": [...]}`` in Painter axes,
    Blender units.  ``environment`` (a file name inside ``link_dir``) is stored
    for ``active`` and as the scene-wide default.  Keys not passed keep their
    previous values, so sending the view alone keeps the lighting.

    ``lighting_key`` fingerprints the world and lamps the new environment was
    baked with.  Other sets keep their own baked environment only while it was
    baked under the same lighting; after a lamp moves they fall back to the new
    map instead of showing the old lighting.

    ``preview`` is Blender's Material Preview lighting (the viewport's studio
    HDRI, the same for every set): ``{"environment": file, "exposure": ev,
    "hdri": name}``.  Painter can switch between it and the render lighting.
    ``{}`` removes it; ``None`` keeps the previous one.
    """
    os.makedirs(link_dir, exist_ok=True)
    path = link_file(link_dir)
    data = read_link(path) or {}
    if data.get("version") != FORMAT_VERSION:
        data = {}
    old_sets = data.get("sets") if isinstance(data.get("sets"), dict) else {}
    new_sets = {}
    for name, info in sets.items():
        entry = dict(info)
        prev = old_sets.get(name) or {}
        if prev.get("environment"):                    # keep per-set lighting
            entry.setdefault("environment", prev["environment"])
            entry.setdefault("exposure", prev.get("exposure", 0.0))
            if prev.get("lighting_key"):
                entry.setdefault("lighting_key", prev["lighting_key"])
        new_sets[name] = entry
    if not replace_sets:
        for name, prev in old_sets.items():
            new_sets.setdefault(name, prev)
    data["version"] = FORMAT_VERSION
    data["stamp"] = time.time()
    data["blend"] = blend_name
    data["sets"] = new_sets
    if active:
        data["active"] = active
    if environment is not None:
        data["environment"] = environment
        data["exposure"] = float(exposure)
        if lighting_key:
            data["lighting_key"] = lighting_key
            for entry in new_sets.values():                # baked under other lighting
                if entry.get("environment") and entry.get("lighting_key") != lighting_key:
                    for k in ("environment", "exposure", "lighting_key"):
                        entry.pop(k, None)
        if active and active in new_sets:
            new_sets[active]["environment"] = environment
            new_sets[active]["exposure"] = float(exposure)
            if lighting_key:
                new_sets[active]["lighting_key"] = lighting_key
    if view is not None:
        data["view"] = view
    if preview is not None:
        if preview.get("environment"):
            data["preview"] = dict(preview)
        else:
            data.pop("preview", None)
    if tone_mapping:
        data["tone_mapping"] = tone_mapping
    # Whole-scene launch: texture size for new texture sets and where to save the
    # Painter project (one per part). Only meant for the launch that sent it.
    if scene:
        data["scene"] = dict(scene)
    else:
        data.pop("scene", None)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    return path


def referenced_environments(data):
    refs = set()
    if not data:
        return refs
    if data.get("environment"):
        refs.add(data["environment"])
    for info in (data.get("sets") or {}).values():
        if isinstance(info, dict) and info.get("environment"):
            refs.add(info["environment"])
    prev = data.get("preview")
    if isinstance(prev, dict) and prev.get("environment"):
        refs.add(prev["environment"])
    return refs


def prune_environments(link_dir, keep, prefix="env_"):
    """Delete baked maps in ``link_dir`` not in ``keep`` (basenames). Best effort."""
    removed = []
    try:
        names = os.listdir(link_dir)
    except OSError:
        return removed
    for n in names:
        if n.startswith(prefix) and n not in keep and n.lower().endswith((".exr", ".hdr")):
            try:
                os.remove(os.path.join(link_dir, n))
                removed.append(n)
            except OSError:
                pass      # Painter may still hold it open; try again next time
    return removed


# --------------------------------------------------------------------------
# Lightmaps: Cycles lighting baked onto each texture set's UVs
# --------------------------------------------------------------------------

LIGHTMAPS_FILE = "blender_lightmaps.json"      # same name in the Painter plugin
LIGHTMAP_PREFIX = "lightmap_"


def lightmap_name(set_name, mode, stamp):
    """File name for a set's lightmap (``mode`` is "render" or "preview")."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(set_name))
    return f"{LIGHTMAP_PREFIX}{mode}_{safe}_{int(stamp)}.exr"


def merge_lightmaps(old, updates):
    """Lightmaps file contents after baking ``updates`` (set -> {"render": file,
    "preview": file or None, ...}); other sets keep their entry, and a mode not
    baked this time is dropped for the updated set (it no longer matches)."""
    sets = {}
    if isinstance(old, dict) and isinstance(old.get("sets"), dict):
        sets = {k: dict(v) for k, v in old["sets"].items() if isinstance(v, dict)}
    for name, info in (updates or {}).items():
        sets[str(name)] = {k: v for k, v in dict(info).items() if v is not None}
    return {"version": FORMAT_VERSION, "sets": sets}


def referenced_lightmaps(data):
    refs = set()
    sets = data.get("sets") if isinstance(data, dict) else None
    for info in (sets or {}).values():
        if isinstance(info, dict):
            for mode in ("render", "preview"):
                if info.get(mode):
                    refs.add(info[mode])
    return refs


def write_lightmaps(link_dir, updates, prune=True):
    """Merge ``updates`` into the lightmaps file (atomic) and delete unused maps.

    ``prune=False`` while a bake still runs: its next maps exist before they
    are listed."""
    os.makedirs(link_dir, exist_ok=True)
    path = os.path.join(link_dir, LIGHTMAPS_FILE)
    data = merge_lightmaps(read_link(path), updates)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    if prune:
        prune_environments(link_dir, referenced_lightmaps(data), prefix=LIGHTMAP_PREFIX)
    return path
