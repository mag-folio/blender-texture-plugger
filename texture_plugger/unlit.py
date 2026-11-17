# SPDX-License-Identifier: GPL-3.0-or-later
"""Show textures that carry their own lighting unlit (emission only).

Painter's "Bake Lighting" button (Blender Link panel) lights a texture set's
Base Color with the Blender lighting, and its exports write
``texture_plugger_lit.json`` next to the textures:
``{"sets": {"Crate": {"lit": true, "base_color": "crate_basecolor", ...}}}``.

A timer here reads that file for every Texture Plugger material and switches
the material to Base Color -> Emission when its set is lit (back to the
Principled BSDF when it is not any more).  Scenes that show such materials get
the "Standard" view transform (no tone mapping, exposure 0, gamma 1) so the
colours look exactly as painted; the previous settings are restored when no
unlit material is left in the scene.
"""

import json
import os

import bpy

from . import node_builder, prefs

LIT_MARKER = "texture_plugger_lit.json"       # same name in the Painter plugin
PROJECT_KEY = "texture_plugger_project"       # = workflow.PROJECT_KEY (no import cycle)
VIEW_KEY = "texture_plugger_view_before_unlit"
INTERVAL = 1.0

_markers = {}            # marker path -> ((mtime_ns, size), {base colour stem: lit})
_view_dirty = True       # scenes' view transforms need a look (file loaded, prefs changed)
_last_unlit = set()      # unlit material names at the last look
last_status = ""         # shown in the panel


def file_stem(path):
    """Lower-case file name without extension or UDIM tile.

    Must match ``file_stem`` in ``painter_plugin/texture_plugger_bridge.py``.
    """
    name = os.path.basename(str(path).replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    for sep in (".", "_"):
        head, s, tail = stem.rpartition(sep)
        if s and head and (tail.upper() == "<UDIM>" or (len(tail) == 4 and tail.isdigit())):
            stem = head
            break
    return stem.lower()


def parse_marker(data):
    """{base colour stem: lit} from a marker's contents."""
    out = {}
    sets = data.get("sets") if isinstance(data, dict) else None
    if not isinstance(sets, dict):
        return out
    for info in sets.values():
        if isinstance(info, dict) and info.get("base_color"):
            out[str(info["base_color"]).lower()] = bool(info.get("lit"))
    return out


def read_marker(textures_dir):
    """Lit state per Base Color stem for a textures folder ({} without a marker)."""
    path = os.path.join(textures_dir, LIT_MARKER)
    try:
        st = os.stat(path)
    except OSError:
        _markers.pop(path, None)
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    hit = _markers.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            lit = parse_marker(json.load(fh))
    except (OSError, ValueError):
        return hit[1] if hit is not None else {}       # being written: keep the old state
    _markers[path] = (sig, lit)
    return lit


def wants_unlit(mat, cache=None):
    """True when Painter marked this material's Base Color as carrying its lighting."""
    try:
        folder = mat.get(PROJECT_KEY)
    except Exception:
        return False
    if not folder or mat.node_tree is None:
        return False
    node = node_builder.base_color_node(mat.node_tree)
    if node is None:
        return False
    tex = os.path.join(str(folder), "textures")
    if cache is not None:
        lit = cache.get(tex)
        if lit is None:
            lit = cache[tex] = read_marker(tex)
    else:
        lit = read_marker(tex)
    return bool(lit.get(file_stem(node.image.filepath)))


def _enabled(p):
    return True if p is None else bool(getattr(p, "unlit_from_painter", True))


def sync():
    """Switch Texture Plugger materials to/from unlit as Painter marked them.

    Returns the names of materials that changed.
    """
    global _view_dirty, _last_unlit, last_status
    p = prefs.get_prefs()
    enabled = _enabled(p)
    cache, changed, unlit = {}, [], set()
    for mat in bpy.data.materials:
        try:
            if mat.library is not None or mat.node_tree is None or not mat.get(PROJECT_KEY):
                continue
            want = enabled and wants_unlit(mat, cache)
            if node_builder.set_unlit(mat, want):
                changed.append(mat.name)
            if want and node_builder.is_unlit(mat):
                unlit.add(mat.name)
        except ReferenceError:
            continue
    if changed or _view_dirty or unlit != _last_unlit:
        _view_dirty = False
        _last_unlit = unlit
        update_views(unlit, p)
        n = len(unlit)
        last_status = (f"{n} material{'s' if n != 1 else ''} unlit (lighting baked in Painter)"
                       if n else "")
        if changed:
            print("Texture Plugger: unlit (Painter baked lighting) updated:", ", ".join(changed))
    return changed


def _scene_uses(scene, names):
    if not names:
        return False
    for ob in scene.objects:
        for slot in getattr(ob, "material_slots", ()):
            m = slot.material
            if m is not None and m.name in names:
                return True
    return False


def update_views(unlit_names, p=None):
    """Standard view transform on scenes showing unlit materials; restore the others."""
    switch = True if p is None else bool(getattr(p, "unlit_view_transform", True))
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        vs = scene.view_settings
        uses = switch and _scene_uses(scene, unlit_names)
        before = scene.get(VIEW_KEY)
        try:
            if uses and before is None:
                scene[VIEW_KEY] = {"view_transform": vs.view_transform, "look": vs.look,
                                   "exposure": float(vs.exposure), "gamma": float(vs.gamma)}
                vs.view_transform = "Standard"
                vs.look = "None"
                vs.exposure = 0.0       # the baked lighting already has Blender's exposure
                vs.gamma = 1.0
                print(f"Texture Plugger: scene '{scene.name}' view transform set to Standard "
                      "so lighting baked in Painter shows as painted")
            elif not uses and before is not None:
                try:
                    before = before.to_dict()
                except AttributeError:
                    before = dict(before)
                if vs.view_transform == "Standard":    # untouched since: put it back
                    for attr in ("view_transform", "look", "exposure", "gamma"):
                        if attr in before:
                            try:
                                setattr(vs, attr, before[attr])
                            except Exception:
                                pass
                del scene[VIEW_KEY]
        except Exception as ex:      # e.g. a custom OCIO config without "Standard"
            print("Texture Plugger: could not change the view transform:", ex)


def user_view(scene):
    """The scene's own colour settings: the ones saved before the Standard switch, if
    any.  The lighting sent to Painter uses these, so the switch never changes it."""
    vs = scene.view_settings
    out = {"view_transform": vs.view_transform, "look": vs.look,
           "exposure": float(vs.exposure), "gamma": float(vs.gamma)}
    saved = scene.get(VIEW_KEY)
    if saved is not None:
        try:
            saved = saved.to_dict()
        except AttributeError:
            saved = dict(saved)
        out.update({k: saved[k] for k in out if k in saved})
    return out


def mark_dirty(*_args):
    global _view_dirty
    _view_dirty = True


def _timer():
    try:
        sync()
    except Exception as ex:
        print("Texture Plugger: unlit sync error:", ex)
    return INTERVAL


@bpy.app.handlers.persistent
def _on_load(*_args):
    _markers.clear()
    mark_dirty()


def register():
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
