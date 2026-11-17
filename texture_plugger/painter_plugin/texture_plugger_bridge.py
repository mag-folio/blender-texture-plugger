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

Everything comes from ``../../texture_plugger_link/blender_link.json`` (next
to the ``python`` folder), written by Blender's "Send to Painter" button.

The maths at the top has no Painter imports so it can be tested with plain
Python.
"""

import json
import math
import os

PLUGIN_VERSION = "1.6.0"
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
            self.widget.set_status(msg)

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
                self.refresh_lit_layers()                    # new Blender lighting: relight
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
        self.refresh_lit_layers()          # baked layers follow the lighting shown
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

    def _env_texture(self, env_name):
        """The Blender lighting map as a project texture (saved inside the .spp, so
        the layer keeps working after Blender replaces or prunes the file)."""
        import substance_painter.resource as rs
        path = os.path.join(self.link_dir, env_name)
        rid = self.tex_cache.get(path)
        if rid is not None:
            return rid
        res_name = "Blender lighting " + os.path.splitext(env_name)[0]
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
            rid = rs.import_project_resource(path, rs.Usage.TEXTURE, name=res_name,
                                             group="Texture Plugger").identifier()
        self.tex_cache[path] = rid
        return rid

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

    def lit_set_names(self):
        return [n for n in self.texture_set_names() if self.light_layer(n) is not None]

    def refresh_lit_layers(self):
        """Give lit sets the lighting Blender sent last (and the mode shown)."""
        if not self.project_ready() or self._baking is not None:
            return
        if not self.data and not self.lightmaps:
            return
        rec = self._lit_record()
        if not rec:
            return
        wanted = {n: self.lighting_for(n) for n in rec}
        changed = []
        for name in lit_updates(rec, wanted):
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
        done, missing, lit_warn, failed = [], [], [], []
        for name in names:
            try:
                stack_channels = list(self._stack(name).all_channels())
            except Exception:
                missing.append(name)              # no such texture set
                continue
            files = channels_for_files(blender_texture_files(folder, name, listing),
                                       ts.ChannelType, stack_channels)
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
        if lit_warn:
            msg += (f". {', '.join(lit_warn[:3])}: Blender's Base Color has lighting baked in "
                    "but this project has no Blender Lighting layer; press Bake Lighting so "
                    "the next export is marked lit again")
        if failed:
            msg += ". Failed: " + "; ".join(failed[:3])
        self.log(msg, warn=bool(missing or lit_warn or failed or not done))
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
    def _on_project_ready(self, _e):
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


def _make_panel(bridge):
    from PySide6 import QtCore, QtWidgets

    class Panel(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("TexturePluggerBlenderLink")
            self.setWindowTitle("Blender Link")
            lay = QtWidgets.QVBoxLayout(self)
            self.info = QtWidgets.QLabel()
            self.info.setWordWrap(True)
            lay.addWidget(self.info)

            self.list = QtWidgets.QListWidget()
            self.list.setToolTip("Double-click to solo a texture set and frame it")
            self.list.itemDoubleClicked.connect(lambda it: bridge.solo(it.data(QtCore.Qt.UserRole)))
            lay.addWidget(self.list, 1)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Solo")
            b.setToolTip("Show only the selected texture set and frame it")
            b.clicked.connect(lambda: bridge.solo(self.selected()))
            row.addWidget(b)
            b = QtWidgets.QPushButton("Frame")
            b.setToolTip("Frame the selected texture set without hiding the others")
            b.clicked.connect(lambda: bridge.frame_set(self.selected()))
            row.addWidget(b)
            b = QtWidgets.QPushButton("Show All")
            b.setToolTip("Leave solo and frame the whole scene")
            b.clicked.connect(lambda: bridge.unsolo())
            row.addWidget(b)
            lay.addLayout(row)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Match Blender View")
            b.clicked.connect(bridge.match_view)
            row.addWidget(b)
            b = QtWidgets.QPushButton("Apply Lighting")
            b.setToolTip("Use the lighting baked in Blender as the environment map")
            b.clicked.connect(lambda: bridge.apply_lighting(self.selected(), force=True))
            row.addWidget(b)
            lay.addLayout(row)

            grid = QtWidgets.QGridLayout()
            for text, tip, col, args in (
                    ("◀", "Orbit left around the object", 0, dict(yaw=-15.0)),
                    ("▲", "Orbit up around the object", 1, dict(pitch=10.0)),
                    ("▼", "Orbit down around the object", 2, dict(pitch=-10.0)),
                    ("▶", "Orbit right around the object", 3, dict(yaw=15.0)),
                    ("Zoom +", "Move closer to the object", 4, dict(zoom=0.8)),
                    ("Zoom -", "Move away from the object", 5, dict(zoom=1.25))):
                b = QtWidgets.QPushButton(text)
                b.setToolTip(tip)
                b.clicked.connect(lambda _=False, a=args: bridge.orbit(set_name=self.selected(), **a))
                grid.addWidget(b, 0, col)
            lay.addLayout(grid)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Export to Blender")
            b.setToolTip("Export every texture set with the settings Blender's Texture Plugger "
                         "expects (Blender (Principled BSDF) template, PNG) into the folder "
                         "Blender watches. Blender reloads them")
            b.clicked.connect(lambda: bridge.export_to_blender())
            row.addWidget(b)
            b = QtWidgets.QPushButton("Export Selected")
            b.setToolTip("Same, for the selected texture set only (faster on big scenes)")
            b.clicked.connect(lambda: bridge.export_to_blender(self.selected()))
            row.addWidget(b)
            lay.addLayout(row)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Load from Blender")
            b.setToolTip("Bring the textures Blender shows back into Painter: a 'From Blender' "
                         "fill layer per texture set (Base Color, Roughness, Metallic, Emission, "
                         "Alpha) from the folder Blender watches. Normal and height are left "
                         "out (the exported normal already holds the baked mesh detail)")
            b.clicked.connect(lambda: bridge.load_from_blender())
            row.addWidget(b)
            b = QtWidgets.QPushButton("Load Selected")
            b.setToolTip("Same, for the selected texture set only")
            b.clicked.connect(lambda: bridge.load_from_blender([self.selected()]))
            row.addWidget(b)
            lay.addLayout(row)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Bake Lighting")
            b.setToolTip("Light the selected texture set's Base Color with its Blender lighting "
                         "(Painter's Baked Lighting Environment filter, in a 'Blender Lighting' "
                         "layer), and add a 'Lit Paint' layer above it to paint lit colours on. "
                         "Bakes the World Space Normal and AO mesh maps first if they are "
                         "missing. Exports then show it unlit in Blender")
            b.clicked.connect(lambda: bridge.bake_lighting([self.selected()]))
            row.addWidget(b)
            b = QtWidgets.QPushButton("Remove Lighting")
            b.setToolTip("Delete the selected set's 'Blender Lighting' layer (your Lit Paint "
                         "layer stays). Export again so Blender shows it lit normally")
            b.clicked.connect(lambda: bridge.remove_lighting([self.selected()]))
            row.addWidget(b)
            lay.addLayout(row)

            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("Lighting:"))
            self.mode_render = QtWidgets.QRadioButton("Render")
            self.mode_render.setToolTip("Blender's scene lighting (world + lamps) baked around the "
                                        "object, like Blender's Rendered view")
            self.mode_preview = QtWidgets.QRadioButton("Material Preview")
            self.mode_preview.setToolTip("The studio HDRI of Blender's Material Preview, with its "
                                         "rotation and strength")
            group = QtWidgets.QButtonGroup(self)
            group.addButton(self.mode_render)
            group.addButton(self.mode_preview)
            self.mode_render.toggled.connect(lambda v: v and bridge.set_light_mode("render"))
            self.mode_preview.toggled.connect(lambda v: v and bridge.set_light_mode("preview"))
            row.addWidget(self.mode_render)
            row.addWidget(self.mode_preview)
            row.addStretch(1)
            lay.addLayout(row)

            self.auto = QtWidgets.QCheckBox("Use Blender lighting automatically")
            self.auto.setChecked(bridge.auto_light)
            self.auto.toggled.connect(self._auto)
            lay.addWidget(self.auto)
            self.follow = QtWidgets.QCheckBox("While soloing, frame the texture set I pick")
            self.follow.setChecked(bridge.follow)
            self.follow.toggled.connect(lambda v: setattr(bridge, "follow", v))
            lay.addWidget(self.follow)
            hint = QtWidgets.QLabel("Arrow buttons orbit around the selected object's centre at the "
                                    "current zoom. Alt + left-drag orbits around the point under "
                                    "the cursor instead. With baked lighting, view the Base Color "
                                    "channel (C) to paint: the material view lights it twice.")
            hint.setWordWrap(True)
            hint.setEnabled(False)
            lay.addWidget(hint)
            self.status = QtWidgets.QLabel()
            self.status.setWordWrap(True)
            lay.addWidget(self.status)
            self.refresh()

        def _auto(self, v):
            bridge.auto_light = v
            if v:
                bridge.apply_lighting(force=True)

        def selected(self):
            it = self.list.currentItem()
            return it.data(QtCore.Qt.UserRole) if it is not None else bridge.active_set_name()

        def select_set(self, name):
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.data(QtCore.Qt.UserRole) == name:
                    self.list.setCurrentItem(it)
                    return

        def set_status(self, text):
            self.status.setText(text)

        def refresh(self):
            d = bridge.data
            if not d:
                self.info.setText("No link from Blender yet. In Blender select your object "
                                  "and press Send to Painter (Texture Plugger panel).")
            else:
                env = d.get("environment") or "none"
                blend = d.get("blend") or "unsaved .blend"
                self.info.setText(f"Linked to {blend}. Lighting: {'yes' if env != 'none' else 'not sent'}"
                                  f"{' (soloing)' if bridge.soloing else ''}")
            for b, mode in ((self.mode_render, "render"), (self.mode_preview, "preview")):
                b.blockSignals(True)
                b.setChecked(bridge.light_mode == mode)
                b.blockSignals(False)
            self.mode_preview.setEnabled(bridge.has_preview() or bridge.light_mode == "preview")
            current = self.selected() if self.list.count() else None
            self.list.clear()
            names = bridge.texture_set_names() if bridge.project_ready() else []
            linked = bridge.sets()
            keys = match_sets(names, linked)
            lit = set(bridge._lit_record()) if names else set()
            for n in names:
                tag = []
                if n in keys:
                    tag.append("bounds")
                    if linked[keys[n]].get("environment"):
                        tag.append("own lighting")
                if bridge.lightmap_for(n):
                    tag.append("Cycles lightmap (outdated)" if bridge.lightmap_is_outdated(n)
                               else "Cycles lightmap")
                if n in lit:
                    tag.append("baked lighting")
                it = QtWidgets.QListWidgetItem(n + (f"   [{', '.join(tag)}]" if tag else "   [not in Blender link]"))
                it.setData(QtCore.Qt.UserRole, n)
                self.list.addItem(it)
            self.select_set(current or bridge.active_set_name())

    return Panel()


_bridge = None
_ui_elements = []


def start_plugin():
    global _bridge
    from PySide6 import QtGui, QtWidgets
    import substance_painter.ui as ui
    _bridge = Bridge()
    panel = _make_panel(_bridge)
    _bridge.widget = panel
    dock = ui.add_dock_widget(panel)
    _ui_elements.append(dock)

    menu = QtWidgets.QMenu("Texture Plugger", ui.get_main_window())
    for text, fn in (("Solo Active Texture Set", lambda: _bridge.solo()),
                     ("Show All", lambda: _bridge.unsolo()),
                     ("Frame Active Texture Set", lambda: _bridge.frame_set(_bridge.active_set_name())),
                     ("Orbit Left", lambda: _bridge.orbit(yaw=-15.0)),
                     ("Orbit Right", lambda: _bridge.orbit(yaw=15.0)),
                     ("Orbit Up", lambda: _bridge.orbit(pitch=10.0)),
                     ("Orbit Down", lambda: _bridge.orbit(pitch=-10.0)),
                     ("Zoom In", lambda: _bridge.orbit(zoom=0.8)),
                     ("Zoom Out", lambda: _bridge.orbit(zoom=1.25)),
                     ("Match Blender View", lambda: _bridge.match_view()),
                     ("Apply Blender Lighting", lambda: _bridge.apply_lighting(force=True)),
                     ("Export to Blender", lambda: _bridge.export_to_blender()),
                     ("Export Active Texture Set to Blender",
                      lambda: _bridge.export_to_blender(_bridge.active_set_name())),
                     ("Load from Blender", lambda: _bridge.load_from_blender()),
                     ("Load Active Texture Set from Blender",
                      lambda: _bridge.load_from_blender([_bridge.active_set_name()])),
                     ("Bake Lighting into Active Texture Set",
                      lambda: _bridge.bake_lighting([_bridge.active_set_name()])),
                     ("Bake Lighting into All Linked Texture Sets",
                      lambda: _bridge.bake_lighting_all()),
                     ("Remove Baked Lighting from Active Texture Set",
                      lambda: _bridge.remove_lighting([_bridge.active_set_name()])),
                     ("Lighting: Blender Render", lambda: _bridge.set_light_mode("render")),
                     ("Lighting: Blender Material Preview", lambda: _bridge.set_light_mode("preview"))):
        act = QtGui.QAction(text, menu)
        act.triggered.connect(fn)
        menu.addAction(act)
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
