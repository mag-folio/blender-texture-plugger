# SPDX-License-Identifier: GPL-3.0-or-later
"""Unwrap objects that have no UVs yet, with the Smart Seams add-on.

Smart Seams is a separate, optional add-on. When it is enabled, Create Sets
for Scene unwraps every object that is not unwrapped yet before the meshes go
to Painter. Objects that already have UVs are never touched: Smart Seams
replaces the active UV map and the seams, and those could be hand-made.
"""

import array
import hashlib

import bpy

# Stamped on a mesh after Smart Seams unwrapped it: its vertex/edge/face counts,
# so "All Objects" doesn't redo it on every run but does after the mesh changes.
STAMP_KEY = "texture_plugger_smart_seams"

# Coordinates closer than this count as the same point (a collapsed UV map).
_EPS = 1e-6


def smart_seams_available():
    """True when the Smart Seams add-on is installed and enabled."""
    if not hasattr(bpy.types.Scene, "smart_seams"):
        return False
    try:
        bpy.ops.uv.smart_seams_unwrap.get_rna_type()   # KeyError when not registered
    except (AttributeError, KeyError, RuntimeError):
        return False
    return True


def needs_unwrap(ob):
    """True when the mesh has no usable UVs: no UV map, or one where every
    coordinate sits on the same point or on one line (nothing to paint on)."""
    me = getattr(ob, "data", None)
    if ob.type != "MESH" or me is None or len(me.polygons) == 0:
        return False
    uv = me.uv_layers.active
    if uv is None:
        return len(me.uv_layers) == 0
    n = len(me.loops)
    if n == 0:
        return False
    co = array.array("f", bytes(8 * n))
    uv.data.foreach_get("uv", co)
    us, vs = co[0::2], co[1::2]
    return (max(us) - min(us) < _EPS) or (max(vs) - min(vs) < _EPS)


def _signature(me):
    return f"{len(me.vertices)}/{len(me.edges)}/{len(me.polygons)}"


def is_optimized(ob):
    """True when Smart Seams already unwrapped this mesh as it is now."""
    me = ob.data
    try:
        return me.get(STAMP_KEY) == _signature(me) and not needs_unwrap(ob)
    except Exception:
        return False


def _snapshot(me):
    """Active UV coordinates and seams, to put back if Smart Seams fails part way."""
    uv = me.uv_layers.active
    co = None
    if uv is not None:
        co = array.array("f", bytes(8 * len(me.loops)))
        uv.data.foreach_get("uv", co)
    seams = array.array("b", bytes(len(me.edges)))
    me.edges.foreach_get("use_seam", seams)
    return (uv.name if uv is not None else None, co, seams)


def _restore(me, snap):
    name, co, seams = snap
    try:
        uv = me.uv_layers.get(name) if name else None
        if uv is not None and co is not None and len(co) == 2 * len(me.loops):
            me.uv_layers.active = uv
            uv.data.foreach_set("uv", co)
        if len(seams) == len(me.edges):
            me.edges.foreach_set("use_seam", seams)
        me.update()
    except Exception:
        pass


def geometry_key(me):
    """Same key = same vertices, same faces, same shape: one unwrap fits both."""
    lv = array.array("i", bytes(4 * len(me.loops)))
    me.loops.foreach_get("vertex_index", lv)
    co = array.array("f", bytes(12 * len(me.vertices)))
    me.vertices.foreach_get("co", co)
    h = hashlib.sha1(lv.tobytes())
    h.update(array.array("f", [round(c, 4) + 0.0 for c in co]).tobytes())
    return (len(me.vertices), len(me.edges), len(me.polygons), h.hexdigest())


def _edge_keys(me):
    ev = array.array("i", bytes(8 * len(me.edges)))
    me.edges.foreach_get("vertices", ev)
    return [(min(a, b), max(a, b)) for a, b in zip(ev[0::2], ev[1::2])]


def copy_unwrap(src, dst):
    """Give ``dst`` (same geometry) the UVs and seams Smart Seams made for ``src``."""
    s, d = src.data, dst.data
    uv = s.uv_layers.active
    co = array.array("f", bytes(8 * len(s.loops)))
    uv.data.foreach_get("uv", co)
    duv = d.uv_layers.active or d.uv_layers.new(name=uv.name)
    duv.data.foreach_set("uv", co)
    seams = array.array("b", bytes(len(s.edges)))
    s.edges.foreach_get("use_seam", seams)
    by_pair = dict(zip(_edge_keys(s), seams))
    d.edges.foreach_set("use_seam", array.array("b", [by_pair.get(k, 0) for k in _edge_keys(d)]))
    d.update()
    d[STAMP_KEY] = _signature(d)


def unwrappable(ob):
    """Smart Seams can only change meshes stored in this file."""
    for idb in (ob, ob.data):
        if idb is None or idb.library is not None:
            return False
        if not getattr(idb, "is_editable", True):
            return False
    return True


def unwrap_one(context, ob):
    """Run Smart Seams on ``ob`` alone. Returns ``None`` on success, else a reason.

    Selection and the active object are put back as they were.
    """
    vl = context.view_layer
    prev_active = vl.objects.active
    prev_selected = [o for o in vl.objects if o.select_get()]
    me = ob.data
    had_layers = len(me.uv_layers) > 0
    snap = _snapshot(me)
    err = None
    try:
        for o in prev_selected:
            o.select_set(False)
        ob.select_set(True)
        vl.objects.active = ob
        try:
            res = bpy.ops.uv.smart_seams_unwrap("EXEC_DEFAULT")
            if "FINISHED" not in res:
                err = getattr(context.scene.smart_seams, "last_message", "") or "cancelled"
        except RuntimeError as ex:
            # With one target Smart Seams only reports an error when that object failed.
            err = str(ex).strip().splitlines()[-1] if str(ex).strip() else "failed"
    finally:
        for o in vl.objects:
            try:
                o.select_set(o in prev_selected)
            except RuntimeError:
                pass
        try:
            vl.objects.active = prev_active
        except Exception:
            pass
    if err is None and needs_unwrap(ob):
        err = getattr(context.scene.smart_seams, "last_message", "") or "no UVs were made"
    if err is not None:
        if not had_layers:
            # Smart Seams adds the UV map before solving: don't leave a half-made one
            # behind, so the object honestly has no UVs and is retried next time.
            while me.uv_layers:
                me.uv_layers.remove(me.uv_layers[0])
        else:
            _restore(me, snap)          # keep the UVs it had
        return err
    me[STAMP_KEY] = _signature(me)
    return None
