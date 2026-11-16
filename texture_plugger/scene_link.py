# SPDX-License-Identifier: GPL-3.0-or-later
"""Send Blender's lighting and view to Substance 3D Painter.

* ``bake_environment`` renders what the object "sees" (world + the rest of the
  scene, lit by your lights) as an equirectangular HDR with Cycles, from the
  object's centre (or the nearest clear spot when no lamp reaches a boxed-in
  part) with the object itself hidden.  Lamps are invisible to camera rays, so
  each one is measured separately (a tiny white probe, direct light only, real
  shadows from the scene's own materials) and painted into the map as a bright
  disc that delivers exactly that light.
* ``view_from_context`` records the 3D viewport (or scene camera) relative to the
  object.
* Both land in ``<set folder>/painter/`` with a JSON sidecar that the Painter
  plugin watches (see ``painter_plugin/``).

All rendering happens in throw-away scenes, so the user's render settings are
never touched.
"""

import math
import os
import shutil
import sys
import time

import bpy
from bpy.props import BoolProperty
from mathutils import Vector

from . import painter_link, prefs, unlit
from .operators import _safe

PLUGIN_NAME = "texture_plugger_bridge.py"
PLUGIN_SRC = os.path.join(os.path.dirname(__file__), "painter_plugin", PLUGIN_NAME)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def world_bounds(obj, depsgraph=None):
    """(centre, radius, size) of the evaluated object's world bounding box."""
    if depsgraph is not None:
        try:
            obj = obj.evaluated_get(depsgraph)
        except Exception:
            pass
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    size = hi - lo
    centre = (lo + hi) / 2
    radius = max(size.length / 2, 1e-4)
    return centre, radius, size


def _render_collections(view_layer):
    """Collections whose objects render in ``view_layer``.

    A collection renders when neither it nor any parent is excluded from the view
    layer or switched off for rendering.  Viewport hiding (the eye icon, H) does
    not count: walls or a ceiling hidden while working still light the scene.
    """
    ok = set()

    def walk(lc, parent_ok):
        good = parent_ok and not lc.exclude and not lc.collection.hide_render
        if good:
            ok.add(lc.collection)
        for child in lc.children:
            walk(child, good)

    walk(view_layer.layer_collection, True)
    return ok


def _render_visible(ob, view_layer, collections=None):
    """True when ``ob`` shows up in a render of ``view_layer``."""
    if ob.hide_render:
        return False
    if collections is None:
        collections = _render_collections(view_layer)
    return any(c in collections for c in ob.users_collection)


def _new_scene(name, src):
    sc = bpy.data.scenes.new(name)
    sc.render.engine = "CYCLES"
    try:
        sc.cycles.device = src.cycles.device
    except AttributeError:
        pass
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = False
    sc.render.use_compositing = False
    sc.render.use_sequencer = False
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    sc.view_settings.exposure = 0.0
    sc.view_settings.gamma = 1.0
    sc.render.image_settings.file_format = "OPEN_EXR"
    sc.render.image_settings.color_depth = "32"
    sc.unit_settings.scale_length = src.unit_settings.scale_length
    return sc


def _pano_camera(sc, location, clip_start):
    cd = bpy.data.cameras.new("TP_PanoCam")
    cd.type = "PANO"
    try:
        cd.panorama_type = "EQUIRECTANGULAR"
    except (AttributeError, TypeError):
        cd.cycles.panorama_type = "EQUIRECTANGULAR"
    cd.clip_start = clip_start
    cd.clip_end = 1e6
    cam = bpy.data.objects.new("TP_PanoCam", cd)
    cam.location = location
    cam.rotation_euler = (math.pi / 2, 0.0, 0.0)   # look along +Y, Z up (see painter_link)
    sc.collection.objects.link(cam)
    sc.camera = cam
    return cam


def _render_to_array(sc, path):
    """Render ``sc`` to an EXR and return (numpy array HxWx4 bottom-up, w, h)."""
    import numpy as np
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True, scene=sc.name)
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    return buf.reshape(h, w, 4), w, h


def _cleanup(scenes, objects, datas):
    for ob in objects:
        try:
            bpy.data.objects.remove(ob, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass
    for sc in scenes:
        try:
            bpy.data.scenes.remove(sc)
        except (ReferenceError, RuntimeError):
            pass
    for d in datas:
        try:
            coll = {bpy.types.Camera: bpy.data.cameras, bpy.types.Mesh: bpy.data.meshes,
                    bpy.types.Material: bpy.data.materials}.get(type(d))
            if coll is not None and d.users == 0:
                coll.remove(d)
        except (ReferenceError, RuntimeError):
            pass


# --------------------------------------------------------------------------
# Light measurement
# --------------------------------------------------------------------------

def light_direction(light_ob, centre):
    """(unit direction from centre towards the light, distance) in Blender world space."""
    if light_ob.data.type == "SUN":
        d = (light_ob.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        return d, math.inf
    v = light_ob.matrix_world.translation - centre
    dist = v.length
    if dist < 1e-9:
        return Vector((0.0, 0.0, 1.0)), 0.0
    return v / dist, dist


def light_size(light):
    if light.type == "AREA":
        sx = light.size
        sy = light.size_y if light.shape in {"RECTANGLE", "ELLIPSE"} else light.size
        return max(sx, sy)
    return 2.0 * getattr(light, "shadow_soft_size", 0.0)


def _emits(mat):
    """True when a material may glow (an Emission shader or Principled emission)."""
    if mat is None or not getattr(mat, "use_nodes", False) or mat.node_tree is None:
        return False
    return _tree_emits(mat.node_tree, set())


def _tree_emits(tree, seen):
    if tree is None or tree.name_full in seen:
        return False
    seen.add(tree.name_full)
    for node in tree.nodes:
        if node.bl_idname == "ShaderNodeEmission":
            return True
        if node.bl_idname == "ShaderNodeVolumePrincipled":
            for name in ("Emission Strength", "Blackbody Intensity"):
                sock = node.inputs.get(name)
                if sock is not None and (sock.is_linked or sock.default_value > 0.0):
                    return True
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            sock = node.inputs.get("Emission Strength")
            if sock is not None and (sock.is_linked or sock.default_value > 0.0):
                col = node.inputs.get("Emission Color") or node.inputs.get("Emission")
                if col is None or col.is_linked or max(col.default_value[:3]) > 0.0:
                    return True
        if node.bl_idname == "ShaderNodeGroup" and _tree_emits(node.node_tree, seen):
            return True
    return False


def _scene_materials(sc):
    """Materials the user's scene uses: object slots, mesh data (Geometry Nodes
    "Set Material") and collection instances included.

    Reads the user's own evaluated depsgraph: building one for a throw-away
    scene made Blender 4.5 crash when that scene was removed.  ``sc`` is only
    the fallback when no depsgraph is available."""
    mats = {}

    def add(ob):
        for slot in getattr(ob, "material_slots", ()):
            if slot.material is not None:
                mats[slot.material.name_full] = slot.material
        for m in getattr(getattr(ob, "data", None), "materials", None) or ():
            if m is not None:
                mats[m.name_full] = m.original if hasattr(m, "original") else m

    try:
        dg = bpy.context.evaluated_depsgraph_get()
        for inst in dg.object_instances:      # references only valid inside the loop
            add(inst.object)
    except (AttributeError, RuntimeError, ReferenceError):
        for ob in sc.collection.all_objects:
            add(ob)
    return list(mats.values())


def _has_emitters(materials):
    return any(_emits(m) for m in materials)


def _has_lamps(src, view_layer):
    rendered = _render_collections(view_layer)
    return any(ob.type == "LIGHT" and getattr(ob.data, "energy", 0.0) > 0.0
               and _render_visible(ob, view_layer, rendered) for ob in src.objects)


def measure_lights(src, view_layer, target, centre, radius, samples=32, occluders=True):
    """Direct irradiance (RGB) each visible lamp delivers at ``centre``.

    Returns ``[(light_object, direction, distance, (r, g, b))]``; lamps that
    deliver nothing there (behind an area light, outside a spot cone, fully
    shadowed) are left out.  When the scene has glowing materials, their own
    light is measured without lamps and taken out, and a lamp then counts only
    above 5% of it (so a very dim lamp next to a bright screen is dropped).
    """
    rendered = _render_collections(view_layer)
    lights = [ob for ob in src.objects
              if ob.type == "LIGHT" and _render_visible(ob, view_layer, rendered)
              and getattr(ob.data, "energy", 0.0) > 0.0]
    if not lights:
        return []
    import numpy as np

    sc = _new_scene("TP_Probe", src)
    made, datas = [], []
    try:
        sc.world = None
        sc.cycles.samples = samples
        sc.cycles.use_denoising = False
        sc.cycles.max_bounces = 0
        sc.render.resolution_x = sc.render.resolution_y = 8
        # the scene's real materials and textures load once, not once per lamp render
        sc.render.use_persistent_data = True
        white = bpy.data.materials.new("TP_ProbeWhite")
        white.use_nodes = True
        nt = white.node_tree
        nt.nodes.clear()
        bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
        bsdf.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs[0], out.inputs[0])
        datas.append(white)

        # Occluders keep their own materials, so shadows match a Cycles render:
        # alpha-cut leaves, Transparent BSDF or "light path" window glass let the
        # sun through (a white override made every window opaque).  A plain
        # refractive Glass / Principled transmission pane still blocks it, as it
        # does in Cycles itself.
        if occluders:
            for ob in src.objects:
                if ob.type == "LIGHT" or ob == target or ob.type == "CAMERA":
                    continue
                if _render_visible(ob, view_layer, rendered):
                    sc.collection.objects.link(ob)
        glowing = occluders and _has_emitters(_scene_materials(sc))

        s = radius * 1e-3
        me = bpy.data.meshes.new("TP_Probe")
        me.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
        me.materials.append(white)
        datas.append(me)
        plane = bpy.data.objects.new("TP_Probe", me)
        sc.collection.objects.link(plane)
        made.append(plane)
        cd = bpy.data.cameras.new("TP_ProbeCam")
        cd.type = "ORTHO"
        cd.ortho_scale = s
        cd.clip_start = s * 0.1
        cd.clip_end = s * 10
        datas.append(cd)
        cam = bpy.data.objects.new("TP_ProbeCam", cd)
        sc.collection.objects.link(cam)
        made.append(cam)
        sc.camera = cam

        tmp = os.path.join(bpy.app.tempdir or os.environ.get("TEMP", "."), "tp_probe.exr")
        result = []
        for lo in lights:
            d, dist = light_direction(lo, centre)
            rot = d.to_track_quat("Z", "Y")      # plane +Z (its normal) faces the light
            plane.matrix_world = rot.to_matrix().to_4x4()
            plane.matrix_world.translation = centre
            cam.matrix_world = rot.to_matrix().to_4x4()
            cam.matrix_world.translation = centre + d * (s * 2)
            base = 0.0
            if glowing:
                # glowing meshes (screens, light panels) already show in the
                # environment render: measure them alone and take them out
                arr, _, _ = _render_to_array(sc, tmp)
                base = arr[..., :3].reshape(-1, 3).mean(axis=0)
            sc.collection.objects.link(lo)
            try:
                arr, _, _ = _render_to_array(sc, tmp)
            finally:
                sc.collection.objects.unlink(lo)
            rgb = np.maximum(arr[..., :3].reshape(-1, 3).mean(axis=0) - base, 0.0) * math.pi
            # with glowing meshes the subtraction leaves sampling noise: a lamp
            # counts only when it adds clearly more than that
            if float(np.max(rgb)) <= max(1e-6, 0.05 * math.pi * float(np.max(base))):
                continue
            result.append((lo, d, dist, tuple(float(c) for c in rgb)))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return result
    finally:
        for ob in list(sc.collection.objects):
            if ob not in made:
                sc.collection.objects.unlink(ob)
        _cleanup([sc], made, datas)


# --------------------------------------------------------------------------
# Environment bake
# --------------------------------------------------------------------------

def _fibonacci_dirs(n):
    """``n`` directions spread evenly over the sphere."""
    out = []
    ga = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        z = 1.0 - 2.0 * (i + 0.5) / n
        r = math.sqrt(max(0.0, 1.0 - z * z))
        out.append(Vector((math.cos(ga * i) * r, math.sin(ga * i) * r, z)))
    return out


def openness(scene, depsgraph, point, ignore, reach, dirs):
    """Share of directions in which nothing but ``ignore`` is hit within ``reach``."""
    clear = 0
    for d in dirs:
        origin, left = point, reach
        for _ in range(8):                       # look through the object being painted
            ok, loc, _n, _i, ob, _m = scene.ray_cast(depsgraph, origin, d, distance=left)
            if not ok:
                clear += 1
                break
            if ob is None or ob.original not in ignore:
                break
            step = (loc - origin).length + 1e-4
            origin, left = loc + d * 1e-4, left - step
            if left <= 0:
                clear += 1
                break
    return clear / max(1, len(dirs))


# Clearance (Blender units) looked at around an object, and how open its centre
# must be.  Measured on a 1000-object office scene: the earlier reach of
# max(0.75, 4 x radius) with 0.6 called 119 of 121 sampled objects "boxed in"
# (anything on a desk, or indoors), so nearly every bake moved away from its
# object.  Being boxed in alone is not enough to move: see bake_environment.
PROBE_MARGIN = 0.3
PROBE_CLOSED = 0.35
PROBE_OPEN = 0.5


def _reach(radius):
    return radius + PROBE_MARGIN


def _see_through(context, target):
    """What the probe's rays pass through: the object itself and everything that
    does not render.  (Rays still miss objects hidden in the viewport, which do
    render: ``scene.ray_cast`` only knows viewport geometry.)"""
    vl = context.view_layer
    rendered = _render_collections(vl)
    out = {target}
    out.update(ob for ob in context.scene.objects
               if ob.type in {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME"}
               and not _render_visible(ob, vl, rendered))
    return out


def boxed_in(context, target, depsgraph, centre, radius):
    """True when other geometry closes in on ``target`` in most directions."""
    return openness(context.scene, depsgraph, centre, _see_through(context, target),
                    _reach(radius), _fibonacci_dirs(48)) < PROBE_CLOSED


def probe_point(context, target, depsgraph, centre, radius):
    """The nearest clear spot around ``target`` that still sees it. Returns ``(point, moved)``.

    For a small part tucked inside other geometry (a drawer handle, a screw, a
    keyboard key) that no lamp reaches: measured at its centre the lighting
    comes out black.  Stays within 0.8 units of the object's bounds, so the
    lamps keep roughly the directions they have at the object.
    """
    scene = context.scene
    ignore = _see_through(context, target)
    reach = _reach(radius)
    dirs = _fibonacci_dirs(48)
    best, best_score = centre, openness(scene, depsgraph, centre, ignore, reach, dirs)
    around = _fibonacci_dirs(26)
    for dist in (radius + 0.15, radius + 0.4, radius + 0.8):
        for d in around:
            p = centre + d * dist
            # stay in the same room: the spot must see the object's centre
            ok, _l, _n, _i, ob, _m = scene.ray_cast(depsgraph, p, (centre - p).normalized(),
                                                    distance=(centre - p).length)
            if ok and ob is not None and ob.original not in ignore:
                continue
            o = openness(scene, depsgraph, p, ignore, reach, dirs)
            score = o - 0.1 * dist / (radius + 0.8)
            if score > best_score:
                best, best_score = p, score
        if best_score >= PROBE_OPEN:
            break
    return best, best is not centre


_SCENE_WORLD = object()


def bake_environment(context, target, out_path, width=1024, samples=64,
                     min_light_deg=2.0, surroundings=True, world=_SCENE_WORLD, lamps=True):
    """Bake the lighting around ``target`` into ``out_path`` (EXR, Painter orientation).

    Measured at the object's centre; when lamps exist but none reaches the centre
    and it is closed in (or, without lamps, properly boxed in), at the nearest
    clear spot instead (``probe_point``).  Returns a dict with ``lights``
    (count), ``exposure``, ``path`` and ``moved`` (True when measured off-centre).

    ``world`` replaces the scene's world (Material Preview's studio HDRI) and
    ``lamps=False`` leaves the lamps out; both are for ``bake_preview``.
    """
    import numpy as np

    src = context.scene
    view_layer = context.view_layer
    depsgraph = context.evaluated_depsgraph_get()
    centre, radius, _size = world_bounds(target, depsgraph)
    # Measure at the object's centre, unless it is boxed in AND no lamp reaches
    # it: then the map would be black and useless for painting, so use the
    # nearest clear spot instead.
    lights, moved = None, False
    if not lamps:
        lights = []
    elif surroundings:
        lights = measure_lights(src, view_layer, target, centre, radius, occluders=True)
        if not lights:
            open_ = openness(src, depsgraph, centre, _see_through(context, target),
                             _reach(radius), _fibonacci_dirs(48))
            # lamps exist but none reaches the centre: move unless it is out in the open.
            # No lamps at all (world light only): move only when properly boxed in.
            limit = PROBE_OPEN if _has_lamps(src, view_layer) else PROBE_CLOSED
            if open_ < limit:
                point, moved = probe_point(context, target, depsgraph, centre, radius)
                if moved:
                    centre, lights = point, None
    rendered = _render_collections(view_layer)
    width = int(max(64, width)) // 2 * 2
    height = width // 2

    # 1) world + surroundings
    sc = _new_scene("TP_EnvBake", src)
    made = []
    try:
        sc.world = src.world if world is _SCENE_WORLD else world
        sc.cycles.samples = samples
        sc.cycles.use_denoising = True
        sc.render.resolution_x, sc.render.resolution_y = width, height
        for ob in src.objects:
            if ob == target or ob.type == "CAMERA" or not _render_visible(ob, view_layer, rendered):
                continue
            if (ob.type == "LIGHT" and lamps) or (ob.type != "LIGHT" and surroundings):
                sc.collection.objects.link(ob)
        cam = _pano_camera(sc, centre, clip_start=min(radius * 0.05, 0.01))
        made.append(cam)
        tmp = out_path + ".pano.exr"
        try:
            env, _, _ = _render_to_array(sc, tmp)
        except RuntimeError:
            # OIDN missing or the GPU refused: try once more plainly
            sc.cycles.use_denoising = False
            env, _, _ = _render_to_array(sc, tmp)
        try:
            os.remove(tmp)
        except OSError:
            pass
    finally:
        for ob in list(sc.collection.objects):
            if ob not in made:
                sc.collection.objects.unlink(ob)
        cam_data = made[0].data if made else None
        _cleanup([sc], made, [cam_data] if cam_data else [])

    env = env[..., :3].astype(np.float32).copy()
    env[~np.isfinite(env)] = 0.0
    np.maximum(env, 0.0, out=env)

    # 2) lamps, as discs that deliver the measured irradiance
    if lights is None:
        lights = measure_lights(src, view_layer, target, centre, radius,
                                occluders=surroundings)
    min_r = math.radians(max(0.25, min_light_deg))
    for lo, d, dist, rgb in lights:
        r = painter_link.angular_radius(lo.data.type, dist, light_size(lo.data),
                                        getattr(lo.data, "angle", 0.0), min_r)
        pix = painter_link.disc_pixels(width, height, tuple(d), r)
        rad = painter_link.light_radiance(rgb, pix)
        rows = np.fromiter((p[0] for p in pix), dtype=np.int64, count=len(pix))
        cols = np.fromiter((p[1] for p in pix), dtype=np.int64, count=len(pix))
        env[rows, cols] += np.asarray(rad, dtype=np.float32)

    # 3) exposure, Painter's orientation
    exposure = float(unlit.user_view(src)["exposure"])     # not the unlit Standard switch
    env *= 2.0 ** exposure
    if painter_link.ENV_MIRROR:
        env = env[:, ::-1]
    shift = int(round(painter_link.ENV_YAW_DEG / 360.0 * width))
    if shift:
        env = np.roll(env, shift, axis=1)

    img = bpy.data.images.new("TP_EnvOut", width, height, alpha=True, float_buffer=True)
    try:
        rgba = np.ones((height, width, 4), dtype=np.float32)
        rgba[..., :3] = env
        try:     # write raw linear values (4.5 would otherwise apply the view transform)
            img.colorspace_settings.is_data = True
        except (AttributeError, TypeError):
            pass
        img.pixels.foreach_set(rgba.ravel())
        img.filepath_raw = out_path
        img.file_format = "OPEN_EXR"
        img.save()
    finally:
        bpy.data.images.remove(img)
    return {"lights": len(lights), "exposure": exposure, "path": out_path, "moved": moved}


# --------------------------------------------------------------------------
# Material Preview lighting
# --------------------------------------------------------------------------

DEFAULT_LOOKDEV = "forest.exr"     # Blender's default Material Preview HDRI
# Mapping-node Z rotation = LOOKDEV_ROT_SIGN * studiolight_rotate_z + LOOKDEV_ROT_OFFSET.
# Measured against viewport captures in Blender 4.5.12 and 5.0.1 (rotate_z 0, +90,
# -45, 180 degrees): same angle, no flip, no offset; studiolight_intensity equals
# Background strength 1:1.
LOOKDEV_ROT_SIGN = 1.0
LOOKDEV_ROT_OFFSET = 0.0


def preview_settings(context):
    """What Blender's Material Preview shows, as a dict, from the user's viewport.

    Keys: ``hdri`` (file path or None), ``name``, ``rotation`` (radians, as a
    Mapping-node Z rotation), ``strength``, ``scene_world`` and
    ``scene_lights`` (Material Preview set to use the scene's own world/lamps).
    """
    space, _rv3d, _region = _find_view3d(context)
    shading = getattr(space, "shading", None)
    out = {"hdri": None, "name": DEFAULT_LOOKDEV, "rotation": 0.0, "strength": 1.0,
           "scene_world": False, "scene_lights": False}
    if shading is not None:
        prev_type = shading.type
        try:
            # studio_light gives the Material Preview HDRI in Material and Rendered
            # mode, but Solid mode's studio light otherwise (measured)
            if prev_type in {"SOLID", "WIREFRAME"}:
                shading.type = "MATERIAL"
            out["name"] = shading.studio_light or DEFAULT_LOOKDEV
            out["rotation"] = (LOOKDEV_ROT_SIGN * float(shading.studiolight_rotate_z)
                               + LOOKDEV_ROT_OFFSET)
            out["strength"] = float(shading.studiolight_intensity)
            out["scene_world"] = bool(getattr(shading, "use_scene_world", False))
            out["scene_lights"] = bool(getattr(shading, "use_scene_lights", False))
        finally:
            if shading.type != prev_type:
                shading.type = prev_type
    light = context.preferences.studio_lights.get(out["name"])
    if light is not None and getattr(light, "path", ""):
        out["hdri"] = light.path
    return out


def _lookdev_world(settings):
    """A throw-away world showing the studio HDRI like Material Preview does."""
    w = bpy.data.worlds.new("TP_LookDev")
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, settings["rotation"])
    tex = nt.nodes.new("ShaderNodeTexEnvironment")
    tex.image = bpy.data.images.load(settings["hdri"], check_existing=True)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = settings["strength"]
    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    nt.links.new(tex.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    return w, tex.image


def bake_preview(context, target, out_path, width=1024, min_light_deg=2.0):
    """Bake Blender's Material Preview lighting (the viewport's studio HDRI,
    rotated and scaled as in the viewport) into ``out_path``, Painter's way up.

    Only the light, no surroundings, as Material Preview shows it.  When
    Material Preview is set to use the scene world and/or scene lights, those
    are used instead.  Returns a dict like ``bake_environment`` plus ``hdri``.
    """
    st = preview_settings(context)
    if st["scene_world"]:
        info = bake_environment(context, target, out_path, width, 16, min_light_deg,
                                surroundings=False, lamps=st["scene_lights"])
        info["hdri"] = "scene world"
        return info
    if not st["hdri"] or not os.path.isfile(st["hdri"]):
        raise RuntimeError(f"Material Preview HDRI '{st['name']}' not found")
    had = {im.name_full for im in bpy.data.images}
    world, image = _lookdev_world(st)
    try:
        info = bake_environment(context, target, out_path, width, 4, min_light_deg,
                                surroundings=False, world=world, lamps=st["scene_lights"])
    finally:
        bpy.data.worlds.remove(world)
        if image is not None and image.name_full not in had and image.users == 0:
            bpy.data.images.remove(image)
    info["hdri"] = st["name"]
    return info


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------

def _find_view3d(context):
    """(space, region_3d, region) of the 3D viewport the user is working in."""
    area = getattr(context, "area", None)
    if area is not None and area.type == "VIEW_3D":
        space = area.spaces.active
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        return space, space.region_3d, region
    screen = getattr(context, "screen", None)
    best = None
    for a in (screen.areas if screen else ()):
        if a.type == "VIEW_3D":
            region = next((r for r in a.regions if r.type == "WINDOW"), None)
            size = (region.width * region.height) if region else 0
            if best is None or size > best[3]:
                best = (a.spaces.active, a.spaces.active.region_3d, region, size)
    return best[:3] if best else (None, None, None)


def view_from_context(context, target):
    """Painter view payload for the user's viewport (or the scene camera), or None."""
    depsgraph = context.evaluated_depsgraph_get()
    centre, radius, _ = world_bounds(target, depsgraph)
    space, rv3d, region = _find_view3d(context)
    scene = context.scene
    aspect = (region.width / max(region.height, 1)) if region else 16 / 9

    if rv3d is not None and rv3d.view_perspective != "CAMERA":
        m = rv3d.view_matrix.inverted()
        pos = m.translation
        fwd = m.to_3x3() @ Vector((0, 0, -1))
        up = m.to_3x3() @ Vector((0, 1, 0))
        ortho = rv3d.view_perspective == "ORTHO"
        fov_wide = painter_link.fov_from_lens(space.lens, painter_link.VIEWPORT_SENSOR)
        fov_h, fov_v = painter_link.split_fov(fov_wide, aspect)
        ortho_h = 0.0
        if ortho:
            # viewport ortho: view_distance spans the wider side like a 72 mm lens
            wide = rv3d.view_distance * painter_link.VIEWPORT_SENSOR / space.lens
            ortho_h = wide / aspect if aspect >= 1 else wide
            pos = rv3d.view_location - fwd * max(radius * 4, rv3d.view_distance)
        return painter_link.view_payload(tuple(pos), tuple(fwd), tuple(up),
                                         fov_h, fov_v, ortho, ortho_h)

    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        return None
    m = cam.matrix_world.normalized()
    pos = m.translation
    fwd = m.to_3x3() @ Vector((0, 0, -1))
    up = m.to_3x3() @ Vector((0, 1, 0))
    cd = cam.data
    r = scene.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / max(1, r.resolution_y * r.pixel_aspect_y)
    ortho = cd.type == "ORTHO"
    if cd.sensor_fit == "VERTICAL":
        fov_v = painter_link.fov_from_lens(cd.lens, cd.sensor_height)
        fov_h = math.degrees(2 * math.atan(math.tan(math.radians(fov_v) / 2) * aspect))
    else:
        sensor = cd.sensor_width
        if cd.sensor_fit == "HORIZONTAL" or aspect >= 1:
            fov_h = painter_link.fov_from_lens(cd.lens, sensor)
            fov_v = math.degrees(2 * math.atan(math.tan(math.radians(fov_h) / 2) / aspect))
        else:
            fov_v = painter_link.fov_from_lens(cd.lens, sensor)
            fov_h = math.degrees(2 * math.atan(math.tan(math.radians(fov_v) / 2) * aspect))
    ortho_h = (cd.ortho_scale / aspect if aspect >= 1 else cd.ortho_scale) if ortho else 0.0
    return painter_link.view_payload(tuple(pos), tuple(fwd), tuple(up),
                                     fov_h, fov_v, ortho, ortho_h)


def set_name_for(ob):
    """Texture set name Painter gives an object's active material (its material name)."""
    mat = ob.active_material
    return mat.name if mat is not None else ob.name


def sets_payload(context):
    """Bounds per texture set (= material) of every visible mesh, in Painter axes.

    Only meshes the export can take (visible in the viewport too): bounds of a
    hidden mesh sharing a material would make the box larger than Painter's
    geometry and throw off framing and Match View.
    """
    depsgraph = context.evaluated_depsgraph_get()
    view_layer = context.view_layer
    rendered = _render_collections(view_layer)
    sets = {}
    for ob in context.scene.objects:
        if ob.type != "MESH" or not _render_visible(ob, view_layer, rendered):
            continue
        try:
            if not ob.visible_get(view_layer=view_layer):
                continue
        except (RuntimeError, TypeError):
            pass
        try:
            ev = ob.evaluated_get(depsgraph)
            pts = [tuple(ev.matrix_world @ Vector(c)) for c in ev.bound_box]
        except (RuntimeError, ReferenceError):
            continue
        box = painter_link.painter_aabb(pts)
        names = {sl.material.name for sl in ob.material_slots if sl.material is not None}
        for name in sorted(names or {ob.name}):
            entry = sets.setdefault(name, {"min": None, "max": None, "objects": []})
            prev = (entry["min"], entry["max"]) if entry["min"] is not None else None
            entry["min"], entry["max"] = painter_link.merge_aabb(prev, box)
            entry["objects"].append(ob.name)
    return sets


# --------------------------------------------------------------------------
# Painter plugin install
# --------------------------------------------------------------------------

def painter_documents():
    """Painter's user folder (Documents/Adobe/Adobe Substance 3D Painter)."""
    docs = ""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid
            fid = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")   # FOLDERID_Documents
            guid = (ctypes.c_byte * 16).from_buffer_copy(fid.bytes_le)
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                          ctypes.byref(ptr)) == 0:
                docs = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
        except Exception:
            docs = ""
    if not docs:
        docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs, "Adobe", "Adobe Substance 3D Painter")


def link_dir():
    """Folder both apps share: <Painter user folder>/texture_plugger_link."""
    override = os.environ.get("TP_LINK_DIR")
    if override:
        return override
    return os.path.join(painter_documents(), painter_link.LINK_DIR_NAME)


def plugin_install_path():
    return os.path.join(painter_documents(), "python", "startup", PLUGIN_NAME)


def install_plugin(force=False):
    """Copy the Painter plugin into Painter's startup folder. Returns (path, changed)."""
    dst = plugin_install_path()
    try:
        with open(PLUGIN_SRC, "rb") as fh:
            new = fh.read()
    except OSError as ex:
        raise RuntimeError(f"Plugin file missing from the add-on: {ex}")
    if not force and os.path.isfile(dst):
        try:
            with open(dst, "rb") as fh:
                if fh.read() == new:
                    return dst, False
        except OSError:
            pass
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(new)
    os.replace(tmp, dst)
    return dst, True


# --------------------------------------------------------------------------
# High level: send to Painter
# --------------------------------------------------------------------------

def _value(v):
    try:
        return tuple(round(float(x), 5) for x in v)
    except (TypeError, ValueError):
        try:
            return round(float(v), 5)
        except (TypeError, ValueError):
            return str(v)


_SKIP_PROPS = {"rna_type", "name", "label", "location", "width", "width_hidden", "height",
               "dimensions", "select", "show_options", "show_preview", "hide", "mute",
               "show_texture", "color", "use_custom_color", "parent", "inputs", "outputs",
               "internal_links", "type", "bl_idname", "bl_label", "bl_description", "bl_icon",
               "bl_static_type", "bl_width_default", "bl_width_min", "bl_width_max",
               "bl_height_default", "bl_height_min", "bl_height_max", "warning_propagation"}


def _tree_parts(tree, seen):
    """Everything in a node tree that changes what it outputs: node settings (a
    Sky Texture's sun elevation is a property, not an input), unlinked input
    values, links, images, and node groups inside it."""
    if tree is None or tree.name_full in seen:
        return []
    seen.add(tree.name_full)
    out = [("tree", tree.name_full)]
    for n in sorted(tree.nodes, key=lambda n: n.name):
        out.append((n.name, n.bl_idname))
        for prop in n.bl_rna.properties:
            k = prop.identifier
            if k in _SKIP_PROPS or prop.type == "COLLECTION":
                continue
            try:
                v = getattr(n, k)
            except AttributeError:
                continue
            if prop.type == "POINTER":
                v = getattr(v, "name_full", None) if v is not None else None
            out.append((n.name, k, _value(v)))
        for i in n.inputs:
            if not i.is_linked and hasattr(i, "default_value"):
                out.append((n.name, i.identifier, _value(i.default_value)))
        if getattr(n, "node_tree", None) is not None:
            out.extend(_tree_parts(n.node_tree, seen))
    for link in tree.links:
        out.append(("link", link.from_node.name, link.from_socket.identifier,
                    link.to_node.name, link.to_socket.identifier))
    return out


def lighting_key(context, surroundings=True):
    """Short fingerprint of what the bake is lit by: world, lamps, exposure.

    Moving other objects is not included (too costly to hash a whole scene);
    moving, adding or changing a lamp or the world is.
    """
    import hashlib
    sc = context.scene
    parts = [("exposure", _value(unlit.user_view(sc)["exposure"])),
             ("surroundings", bool(surroundings))]
    w = sc.world
    parts.append(("world", w.name_full if w else None))
    if w is not None and w.use_nodes and w.node_tree is not None:
        parts.extend(_tree_parts(w.node_tree, set()))
    elif w is not None:
        parts.append(("world colour", _value(w.color)))
    rendered = _render_collections(context.view_layer)
    for ob in sorted((o for o in sc.objects if o.type == "LIGHT"), key=lambda o: o.name_full):
        if not _render_visible(ob, context.view_layer, rendered):
            continue
        d = ob.data
        parts.append((ob.name_full, d.type, _value(ob.matrix_world.col[0]), _value(ob.matrix_world.col[1]),
                      _value(ob.matrix_world.col[2]), _value(ob.matrix_world.col[3]),
                      _value(d.energy), _value(d.color),
                      *(_value(getattr(d, a)) for a in ("size", "size_y", "shadow_soft_size",
                                                        "angle", "spot_size", "spot_blend")
                        if hasattr(d, a))))
        if getattr(d, "use_nodes", False) and d.node_tree is not None:
            parts.extend(_tree_parts(d.node_tree, set()))
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()[:16]


def send(context, target, lighting=True, view=True, scene=None):
    """Bake/record and write the link file. Returns (message, warnings)."""
    p = prefs.get_prefs(context)
    width = int(getattr(p, "env_resolution", "1024")) if p else 1024
    samples = int(getattr(p, "env_samples", 64)) if p else 64
    min_deg = float(getattr(p, "light_min_size", 2.0)) if p else 2.0
    surroundings = bool(getattr(p, "env_surroundings", True)) if p else True
    with_preview = bool(getattr(p, "env_preview", True)) if p else True
    warnings = []
    try:
        context.view_layer.update()     # matrices of objects moved by scripts
    except Exception:
        pass
    folder = link_dir()
    os.makedirs(folder, exist_ok=True)
    env_name, info, tone, key, preview = None, None, None, None, None
    if lighting:
        try:
            key = lighting_key(context, surroundings)
        except Exception:       # never lose the lighting over its fingerprint
            key = None
        env_name = f"env_{int(time.time() * 1000)}.exr"
        if key is None:         # unknown lighting: no older per-set map can be trusted
            key = env_name
        info = bake_environment(context, target, os.path.join(folder, env_name), width,
                                samples, min_deg, surroundings)
        if context.scene.world is None and info["lights"] == 0:
            warnings.append("The scene has no world and no lights: the map is black")
        vt = unlit.user_view(context.scene)["view_transform"]
        tone = painter_link.tone_mapping_for(vt)
        if with_preview:
            pv_name = f"env_preview_{int(time.time() * 1000)}.exr"
            try:
                pv = bake_preview(context, target, os.path.join(folder, pv_name), width, min_deg)
                preview = {"environment": pv_name, "exposure": pv["exposure"], "hdri": pv["hdri"]}
            except Exception as ex:      # the render lighting still goes through
                warnings.append(f"Material Preview lighting not sent: {ex}")
                preview = {}
        else:
            preview = {}
    view_data = view_from_context(context, target) if view else None
    if view and view_data is None:
        warnings.append("No 3D viewport or scene camera found: the view was not sent")
    blend = os.path.basename(bpy.data.filepath) if bpy.data.filepath else ""
    path = painter_link.write_link(
        folder, sets_payload(context), active=set_name_for(target), environment=env_name,
        exposure=info["exposure"] if info else 0.0, view=view_data, blend_name=blend,
        scene=scene, tone_mapping=tone, lighting_key=key, preview=preview)
    painter_link.prune_environments(
        folder, painter_link.referenced_environments(painter_link.read_link(path)))
    parts = []
    if info:
        where = "from a clear spot next to" if info.get("moved") else "around"
        parts.append(f"lighting {where} '{target.name}' ({info['lights']} lamp(s))")
    if preview:
        parts.append(f"Material Preview lighting ({preview.get('hdri')})")
    if view_data:
        parts.append("the view")
    msg = ("Sent " + " and ".join(parts) + " to Painter") if parts else "Sent object bounds"
    return msg, warnings


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class TEXTUREPLUGGER_OT_send_to_painter(bpy.types.Operator):
    """Send the active object's lighting (world, lamps and surroundings baked into an HDR), every object's bounds and the current view to Substance Painter, where the Texture Plugger plugin picks them up"""
    bl_idname = "texture_plugger.send_to_painter"
    bl_label = "Send to Painter"
    bl_options = {"REGISTER"}

    lighting: BoolProperty(name="Lighting", default=True, options={"SKIP_SAVE"})
    view: BoolProperty(name="View", default=True, options={"SKIP_SAVE"})

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        from . import workflow
        obj = workflow._active_object(context)
        if obj is None or obj.type != "MESH":
            self.report({"WARNING"}, "Select the mesh you want to paint")
            return {"CANCELLED"}
        try:
            _path, changed = install_plugin()
            if changed:
                self.report({"INFO"}, "Installed the Texture Plugger plugin for Painter "
                                      "(restart Painter if it is open)")
        except (OSError, RuntimeError) as ex:
            self.report({"WARNING"}, f"Could not install the Painter plugin: {ex}")
        msg, warnings = send(context, obj, self.lighting, self.view)
        for w in warnings:
            self.report({"WARNING"}, w)
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_install_painter_plugin(bpy.types.Operator):
    """Copy the Texture Plugger plugin into Substance Painter's startup folder (restart Painter to load it)"""
    bl_idname = "texture_plugger.install_painter_plugin"
    bl_label = "Install Painter Plugin"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            path, changed = install_plugin(force=True)
        except (OSError, RuntimeError) as ex:
            self.report({"ERROR"}, f"Could not install the Painter plugin: {ex}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Installed {path} (restart Painter if it is running)")
        return {"FINISHED"}


classes = (TEXTUREPLUGGER_OT_send_to_painter, TEXTUREPLUGGER_OT_install_painter_plugin)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
