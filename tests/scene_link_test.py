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
    render = [f for f in files if not f.startswith("env_preview_")]
    check("only referenced maps kept on disk (2 render + the latest Material Preview)",
          len(render) == 2 and len(files) == 3, files)

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
    enclosed(mod)
    occluders(mod)
    fingerprint(mod)
    preview(mod)


def _env_mean(path):
    img = bpy.data.images.load(path)
    try:
        px = np.empty(img.size[0] * img.size[1] * 4, np.float32)
        img.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(img)
    return float(px.reshape(-1, 4)[:, :3].mean())


def _sun(name, rot, energy=3.0):
    sun = bpy.data.lights.new(name, "SUN")
    sun.energy = energy
    so = bpy.data.objects.new(name, sun)
    bpy.context.scene.collection.objects.link(so)
    so.rotation_euler = rot
    return so


def enclosed(mod):
    """A small part in a nook: measured at its centre while light reaches it,
    moved to the nearest clear spot only when no lamp does."""
    sl = mod.scene_link
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    sc = bpy.context.scene
    sc.world.node_tree.nodes["Background"].inputs[1].default_value = 0.2
    screw = cube("Screw", (0, 0, 0), 0.04, "Screw")
    # a 30 cm box open at the top (five walls)
    for i, (loc, scale) in enumerate((((0, 0, -0.15), (0.16, 0.16, 0.01)),
                                      ((0.15, 0, 0), (0.01, 0.16, 0.16)), ((-0.15, 0, 0), (0.01, 0.16, 0.16)),
                                      ((0, 0.15, 0), (0.16, 0.01, 0.16)), ((0, -0.15, 0), (0.16, 0.01, 0.16)))):
        wall = cube(f"Wall{i}", loc, 2.0, f"Wall{i}")
        wall.scale = scale
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    centre, radius, _ = sl.world_bounds(screw, dg)
    check("part in a nook counts as boxed in", sl.boxed_in(bpy.context, screw, dg, centre, radius))

    # sun from high up shines into the nook: keep the centre
    high = _sun("SunHigh", (0.3, 0.4, 0.0))
    info = sl.bake_environment(bpy.context, screw, os.path.join(TMP, "nook_lit.exr"), 128, 4, 2.0, True)
    check("lit nook is measured at the part itself", not info["moved"] and info["lights"] == 1, info)
    bpy.data.objects.remove(high, do_unlink=True)

    # low sun hits the walls: at the centre no lamp reaches, so move out of the nook
    _sun("SunLow", (1.45, 0.0, 0.0))
    bpy.context.view_layer.update()
    out = os.path.join(TMP, "nook_dark.exr")
    info = sl.bake_environment(bpy.context, screw, out, 128, 4, 2.0, True)
    mean = _env_mean(out)
    check("dark nook moves to a clear spot that the sun reaches",
          info["moved"] and info["lights"] == 1 and mean > 0.05, (info, mean))

    # an object out in the open, or resting on a floor, is never boxed in
    free = cube("Free", (3, 0, 0), 0.5, "Free")
    floor = cube("Floor", (-3, 0, -0.26), 2.0, "Floor")
    floor.scale = (1.0, 1.0, 0.01)
    mug = cube("Mug", (-3, 0, 0), 0.5, "Mug")
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    for ob in (free, mug):
        c2, r2, _ = sl.world_bounds(ob, dg)
        check(f"{ob.name}: not boxed in", not sl.boxed_in(bpy.context, ob, dg, c2, r2))


def occluders(mod):
    """Shadows in the lamp measurement match a render: glass lets the sun in,
    and walls hidden only in the viewport still cast shadows."""
    sl = mod.scene_link
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    sc = bpy.context.scene
    target = cube("Target", (0, 0, 0), 0.2, "Target")
    _sun("SunTop", (0.0, 0.0, 0.0))              # straight down
    pane = cube("Window", (0, 0, 1.0), 2.0, "WindowGlass")
    pane.scale = (1.0, 1.0, 0.01)
    m = pane.active_material
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tr.outputs[0], o.inputs[0])
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    c, r, _ = sl.world_bounds(target, dg)
    got = sl.measure_lights(sc, bpy.context.view_layer, target, c, r, samples=4)
    check("sun through a clear window is measured", len(got) == 1 and max(got[0][3]) > 1.0, got)

    roof = cube("Roof", (0, 0, 1.5), 2.0, "Roof")
    roof.scale = (1.0, 1.0, 0.01)
    roof.hide_set(True)                          # hidden in the viewport, still renders
    bpy.context.view_layer.update()
    got = sl.measure_lights(sc, bpy.context.view_layer, target, c, r, samples=4)
    check("roof hidden only in the viewport still shades", not got, got)
    roof.hide_render = True
    got = sl.measure_lights(sc, bpy.context.view_layer, target, c, r, samples=4)
    check("roof off for rendering lets the sun in", len(got) == 1, got)

    # a real glass pane (refractive) shades the sun, exactly as a Cycles render does
    roof.hide_render = False
    roof.hide_set(False)
    bpy.data.objects.remove(roof, do_unlink=True)
    nt.nodes.clear()
    gl = nt.nodes.new("ShaderNodeBsdfGlass")
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(gl.outputs[0], o.inputs[0])
    got = sl.measure_lights(sc, bpy.context.view_layer, target, c, r, samples=4)
    print("  INFO  sun through refractive glass:", got and [round(x, 3) for x in got[0][3]])

    # a glowing panel next to the target must not make a blocked lamp count
    nt.nodes.clear()
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    df = nt.nodes.new("ShaderNodeBsdfDiffuse")
    nt.links.new(df.outputs[0], o.inputs[0])                # the pane is opaque now
    glow = cube("Glow", (0.4, 0, 0.3), 0.3, "Glow")
    gm = glow.active_material
    gm.use_nodes = True
    gm.node_tree.nodes.clear()
    em = gm.node_tree.nodes.new("ShaderNodeEmission")
    em.inputs["Strength"].default_value = 20.0
    go = gm.node_tree.nodes.new("ShaderNodeOutputMaterial")
    gm.node_tree.links.new(em.outputs[0], go.inputs[0])
    bpy.context.view_layer.update()
    got = sl.measure_lights(sc, bpy.context.view_layer, target, c, r, samples=4)
    check("glowing mesh does not turn a blocked lamp on", not got, got)


def fingerprint(mod):
    """The lighting key changes with the lighting and only with it."""
    sl = mod.scene_link
    ctx = bpy.context
    sc = ctx.scene
    w = sc.world
    sky = w.node_tree.nodes.new("ShaderNodeTexSky")
    w.node_tree.links.new(sky.outputs[0], w.node_tree.nodes["Background"].inputs[0])
    lamp = next(o for o in sc.objects if o.type == "LIGHT")
    k1 = sl.lighting_key(ctx)
    check("lighting key is stable", sl.lighting_key(ctx) == k1)
    sc.objects["Target"].location.x += 1.0
    ctx.view_layer.update()
    check("moving a non-lamp object keeps the key", sl.lighting_key(ctx) == k1)
    if hasattr(sky, "sun_elevation"):
        sky.sun_elevation += 0.2
        k2 = sl.lighting_key(ctx)
        check("sky sun elevation changes the key", k2 != k1)
        k1 = k2
    lamp.rotation_euler.x += 0.3
    ctx.view_layer.update()
    k3 = sl.lighting_key(ctx)
    check("turning a lamp changes the key", k3 != k1)
    lamp.data.energy *= 2
    check("lamp strength changes the key", sl.lighting_key(ctx) != k3)


def preview(mod):
    """Material Preview lighting: the studio HDRI, baked and linked for Painter."""
    sl = mod.scene_link
    ctx = bpy.context
    st = sl.preview_settings(ctx)
    check("headless: default Material Preview HDRI found",
          st["name"] == sl.DEFAULT_LOOKDEV and st["hdri"] and os.path.isfile(st["hdri"]), st)
    target = next(o for o in ctx.scene.objects if o.type == "MESH")
    n_worlds, n_images = len(bpy.data.worlds), len(bpy.data.images)
    out = os.path.join(TMP, "preview.exr")
    info = sl.bake_preview(ctx, target, out, 256)
    mean = _env_mean(out)
    check("preview map baked from the studio HDRI",
          info["hdri"] == sl.DEFAULT_LOOKDEV and info["lights"] == 0 and mean > 0.05, (info, mean))
    check("preview bake leaves no temp world or image",
          (len(bpy.data.worlds), len(bpy.data.images)) == (n_worlds, n_images),
          (len(bpy.data.worlds), len(bpy.data.images)))
    msg, warns = sl.send(ctx, target)
    data = mod.painter_link.read_link(mod.painter_link.link_file(sl.link_dir()))
    pv = (data or {}).get("preview") or {}
    check("send links render and Material Preview lighting",
          pv.get("hdri") == sl.DEFAULT_LOOKDEV and pv.get("environment") != data.get("environment")
          and os.path.isfile(os.path.join(sl.link_dir(), pv.get("environment", "?"))), (msg, warns, pv))


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
