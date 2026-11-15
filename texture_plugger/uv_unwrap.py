# SPDX-License-Identifier: GPL-3.0-or-later
"""Unwrap objects that have no UVs yet, with the Smart Seams add-on.

Smart Seams is a separate, optional add-on. When it is enabled, Create Sets
for Scene unwraps every object that is not unwrapped yet before the meshes go
to Painter. Objects that already have UVs are never touched: Smart Seams
replaces the active UV map and the seams, and those could be hand-made.
"""

import array

import bpy

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
    if err is not None and not had_layers:
        # Smart Seams adds the UV map before solving: don't leave a half-made one
        # behind, so the object honestly has no UVs and is retried next time.
        while me.uv_layers:
            me.uv_layers.remove(me.uv_layers[0])
    return err
