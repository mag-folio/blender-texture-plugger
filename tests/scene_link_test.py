"""Headless Blender test of the Painter link (lighting bake, bounds, view).

Run via:  .\\tests\\run_blender_tests.ps1 -Script scene_link_test.py
Writes only to a temp folder (TP_LINK_DIR); never touches Painter's real folder.
"""

import importlib
import json
import math
import os
import shutil
import sys
import tempfile

import bpy
import numpy as np

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
MODULE = "bl_ext.user_default.texture_plugger"
TMP = tempfile.mkdtemp(prefix="tp link ü_")
os.environ["TP_LINK_DIR"] = os.path.join(TMP, "link")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  -- {detail}"))
    return bool(cond)


def install():
    if "--zip" in ARGS:
        zip_path = os.path.abspath(ARGS[ARGS.index("--zip") + 1])
        bpy.ops.extensions.package_install_files(filepath=zip_path, repo="user_default",
                                                 enable_on_install=True)
        return importlib.import_module(MODULE)
    return importlib.import_module("texture_plugger")


def cube(name, loc, size, mat_name):
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    m = bpy.data.materials.new(mat_name)
    ob.data.materials.append(m)
    return ob


def build_scene(world_strength=0.0):
    # clear by hand: read_factory_settings would also disable the extension
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    sc = bpy.context.scene
    w = bpy.data.worlds.new("W")
    sc.world = w
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[1].default_value = world_strength
    crate = cube("Crate", (0, 0, 0), 2.0, "Crate")
    cube("Barrel", (4, 0, 0), 1.0, "Barrel")
    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = 3.0
    sun.color = (1.0, 0.5, 0.25)
    sun.angle = math.radians(1.0)
    so = bpy.data.objects.new("Sun", sun)
    sc.collection.objects.link(so)
    # sun pointing down -Z rotated: light comes FROM direction (+X, +Z) at 45 deg
    so.rotation_euler = (0.0, math.radians(45.0), 0.0)
    cd = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cd)
    sc.collection.objects.link(cam)
    cam.location = (0.0, -10.0, 0.0)
    cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)   # looks along +Y
    sc.camera = cam
    for o in sc.objects:
        o.select_set(False)
    bpy.context.view_layer.objects.active = crate
    crate.select_set(True)
    return crate, so


def test(mod):
    pl = mod.painter_link
    sl = mod.scene_link
    crate, sun = build_scene(0.0)       # factory reset first: it also resets add-on prefs
    p = mod.prefs.get_prefs()
    check("prefs have lighting options", p is not None and hasattr(p, "env_resolution"))
    p.env_resolution = "512"
    p.env_samples = 16
    p.env_surroundings = False
    bpy.context.view_layer.update()
    for scr_area in bpy.context.window.screen.areas if bpy.context.window else ():
        if scr_area.type == "VIEW_3D":
            scr_area.spaces.active.region_3d.view_perspective = "CAMERA"
    print("  INFO  sun dir", tuple(round(x, 3) for x in sun.matrix_world.to_3x3().col[2]))
    sun_dir = (sun.matrix_world.to_3x3() @ __import__("mathutils").Vector((0, 0, 1))).normalized()
    msg, warns = sl.send(bpy.context, crate)
    print("  INFO ", msg, warns)
    link = os.path.join(os.environ["TP_LINK_DIR"], pl.LINK_FILE)
    data = pl.read_link(link)
    check("link file written", data is not None, link)
    if data is None:
        return
    check("format version", data.get("version") == pl.FORMAT_VERSION)
    check("active set is the object's material", data.get("active") == "Crate", data.get("active"))
    sets = data.get("sets", {})
    check("bounds for every material", set(sets) == {"Crate", "Barrel"}, sorted(sets))
    c = sets.get("Crate", {})
    check("Crate bounds in Painter axes",
          all(abs(a - b) < 1e-4 for a, b in zip(c.get("min", []), [-1, -1, -1]))
          and all(abs(a - b) < 1e-4 for a, b in zip(c.get("max", []), [1, 1, 1])), c)
    b = sets.get("Barrel", {})
    check("Barrel bounds offset along Painter X",
          abs(b.get("min", [0])[0] - 3.5) < 1e-4 and abs(b.get("max", [0])[0] - 4.5) < 1e-4, b)
    check("environment stored per set", sets.get("Crate", {}).get("environment") == data.get("environment"))
    check("other set has no lighting of its own", "environment" not in sets.get("Barrel", {}))
    view = data.get("view") or {}
    check("view from scene camera (no viewport in background)",
          view and all(abs(a - e) < 1e-4 for a, e in zip(view["position"], [0, 0, 10]))
          and all(abs(a - e) < 1e-4 for a, e in zip(view["forward"], [0, 0, -1]))
          and all(abs(a - e) < 1e-4 for a, e in zip(view["up"], [0, 1, 0])), view)

    env_path = os.path.join(os.environ["TP_LINK_DIR"], data.get("environment", "?"))
    check("environment EXR exists", os.path.isfile(env_path), env_path)
    img = bpy.data.images.load(env_path)
    w, h = img.size
    check("map size 512x256", (w, h) == (512, 256), (w, h))
    arr = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(arr)
    arr = arr.reshape(h, w, 4)[..., :3]
    # undo the Painter roll to get back to the panorama mapping
    shift = int(round(pl.ENV_YAW_DEG / 360.0 * w))
    pano = np.roll(arr, -shift, axis=1)
    if pl.ENV_MIRROR:
        pano = pano[:, ::-1]
    lum = pano.sum(axis=2)
    row, col = np.unravel_index(np.argmax(lum), lum.shape)
    d = pl.uv_to_dir((col + 0.5) / w, (row + 0.5) / h)
    ang = math.degrees(math.acos(max(-1, min(1, sum(a * b for a, b in zip(d, sun_dir))))))
    check("brightest pixel points at the sun", ang < 3.0, f"{ang:.2f} deg, at {tuple(round(x,3) for x in d)} want {tuple(round(x,3) for x in sun_dir)}")
    # irradiance on a plane facing the sun = sum L * omega * cos
    E = np.zeros(3)
    for r in range(h):
        om = pl.pixel_solid_angle(r, h, w)
        for cc in range(w):
            if lum[r, cc] <= 0:
                continue
            dd = pl.uv_to_dir((cc + 0.5) / w, (r + 0.5) / h)
            cs = sum(a * b for a, b in zip(dd, sun_dir))
            if cs > 0:
                E += pano[r, cc] * om * cs
    want = np.array([3.0, 1.5, 0.75])
    rel = np.abs(E - want) / want
    check("map delivers the sun's irradiance (Cycles strength = W/m2)",
          float(rel.max()) < 0.06, f"E={E.round(3)} want={want}")
    bpy.data.images.remove(img)

    # sending the view only keeps the lighting
    env_before = data.get("environment")
    sl.send(bpy.context, crate, lighting=False, view=True)
    data2 = pl.read_link(link)
    check("view-only send keeps lighting", data2.get("environment") == env_before)

    # a second object gets its own lighting; the first keeps its own
    barrel = bpy.data.objects["Barrel"]
    sl.send(bpy.context, barrel, lighting=True, view=False)
    data3 = pl.read_link(link)
    s3 = data3["sets"]
    check("per-object lighting kept for both sets",
          s3["Crate"].get("environment") == env_before and s3["Barrel"].get("environment")
          and s3["Barrel"]["environment"] != env_before, s3)
    files = sorted(f for f in os.listdir(os.environ["TP_LINK_DIR"]) if f.startswith("env_"))
    check("only referenced maps kept on disk", len(files) == 2, files)

    # world + surroundings bake: no crash, emitted world shows up, user scene untouched
    sc = bpy.context.scene
    sc.world.node_tree.nodes["Background"].inputs[1].default_value = 0.5
    engine, res = sc.render.engine, (sc.render.resolution_x, sc.render.resolution_y)
    p.env_surroundings = True
    n_scenes = len(bpy.data.scenes)
    sl.send(bpy.context, crate)
    check("user render settings untouched",
          sc.render.engine == engine and (sc.render.resolution_x, sc.render.resolution_y) == res)
    check("temp scenes removed", len(bpy.data.scenes) == n_scenes, len(bpy.data.scenes))
    check("no temp objects left", not [o.name for o in bpy.data.objects if o.name.startswith("TP_")],
          [o.name for o in bpy.data.objects if o.name.startswith("TP_")])

    # plugin install goes to the given folder
    sl_dst = os.path.join(TMP, "painter", "python", "startup", sl.PLUGIN_NAME)
    orig = sl.plugin_install_path
    sl.plugin_install_path = lambda: sl_dst
    try:
        path, changed = sl.install_plugin()
        check("plugin installed", changed and os.path.isfile(sl_dst))
        _p, changed2 = sl.install_plugin()
        check("plugin install idempotent", not changed2)
    finally:
        sl.plugin_install_path = orig


def main():
    print(f"Blender {bpy.app.version_string}  scene link")
    try:
        mod = install()
        test(mod)
    except Exception:
        import traceback
        traceback.print_exc()
        check("no exception", False)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        sys.exit(1)


main()
