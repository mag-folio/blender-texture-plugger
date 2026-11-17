# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture Plugger link for Adobe Substance 3D Painter.

Installed by the Texture Plugger Blender add-on into Painter's
``Documents/Adobe/Adobe Substance 3D Painter/python/startup`` folder, so it
loads with Painter.  It adds a "Blender Link" panel (Window > Views) and a
"Texture Plugger" menu:

* Lighting: Blender bakes its world, lamps and surroundings into an HDR seen
  from the object; this plugin makes it Painter's environment map, and swaps
  to the right object's lighting when you solo another texture set.  A switch
  shows Blender's Material Preview lighting (its studio HDRI) instead.
* Solo: shows only one texture set (Painter's own Focus Mode) and frames the
  camera on it.  While soloing, picking another texture set in Painter's
  Texture Set List frames that one.  Orbit with Alt + left-drag on the object:
  Painter orbits around the point under the cursor.
* Match Blender View: puts Painter's camera where your Blender viewport is.
* Export to Blender: exports with the template and file type Blender's add-on
  reads (Blender (Principled BSDF), PNG) into the folder Blender watches.
* Bake Lighting: lights a texture set's Base Color with its Blender lighting
  (Painter's Baked Lighting Environment filter in a "Blender Lighting" layer),
  so you paint and colour-pick lit colours on the "Lit Paint" layer above it.
  Exports mark those sets (texture_plugger_lit.json) and Blender shows them
  unlit, with emission only.

The panel groups its buttons into LIGHTING, TEXTURES and VIEW.  Each button
carries a painted badge saying what it moves (bulb, texture swatch, eye) and
which way (Blender -> Painter or Painter -> Blender).  Anything that overwrites
files, covers paint or deletes a layer asks first (Cancel is the default), only
one action runs at a time, and baked lighting layers change only when you press
Update Baked Lighting unless you turn on automatic updates.

Everything comes from ``../../texture_plugger_link/blender_link.json`` (next
to the ``python`` folder), written by Blender's "Send to Painter" button.

The maths at the top has no Painter imports so it can be tested with plain
Python.
"""

import json
import math
import os

PLUGIN_VERSION = "1.7.2"
LINK_FILE = "blender_link.json"
LINK_DIR_NAME = "texture_plugger_link"
FRAME_MARGIN = 1.15
POLL_MS = 700


# ==========================================================================
# Pure maths (no Painter imports)
# ==========================================================================

def _norm(v):
    n = math.sqrt(sum(c * c for c in v))
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return [c / n for c in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def basis_from_euler(rot):
    """(forward, up) of a Painter camera with rotation ``rot`` (degrees).

    Painter's camera looks down -Z with +Y up at (0, 0, 0) and composes
    R = Rx * Ry * Rz (measured in Painter 12.2: [45, -90, 0] reads back as
    [0, -90, -45], [-30, 45, 0] as [150, 135, 180]).
    """
    a, b, c = (math.radians(x) for x in rot)
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)

    def rx(v):
        return [v[0], ca * v[1] - sa * v[2], sa * v[1] + ca * v[2]]

    def ry(v):
        return [cb * v[0] + sb * v[2], v[1], -sb * v[0] + cb * v[2]]

    def rz(v):
        return [cc * v[0] - sc * v[1], sc * v[0] + cc * v[1], v[2]]

    return rx(ry(rz([0.0, 0.0, -1.0]))), rx(ry(rz([0.0, 1.0, 0.0])))


def euler_from_basis(forward, up=(0.0, 1.0, 0.0)):
    """Painter rotation (degrees) for a camera looking along ``forward``."""
    f = _norm(forward)
    u = list(up)
    r = _cross(f, u)
    if math.sqrt(sum(x * x for x in r)) < 1e-6:          # up parallel to forward
        u = [0.0, 0.0, -1.0] if f[1] > 0 else [0.0, 0.0, 1.0]
        r = _cross(f, u)
    r = _norm(r)
    u = _cross(r, f)
    z = [-f[0], -f[1], -f[2]]                              # camera +Z
    # R = Rx(a) Ry(b) Rz(c):  R*ez = (sin b, -sin a cos b, cos a cos b)
    b = math.asin(max(-1.0, min(1.0, z[0])))
    if abs(math.cos(b)) > 1e-6:
        a = math.atan2(-z[1], z[2])
        c = math.atan2(-u[0], r[0])
    else:                                                  # gimbal: fold roll into a
        a = math.atan2(u[2], u[1])
        c = 0.0
    return [math.degrees(a), math.degrees(b), math.degrees(c)]


def box_center_radius(lo, hi):
    c = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
    r = math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))) / 2.0
    return c, max(r, 1e-6)


def orbit_around(pos, center, yaw_deg, pitch_deg, max_pitch=85.0):
    """Camera position and forward after orbiting ``yaw``/``pitch`` degrees about ``center``.

    Painter's API has no orbit pivot (its mouse orbit turns about the point under
    the cursor), so the plugin orbits explicitly: the distance to ``center`` is
    kept, world up is +Y and the pitch is clamped so the view never flips.
    """
    v = [pos[i] - center[i] for i in range(3)]
    d = math.sqrt(sum(c * c for c in v))
    if d < 1e-9:
        d, v = 1.0, [0.0, 0.0, 1.0]
    az = math.atan2(v[0], v[2]) + math.radians(yaw_deg)
    lim = math.radians(max_pitch)
    el = max(-lim, min(lim, math.asin(max(-1.0, min(1.0, v[1] / d))) + math.radians(pitch_deg)))
    new = [center[0] + d * math.cos(el) * math.sin(az),
           center[1] + d * math.sin(el),
           center[2] + d * math.cos(el) * math.cos(az)]
    return new, _norm([center[i] - new[i] for i in range(3)])


def zoom_about(pos, center, factor, min_dist=1e-3):
    """Move ``pos`` towards (factor < 1) or away from (> 1) ``center``."""
    v = [pos[i] - center[i] for i in range(3)]
    d = math.sqrt(sum(c * c for c in v))
    if d < 1e-9:
        return list(pos)
    k = max(min_dist, d * max(factor, 1e-3)) / d
    return [center[i] + v[i] * k for i in range(3)]


def fit_transform(blender_lo, blender_hi, painter_center, painter_dims):
    """(scale, b_center, p_center) mapping link coordinates onto Painter's scene.

    The link stores Painter axes in Blender units; Painter works in its own
    units (centimetres for a metre scene) around its own bounding box.
    """
    bc = [(blender_lo[i] + blender_hi[i]) / 2.0 for i in range(3)]
    bd = math.sqrt(sum((blender_hi[i] - blender_lo[i]) ** 2 for i in range(3)))
    pd = math.sqrt(sum(d * d for d in painter_dims))
    scale = pd / bd if bd > 1e-9 and pd > 1e-9 else 100.0
    return scale, bc, list(painter_center)


def apply_fit(fit, p):
    scale, bc, pc = fit
    return [(p[i] - bc[i]) * scale + pc[i] for i in range(3)]


def frame_position(center, radius, forward, fov_v_deg, aspect, margin=FRAME_MARGIN):
    """Camera position looking along ``forward`` so a sphere fills the view."""
    fv = math.radians(max(3.0, min(179.0, fov_v_deg)))
    fh = 2.0 * math.atan(math.tan(fv / 2.0) * max(aspect, 1e-3))
    half = min(fv, fh) / 2.0
    dist = radius * margin / math.sin(half)
    f = _norm(forward)
    return [center[i] - f[i] * dist for i in range(3)], dist


def union_box(sets, names):
    lo = hi = None
    for n in names:
        info = sets.get(n)
        if not isinstance(info, dict) or info.get("min") is None:
            continue
        if lo is None:
            lo, hi = list(info["min"]), list(info["max"])
        else:
            lo = [min(lo[i], info["min"][i]) for i in range(3)]
            hi = [max(hi[i], info["max"][i]) for i in range(3)]
    return lo, hi


def _name_key(name):
    """Loose form of a texture set name: Painter may change punctuation and case."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def match_sets(painter_names, link_sets):
    """Map each Painter texture set name to its entry key in the link (or leave it out).

    Exact names win; otherwise names that only differ in case or punctuation
    (``Material.001`` vs ``Material_001``) match, when that is unambiguous.
    """
    out = {}
    loose = {}
    for key, info in link_sets.items():
        if isinstance(info, dict):
            loose.setdefault(_name_key(key), []).append(key)
    for n in painter_names:
        info = link_sets.get(n)
        if isinstance(info, dict):
            out[n] = n
            continue
        cands = loose.get(_name_key(n), [])
        if len(cands) == 1:
            out[n] = cands[0]
    return out


LIGHT_MODES = ("render", "preview")

# What Blender's Texture Plugger reads: <set>_BaseColor.png, <set>_Normal.png, ...
# exactly as Painter's own "Blender (Principled BSDF)" template names them, as PNG.
BLENDER_PRESET = "Blender (Principled BSDF)"
BLENDER_FORMAT = "png"


def blender_textures_dir(project_file, mesh_file, default_path=""):
    """The ``textures`` folder Blender watches for this Painter project, or "".

    Texture Plugger keeps ``<folder>/<name>.spp``, ``<folder>/mesh/<name>.fbx``
    and ``<folder>/textures``; Blender also starts Painter with
    ``--export-path <folder>/textures``.
    """
    bases = []
    if project_file:
        bases.append(os.path.dirname(os.path.abspath(project_file)))
    if mesh_file:
        d = os.path.dirname(os.path.abspath(mesh_file))
        bases.append(os.path.dirname(d) if os.path.basename(d).lower() == "mesh" else d)
    for base in bases:
        t = os.path.join(base, "textures")
        if os.path.isdir(t):
            return t
    if (default_path and os.path.basename(os.path.normpath(default_path)).lower() == "textures"
            and os.path.isdir(default_path)):
        return default_path
    return ""


FROM_BLENDER_LAYER_NAME = "From Blender"
# Exported map suffix -> Painter channel it goes back into.  Normal and
# Displacement are left out on purpose: the exported Normal already contains the
# baked mesh normal map, so loading it back would apply the mesh detail twice.
# Each suffix lists the channel's OpenPBR name first (Painter 12's default
# projects: SpecularRoughness, BaseMetalness, EmissionColor) and its legacy
# name second; the legacy names are aliases of the same channel types.
LOAD_CHANNELS = (("BaseColor", ("BaseColor",)),
                 ("Roughness", ("SpecularRoughness", "Roughness")),
                 ("Metallic", ("BaseMetalness", "Metallic")),
                 ("Emission", ("EmissionColor", "Emissive")),
                 ("Alpha", ("Opacity",)))


def blender_texture_files(textures_dir, set_name, names=None):
    """{exported suffix: file} of a set's maps that can go back into Painter.

    Matches ``<set>_<Suffix>.png`` case-insensitively, as Export to Blender wrote
    them.  ``names`` (the folder listing) can be passed to avoid listing it again.
    """
    if names is None:
        try:
            names = os.listdir(textures_dir)
        except OSError:
            return {}
    by_lower = {n.lower(): n for n in names}
    out = {}
    for suffix, _channels in LOAD_CHANNELS:
        hit = by_lower.get(f"{set_name}_{suffix}.png".lower())
        if hit:
            out[suffix] = os.path.join(textures_dir, hit)
    return out


PLACEHOLDER_MAX_PX = 127     # Blender's scaffold writes 64 px stand-ins; Painter exports >= 128


def png_size(path):
    """(width, height) from a PNG header, or None."""
    import struct
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def is_placeholder_png(path):
    """True for the tiny blank textures Blender makes before anything was painted
    (loading them would put flat colours over the paint)."""
    size = png_size(path)
    return bool(size) and max(size) <= PLACEHOLDER_MAX_PX


def channels_for_files(files, channel_enum, stack_channels):
    """{Painter channel type: file} for the channels this stack actually has.

    Compares channel *types*, not names: Painter reports an OpenPBR project's
    roughness as ``SpecularRoughness`` while ``ChannelType.Roughness`` is an
    alias of it, so name matching dropped Roughness and Metallic (Painter 12.2).
    """
    have = set(stack_channels)
    out = {}
    for suffix, candidates in LOAD_CHANNELS:
        if suffix not in files:
            continue
        for name in candidates:
            ch = getattr(channel_enum, name, None)
            if ch is not None and ch in have:
                out[ch] = files[suffix]
                break
    return out


def project_save_path(folder, existing=()):
    """Where to save a never-saved project: ``<folder>/<folder name>.spp``, or a
    numbered name when that file already exists (never overwrite another project)."""
    stem = os.path.basename(os.path.normpath(folder)) or "project"
    taken = {str(e).lower() for e in existing}
    name, n = f"{stem}.spp", 1
    while name.lower() in taken:
        name, n = f"{stem}_{n}.spp", n + 1
    return os.path.join(folder, name)


def blender_export_config(export_dir, set_names, preset_url):
    """Painter export settings that give Blender exactly the files it expects."""
    return {
        "exportShaderParams": False,
        "exportPath": export_dir,
        "defaultExportPreset": preset_url,
        "exportList": [{"rootPath": n} for n in set_names],
        # Painter can't resolve the padding from the template alone when exporting
        # from Python ("padding algorithm could not be resolved", Painter 12.2):
        # "infinite" is its usual dilation, so seams never show black edges.
        "exportParameters": [{"parameters": {"fileFormat": BLENDER_FORMAT,
                                             "paddingAlgorithm": "infinite"}}],
    }


def pick_environment(data, set_info, mode="render"):
    """(file, kind) of the environment to show; kind is "render" or "preview".

    "render" is Blender's scene lighting (world + lamps) baked around the
    object: the set's own map, else the scene-wide one.  "preview" is Blender's
    Material Preview studio HDRI, the same for every set; when Blender did not
    send one, the render lighting is used.
    """
    data = data or {}
    if mode == "preview":
        pv = data.get("preview")
        if isinstance(pv, dict) and pv.get("environment"):
            return pv["environment"], "preview"
    if isinstance(set_info, dict) and set_info.get("environment"):
        return set_info["environment"], "render"
    return data.get("environment"), "render"


def ts_name(texture_set):
    """A Texture Set's name on any Painter version.

    Painter 12 turned ``name()`` into a property; calling it still works but logs a
    deprecation warning every time (thousands a second with a big scene).
    """
    n = texture_set.name
    return str(n) if isinstance(n, str) else n()


def default_link_dir(plugin_file=__file__):
    """<Painter user folder>/texture_plugger_link (plugin sits in python/startup)."""
    env = os.environ.get("TP_LINK_DIR")
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(plugin_file))))
    return os.path.join(root, LINK_DIR_NAME)


def read_link(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data):
    """Write ``data`` as JSON atomically (readers never see half a file)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---- Lighting baked into Base Color ("Bake Lighting") ----------------------
#
# Painter's own "Baked Lighting Environment" filter lights the Base Color of the
# layers below it with an environment map.  The plugin puts it in a layer of its
# own at the top of a texture set ("Blender Lighting", Passthrough on Base
# Color), feeds it that object's Blender lighting, and adds an empty "Lit Paint"
# layer above it to paint on with colours picked from the lit result.  Exports
# then carry the lighting in <set>_BaseColor.png, and a marker file next to the
# textures tells Blender to show those sets unlit (emission only).

LIGHT_LAYER_NAME = "Blender Lighting"
LIT_PAINT_LAYER_NAME = "Lit Paint"
LIGHT_FILTER = "baked_lighting_environment"
LIT_MARKER = "texture_plugger_lit.json"        # same name in Blender's unlit.py
META_CONTEXT = "texture_plugger"
META_LIT_KEY = "lit_environments"
# The filter's "Horizontal Rotation" (in turns, 0..1) that lines its environment up
# with Painter's viewport environment, which the Blender map is already turned to.
FILTER_YAW_TURNS = 0.0


def passthrough_mode(blending_mode_enum):
    """Painter's Passthrough blending mode.

    Painter 12.2's documentation calls it ``BlendingMode.PassThrough``, but the
    application itself names it ``Passthrough`` (measured: "type object
    'BlendingMode' has no attribute 'PassThrough'"), so accept either spelling.
    """
    for name in ("Passthrough", "PassThrough"):
        mode = getattr(blending_mode_enum, name, None)
        if mode is not None:
            return mode
    raise RuntimeError("Painter has no Passthrough blending mode")


def file_stem(path):
    """Lower-case file name without extension or UDIM tile: the key Blender matches on.

    ``C:/t/Crate_BaseColor.1001.png`` and ``Crate_BaseColor.<UDIM>.png`` both give
    ``crate_basecolor``.  Blender's ``unlit.file_stem`` must give the same.
    """
    name = os.path.basename(str(path).replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    for sep in (".", "_"):
        head, s, tail = stem.rpartition(sep)
        if s and head and (tail.upper() == "<UDIM>" or (len(tail) == 4 and tail.isdigit())):
            stem = head
            break
    return stem.lower()


def base_color_stem(set_name, files):
    """``file_stem`` of the Base Color file exported for ``set_name`` (or the usual name)."""
    want = str(set_name).lower() + "_basecolor"
    for f in files or ():
        s = file_stem(f)
        if s.startswith(want):
            return s
    for f in files or ():
        s = file_stem(f)
        if "basecolor" in s or "base_color" in s:
            return s
    return want


def merge_lit_marker(old, exported):
    """Marker contents after an export.

    ``exported`` maps each texture set just exported to ``{"lit": bool,
    "base_color": stem, ...}``; sets exported earlier keep their entry.
    """
    sets = {}
    if isinstance(old, dict) and isinstance(old.get("sets"), dict):
        sets = {k: v for k, v in old["sets"].items() if isinstance(v, dict)}
    for name, info in (exported or {}).items():
        sets[str(name)] = dict(info)
    return {"version": 1, "sets": sets}


LIGHTMAPS_FILE = "blender_lightmaps.json"     # written by Blender's Bake Lighting to UVs
LIGHTMAP_PREFIX = "lightmap_"


def lightmap_entry(lightmaps, set_name):
    """A texture set's entry in ``blender_lightmaps.json`` contents, or None."""
    sets = lightmaps.get("sets") if isinstance(lightmaps, dict) else None
    if not isinstance(sets, dict) or not set_name:
        return None
    key = match_sets([set_name], sets).get(set_name)
    info = sets.get(key) if key else None
    return info if isinstance(info, dict) else None


def pick_lightmap(lightmaps, set_name, mode="render"):
    """Cycles lightmap file for a texture set and lighting mode, or None.

    ``mode`` "preview" only takes a studio-HDRI lightmap; without one that mode
    uses the Material Preview environment through the filter instead.
    """
    info = lightmap_entry(lightmaps, set_name)
    return (info.get(mode) or None) if info else None


def lightmap_outdated(entry, link):
    """True when Blender sent different lighting (lamps, world, exposure) after
    this lightmap was baked."""
    if not isinstance(entry, dict) or not isinstance(link, dict):
        return False
    key, now = entry.get("lighting_key"), link.get("lighting_key")
    if not key or not now or key == now:
        return False
    try:
        return float(link.get("stamp") or 0) > float(entry.get("time") or 0)
    except (TypeError, ValueError):
        return False


def is_lightmap(file_name):
    return bool(file_name) and os.path.basename(str(file_name)).startswith(LIGHTMAP_PREFIX)


def lit_updates(recorded, wanted):
    """Lit sets whose lighting layer shows another map than the one they should now.

    ``recorded``: set -> map file the layer was given; ``wanted``: set -> map file
    for the current link and lighting mode.  Sets without a wanted map are left alone.
    """
    return sorted(n for n, env in (recorded or {}).items()
                  if wanted.get(n) and wanted.get(n) != env)


LIGHT_RES_PREFIX = "Blender lighting "     # name of every lighting map the plugin imports
PROJECT_LIGHT_DIR = "blender_lighting"     # copies of them, next to the .spp
TODO_FILE = "painter_todo.json"            # one-time jobs Blender leaves for a project


def lighting_stem(resource_name):
    """File stem of a lighting map from its project resource name, or None."""
    name = str(resource_name or "")
    if not name.startswith(LIGHT_RES_PREFIX):
        return None
    stem = name[len(LIGHT_RES_PREFIX):].strip()
    return stem or None


def lightmap_set_from_stem(stem):
    """Texture set name a lightmap file stem was baked for, or None.

    Stems look like ``lightmap_render_<set>_<stamp>`` (``painter_link.lightmap_name``).
    """
    stem = str(stem or "")
    for mode in ("render", "preview"):
        head = f"{LIGHTMAP_PREFIX}{mode}_"
        if stem.startswith(head):
            body, sep, stamp = stem[len(head):].rpartition("_")
            if sep and body and stamp.isdigit():
                return body
    return None


def replacement_for(stem, lightmaps, link):
    """Current file that best stands in for a lighting map that no longer exists:
    the set's current lightmap, else Blender's current environment map."""
    set_name = lightmap_set_from_stem(stem)
    if set_name:
        mode = "preview" if stem.startswith(f"{LIGHTMAP_PREFIX}preview_") else "render"
        entry = lightmap_entry(lightmaps, set_name)
        if isinstance(entry, dict) and (entry.get(mode) or entry.get("render")):
            return entry.get(mode) or entry.get("render")
        return None
    if stem.startswith("env_preview_"):
        pv = (link or {}).get("preview")
        return pv.get("environment") if isinstance(pv, dict) else None
    if stem.startswith("env_"):
        return (link or {}).get("environment")
    return None


def lit_kind_swaps(recorded, wanted):
    """Lit sets whose update would change the KIND of lighting layer (Cycles
    lightmap <-> environment filter).  Such an update deletes the layer and makes
    a new one, so settings tweaked on it in Painter (filter sliders, opacity,
    masks) are lost: never done without asking."""
    return sorted(n for n in lit_updates(recorded, wanted)
                  if is_lightmap(recorded.get(n)) != is_lightmap(wanted.get(n)))


# Layers the plugin makes itself; every other layer is the user's work.
def user_layers(layer_names):
    """The user's own layers among a texture set's root layer names."""
    ours = {LIGHT_LAYER_NAME, LIT_PAINT_LAYER_NAME, FROM_BLENDER_LAYER_NAME}
    return [n for n in (layer_names or ()) if n not in ours]


def load_would_cover(layer_names):
    """True when Load from Blender would put a new opaque "From Blender" layer
    above the user's own layers (hiding their paint): the set has user layers and
    no "From Blender" layer yet (an existing one is only updated in place)."""
    names = list(layer_names or ())
    return bool(user_layers(names)) and FROM_BLENDER_LAYER_NAME not in names


def older_than(file_times, reference_time, slack=2.0):
    """Names whose time is older than ``reference_time`` (minus ``slack`` seconds)."""
    if not reference_time:
        return []
    return sorted(n for n, t in (file_times or {}).items()
                  if t and t < float(reference_time) - slack)


def short_list(names, n=4):
    """'a, b, c and 3 more'."""
    names = list(names or ())
    head = ", ".join(names[:n])
    return head + (f" and {len(names) - n} more" if len(names) > n else "")


# What each panel button does and which way data goes, for the badges drawn on it.
# direction: "b2p" Blender -> Painter, "p2b" Painter -> Blender, "painter" stays
# in Painter.  kind: "light", "texture" or "view".
ACTIONS = {
    "show_light":   ("b2p", "light"),
    "bake_light":   ("b2p", "light"),
    "bake_all":     ("b2p", "light"),
    "update_light": ("b2p", "light"),
    "remove_light": ("painter", "light"),
    "send_sel":     ("p2b", "texture"),
    "send_all":     ("p2b", "texture"),
    "get_sel":      ("b2p", "texture"),
    "get_all":      ("b2p", "texture"),
    "match_view":   ("b2p", "view"),
    "solo":         ("painter", "view"),
    "frame":        ("painter", "view"),
    "show_all":     ("painter", "view"),
}
QUEUE_OK = {"bake_light", "bake_all"}      # may be pressed while mesh maps bake: they queue
DIRECTION_WORDS = {"b2p": "Blender → Painter", "p2b": "Painter → Blender",
                   "painter": "Painter only"}


# ==========================================================================
# Painter side
# ==========================================================================

class Bridge:
    """All state of the plugin; one instance while the plugin is started."""

    def __init__(self):
        from PySide6 import QtCore
        import substance_painter.event as ev
        self.link_dir = default_link_dir()
        self.link_path = os.path.join(self.link_dir, LINK_FILE)
        self.data = None
        self.mtime = None
        self.env_cache = {}          # file path -> resource url
        self.applied_env = None      # file name last applied
        self.soloing = False
        self.last_active_set = None
        self.last_set_names = ()
        self.auto_light = True
        self.light_mode = self._load_mode()
        # Off by default: baked lighting layers change only when you press Update
        # Baked Lighting, never behind your back when Blender sends something.
        self.auto_relight = self._load_flag("auto_relight", False)
        self.follow = True
        self.widget = None
        self.menu = None
        self.tex_cache = {}          # lighting map path -> ResourceID (project texture)
        self.lightmaps = None        # blender_lightmaps.json (Cycles UV bakes)
        self.lm_mtime = None
        self._filter_id = None       # the Baked Lighting Environment filter
        self._lit_queue = []         # sets still to light (Bake Lighting)
        self._baking = None          # set whose mesh maps are being baked
        self._bake_restore = None    # (set, baker selection to put back)
        self._baked_once = set()     # sets whose mesh maps we baked (never loop)
        self._handlers = []
        self.timer = QtCore.QTimer()
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._tick)
        for event, fn in ((ev.ProjectEditionEntered, self._on_project_ready),
                          (ev.ProjectAboutToSave, self._on_about_to_save),
                          (ev.ProjectAboutToClose, self._on_project_closing),
                          (ev.ExportTexturesEnded, self._on_export_ended),
                          (ev.BakingProcessEnded, self._on_baking_ended)):
            ev.DISPATCHER.connect(event, fn)
            self._handlers.append((event, fn))

    # ---------------------------------------------------------------- helpers
    def log(self, msg, warn=False):
        import substance_painter.logging as lg
        (lg.warning if warn else lg.info)("Texture Plugger: " + msg)
        if self.widget is not None:
            self.widget.set_status(msg, warn)

    @staticmethod
    def project_ready():
        import substance_painter.project as pj
        try:
            return pj.is_open() and pj.is_in_edition_state()
        except Exception:
            return False

    def texture_set_names(self):
        import substance_painter.textureset as ts
        try:
            return [ts_name(t) for t in ts.all_texture_sets()]
        except Exception:
            return []

    def active_set_name(self):
        import substance_painter.textureset as ts
        try:
            return ts_name(ts.get_active_stack().material())
        except Exception:
            return None

    def focus_button(self):
        """Painter's Focus Mode button (Texture Set List toolbar), or None.

        Measured in Painter 12.2: clicking this button isolates the active
        texture set and reflects its state; triggering the "FocusMode" QAction
        from Python does nothing (Painter keeps two, the first a dead wrapper).
        Looked up fresh every time because Painter rebuilds that toolbar.
        """
        from PySide6 import QtWidgets
        found = []
        for w in QtWidgets.QApplication.allWidgets():
            try:
                if not isinstance(w, QtWidgets.QToolButton):
                    continue
                a = w.defaultAction()
                if a is None or a.objectName() != "FocusMode":
                    continue
                p = w.parent()
                found.append((w.isVisible(), p is not None and p.objectName() == "TextureSetsView", w))
            except RuntimeError:
                continue
        found.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return found[0][2] if found else None

    def set_focus_mode(self, on):
        """Turn Painter's Focus Mode on/off. Returns True on success, None if not found."""
        b = self.focus_button()
        if b is None:
            return None
        try:
            if b.isChecked() != on:
                b.click()
            return True
        except RuntimeError:
            return None

    def viewport_aspect(self):
        from PySide6 import QtWidgets
        best = None
        for w in QtWidgets.QApplication.allWidgets():
            try:
                if w.objectName() == "Viewer3D" and w.isVisible():
                    if best is None or w.width() * w.height() > best[0] * best[1]:
                        best = (w.width(), w.height())
            except RuntimeError:
                continue
        return best[0] / max(best[1], 1) if best else 1.0

    def sets(self):
        s = (self.data or {}).get("sets")
        return s if isinstance(s, dict) else {}

    def set_info(self, set_name):
        """Link entry for a Painter texture set (tolerates renamed punctuation)."""
        if not set_name:
            return None
        key = match_sets([set_name], self.sets()).get(set_name)
        return self.sets().get(key) if key else None

    def fit(self):
        """Mapping from link coordinates to Painter's scene, or None."""
        import substance_painter.project as pj
        keys = list(match_sets(self.texture_set_names(), self.sets()).values())
        if not keys:
            return None
        lo, hi = union_box(self.sets(), keys)
        if lo is None:
            return None
        bb = pj.get_scene_bounding_box()
        return fit_transform(lo, hi, bb.center, bb.dimensions)

    # --------------------------------------------------------------- link file
    def reload_link(self, force=False):
        try:
            mtime = os.path.getmtime(self.link_path)
        except OSError:
            if self.data is not None:
                self.data = None
                self.refresh_ui()
            return False
        if not force and mtime == self.mtime:
            return False
        data = read_link(self.link_path)
        if data is None:
            return False                    # being written: try again next tick
        self.mtime = mtime
        self.data = data
        self.refresh_ui()
        return True

    def _tick(self):
        try:
            before = (self.data or {}).get("environment")
            changed = self.reload_link()
            if not self.project_ready():
                return
            if changed and self.auto_light:
                if (self.data or {}).get("environment") != before:
                    self.apply_lighting(self.sent_set())     # new lighting: the object just sent
                else:                                        # e.g. a view-only send: stay on
                    self.apply_lighting()                    # the set being worked on
            if self.reload_lightmaps() or changed:
                self.refresh_lit_layers(auto=True)           # new Blender lighting
                self.refresh_ui()                            # "N waiting" on the button
            names = tuple(self.texture_set_names())
            if names != self.last_set_names:      # mesh reimported / sets added
                self.last_set_names = names
                self.refresh_ui()
            active = self.active_set_name()
            if active != self.last_active_set:
                self.last_active_set = active
                if self.soloing and self.follow and active:
                    self.frame_set(active)
                if self.auto_light:
                    self.apply_lighting()
                if self.widget is not None:
                    self.widget.select_set(active)
        except Exception as ex:      # never let the timer die
            self.log(f"error: {ex}", warn=True)

    # --------------------------------------------------------------- lighting
    def sent_set(self):
        """Painter name of the texture set Blender last sent (the one its lighting
        was baked around), or None."""
        want = (self.data or {}).get("active")
        if not want:
            return None
        for painter_name, key in match_sets(self.texture_set_names(), self.sets()).items():
            if key == want:
                return painter_name
        return None

    def environment_for(self, set_name):
        return pick_environment(self.data, self.set_info(set_name), self.light_mode)[0]

    @staticmethod
    def _settings():
        from PySide6 import QtCore
        return QtCore.QSettings("TexturePlugger", "BlenderLink")

    def _load_flag(self, key, default):
        try:
            v = self._settings().value(key, default)
        except Exception:
            return default
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return bool(v)

    def _save_flag(self, key, value):
        try:
            self._settings().setValue(key, bool(value))
        except Exception:
            pass

    def set_auto_relight(self, on):
        self.auto_relight = bool(on)
        self._save_flag("auto_relight", on)
        if on:
            self.refresh_lit_layers(auto=True)
        self.refresh_ui()

    def _load_mode(self):
        try:
            mode = str(self._settings().value("light_mode", "render"))
        except Exception:
            mode = "render"
        return mode if mode in LIGHT_MODES else "render"

    def has_preview(self):
        pv = (self.data or {}).get("preview")
        return isinstance(pv, dict) and bool(pv.get("environment"))

    def set_light_mode(self, mode):
        """Switch between Blender's render lighting and its Material Preview lighting."""
        if mode not in LIGHT_MODES:
            return False
        self.light_mode = mode
        try:
            self._settings().setValue("light_mode", mode)       # remembered next time
        except Exception:
            pass
        if mode == "preview" and self.data and not self.has_preview():
            self.log("Blender sent no Material Preview lighting: press Send to Painter "
                     "in Blender (the add-on sends it with the render lighting)", warn=True)
        self.apply_lighting(force=True)
        self.refresh_lit_layers(auto=True)  # baked layers follow, if you allowed that
        self.refresh_ui()
        return True

    def apply_lighting(self, set_name=None, force=False):
        import substance_painter.display as dp
        import substance_painter.resource as rs
        if not self.project_ready() or not self.data:
            return False
        if not match_sets(self.texture_set_names(), self.sets()):
            return False        # an unrelated project: never replace its lighting
        name = self.environment_for(set_name or self.active_set_name())
        if not name:
            return False
        if name == self.applied_env and not force:
            self.apply_tone_mapping()      # tone mapping may change on its own
            return True
        path = os.path.join(self.link_dir, name)
        if not os.path.isfile(path):
            self.log(f"lighting file missing: {name}", warn=True)
            return False
        url = self.env_cache.get(path)
        if url is None:
            res = None
            try:
                res = rs.import_session_resource(path, rs.Usage.ENVIRONMENT,
                                                 name="Blender " + os.path.splitext(name)[0],
                                                 group="Texture Plugger")
            except Exception:
                res = rs.import_project_resource(path, rs.Usage.ENVIRONMENT,
                                                 name="Blender " + os.path.splitext(name)[0],
                                                 group="Texture Plugger")
            url = res.identifier().url()
            self.env_cache[path] = url
        dp.set_environment_resource(rs.ResourceID.from_url(url))
        self.apply_tone_mapping()
        self.applied_env = name
        pv = (self.data or {}).get("preview")
        if isinstance(pv, dict) and pv.get("environment") == name:
            hdri = pv.get("hdri")
            self.log("Blender Material Preview lighting applied" + (f" ({hdri})" if hdri else ""))
        else:
            owner = next((n for n, i in self.sets().items()
                          if isinstance(i, dict) and i.get("environment") == name), None)
            self.log("Blender render lighting applied" + (f" (baked around '{owner}')" if owner else ""))
        return True

    # ------------------------------------------------- lighting in Base Color
    def _lit_record(self):
        """Texture set -> lighting map its "Blender Lighting" layer was given (saved
        with the project)."""
        import substance_painter.project as pj
        try:
            rec = pj.Metadata(META_CONTEXT).get(META_LIT_KEY)
        except Exception:
            rec = None
        return {str(k): str(v) for k, v in rec.items()} if isinstance(rec, dict) else {}

    def _save_lit_record(self, rec):
        import substance_painter.project as pj
        try:
            pj.Metadata(META_CONTEXT).set(META_LIT_KEY, dict(rec))
        except Exception as ex:
            self.log(f"could not remember the lit texture sets: {ex}", warn=True)

    @staticmethod
    def _stack(set_name):
        import substance_painter.textureset as ts
        return ts.TextureSet.from_name(set_name).get_stack()

    def light_layer(self, set_name):
        """The "Blender Lighting" layer of a texture set, or None."""
        import substance_painter.layerstack as ls
        try:
            nodes = ls.get_root_layer_nodes(self._stack(set_name))
        except Exception:
            return None
        for node in nodes:
            try:
                if node.get_name() == LIGHT_LAYER_NAME:
                    return node
            except Exception:
                continue
        return None

    def _filter_resource(self):
        """ResourceID of Painter's "Baked Lighting Environment" filter (starter assets)."""
        import substance_painter.resource as rs
        if self._filter_id is not None:
            return self._filter_id
        found = []
        try:
            found = rs.Resource.retrieve(rs.ResourceID(context="starter_assets", name=LIGHT_FILTER))
        except Exception:
            pass
        if not found:
            try:
                found = [r for r in rs.search(LIGHT_FILTER)
                         if LIGHT_FILTER in r.identifier().name.lower()]
            except Exception:
                found = []
        todo = list(found)
        while todo:              # the .sbsar package or the graph inside it
            r = todo.pop(0)
            try:
                if rs.Usage.FILTER in r.usages():
                    self._filter_id = r.identifier()
                    return self._filter_id
                todo.extend(r.children())
            except Exception:
                continue
        return None

    def reload_lightmaps(self):
        """Re-read blender_lightmaps.json when Blender rewrote it. Returns True if changed."""
        path = os.path.join(self.link_dir, LIGHTMAPS_FILE)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            changed = self.lightmaps is not None
            self.lightmaps, self.lm_mtime = None, None
            return changed
        if mtime == self.lm_mtime:
            return False
        data = read_link(path)
        if data is None:
            return False                     # being written: next tick
        self.lightmaps, self.lm_mtime = data, mtime
        self.refresh_ui()
        return True

    def lightmap_for(self, set_name):
        return pick_lightmap(self.lightmaps, set_name, self.light_mode)

    def lightmap_is_outdated(self, set_name):
        return lightmap_outdated(lightmap_entry(self.lightmaps, set_name), self.data)

    def lighting_for(self, set_name):
        """What lights a set's Base Color: its Cycles lightmap if Blender baked one,
        else its environment map (through Painter's filter)."""
        return self.lightmap_for(set_name) or self.environment_for(set_name)

    def project_lighting_dir(self):
        """Folder next to the saved .spp that keeps the plugin's own copies of the
        lighting maps, or None for a never-saved project."""
        import substance_painter.project as pj
        try:
            spp = pj.file_path() or ""
        except Exception:
            spp = ""
        return os.path.join(os.path.dirname(spp), PROJECT_LIGHT_DIR) if spp else None

    def _env_texture(self, env_name):
        """The Blender lighting map as a project texture.

        Painter reads an imported texture's file again when it saves the project.
        Blender deletes lighting maps nothing lists any more from the shared link
        folder, and a project still holding one then could not be saved at all
        ("null surface").  So the map is first copied into the project's own
        ``blender_lighting`` folder and imported from there.
        """
        import shutil
        import substance_painter.resource as rs
        path = os.path.join(self.link_dir, env_name)
        rid = self.tex_cache.get(path)
        if rid is not None:
            return rid
        res_name = LIGHT_RES_PREFIX + os.path.splitext(env_name)[0]
        try:
            have = rs.Resource.retrieve(rs.ResourceID.from_project(res_name))
        except Exception:
            have = []
        if have:
            rid = have[0].identifier()
        else:
            if not os.path.isfile(path):
                raise RuntimeError(f"lighting file missing: {env_name} (press Send to Painter "
                                   "in Blender)")
            src = path
            keep = self.project_lighting_dir()
            if keep:
                try:
                    os.makedirs(keep, exist_ok=True)
                    dst = os.path.join(keep, env_name)
                    if not os.path.isfile(dst):
                        shutil.copy2(path, dst)
                    src = dst
                except OSError as ex:
                    self.log(f"could not copy {env_name} next to the project ({ex}); "
                             "using the shared copy", warn=True)
            rid = rs.import_project_resource(src, rs.Usage.TEXTURE, name=res_name,
                                             group="Texture Plugger").identifier()
        self.tex_cache[path] = rid
        return rid

    def repair_lighting_resources(self):
        """Put a file back for every lighting map of this project whose file is gone.

        Painter re-reads them when saving and refuses to save the whole project if
        one is missing.  The set's current lightmap (or Blender's current lighting
        map) stands in at the old path; layers that still use it then simply show
        the current lighting.  Returns the number of files put back.
        """
        import shutil
        import substance_painter.resource as rs
        if not self.project_ready():
            return 0
        names = set()
        for query in (getattr(rs.StandardQuery, "PROJECT_RESOURCES", "s:project!"),
                      "Blender lighting", "lightmap_render", "env_"):
            try:
                for res in rs.search(query):
                    for n in (res.identifier().name, res.gui_name()):
                        if lighting_stem(n):
                            names.add(n)
            except Exception:
                pass
        try:
            for rid in rs.list_project_resources():
                if lighting_stem(rid.name):
                    names.add(rid.name)
        except Exception:
            pass
        self.reload_lightmaps()
        keep = self.project_lighting_dir()
        fixed, lost, ok = 0, [], 0
        for name in sorted(names):
            stem = lighting_stem(name)
            file_name = stem + ".exr"
            places = [os.path.join(self.link_dir, file_name)]
            if keep:
                places.append(os.path.join(keep, file_name))
            if any(os.path.isfile(p) for p in places):
                ok += 1
                continue
            stand_in = replacement_for(stem, self.lightmaps, self.data)
            src = os.path.join(self.link_dir, stand_in) if stand_in else ""
            if not src or not os.path.isfile(src):
                lost.append(stem)
                continue
            for dst in places:            # wherever Painter imported it from
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                except OSError:
                    pass
            fixed += 1
        self.log(f"lighting maps checked: {len(names)} in the project, {ok} present, "
                 f"{fixed} put back" + (f", {len(lost)} without a stand-in" if lost else ""),
                 warn=bool(lost))
        if lost:
            self.log(f"no stand-in for {short_list(lost, 3)}: saving may fail; press Bake "
                     "Lighting to UVs in Blender, then save again", warn=True)
        return fixed

    def _root_layer(self, set_name, layer_name):
        import substance_painter.layerstack as ls
        try:
            for node in ls.get_root_layer_nodes(self._stack(set_name)):
                if node.get_name() == layer_name:
                    return node
        except Exception:
            pass
        return None

    def _insert_position(self, set_name):
        """Top of the stack, or just below an existing Lit Paint layer (so its strokes
        are never lit a second time)."""
        import substance_painter.layerstack as ls
        lit_paint = self._root_layer(set_name, LIT_PAINT_LAYER_NAME)
        if lit_paint is not None:
            return ls.InsertPosition.below_node(lit_paint)
        return ls.InsertPosition.from_textureset_stack(self._stack(set_name))

    def _select_lit_paint(self, set_name, paint):
        import substance_painter.layerstack as ls
        if paint is not None and set_name == self.active_set_name():
            try:
                ls.set_selected_nodes([paint])       # ready to paint above the lighting
            except Exception:
                pass

    def _apply_light_layer(self, set_name, source):
        """Create or update a set's "Blender Lighting" layer from a Cycles lightmap
        or an environment map. Returns True if the layer was created."""
        if is_lightmap(source):
            return self._apply_lightmap_layer(set_name, source)
        return self._apply_filter_layer(set_name, source)

    def _ensure_float_base(self, set_name):
        """Base Color in 16-bit float, so light brighter than 1 survives the Multiply
        (in 8 bit the lightmap would be clipped to 1 first and sunlit areas come out
        too dark).  Exports to PNG still clip the final colour, as they should."""
        import substance_painter.textureset as ts
        stack = self._stack(set_name)
        ch = stack.get_channel(ts.ChannelType.BaseColor)
        fmt = str(ch.format()).rsplit(".", 1)[-1]
        if not fmt.endswith("F"):
            stack.edit_channel(ts.ChannelType.BaseColor, ts.ChannelFormat.RGB16F)
            self.log(f"'{set_name}': Base Color switched to 16-bit float (was {fmt}) for the "
                     "baked lighting")

    def _apply_lightmap_layer(self, set_name, lightmap):
        """Fill layer, Base Color only, Multiply: albedo x Cycles lighting."""
        import substance_painter.layerstack as ls
        import substance_painter.textureset as ts
        rid = self._env_texture(lightmap)
        layer = self.light_layer(set_name)
        if layer is not None and not isinstance(layer, ls.FillLayerNode):
            ls.delete_node(layer)                    # was the filter version
            layer = None
        self._ensure_float_base(set_name)
        created = paint = None
        with ls.ScopedModification("Blender lighting"):
            if layer is None:
                layer = ls.insert_fill(self._insert_position(set_name))
                layer.set_name(LIGHT_LAYER_NAME)
                layer.active_channels = {ts.ChannelType.BaseColor}
                layer.set_blending_mode(ls.BlendingMode.Multiply, ts.ChannelType.BaseColor)
                created = True
            src = layer.set_source(ts.ChannelType.BaseColor, rid)
            try:        # the EXR holds linear light values
                spaces = src.list_available_color_spaces()
                lin = next((c for c in spaces if "linear" in str(c).lower()
                            or "raw" in str(c).lower()), None)
                if lin is not None:
                    src.set_color_space(lin)
            except Exception:
                pass
            if self._root_layer(set_name, LIT_PAINT_LAYER_NAME) is None:
                paint = ls.insert_paint(ls.InsertPosition.above_node(layer))
                paint.set_name(LIT_PAINT_LAYER_NAME)
        self._select_lit_paint(set_name, paint)
        return bool(created)

    def _apply_filter_layer(self, set_name, env_name):
        """Passthrough layer with Painter's Baked Lighting Environment filter."""
        import substance_painter.layerstack as ls
        import substance_painter.textureset as ts
        fres = self._filter_resource()
        if fres is None:
            raise RuntimeError("Painter's 'Baked Lighting Environment' filter is not in the shelf")
        env_id = self._env_texture(env_name)
        layer = self.light_layer(set_name)
        if layer is not None and isinstance(layer, ls.FillLayerNode):
            ls.delete_node(layer)                    # was the lightmap version
            layer = None
        created = paint = None
        passthrough = passthrough_mode(ls.BlendingMode)
        with ls.ScopedModification("Blender lighting"):
            if layer is None:
                layer = ls.insert_paint(self._insert_position(set_name))
                layer.set_name(LIGHT_LAYER_NAME)
                created = True
            # Passthrough: the filter works on everything below this layer.  Set on
            # every press, so a layer left half-made by an earlier failed press
            # (Normal blending, no filter) is repaired, not reused as it is.
            if layer.get_blending_mode(ts.ChannelType.BaseColor) != passthrough:
                layer.set_blending_mode(passthrough, ts.ChannelType.BaseColor)
            filt = next((e for e in layer.content_effects()
                         if isinstance(e, ls.FilterEffectNode)), None)
            new_filter = filt is None
            if new_filter:
                filt = ls.insert_filter_effect(
                    ls.InsertPosition.inside_node(layer, ls.NodeStack.Content), fres)
                try:
                    filt.active_channels = {ts.ChannelType.BaseColor}
                except Exception:
                    pass
            src = filt.get_source()
            src.set_source("environment_map", env_id)
            if new_filter and FILTER_YAW_TURNS:
                try:     # later rotation tweaks made in Painter are kept
                    src.set_parameters({"horizontal_rotation": float(FILTER_YAW_TURNS)})
                except Exception:
                    pass
            if self._root_layer(set_name, LIT_PAINT_LAYER_NAME) is None:
                paint = ls.insert_paint(ls.InsertPosition.above_node(layer))
                paint.set_name(LIT_PAINT_LAYER_NAME)
        self._select_lit_paint(set_name, paint)
        return bool(created)

    def _missing_mesh_maps(self, set_name):
        """Mesh maps the filter uses that the set doesn't have yet.

        World Space Normal is required (which way each pixel faces); AO is used
        when present.
        """
        import substance_painter.textureset as ts
        try:
            t = ts.TextureSet.from_name(set_name)
            return [u for u in (ts.MeshMapUsage.WorldSpaceNormal, ts.MeshMapUsage.AO)
                    if t.get_mesh_map_resource(u) is None]
        except Exception:
            return []

    def _has_world_normals(self, set_name):
        import substance_painter.textureset as ts
        return ts.MeshMapUsage.WorldSpaceNormal not in self._missing_mesh_maps(set_name)

    def _bake_mesh_maps(self, set_name, missing):
        """Bake only the ``missing`` mesh maps of one set (asynchronous).

        Maps the set already has are not rebaked; the baker selection is put
        back when the bake ends.
        """
        import substance_painter.baking as bk
        import substance_painter.textureset as ts
        t = ts.TextureSet.from_name(set_name)
        params = bk.BakingParameters.from_texture_set(t)
        before = list(params.get_enabled_bakers())
        params.set_enabled_bakers(list(missing))
        self._baking = set_name
        self._bake_restore = (set_name, before)
        self._baked_once.add(set_name)
        try:
            bk.bake_async(t)
        except Exception:
            self._restore_bakers()
            self._baking = None
            raise
        self.log(f"baking the mesh maps the lighting needs for '{set_name}' first...")

    def _restore_bakers(self):
        import substance_painter.baking as bk
        import substance_painter.textureset as ts
        restore, self._bake_restore = self._bake_restore, None
        if not restore:
            return
        try:
            params = bk.BakingParameters.from_texture_set(ts.TextureSet.from_name(restore[0]))
            params.set_enabled_bakers(restore[1])
        except Exception:
            pass

    def bake_lighting(self, set_names=None):
        """Bake Lighting: light each set's Base Color with its Blender lighting.

        Sets without baked mesh maps get them baked first (one at a time); the
        layer is added when their bake ends.
        """
        if not self.project_ready():
            self.log("open a project first", warn=True)
            return False
        self.reload_lightmaps()
        if not self.data and not self.lightmaps:
            self.log("no Blender lighting yet: select the object in Blender and press "
                     "Send to Painter (or Bake Lighting to UVs)", warn=True)
            return False
        names = [n for n in (set_names or [self.active_set_name()]) if n]
        if not names:
            return False
        if self._baking is not None:
            self._lit_queue.extend(n for n in names if n not in self._lit_queue)
            self.log(f"queued after the bake of '{self._baking}'")
            return True
        self._lit_queue = list(names)
        self._baked_once.clear()          # a new press may bake again
        self._continue_lit_queue()
        return True

    def _continue_lit_queue(self):
        rec = self._lit_record()
        done, borrowed, outdated = [], [], []
        while self._lit_queue:
            name = self._lit_queue.pop(0)
            env = self.lighting_for(name)
            if not env:
                self.log(f"no Blender lighting for '{name}': press Send to Painter in Blender",
                         warn=True)
                continue
            missing = [] if is_lightmap(env) else self._missing_mesh_maps(name)
            if missing and name not in self._baked_once:
                try:
                    self._bake_mesh_maps(name, missing)
                    self._lit_queue.insert(0, name)      # light it when the bake ends
                    break
                except Exception as ex:
                    self.log(f"could not bake mesh maps for '{name}': {ex}", warn=True)
                    continue
            if not is_lightmap(env) and not self._has_world_normals(name):   # baked once: stop
                self.log(f"'{name}' has no World Space Normal mesh map: bake it in Painter's "
                         "Baking mode, then press Bake Lighting again", warn=True)
                continue
            try:
                self._apply_light_layer(name, env)
            except Exception as ex:
                self.log(f"could not bake the lighting into '{name}': {ex}", warn=True)
                continue
            if is_lightmap(env) and self.lightmap_is_outdated(name):
                outdated.append(name)
            rec[name] = env
            done.append(name)
            info = self.set_info(name)
            if (not is_lightmap(env) and self.light_mode == "render"
                    and not (isinstance(info, dict) and info.get("environment"))):
                borrowed.append(name)
        self._save_lit_record(rec)
        if done:
            msg = (f"lighting baked into the Base Color of {', '.join(done[:3])}"
                   f"{f' and {len(done) - 3} more' if len(done) > 3 else ''}. Paint on "
                   f"'{LIT_PAINT_LAYER_NAME}' and view the Base Color channel (C) to see it flat")
            if borrowed:
                msg += (f". {len(borrowed)} set(s) have no lighting of their own and use the "
                        "last object sent (Send to Painter on them in Blender to fix)")
            if outdated:
                msg += (f". The Cycles lightmap of {', '.join(outdated[:3])} is older than the "
                        "lighting Blender sent last: press Bake Lighting to UVs in Blender again")
            self.log(msg, warn=bool(outdated))
        self.refresh_ui()

    def _on_baking_ended(self, e):
        from PySide6 import QtCore
        name, self._baking = self._baking, None
        if name is None:
            return                     # a bake the user started: not ours
        self._restore_bakers()
        try:
            import substance_painter.ui as ui
            ui.switch_to_mode(ui.UIMode.Edition)
        except Exception:
            pass
        status = str(getattr(e, "status", ""))
        if not status.endswith("Success"):
            self.log(f"mesh map bake for '{name}' did not finish ({status}): lighting not added",
                     warn=True)
            self._lit_queue = [n for n in self._lit_queue if n != name]

        def go():                      # after Painter is back in edit mode
            try:
                self._continue_lit_queue()
            except Exception as ex:
                self.log(f"could not bake the lighting: {ex}", warn=True)
        QtCore.QTimer.singleShot(0, go)

    def bake_lighting_all(self):
        """Bake Lighting for every linked texture set (asks first on big scenes)."""
        names = list(match_sets(self.texture_set_names(), self.sets()))
        if len(names) > 25:
            from PySide6 import QtWidgets
            import substance_painter.ui as ui
            ans = QtWidgets.QMessageBox.question(
                ui.get_main_window(), "Bake Lighting",
                f"Add a lighting layer (and bake missing mesh maps) for {len(names)} texture "
                "sets? This can take a long time and makes the project heavier.")
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return False
        return self.bake_lighting(names)

    def remove_lighting(self, set_names=None):
        """Delete the "Blender Lighting" layer (the Lit Paint layer and its paint stay)."""
        import substance_painter.layerstack as ls
        if not self.project_ready():
            return False
        rec = self._lit_record()
        removed = []
        for name in [n for n in (set_names or [self.active_set_name()]) if n]:
            layer = self.light_layer(name)
            if layer is not None:
                try:
                    ls.delete_node(layer)
                    removed.append(name)
                except Exception as ex:
                    self.log(f"could not remove the lighting of '{name}': {ex}", warn=True)
                    continue
            rec.pop(name, None)
        self._save_lit_record(rec)
        self.log(f"baked lighting removed from {', '.join(removed)}" if removed
                 else "no baked lighting layer on that texture set")
        self.refresh_ui()
        return bool(removed)

    def layer_names(self, set_name):
        """Names of a texture set's root layers (top first), or [] if unreadable."""
        import substance_painter.layerstack as ls
        try:
            return [n.get_name() for n in ls.get_root_layer_nodes(self._stack(set_name))]
        except Exception:
            return []

    def light_layer_kind(self, set_name):
        """"lightmap", "filter" or None: what the set's Blender Lighting layer is."""
        rec = self._lit_record().get(set_name)
        if self.light_layer(set_name) is None:
            return None
        return "lightmap" if is_lightmap(rec) else "filter"

    def project_saved_time(self):
        """Modification time of the open project's .spp (0 if never saved)."""
        import substance_painter.project as pj
        try:
            path = pj.file_path() or ""
            return os.path.getmtime(path) if path else 0.0
        except Exception:
            return 0.0

    def blender_base_color_times(self, set_names):
        """Set -> time of the Base Color PNG Blender shows (sets without one left out)."""
        folder = self.export_dir()
        try:
            listing = os.listdir(folder) if folder else []
        except OSError:
            listing = []
        out = {}
        for name in set_names:
            f = blender_texture_files(folder, name, listing).get("BaseColor")
            if f and not is_placeholder_png(f):
                try:
                    out[name] = os.path.getmtime(f)
                except OSError:
                    pass
        return out

    def lit_set_names(self):
        return [n for n in self.texture_set_names() if self.light_layer(n) is not None]

    def borrows_lighting(self, set_name):
        """True when a set has no lighting of its own (no environment sent for it,
        no Cycles lightmap) and would be lit with the last object sent: wrong place,
        often too blue or too bright.  Such layers are never relit automatically."""
        if self.light_mode != "render" or self.lightmap_for(set_name):
            return False
        return not (self.set_info(set_name) or {}).get("environment")

    def pending_relight(self):
        """(sets whose baked lighting layer shows older lighting than Blender sent,
        those of them whose layer would change kind).  Cheap: no layer access."""
        if not self.project_ready() or (not self.data and not self.lightmaps):
            return [], []
        rec = self._lit_record()
        if not rec:
            return [], []
        wanted = {n: self.lighting_for(n) for n in rec}
        return ([n for n in lit_updates(rec, wanted) if not self.borrows_lighting(n)],
                [n for n in lit_kind_swaps(rec, wanted) if not self.borrows_lighting(n)])

    def refresh_lit_layers(self, names=None, auto=False, allow_swaps=False):
        """Give lit sets the lighting Blender sent last (and the mode shown).

        ``auto``: called because Blender sent something; does nothing unless
        "update automatically" is on.  A layer that would change kind (lightmap <->
        filter, which deletes it with its settings) is only replaced with
        ``allow_swaps`` (an explicit, confirmed press)."""
        if auto and not self.auto_relight:
            return
        if not self.project_ready() or self._baking is not None:
            return
        if not self.data and not self.lightmaps:
            return
        rec = self._lit_record()
        if not rec:
            return
        wanted = {n: self.lighting_for(n) for n in rec}
        swaps = set(lit_kind_swaps(rec, wanted))
        changed = []
        for name in lit_updates(rec, wanted):
            if names is not None and name not in names:
                continue
            if name in swaps and not allow_swaps:
                continue
            if self.borrows_lighting(name):
                continue        # would take another object's lighting: Bake Lighting asks
            if self.light_layer(name) is None:
                rec.pop(name, None)            # layer deleted by hand
                continue
            try:
                self._apply_light_layer(name, wanted[name])
                rec[name] = wanted[name]
                changed.append(name)
            except Exception as ex:
                self.log(f"could not update the baked lighting of '{name}': {ex}", warn=True)
        if changed or len(rec) != len(self._lit_record()):
            self._save_lit_record(rec)
        if changed:
            self.log(f"baked lighting updated in {len(changed)} texture set(s)")

    def write_lit_markers(self, textures):
        """Tell Blender which exported sets carry the lighting in their Base Color.

        ``textures`` is Painter's export result: {(set, stack): [files]}.  Writes
        ``texture_plugger_lit.json`` next to the files.
        """
        rec = self._lit_record()
        by_dir = {}
        for key, files in (textures or {}).items():
            set_name = key[0] if isinstance(key, (tuple, list)) else str(key)
            files = [str(f) for f in (files or [])]
            if not files:
                continue
            layer = self.light_layer(set_name)
            try:
                lit = layer is not None and layer.is_visible()
            except Exception:
                lit = False
            info = {"lit": bool(lit), "base_color": base_color_stem(set_name, files)}
            if lit and rec.get(set_name):
                info["source"] = "cycles lightmap" if is_lightmap(rec[set_name]) else "filter"
                info["environment"] = rec[set_name]
                info["mode"] = self.light_mode
            by_dir.setdefault(os.path.dirname(os.path.abspath(files[0])), {})[set_name] = info
        for folder, exported in by_dir.items():
            path = os.path.join(folder, LIT_MARKER)
            old = read_link(path)
            if old is None and not any(i["lit"] for i in exported.values()):
                continue                       # nothing lit, now or before: no file
            try:
                write_json(path, merge_lit_marker(old, exported))
            except OSError as ex:
                self.log(f"could not write {LIT_MARKER}: {ex}", warn=True)

    def _on_export_ended(self, e):
        try:
            self.write_lit_markers(getattr(e, "textures", None))
        except Exception as ex:
            self.log(f"could not mark the lit texture sets for Blender: {ex}", warn=True)

    # ------------------------------------------------------------------ export
    def export_dir(self):
        import substance_painter.export as ex
        import substance_painter.project as pj
        project = mesh = default = ""
        try:
            project = pj.file_path() or ""
        except Exception:
            pass
        try:
            mesh = pj.last_imported_mesh_path() or ""
        except Exception:
            pass
        try:
            default = ex.get_default_export_path() or ""
        except Exception:
            pass
        return blender_textures_dir(project, mesh, default)

    def export_to_blender(self, only_set=None):
        """Export with the settings Blender's Texture Plugger expects (Blender
        template, PNG) into the folder Blender watches; Blender reloads them."""
        import substance_painter.export as ex
        import substance_painter.resource as rs
        if not self.project_ready():
            self.log("open a project first", warn=True)
            return False
        out = self.export_dir()
        if not out:
            self.log("no Texture Plugger 'textures' folder for this project: open it from "
                     "Blender (Edit in Substance Painter) or save it in its texture set folder",
                     warn=True)
            return False
        saved = self.save_project(out)   # Painter keeps the layers only in the .spp
        names = [only_set] if only_set else self.texture_set_names()
        preset = rs.ResourceID(context="starter_assets", name=BLENDER_PRESET).url()
        try:
            res = ex.export_project_textures(blender_export_config(out, names, preset))
        except Exception as exc:
            self.log(f"export failed: {exc}", warn=True)
            return False
        if res.status != ex.ExportStatus.Success:
            self.log(f"export {res.status.name.lower()}: {res.message}", warn=True)
            return False
        try:      # also done by the export event; repeated here in case it isn't sent
            self.write_lit_markers(res.textures)
        except Exception as exc:
            self.log(f"could not mark the lit texture sets for Blender: {exc}", warn=True)
        n = sum(len(v) for v in res.textures.values())
        lit = [k[0] for k in res.textures if self.light_layer(k[0]) is not None]
        self.log(f"exported {n} texture(s) for Blender to {out}"
                 + (f" ({len(lit)} set(s) with baked lighting: Blender shows them unlit)" if lit else "")
                 + (f"; project saved ({os.path.basename(saved)})" if saved else ""))
        return True

    def save_project(self, export_dir):
        """Save the Painter project before exporting, so it never falls behind the
        textures Blender shows.  A never-saved project is saved into its texture
        set folder (next to ``textures``), where Blender's Edit in Painter finds it.
        Returns the saved path, or "" when nothing was saved."""
        import glob
        import substance_painter.project as pj
        try:
            path = pj.file_path() or ""
            if path:
                if not pj.needs_saving():
                    return ""
                pj.save()
                return path
            folder = os.path.dirname(os.path.normpath(export_dir))
            existing = [os.path.basename(f) for f in glob.glob(os.path.join(glob.escape(folder), "*.spp"))]
            path = project_save_path(folder, existing)
            pj.save_as(path)
            return path
        except Exception as exc:          # busy, read-only...: still export
            self.log(f"could not save the project before exporting: {exc}. Save it with "
                     "Ctrl+S", warn=True)
            return ""

    # --------------------------------------------------------- load from Blender
    def _from_blender_texture(self, path):
        """A Blender texture as a project resource (saved inside the .spp). The file's
        time is in the name, so a newer export is imported again, not reused."""
        import substance_painter.resource as rs
        try:
            stamp = int(os.path.getmtime(path))
        except OSError:
            stamp = 0
        res_name = f"From Blender {os.path.splitext(os.path.basename(path))[0]} {stamp}"
        try:
            have = rs.Resource.retrieve(rs.ResourceID.from_project(res_name))
        except Exception:
            have = []
        if have:
            return have[0].identifier()
        return rs.import_project_resource(path, rs.Usage.TEXTURE, name=res_name,
                                          group="Texture Plugger").identifier()

    def load_from_blender(self, set_names=None):
        """Put the textures Blender shows back into Painter: one "From Blender" fill
        layer per texture set (Base Color, Roughness, Metallic, Emission, Alpha) from
        the files in the folder Blender watches.  For a set with baked lighting it
        goes above "Blender Lighting" (below "Lit Paint"), so it is not lit twice."""
        import substance_painter.layerstack as ls
        import substance_painter.textureset as ts
        if not self.project_ready():
            self.log("open a project first", warn=True)
            return False
        folder = self.export_dir()
        if not folder:
            self.log("no Texture Plugger 'textures' folder for this project: open it from "
                     "Blender (Edit in Substance Painter)", warn=True)
            return False
        try:
            listing = os.listdir(folder)
        except OSError as exc:
            self.log(f"cannot read {folder}: {exc}", warn=True)
            return False
        marker = read_link(os.path.join(folder, LIT_MARKER)) or {}
        lit_sets = {k for k, v in (marker.get("sets") or {}).items()
                    if isinstance(v, dict) and v.get("lit")}
        names = [n for n in (set_names or self.texture_set_names()) if n]
        done, missing, lit_warn, failed, blank = [], [], [], [], []
        for name in names:
            try:
                stack_channels = list(self._stack(name).all_channels())
            except Exception:
                missing.append(name)              # no such texture set
                continue
            files = channels_for_files(blender_texture_files(folder, name, listing),
                                       ts.ChannelType, stack_channels)
            real = {ch: f for ch, f in files.items() if not is_placeholder_png(f)}
            if files and not real:
                blank.append(name)                # nothing painted/exported yet
                continue
            files = real
            if not files:
                missing.append(name)
                continue
            try:
                ids = {ch: self._from_blender_texture(f) for ch, f in files.items()}
                with ls.ScopedModification("Load from Blender"):
                    layer = self._root_layer(name, FROM_BLENDER_LAYER_NAME)
                    if layer is None:
                        layer = ls.insert_fill(self._insert_position(name))
                        layer.set_name(FROM_BLENDER_LAYER_NAME)
                    layer.active_channels = set(ids)
                    for ch, rid in ids.items():
                        layer.set_source(ch, rid)
            except Exception as exc:
                failed.append(f"{name} ({exc})")
                continue
            done.append(name)
            if name in lit_sets and self.light_layer(name) is None:
                lit_warn.append(name)
        msg = (f"loaded Blender's textures into {len(done)} texture set(s) "
               f"(layer '{FROM_BLENDER_LAYER_NAME}')") if done else "nothing loaded from Blender"
        if missing:
            msg += f"; no exported textures for {', '.join(missing[:3])}" + (
                f" and {len(missing) - 3} more" if len(missing) > 3 else "")
        if blank:
            msg += (f"; skipped {short_list(blank, 3)}: Blender only has blank placeholder "
                    "textures for them (nothing exported from Painter yet)")
        if lit_warn:
            msg += (f". {', '.join(lit_warn[:3])}: Blender's Base Color has lighting baked in "
                    "but this project has no Blender Lighting layer; press Bake Lighting so "
                    "the next export is marked lit again")
        if failed:
            msg += ". Failed: " + "; ".join(failed[:3])
        self.log(msg, warn=bool(missing or lit_warn or failed or blank or not done))
        self.refresh_ui()
        return bool(done)

    # ------------------------------------------------------------------ camera
    def frame_set(self, set_name):
        """Point the camera at one texture set (keeps the viewing direction)."""
        import substance_painter.display as dp
        import substance_painter.project as pj
        cam = dp.Camera.get_default_camera()
        fwd, _up = basis_from_euler(cam.rotation)
        info = self.set_info(set_name)
        fit = self.fit()
        if info and fit and info.get("min") is not None:
            lo = apply_fit(fit, info["min"])
            hi = apply_fit(fit, info["max"])
            center, radius = box_center_radius(lo, hi)
        else:
            bb = pj.get_scene_bounding_box()
            center, radius = list(bb.center), bb.radius
            if set_name:
                self.log(f"no Blender bounds for '{set_name}': framed the whole scene "
                         "(press Send to Painter in Blender)", warn=True)
        pos, dist = frame_position(center, radius, fwd, cam.field_of_view, self.viewport_aspect())
        cam.position = pos
        try:
            cam.focus_distance = dist
        except Exception:
            pass
        if cam.projection_type == dp.CameraProjectionType.Orthographic:
            cam.orthographic_height = radius * 2 * FRAME_MARGIN
        return True

    def focus_center(self, set_name=None):
        """(centre, radius) of the texture set being worked on, in Painter's scene."""
        import substance_painter.project as pj
        info = self.set_info(set_name or self.active_set_name())
        fit = self.fit()
        if info and fit and info.get("min") is not None:
            return box_center_radius(apply_fit(fit, info["min"]), apply_fit(fit, info["max"]))
        bb = pj.get_scene_bounding_box()
        return list(bb.center), bb.radius

    def orbit(self, yaw=0.0, pitch=0.0, zoom=1.0, set_name=None):
        """Orbit and/or zoom about the focused object's centre (not the cursor)."""
        import substance_painter.display as dp
        if not self.project_ready():
            return False
        cam = dp.Camera.get_default_camera()
        center, _r = self.focus_center(set_name)
        pos, _f = orbit_around(cam.position, center, yaw, pitch)
        if zoom != 1.0:
            pos = zoom_about(pos, center, zoom)
            if cam.projection_type == dp.CameraProjectionType.Orthographic:
                cam.orthographic_height = max(1e-3, cam.orthographic_height * zoom)
        fwd = _norm([center[i] - pos[i] for i in range(3)])
        cam.position = pos
        cam.rotation = euler_from_basis(fwd)
        try:
            cam.focus_distance = math.sqrt(sum((pos[i] - center[i]) ** 2 for i in range(3)))
        except Exception:
            pass
        return True

    def match_view(self):
        import substance_painter.display as dp
        view = (self.data or {}).get("view")
        fit = self.fit()
        if not view or not fit:
            self.log("no Blender view yet: press Send to Painter in Blender", warn=True)
            return False
        cam = dp.Camera.get_default_camera()
        cam.projection_type = (dp.CameraProjectionType.Orthographic if view.get("ortho")
                               else dp.CameraProjectionType.Perspective)
        cam.field_of_view = float(view.get("fov_v", 40.0))      # Painter's FOV is vertical
        cam.position = apply_fit(fit, view["position"])
        cam.rotation = euler_from_basis(view["forward"], view["up"])
        if view.get("ortho"):
            cam.orthographic_height = float(view.get("ortho_height", 1.0)) * fit[0]
        self.log("camera matched to the Blender view")
        return True

    # -------------------------------------------------------------------- solo
    def solo(self, set_name=None):
        import substance_painter.textureset as ts
        if not self.project_ready():
            return False
        set_name = set_name or self.active_set_name()
        if not set_name:
            return False
        try:
            ts.set_active_stack(ts.Stack.from_name(set_name))
        except Exception as ex:
            self.log(f"cannot select '{set_name}': {ex}", warn=True)
            return False
        from PySide6 import QtCore

        def isolate():       # one event-loop turn after the set change has landed
            if self.soloing and self.set_focus_mode(True) is None:
                self.log("Painter's Focus Mode button was not found: click Focus Mode in "
                         "the Texture Set List to hide the other sets", warn=True)
        QtCore.QTimer.singleShot(0, isolate)
        self.soloing = True
        self.last_active_set = set_name
        self.frame_set(set_name)
        if self.auto_light:
            self.apply_lighting(set_name)
        self.log(f"solo '{set_name}' (Alt + drag on it to orbit)")
        self.refresh_ui()
        return True

    def unsolo(self, frame_all=True):
        import substance_painter.display as dp
        import substance_painter.project as pj
        self.set_focus_mode(False)
        self.soloing = False
        self.log("showing all texture sets")
        if frame_all and self.project_ready():
            cam = dp.Camera.get_default_camera()
            fwd, _ = basis_from_euler(cam.rotation)
            bb = pj.get_scene_bounding_box()
            cam.position, _d = frame_position(bb.center, bb.radius, fwd, cam.field_of_view,
                                              self.viewport_aspect())
        self.refresh_ui()

    # ------------------------------------------------------------------ events
    def _on_about_to_save(self, _e):
        try:
            self.repair_lighting_resources()
        except Exception as ex:          # never block a save
            self.log(f"could not check the lighting maps before saving: {ex}", warn=True)

    def _on_project_ready(self, _e):
        try:
            self.repair_lighting_resources()
        except Exception as ex:
            self.log(f"could not check the lighting maps: {ex}", warn=True)
        self.env_cache.clear()
        self.tex_cache.clear()
        self._lit_queue = []
        self._baking = None
        self._bake_restore = None
        self._baked_once.clear()
        self.applied_env = None
        self.soloing = False
        # already "seen": the first tick must not swap to the first set's lighting
        self.last_active_set = self.active_set_name()
        self.reload_link(force=True)
        if self.auto_light:
            try:
                # Painter opens on its first texture set; light the one Blender sent
                self.apply_lighting(self.sent_set())
            except Exception as ex:
                self.log(f"could not apply lighting: {ex}", warn=True)
        self.refresh_ui()
        from PySide6 import QtCore

        def setup():
            try:
                self.apply_scene_setup()
            except Exception as ex:
                self.log(f"scene setup failed: {ex}", warn=True)
        QtCore.QTimer.singleShot(1500, setup)      # let Painter finish loading the mesh
        QtCore.QTimer.singleShot(4000, self.run_todo)

    def run_todo(self):
        """One-time jobs left for this project in ``painter_todo.json`` (link folder).

        ``{"project": <.spp path>, "bake_lighting": true, "save": true}``: give
        every linked set that has a Cycles lightmap its Blender Lighting layer
        (replacing an older layer of the other kind), then save.  Used to hand
        over a project "ready to paint" after a bake made while Painter was
        closed.  Only a request naming the open project runs, and only once.
        """
        import substance_painter.project as pj
        path = os.path.join(self.link_dir, TODO_FILE)
        todo = read_link(path)
        if not isinstance(todo, dict) or not self.project_ready():
            return False
        try:
            here = os.path.normcase(os.path.abspath(pj.file_path() or ""))
        except Exception:
            here = ""
        if not here or os.path.normcase(os.path.abspath(str(todo.get("project") or ""))) != here:
            return False
        try:
            os.remove(path)                     # once, even if something below fails
        except OSError:
            pass
        self.log("running the job Blender left for this project...")
        if todo.get("bake_lighting"):
            self.reload_lightmaps()
            names = [n for n in match_sets(self.texture_set_names(), self.sets())
                     if self.lightmap_for(n)]
            self.bake_lighting(names)
        if todo.get("save"):
            try:
                pj.save()
                self.log(f"project saved with baked lighting in {len(self.lit_set_names())} "
                         "texture set(s): ready to paint on 'Lit Paint'")
            except Exception as ex:
                self.log(f"could not save the project: {ex}. Save it with Ctrl+S", warn=True)
        self.refresh_ui()
        return True

    def apply_tone_mapping(self):
        """Use the Painter tone mapping that matches Blender's view transform.

        Blender's AgX/Filmic roll bright light off; Painter's default "Linear" clips
        it, so a room lit by strong lamps would look blown out. Not available in
        colour-managed (OCIO/ACEScg) projects, which handle this themselves.
        """
        import substance_painter.display as dp
        want = (self.data or {}).get("tone_mapping")
        if not want:
            return False
        try:
            fn = getattr(dp.ToneMappingFunction, str(want))
            if dp.get_tone_mapping() != fn:
                dp.set_tone_mapping(fn)
            return True
        except Exception:      # e.g. colour-managed projects: never break the lighting
            return False

    def apply_scene_setup(self):
        """New whole-scene project from Blender: size its texture sets and save it.

        Blender picks a texture size that keeps the whole scene within the
        computer's memory and a project file per part; both are applied once,
        to a project that has never been saved, so later choices made in
        Painter are never overridden.
        """
        import substance_painter.project as pj
        import substance_painter.textureset as ts
        sc = (self.data or {}).get("scene")
        if not isinstance(sc, dict) or not self.project_ready():
            return False
        try:
            if pj.file_path():
                return False
        except Exception:
            return False
        names = self.texture_set_names()
        linked = match_sets(names, self.sets())
        if not linked:
            return False
        done = []
        try:
            res = int(sc.get("resolution") or 0)
        except (TypeError, ValueError):
            res = 0
        if res >= 128:
            try:
                todo = [t for t in ts.all_texture_sets()
                        if ts_name(t) in linked and t.get_resolution().width != res]
                if todo:
                    ts.set_resolutions(todo, ts.Resolution(res, res))
                done.append(f"{len(linked)} texture sets at {res} px")
            except Exception as ex:
                self.log(f"could not set the texture size: {ex}", warn=True)
        proj = sc.get("project")
        if proj and not os.path.exists(proj):
            try:
                pj.save_as(proj)
                done.append(f"saved as {os.path.basename(proj)}")
            except Exception as ex:
                self.log(f"could not save the project: {ex}", warn=True)
        if sc.get("parts", 1) and int(sc.get("parts", 1)) > 1:
            done.insert(0, f"part {sc.get('part')} of {sc.get('parts')}")
        if done:
            self.log("scene ready: " + ", ".join(done))
        return True

    def _on_project_closing(self, _e):
        self.soloing = False
        self.applied_env = None
        self.tex_cache.clear()
        self._lit_queue = []
        self._baking = None
        self._bake_restore = None
        self._baked_once.clear()

    def refresh_ui(self):
        if self.widget is not None:
            try:
                self.widget.refresh()
            except RuntimeError:
                pass

    def close(self):
        import substance_painter.event as ev
        self.timer.stop()
        for event, fn in self._handlers:
            try:
                ev.DISPATCHER.disconnect(event, fn)
            except Exception:
                pass


# ---- Panel art: small badges painted in code (no image files to install) ----

BLENDER_ORANGE = "#F5792A"
PAINTER_RED = "#E8475F"
KIND_COLORS = {"light": "#F2C94C", "texture": "#4FB3E8", "view": "#9AA5B1"}
ICON_W, ICON_H = 78, 26


def rgba(hex_color, alpha):
    """'rgba(r,g,b,a)' for style sheets (Qt reads 8-digit hex as #AARRGGBB, not
    #RRGGBBAA, so alpha is never appended to a hex colour)."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{int(alpha)})"


def _paint_glyph(p, kind, x, y, s):
    """A little picture of WHAT a button moves: a light bulb, a texture swatch or an eye."""
    from PySide6 import QtCore, QtGui
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    if kind == "light":
        p.setPen(QtGui.QPen(QtGui.QColor("#FFE9A0"), 1.4))
        cx, cy = x + s * 0.5, y + s * 0.38
        for a in range(0, 360, 45):                     # rays
            if 100 < a < 260:
                continue
            r = math.radians(a)
            p.drawLine(QtCore.QPointF(cx + math.cos(r) * s * 0.36, cy - math.sin(r) * s * 0.36),
                       QtCore.QPointF(cx + math.cos(r) * s * 0.48, cy - math.sin(r) * s * 0.48))
        p.setPen(QtGui.QPen(QtGui.QColor("#B8901C"), 1.2))
        p.setBrush(QtGui.QColor("#FFD54A"))
        p.drawEllipse(QtCore.QPointF(cx, cy), s * 0.27, s * 0.27)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor("#A7AFB8"))
        p.drawRoundedRect(QtCore.QRectF(cx - s * 0.13, cy + s * 0.25, s * 0.26, s * 0.2), 1.5, 1.5)
    elif kind == "texture":
        cols = ("#4FC3F7", "#F06292", "#AED581", "#FFB74D")
        t = s * 0.38
        ox, oy = x + s * 0.12, y + s * 0.12
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(ox, oy, 2 * t, 2 * t), 3, 3)
        p.setClipPath(path)
        p.setPen(QtCore.Qt.NoPen)
        for i, c in enumerate(cols):
            p.setBrush(QtGui.QColor(c))
            p.drawRect(QtCore.QRectF(ox + (i % 2) * t, oy + (i // 2) * t, t, t))
        p.setClipping(False)
        p.setPen(QtGui.QPen(QtGui.QColor("#FFFFFF"), 1.6))   # a brush stroke across it
        p.drawLine(QtCore.QPointF(ox + 2 * t - 1, oy + 1), QtCore.QPointF(ox + t * 0.9, oy + t * 1.1))
    elif kind == "view":
        p.setPen(QtGui.QPen(QtGui.QColor("#CFD8DC"), 1.5))
        p.setBrush(QtCore.Qt.NoBrush)
        path = QtGui.QPainterPath()
        path.moveTo(x + s * 0.08, y + s * 0.5)
        path.quadTo(x + s * 0.5, y + s * 0.1, x + s * 0.92, y + s * 0.5)
        path.quadTo(x + s * 0.5, y + s * 0.9, x + s * 0.08, y + s * 0.5)
        p.drawPath(path)
        p.setBrush(QtGui.QColor("#4FB3E8"))
        p.drawEllipse(QtCore.QPointF(x + s * 0.5, y + s * 0.5), s * 0.15, s * 0.15)
    p.restore()


def _paint_app(p, app, x, y, h):
    """A round orange "B" (Blender) or a red "Pt" tile (Painter).  Returns its width."""
    from PySide6 import QtCore, QtGui
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    f = QtGui.QFont()
    f.setBold(True)
    f.setPixelSize(int(h * 0.55))
    p.setFont(f)
    p.setPen(QtCore.Qt.NoPen)
    if app == "blender":
        w = h
        p.setBrush(QtGui.QColor(BLENDER_ORANGE))
        p.drawEllipse(QtCore.QRectF(x, y, w, h))
        text = "B"
    else:
        w = h * 1.15
        p.setBrush(QtGui.QColor(PAINTER_RED))
        p.drawRoundedRect(QtCore.QRectF(x, y, w, h), 4, 4)
        text = "Pt"
    p.setPen(QtGui.QColor("white"))
    p.drawText(QtCore.QRectF(x, y, w, h), QtCore.Qt.AlignCenter, text)
    p.restore()
    return w


def _paint_arrow(p, x0, x1, y):
    from PySide6 import QtCore, QtGui
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtGui.QPen(QtGui.QColor("#E8EAED"), 2.0, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
    p.drawLine(QtCore.QPointF(x0, y), QtCore.QPointF(x1 - 3, y))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor("#E8EAED"))
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(x1, y), QtCore.QPointF(x1 - 5, y - 4),
                                   QtCore.QPointF(x1 - 5, y + 4)]))
    p.restore()


def badge_pixmap(direction, kind=None, w=ICON_W, h=ICON_H, scale=2.0):
    """[what] [from] -> [to]: e.g. a bulb, then Blender -> Painter."""
    from PySide6 import QtCore, QtGui
    pm = QtGui.QPixmap(int(w * scale), int(h * scale))
    pm.setDevicePixelRatio(scale)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    x = 0.0
    if kind:
        _paint_glyph(p, kind, 0, 1, h - 2)
        x = h + 1
    bh = h - 8
    by = 4
    if direction == "b2p":
        x += _paint_app(p, "blender", x, by, bh) + 2
        _paint_arrow(p, x, x + 11, h / 2)
        _paint_app(p, "painter", x + 13, by, bh)
    elif direction == "p2b":
        x += _paint_app(p, "painter", x, by, bh) + 2
        _paint_arrow(p, x, x + 11, h / 2)
        _paint_app(p, "blender", x + 13, by, bh)
    else:
        _paint_app(p, "painter", x, by, bh)
    p.end()
    return pm


def _make_panel(bridge):
    from PySide6 import QtCore, QtGui, QtWidgets

    class Panel(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("TexturePluggerBlenderLink")
            self.setWindowTitle("Blender Link")
            self.buttons = {}            # action id -> button
            self.needs_selection = set() # action ids that act on the selected set
            self._busy = False
            self._dont_ask = set()       # confirmations turned off until restart
            self._icons = {}

            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(4, 4, 4, 4)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            inner = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(inner)
            lay.setContentsMargins(2, 2, 2, 2)
            lay.setSpacing(8)
            scroll.setWidget(inner)
            outer.addWidget(scroll, 1)

            # ---- link state and the legend of the badges
            self.info = QtWidgets.QLabel()
            self.info.setWordWrap(True)
            lay.addWidget(self.info)
            legend = QtWidgets.QHBoxLayout()
            for direction in ("b2p", "p2b"):
                pic = QtWidgets.QLabel()
                pic.setPixmap(badge_pixmap(direction, None, 50, 22))
                legend.addWidget(pic)
                t = QtWidgets.QLabel(DIRECTION_WORDS[direction])
                t.setStyleSheet("color: #BFC5CC;")
                legend.addWidget(t)
                legend.addSpacing(10)
            legend.addStretch(1)
            lay.addLayout(legend)

            # ---- texture sets
            self.list = QtWidgets.QListWidget()
            self.list.setMinimumHeight(110)
            self.list.setMaximumHeight(240)
            self.list.setToolTip("Pick the texture set the 'Selected' buttons work on. "
                                 "Double-click to solo it and frame it")
            self.list.itemDoubleClicked.connect(lambda it: bridge.solo(it.data(QtCore.Qt.UserRole)))
            self.list.currentItemChanged.connect(lambda *_: self.update_actions())
            lay.addWidget(self.list)
            self.target = QtWidgets.QLabel()
            self.target.setWordWrap(True)
            self.target.setTextFormat(QtCore.Qt.RichText)
            self.target.setStyleSheet("padding: 4px 6px; border-radius: 4px; "
                                      "background: rgba(255,255,255,0.06);")
            lay.addWidget(self.target)

            # ---- LIGHTING
            box = self._section(lay, "light", "LIGHTING",
                                "How Blender's light looks on your paint. Only light moves "
                                "here: your painted textures are never touched.")
            self._button(box, "show_light", "Show Blender Lighting in Viewport",
                         "Painter's viewport environment becomes the lighting Blender sent "
                         "(nothing in your layers changes)",
                         lambda: bridge.apply_lighting(self.selected(), force=True))
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("Light from Blender's:"))
            self.mode_render = QtWidgets.QRadioButton("Render")
            self.mode_render.setToolTip("Blender's scene lighting (world + lamps) baked around "
                                        "the object, like Blender's Rendered view")
            self.mode_preview = QtWidgets.QRadioButton("Material Preview")
            self.mode_preview.setToolTip("The studio HDRI of Blender's Material Preview, with "
                                         "its rotation and strength (often bluer or greener "
                                         "than your scene)")
            group = QtWidgets.QButtonGroup(self)
            group.addButton(self.mode_render)
            group.addButton(self.mode_preview)
            self.mode_render.toggled.connect(lambda v: v and self.do_mode("render"))
            self.mode_preview.toggled.connect(lambda v: v and self.do_mode("preview"))
            row.addWidget(self.mode_render)
            row.addWidget(self.mode_preview)
            row.addStretch(1)
            box.addLayout(row)
            self._button(box, "bake_light", "Bake Lighting into Selected Set",
                         "Adds a 'Blender Lighting' layer to the selected set's Base Color "
                         "and an empty 'Lit Paint' layer above it to paint lit colours on. "
                         "Bakes missing World Space Normal / AO mesh maps first. Exports then "
                         "show it unlit in Blender",
                         lambda: self.do_bake(False), selection=True)
            self._button(box, "bake_all", "Bake Lighting into All Linked Sets…",
                         "Same for every texture set Blender linked (asks first)",
                         lambda: self.do_bake(True))
            self._button(box, "update_light", "Update Baked Lighting",
                         "Blender sent new lighting: give the baked lighting layers that "
                         "lighting. Asks first, and says which layers would lose settings",
                         self.do_update)
            self._button(box, "remove_light", "Remove Baked Lighting from Selected…",
                         "Deletes the selected set's 'Blender Lighting' layer (your Lit Paint "
                         "layer and its paint stay). Asks first",
                         self.do_remove, selection=True, danger=True)
            self.auto = QtWidgets.QCheckBox("Show new Blender lighting in the viewport automatically")
            self.auto.setChecked(bridge.auto_light)
            self.auto.toggled.connect(self._auto)
            box.addWidget(self.auto)
            self.auto_relight = QtWidgets.QCheckBox("Also update baked lighting layers automatically")
            self.auto_relight.setToolTip(
                "Off (safer): your 'Blender Lighting' layers keep their lighting until you press "
                "Update Baked Lighting. On: they follow every Send to Painter from Blender "
                "(a layer that would change kind is still never replaced without asking)")
            self.auto_relight.setChecked(bridge.auto_relight)
            self.auto_relight.toggled.connect(bridge.set_auto_relight)
            box.addWidget(self.auto_relight)

            # ---- TEXTURES
            box = self._section(lay, "texture", "TEXTURES",
                                "Your painted maps (Base Color, Roughness, Metallic...). "
                                "Lighting is not moved here.")
            self._button(box, "send_sel", "Send Selected Set to Blender…",
                         "Exports the selected set's textures (Blender template, PNG) into "
                         "the folder Blender watches; Blender reloads them. Saves the "
                         "project first", lambda: self.do_send(False), selection=True)
            self._button(box, "send_all", "Send All Sets to Blender…",
                         "Same for every texture set", lambda: self.do_send(True))
            self._button(box, "get_sel", "Get Selected Set from Blender…",
                         "Brings the textures Blender shows back in as a 'From Blender' fill "
                         "layer. Warns first if it would cover your paint or if Painter's work "
                         "is newer", lambda: self.do_get(False), selection=True)
            self._button(box, "get_all", "Get All Sets from Blender…",
                         "Same for every texture set", lambda: self.do_get(True))

            # ---- VIEW (folds away)
            box = self._section(lay, "view", "VIEW", "Camera only: nothing in your file "
                                "changes.", foldable=True)
            self._button(box, "match_view", "Match Blender Camera",
                         "Puts Painter's camera where your Blender viewport is",
                         bridge.match_view)
            row = QtWidgets.QHBoxLayout()
            self._button(row, "solo", "Solo", "Show only the selected texture set and frame it",
                         lambda: bridge.solo(self.selected()), selection=True, small=True)
            self._button(row, "frame", "Frame", "Frame the selected texture set without hiding "
                         "the others", lambda: bridge.frame_set(self.selected()),
                         selection=True, small=True)
            self._button(row, "show_all", "Show All", "Leave solo and frame the whole scene",
                         lambda: bridge.unsolo(), small=True)
            box.addLayout(row)
            grid = QtWidgets.QGridLayout()
            for text, tip, col, args in (
                    ("←", "Orbit left around the object", 0, dict(yaw=-15.0)),
                    ("↑", "Orbit up around the object", 1, dict(pitch=10.0)),
                    ("↓", "Orbit down around the object", 2, dict(pitch=-10.0)),
                    ("→", "Orbit right around the object", 3, dict(yaw=15.0)),
                    ("Zoom +", "Move closer to the object", 4, dict(zoom=0.8)),
                    ("Zoom -", "Move away from the object", 5, dict(zoom=1.25))):
                b = QtWidgets.QPushButton(text)
                b.setToolTip(tip)
                b.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
                b.clicked.connect(lambda _=False, a=args: bridge.orbit(set_name=self.selected(), **a))
                grid.addWidget(b, 0, col)
            box.addLayout(grid)
            self.follow = QtWidgets.QCheckBox("While soloing, frame the texture set I pick")
            self.follow.setChecked(bridge.follow)
            self.follow.toggled.connect(lambda v: setattr(bridge, "follow", v))
            box.addWidget(self.follow)
            hint = QtWidgets.QLabel("Arrows orbit around the selected object's centre. "
                                    "Alt + left-drag orbits around the point under the cursor.")
            hint.setWordWrap(True)
            hint.setEnabled(False)
            box.addWidget(hint)
            lay.addStretch(0)

            # ---- status: always visible, outside the scroll area
            self.status = QtWidgets.QLabel()
            self.status.setWordWrap(True)
            self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            outer.addWidget(self.status)
            self.set_status("")
            self.refresh()

        # ------------------------------------------------------------ building
        def _section(self, parent, kind, title, subtitle, foldable=False):
            color = KIND_COLORS[kind]
            frame = QtWidgets.QFrame()
            frame.setObjectName(f"tpSection_{kind}")
            frame.setStyleSheet(
                f"QFrame#tpSection_{kind} {{ border: 1px solid {rgba(color, 90)}; border-left: "
                f"4px solid {color}; border-radius: 5px; background: {rgba(color, 22)}; }}")
            v = QtWidgets.QVBoxLayout(frame)
            v.setContentsMargins(8, 6, 8, 8)
            v.setSpacing(5)
            head = QtWidgets.QHBoxLayout()
            pic = QtWidgets.QLabel()
            pm = QtGui.QPixmap(48, 48)
            pm.setDevicePixelRatio(2.0)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            _paint_glyph(p, kind, 0, 0, 24)
            p.end()
            pic.setPixmap(pm)
            head.addWidget(pic)
            t = QtWidgets.QLabel(title)
            f = t.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() * 1.15)
            t.setFont(f)
            t.setStyleSheet(f"color: {color}; border: none; background: transparent;")
            head.addWidget(t)
            head.addStretch(1)
            body = QtWidgets.QWidget()
            body.setObjectName("tpBody")
            body.setStyleSheet("QWidget#tpBody { background: transparent; }")
            bv = QtWidgets.QVBoxLayout(body)
            bv.setContentsMargins(0, 0, 0, 0)
            bv.setSpacing(5)
            if foldable:
                tb = QtWidgets.QToolButton()
                tb.setCheckable(True)
                tb.setChecked(True)
                tb.setArrowType(QtCore.Qt.DownArrow)
                tb.setAutoRaise(True)
                tb.setToolTip("Fold / unfold")

                def fold(on, tb=tb, body=body):
                    body.setVisible(on)
                    tb.setArrowType(QtCore.Qt.DownArrow if on else QtCore.Qt.RightArrow)
                tb.toggled.connect(fold)
                head.addWidget(tb)
            v.addLayout(head)
            sub = QtWidgets.QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet("color: #AEB4BB; border: none; background: transparent;")
            bv.addWidget(sub)
            v.addWidget(body)
            parent.addWidget(frame)
            return bv

        def _section_icon(self, kind):
            pm = QtGui.QPixmap(48, 48)
            pm.setDevicePixelRatio(2.0)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            _paint_glyph(p, kind, 0, 0, 24)
            p.end()
            return QtGui.QIcon(pm)

        def _icon(self, action):
            if action not in self._icons:
                direction, kind = ACTIONS[action]
                self._icons[action] = QtGui.QIcon(badge_pixmap(direction, kind))
            return self._icons[action]

        def _button(self, parent, action, text, tip, fn, selection=False, danger=False,
                    small=False):
            direction, kind = ACTIONS[action]
            b = QtWidgets.QPushButton(text)
            if not small:
                b.setIcon(self._icon(action))
                b.setIconSize(QtCore.QSize(ICON_W, ICON_H))
                b.setMinimumHeight(ICON_H + 8)
            b.setToolTip(f"<b>{DIRECTION_WORDS[direction]}</b><br>{tip}")
            style = "QPushButton { text-align: left; padding: 3px 8px; }"
            if danger:
                style += (" QPushButton { color: #FF8A80; border: 1px solid "
                          f"{rgba('#FF8A80', 110)}; border-radius: 3px; }} "
                          "QPushButton:disabled { color: #777; }")
            b.setStyleSheet(style)
            b.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            queue_ok = action in QUEUE_OK
            b.clicked.connect(lambda _=False: self.run(fn, queue_ok))
            parent.addWidget(b)
            self.buttons[action] = b
            if selection:
                self.needs_selection.add(action)
            return b

        # ----------------------------------------------------------- safety net
        def run(self, fn, queue_ok=False):
            """One action at a time; buttons are greyed out while it runs, so a
            double click or an impatient second press is ignored.  ``queue_ok``:
            Bake Lighting queues behind a mesh-map bake that is still running."""
            if self._busy:
                return
            if bridge._baking is not None and not queue_ok:
                bridge.log(f"Painter is still baking mesh maps for '{bridge._baking}': wait "
                           "until it finishes", warn=True)
                return
            self._busy = True
            self.update_actions()
            try:
                fn()
            except Exception as ex:
                bridge.log(f"error: {ex}", warn=True)
            finally:
                QtCore.QTimer.singleShot(500, self._unbusy)

        def _unbusy(self):
            self._busy = False
            self.update_actions()

        def ask(self, title, text, details="", key=None, danger=False, ok="Continue"):
            """Yes/no box; Cancel is the default, so Enter or Esc never does damage.
            ``key``: offers "don't ask again" (only for safe, routine questions)."""
            if key and not danger and key in self._dont_ask:
                return True
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Warning if danger
                        else QtWidgets.QMessageBox.Question)
            box.setWindowTitle(title)
            box.setText(text)
            if details:
                box.setInformativeText(details)
            yes = box.addButton(ok, QtWidgets.QMessageBox.AcceptRole)
            no = box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
            box.setDefaultButton(no)
            box.setEscapeButton(no)
            cb = None
            if key and not danger:
                cb = QtWidgets.QCheckBox("Don't ask again until Painter restarts")
                box.setCheckBox(cb)
            box.exec()
            if box.clickedButton() is not yes:
                bridge.log("cancelled: nothing changed")
                return False
            if cb is not None and cb.isChecked():
                self._dont_ask.add(key)
            return True

        def _target_names(self, all_sets, name=None):
            if all_sets:
                return bridge.texture_set_names()
            name = name or self.selected()
            if not name:
                bridge.log("pick a texture set in the list first", warn=True)
                return []
            return [name]

        # -------------------------------------------------------------- actions
        def do_send(self, all_sets, name=None):
            names = self._target_names(all_sets, name)
            if not names:
                return
            out = bridge.export_dir()
            if not out:
                bridge.export_to_blender(None if all_sets else names[0])   # explains why
                return
            what = f"all {len(names)} texture sets" if all_sets else f"'{names[0]}'"
            details = (f"This replaces the PNG files Blender uses for {what} in:\n{out}\n\n"
                       "Your Painter project is saved first. Blender reloads the new files.")
            lit = [n for n in names if bridge.light_layer(n) is not None]
            if lit:
                details += (f"\n\n{short_list(lit)} {'has' if len(lit) == 1 else 'have'} baked "
                            "lighting: Blender will show them unlit (the light is in the colour).")
            if not self.ask("Send textures to Blender",
                            f"Painter → Blender: send the textures of {what}?",
                            details, key="send_all" if all_sets else "send_sel",
                            ok="Send to Blender"):
                return
            bridge.export_to_blender(None if all_sets else names[0])

        def do_get(self, all_sets, name=None):
            names = self._target_names(all_sets, name)
            if not names:
                return
            times = bridge.blender_base_color_times(names)
            if not times:
                bridge.load_from_blender(names)                   # explains why not
                return
            cover = [n for n in names if n in times and load_would_cover(bridge.layer_names(n))]
            stale = older_than(times, bridge.project_saved_time())
            what = f"{len(times)} texture sets" if all_sets else f"'{names[0]}'"
            details = ("Adds (or updates) a 'From Blender' fill layer with Base Color, "
                       "Roughness, Metallic, Emission and Alpha. None of your layers is "
                       "deleted, and Ctrl+Z undoes it.")
            if cover:
                details += (f"\n\n⚠ In {len(cover)} set(s) ({short_list(cover)}) the new "
                            "layer goes ABOVE your own layers and hides your paint until you "
                            "hide or delete it.")
            if stale:
                details += (f"\n\n⚠ Blender's textures of {short_list(stale)} are OLDER "
                            "than your last Painter save: Painter probably has the newer work. "
                            "Did you mean Send to Blender?")
            danger = bool(cover or stale)
            if not self.ask("Get textures from Blender",
                            f"Blender → Painter: load Blender's textures into {what}?",
                            details, key=None if danger else "get", danger=danger,
                            ok="Load anyway" if danger else "Load"):
                return
            bridge.load_from_blender(names)

        def _kind_changes(self, names):
            out = []
            for n in names:
                have = bridge.light_layer_kind(n)
                want = bridge.lighting_for(n)
                if have and want and (have == "lightmap") != is_lightmap(want):
                    out.append(n)
            return out

        def do_bake(self, all_sets, name=None):
            if all_sets:
                names = list(match_sets(bridge.texture_set_names(), bridge.sets()))
            else:
                names = self._target_names(False, name)
            if not names:
                bridge.log("no texture set here is linked to Blender: press Send to Painter "
                           "in Blender", warn=True)
                return
            swaps = self._kind_changes(names)
            borrowed = [n for n in names
                        if not bridge.lightmap_for(n) and bridge.light_mode == "render"
                        and not (bridge.set_info(n) or {}).get("environment")]
            details = ""
            if swaps:
                details += (f"⚠ {short_list(swaps)} already {'has' if len(swaps) == 1 else 'have'} "
                            "a Blender Lighting layer of another kind (Cycles lightmap vs "
                            "environment filter). It is deleted and made again, so changes you "
                            "made to it (sliders, opacity, masks) are lost.\n\n")
            if borrowed:
                owner = (bridge.data or {}).get("active") or "the last object sent"
                details += (f"{short_list(borrowed)} {'has' if len(borrowed) == 1 else 'have'} no "
                            f"lighting of its own and will be lit as if it stood where "
                            f"'{owner}' is, which can look too blue or too bright. For its own "
                            "lighting select it in Blender and press Send to Painter (or Bake "
                            "Lighting to UVs) first.")
            if all_sets:
                if not self.ask("Bake Lighting",
                                f"Blender → Painter: add baked lighting to {len(names)} "
                                "texture sets?",
                                (details + "\n\n" if details else "") +
                                "Missing mesh maps are baked first: this can take a long "
                                "time and makes the project heavier.",
                                danger=bool(swaps), ok="Bake all"):
                    return
                bridge.bake_lighting(names)
                return
            if details and not self.ask("Bake Lighting",
                                        f"Blender → Painter: bake lighting into '{names[0]}'?",
                                        details, danger=bool(swaps),
                                        key=None if swaps else "borrowed", ok="Bake"):
                return
            bridge.bake_lighting(names)

        def do_update(self):
            updates, swaps = bridge.pending_relight()
            borrowed = [x for x in bridge._lit_record() if bridge.borrows_lighting(x)]
            if not updates:
                bridge.log("baked lighting is already up to date"
                           + (f" ({short_list(borrowed)}: no lighting of their own, left as they "
                              "are; Send them to Painter from Blender, then Bake Lighting)"
                              if borrowed else ""))
                return
            details = "Sets: " + short_list(updates, 8)
            if borrowed:
                details += (f"\n\nLeft as they are: {short_list(borrowed)} (no lighting of "
                            "their own; they would get another object's lighting).")
            if swaps:
                details += (f"\n\n⚠ {short_list(swaps)}: the layer changes kind (Cycles "
                            "lightmap vs environment filter) and is made again, so changes you "
                            "made to it are lost. Ctrl+Z undoes it.")
            if not self.ask("Update baked lighting",
                            f"Blender → Painter: give {len(updates)} baked lighting "
                            "layer(s) the lighting Blender sent last?",
                            details, danger=bool(swaps), key=None if swaps else "update",
                            ok="Update"):
                return
            bridge.refresh_lit_layers(allow_swaps=True)
            self.refresh()

        def do_remove(self, name=None):
            names = self._target_names(False, name)
            if not names:
                return
            if bridge.light_layer(names[0]) is None:
                bridge.log(f"'{names[0]}' has no Blender Lighting layer")
                return
            if not self.ask("Remove baked lighting",
                            f"Delete the 'Blender Lighting' layer of '{names[0]}'?",
                            "Your 'Lit Paint' layer and its paint stay. Settings you changed on "
                            "the lighting layer are lost (Ctrl+Z brings it back). Export again "
                            "so Blender shows the set lit normally.",
                            danger=True, ok="Delete layer"):
                return
            bridge.remove_lighting(names)

        def do_mode(self, mode):
            bridge.set_light_mode(mode)
            updates, _swaps = bridge.pending_relight()
            if updates and not bridge.auto_relight:
                bridge.log(f"viewport shows Blender's {'Material Preview' if mode == 'preview' else 'Render'} "
                           f"lighting; {len(updates)} baked lighting layer(s) still show the "
                           "old lighting: press Update Baked Lighting to change them")

        def _auto(self, v):
            bridge.auto_light = v
            if v:
                bridge.apply_lighting(force=True)

        # ---------------------------------------------------------------- state
        def selected(self):
            it = self.list.currentItem()
            return it.data(QtCore.Qt.UserRole) if it is not None else bridge.active_set_name()

        def select_set(self, name):
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.data(QtCore.Qt.UserRole) == name:
                    self.list.setCurrentItem(it)
                    return

        def set_status(self, text, warn=False):
            self.status.setText(text)
            if not text:
                self.status.setStyleSheet("")
                return
            c = "#F2994A" if warn else "#6FCF97"
            self.status.setStyleSheet(f"padding: 4px 6px; border-radius: 4px; color: {c}; "
                                      f"border: 1px solid {rgba(c, 110)};")

        def update_actions(self):
            ready = bridge.project_ready()
            busy = self._busy or bridge._baking is not None
            sel = self.selected() if ready else None
            for action, b in self.buttons.items():
                blocked = self._busy or (bridge._baking is not None and action not in QUEUE_OK)
                ok = ready and not blocked and (bool(sel) or action not in self.needs_selection)
                b.setEnabled(ok)
            if ready and sel:
                kind = bridge.light_layer_kind(sel)
                self.buttons["remove_light"].setEnabled(not busy and kind is not None)
                tags = []
                if kind == "lightmap":
                    tags.append("\U0001F4A1 baked lighting (Cycles lightmap)")
                elif kind == "filter":
                    tags.append("\U0001F4A1 baked lighting (environment filter)")
                if bridge.set_info(sel) is None:
                    tags.append("not linked to Blender")
                elif not (bridge.set_info(sel) or {}).get("environment") and not bridge.lightmap_for(sel):
                    tags.append("no lighting of its own")
                sep = " • "
                self.target.setText(f"Selected: <b>{sel}</b>"
                                    + (f"<br><span style='color:#AEB4BB'>{sep.join(tags)}</span>"
                                       if tags else ""))
            else:
                self.target.setText("Selected: <i>none</i> (pick a texture set above)"
                                    if ready else "<i>Open a project to use these buttons</i>")
            updates, swaps = bridge.pending_relight() if ready else ([], [])
            b = self.buttons["update_light"]
            b.setText(f"Update Baked Lighting ({len(updates)} waiting)" if updates
                      else "Update Baked Lighting (up to date)")
            if not updates:
                b.setEnabled(False)
            b.setStyleSheet("QPushButton { text-align: left; padding: 3px 8px; }"
                            + (" QPushButton:enabled { border: 1px solid #F2C94C; "
                               "border-radius: 3px; color: #F2C94C; }" if updates else ""))
            self.auto_relight.blockSignals(True)
            self.auto_relight.setChecked(bridge.auto_relight)
            self.auto_relight.blockSignals(False)

        def refresh(self):
            d = bridge.data
            if not d:
                self.info.setText("No link from Blender yet. In Blender select your object "
                                  "and press Send to Painter (Texture Plugger panel).")
            else:
                env = d.get("environment") or "none"
                blend = d.get("blend") or "unsaved .blend"
                self.info.setText(f"Linked to <b>{blend}</b>. Lighting: "
                                  f"{'yes' if env != 'none' else 'not sent'}"
                                  f"{' (soloing)' if bridge.soloing else ''}")
            for b, mode in ((self.mode_render, "render"), (self.mode_preview, "preview")):
                b.blockSignals(True)
                b.setChecked(bridge.light_mode == mode)
                b.blockSignals(False)
            self.mode_preview.setEnabled(bridge.has_preview() or bridge.light_mode == "preview")
            current = self.selected() if self.list.count() else None
            self.list.blockSignals(True)
            self.list.clear()
            names = bridge.texture_set_names() if bridge.project_ready() else []
            linked = bridge.sets()
            keys = match_sets(names, linked)
            lit = set(bridge._lit_record()) if names else set()
            for n in names:
                tag = []
                if n in keys:
                    if linked[keys[n]].get("environment"):
                        tag.append("own lighting")
                else:
                    tag.append("not in Blender link")
                if bridge.lightmap_for(n):
                    tag.append("Cycles lightmap (outdated)" if bridge.lightmap_is_outdated(n)
                               else "Cycles lightmap")
                if n in lit:
                    tag.append("baked lighting")
                it = QtWidgets.QListWidgetItem(("\U0001F4A1 " if n in lit else "     ") + n
                                               + (f"   [{', '.join(tag)}]" if tag else ""))
                it.setData(QtCore.Qt.UserRole, n)
                self.list.addItem(it)
            self.select_set(current or bridge.active_set_name())
            self.list.blockSignals(False)
            self.update_actions()

    return Panel()


_bridge = None
_ui_elements = []


def _build_menu(menu, panel):
    """The Texture Plugger menu: the same guarded actions as the panel, grouped."""
    from PySide6 import QtGui, QtWidgets
    b = _bridge
    active = b.active_set_name
    subs = []

    def submenu(kind, title):
        # Parented and kept: a bare addMenu() result can be collected by Python,
        # leaving an empty menu entry.
        sub = QtWidgets.QMenu(title, menu)
        sub.setIcon(panel._section_icon(kind))
        menu.addMenu(sub)
        subs.append(sub)
        return sub

    def add(sub, action, text, fn):
        # Menus are 16 px high: the section's picture as icon, the direction in words.
        if action:
            direction, kind = ACTIONS[action]
            text = f"{text}    ({DIRECTION_WORDS[direction]})"
        act = QtGui.QAction(text, sub)
        act.triggered.connect(lambda _=False: panel.run(fn, action in QUEUE_OK))
        sub.addAction(act)

    light = submenu("light", "Lighting")
    add(light, "show_light", "Show Blender Lighting in Viewport", lambda: b.apply_lighting(force=True))
    add(light, "bake_light", "Bake Lighting into Active Texture Set",
        lambda: panel.do_bake(False, active()))
    add(light, "bake_all", "Bake Lighting into All Linked Texture Sets…",
        lambda: panel.do_bake(True))
    add(light, "update_light", "Update Baked Lighting…", panel.do_update)
    light.addSeparator()
    add(light, None, "Light from Blender's Render", lambda: panel.do_mode("render"))
    add(light, None, "Light from Blender's Material Preview", lambda: panel.do_mode("preview"))
    light.addSeparator()
    add(light, "remove_light", "Remove Baked Lighting from Active Texture Set…",
        lambda: panel.do_remove(active()))

    tex = submenu("texture", "Textures")
    add(tex, "send_sel", "Send Active Texture Set to Blender…",
        lambda: panel.do_send(False, active()))
    add(tex, "send_all", "Send All to Blender…", lambda: panel.do_send(True))
    tex.addSeparator()
    add(tex, "get_sel", "Get Active Texture Set from Blender…",
        lambda: panel.do_get(False, active()))
    add(tex, "get_all", "Get All from Blender…", lambda: panel.do_get(True))

    view = submenu("view", "View")
    add(view, "match_view", "Match Blender Camera", lambda: b.match_view())
    add(view, "solo", "Solo Active Texture Set", lambda: b.solo())
    add(view, "frame", "Frame Active Texture Set", lambda: b.frame_set(active()))
    add(view, "show_all", "Show All", lambda: b.unsolo())
    view.addSeparator()
    for text, args in (("Orbit Left", dict(yaw=-15.0)), ("Orbit Right", dict(yaw=15.0)),
                       ("Orbit Up", dict(pitch=10.0)), ("Orbit Down", dict(pitch=-10.0)),
                       ("Zoom In", dict(zoom=0.8)), ("Zoom Out", dict(zoom=1.25))):
        add(view, None, text, lambda a=args: b.orbit(**a))
    menu._tp_submenus = subs
    return subs


def start_plugin():
    global _bridge
    from PySide6 import QtWidgets
    import substance_painter.ui as ui
    _bridge = Bridge()
    _bridge.log(f"plugin {PLUGIN_VERSION} started")
    panel = _make_panel(_bridge)
    _bridge.widget = panel
    dock = ui.add_dock_widget(panel)
    _ui_elements.append(dock)

    menu = QtWidgets.QMenu("Texture Plugger", ui.get_main_window())
    _build_menu(menu, panel)
    ui.add_menu(menu)
    _ui_elements.append(menu)
    _bridge.menu = menu
    _bridge.reload_link(force=True)
    _bridge.timer.start()
    if _bridge.project_ready():
        _bridge._on_project_ready(None)


def close_plugin():
    global _bridge
    import substance_painter.ui as ui
    if _bridge is not None:
        _bridge.close()
    for el in _ui_elements:
        try:
            ui.delete_ui_element(el)
        except Exception:
            pass
    _ui_elements.clear()
    _bridge = None


if __name__ == "__main__":
    start_plugin()
