# SPDX-License-Identifier: GPL-3.0-or-later
"""Send Blender's lighting and view to Substance 3D Painter.

* ``bake_environment`` renders what the object "sees" (world + the rest of the
  scene, lit by your lights) as an equirectangular HDR with Cycles, from the
  object's centre with the object itself hidden.  Lamps are invisible to camera
  rays, so each one is measured separately (a tiny white probe at the object's
  centre, direct light only, real shadows from the scene) and painted into the
  map as a bright disc that delivers exactly that light.
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

from . import painter_link, prefs
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


def _render_visible(ob, view_layer):
    if ob.hide_render:
        return False
    try:
        if not ob.visible_get(view_layer=view_layer):
            return False
    except (RuntimeError, TypeError):
        pass
    return not any(c.hide_render for c in ob.users_collection)


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


def measure_lights(src, view_layer, target, centre, radius, samples=32, occluders=True):
    """Direct irradiance (RGB) each visible lamp delivers at ``centre``.

    Returns ``[(light_object, direction, distance, (r, g, b))]``; lamps that
    deliver nothing there (behind an area light, outside a spot cone, fully
    shadowed) are left out.
    """
    lights = [ob for ob in src.objects
              if ob.type == "LIGHT" and _render_visible(ob, view_layer)
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
        white = bpy.data.materials.new("TP_ProbeWhite")
        white.use_nodes = True
        nt = white.node_tree
        nt.nodes.clear()
        bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
        bsdf.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs[0], out.inputs[0])
        datas.append(white)
        sc.view_layers[0].material_override = white     # nothing else emits

        if occluders:
            for ob in src.objects:
                if ob.type == "LIGHT" or ob == target or ob.type == "CAMERA":
                    continue
                if _render_visible(ob, view_layer):
                    sc.collection.objects.link(ob)

        s = radius * 1e-3
        me = bpy.data.meshes.new("TP_Probe")
        me.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
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
            sc.collection.objects.link(lo)
            try:
                arr, _, _ = _render_to_array(sc, tmp)
            finally:
                sc.collection.objects.unlink(lo)
            rgb = arr[..., :3].reshape(-1, 3).mean(axis=0) * math.pi   # radiance -> irradiance
            if float(np.max(rgb)) <= 1e-6:
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

def bake_environment(context, target, out_path, width=1024, samples=64,
                     min_light_deg=2.0, surroundings=True):
    """Bake the lighting around ``target`` into ``out_path`` (EXR, Painter orientation).

    Returns a dict with ``lights`` (count), ``exposure`` and ``path``.
    """
    import numpy as np

    src = context.scene
    view_layer = context.view_layer
    depsgraph = context.evaluated_depsgraph_get()
    centre, radius, _size = world_bounds(target, depsgraph)
    width = int(max(64, width)) // 2 * 2
    height = width // 2

    # 1) world + surroundings
    sc = _new_scene("TP_EnvBake", src)
    made = []
    try:
        sc.world = src.world
        sc.cycles.samples = samples
        sc.cycles.use_denoising = True
        sc.render.resolution_x, sc.render.resolution_y = width, height
        for ob in src.objects:
            if ob == target or ob.type == "CAMERA" or not _render_visible(ob, view_layer):
                continue
            if ob.type == "LIGHT" or surroundings:
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
    exposure = float(getattr(src.view_settings, "exposure", 0.0))
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
    return {"lights": len(lights), "exposure": exposure, "path": out_path}


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
    """Bounds per texture set (= material) of every visible mesh, in Painter axes."""
    depsgraph = context.evaluated_depsgraph_get()
    view_layer = context.view_layer
    sets = {}
    for ob in context.scene.objects:
        if ob.type != "MESH" or not _render_visible(ob, view_layer):
            continue
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

def send(context, target, lighting=True, view=True):
    """Bake/record and write the link file. Returns (message, warnings)."""
    p = prefs.get_prefs(context)
    width = int(getattr(p, "env_resolution", "1024")) if p else 1024
    samples = int(getattr(p, "env_samples", 64)) if p else 64
    min_deg = float(getattr(p, "light_min_size", 2.0)) if p else 2.0
    surroundings = bool(getattr(p, "env_surroundings", True)) if p else True
    warnings = []
    try:
        context.view_layer.update()     # matrices of objects moved by scripts
    except Exception:
        pass
    folder = link_dir()
    os.makedirs(folder, exist_ok=True)
    env_name, info = None, None
    if lighting:
        env_name = f"env_{int(time.time() * 1000)}.exr"
        info = bake_environment(context, target, os.path.join(folder, env_name), width,
                                samples, min_deg, surroundings)
        if context.scene.world is None and info["lights"] == 0:
            warnings.append("The scene has no world and no lights: the map is black")
        vt = getattr(context.scene.view_settings, "view_transform", "")
        if vt not in {"Standard", ""}:
            warnings.append(f"Blender uses the '{vt}' view transform; Painter tone maps "
                            "differently, so contrast can look a little different")
    view_data = view_from_context(context, target) if view else None
    if view and view_data is None:
        warnings.append("No 3D viewport or scene camera found: the view was not sent")
    blend = os.path.basename(bpy.data.filepath) if bpy.data.filepath else ""
    path = painter_link.write_link(
        folder, sets_payload(context), active=set_name_for(target), environment=env_name,
        exposure=info["exposure"] if info else 0.0, view=view_data, blend_name=blend)
    painter_link.prune_environments(
        folder, painter_link.referenced_environments(painter_link.read_link(path)))
    parts = []
    if info:
        parts.append(f"lighting around '{target.name}' ({info['lights']} lamp(s))")
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
