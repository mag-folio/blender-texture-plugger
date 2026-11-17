"""Diagnostic: how close is the lighting sent to Painter to Blender's real render?

Not a pass/fail test: it prints numbers.  It builds a small lit room, bakes the
Painter lighting map around a few objects exactly as Send to Painter does, then
renders each object twice with Cycles from the same camera:

* REF  - the object in the real scene (lamps, world, other objects, shadows).
* MAP  - the object alone, lit only by its baked map as the world (the best
         Painter could ever show with that map: same renderer, same material).

The gap between the two is the map's own error (one viewpoint at the object's
centre, lamps as discs).  What is left after that is Painter's display (its
shader and tone mapping), which Blender cannot measure.  Also compared: the
view transforms (AgX vs the ACES Painter offers) and the Material Preview map
against an EEVEE render with the same studio HDRI.

Run:  .\\tests\\run_blender_tests.ps1 -Script lighting_match_diag.py
Part 2 (Material Preview map vs its HDRI, view transforms): set TP_DIAG_PART=2 first.

The Material Preview map has no surroundings on purpose (Material Preview shows
only the studio light), so "map vs Cycles room" there includes the room's bounce.
"""

import importlib
import math
import os
import shutil
import sys
import tempfile
import traceback

import bpy
import numpy as np
from mathutils import Vector

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
MODULE = "bl_ext.user_default.texture_plugger"
TMP = tempfile.mkdtemp(prefix="tp_light_diag_")
os.environ["TP_LINK_DIR"] = os.path.join(TMP, "link")
RES = 160
SAMPLES = 64
ENV_W = 512


def install():
    if "--zip" in ARGS:
        zip_path = os.path.abspath(ARGS[ARGS.index("--zip") + 1])
        bpy.ops.extensions.package_install_files(filepath=zip_path, repo="user_default",
                                                 enable_on_install=True)
        return importlib.import_module(MODULE)
    return importlib.import_module("texture_plugger")


# --------------------------------------------------------------------------
# Scene
# --------------------------------------------------------------------------

def material(name, color, rough, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    return m


def add(kind, name, loc, scale=(1, 1, 1), mat=None, **kw):
    if kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, location=loc, **kw)
        bpy.ops.object.shade_smooth()
    else:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = scale
    if mat is not None:
        ob.data.materials.append(mat)
    return ob


def build():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    sc.view_settings.exposure = 0.0
    w = bpy.data.worlds.new("Sky")
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.35, 0.5, 0.8, 1.0)
    bg.inputs[1].default_value = 0.6
    sc.world = w

    grey = material("Floor", (0.5, 0.5, 0.5), 0.8)
    red = material("Wall", (0.7, 0.15, 0.1), 0.9)
    white = material("White", (0.8, 0.8, 0.8), 1.0)
    glossy = material("Glossy", (0.8, 0.8, 0.8), 0.25)
    add("cube", "Floor", (0, 0, -0.05), (12, 12, 0.1), grey)
    add("cube", "Wall", (-3.0, 0, 1.5), (0.2, 8, 3), red)

    sun_d = bpy.data.lights.new("Sun", "SUN")
    sun_d.energy = 3.0
    sun_d.angle = math.radians(1.0)
    sun = bpy.data.objects.new("Sun", sun_d)
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(35))
    sc.collection.objects.link(sun)
    area_d = bpy.data.lights.new("Area", "AREA")
    area_d.energy = 150.0
    area_d.size = 1.0
    area_d.color = (1.0, 0.75, 0.5)
    area = bpy.data.objects.new("Area", area_d)
    area.location = (2.0, 2.5, 2.5)
    area.rotation_euler = (math.radians(-35), math.radians(25), 0.0)
    sc.collection.objects.link(area)
    bpy.context.view_layer.update()

    to_sun = (sun.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
    h = Vector((to_sun.x, to_sun.y, 0)).normalized()
    side = Vector((-h.y, h.x, 0))

    targets = {}
    targets["SphereOpen"] = add("sphere", "SphereOpen", (1.2, -1.5, 0.4), mat=white, radius=0.4)
    # half in the shadow of a pillar standing between it and the sun
    sp = Vector((-0.8, 1.0, 0.4))
    targets["SphereHalfShadow"] = add("sphere", "SphereHalfShadow", sp, mat=white, radius=0.4)
    add("cube", "Pillar", sp + h * 1.1 + side * 0.45 + Vector((0, 0, 0.6)), (0.6, 0.6, 2.0), grey)
    # a big flat slab with the shadow of a crate across part of it
    slab_c = Vector((2.4, 1.6, 0.03))
    targets["SlabCrossShadow"] = add("cube", "SlabCrossShadow", slab_c, (2.4, 2.4, 0.06), white)
    add("cube", "Crate", slab_c + h * 1.9 + Vector((0, 0, 0.5)), (0.8, 0.8, 1.0), red)
    targets["SphereGlossy"] = add("sphere", "SphereGlossy", (0.2, -2.6, 0.4), mat=glossy, radius=0.4)
    bpy.context.view_layer.update()
    up = Vector((0, 0, 1))
    views = {"SphereOpen": (-side + up * 0.6, 6.0),           # side-on: lit and dark halves
             "SphereHalfShadow": (-side + up * 0.6, 6.0),     # the pillar is on +side
             "SlabCrossShadow": (-h * 0.5 + up * 1.6, 3.2),   # from above, crate behind
             "SphereGlossy": (-side - h * 0.3 + up * 0.5, 6.0)}
    return sc, targets, {"sun": to_sun, "area": area, "views": views}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def camera_for(sc, target, off, dist):
    """Camera looking at ``target`` from direction ``off``, ``dist`` x its radius away
    (chosen so nothing else stands in between)."""
    cd = bpy.data.cameras.new("DiagCam")
    cd.lens = 50
    cam = bpy.data.objects.new("DiagCam", cd)
    sc.collection.objects.link(cam)
    c = target.matrix_world.translation
    r = max(target.dimensions) * 0.5
    cam.location = c + off.normalized() * (r * dist)
    cam.rotation_euler = (c - cam.location).to_track_quat("-Z", "Y").to_euler()
    return cam


def render(sc, path):
    sc.render.resolution_x = sc.render.resolution_y = RES
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "OPEN_EXR"
    sc.render.image_settings.color_depth = "32"
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True, scene=sc.name)
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    buf = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    return buf.reshape(h, w, 4)


def lum(a):
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def compare(ref, test, mask):
    lr, lt = lum(ref)[mask], lum(test)[mask]
    mr = float(lr.mean())
    rel = np.abs(lt - lr) / np.maximum(lr, 0.03)
    chan = [float(test[..., i][mask].mean() / max(ref[..., i][mask].mean(), 1e-6)) for i in range(3)]
    return {"bright": float(lt.mean() / max(mr, 1e-6)),
            "rmse": float(np.sqrt(((lt - lr) ** 2).mean()) / max(mr, 1e-6)),
            "bad": float((rel > 0.25).mean()),
            "rgb": chan}


def fmt(m):
    return (f"brightness {m['bright']:5.2f}x  rel.RMSE {m['rmse'] * 100:5.1f}%  "
            f"pixels off >25%: {m['bad'] * 100:5.1f}%  R/G/B {m['rgb'][0]:.2f}/{m['rgb'][1]:.2f}/{m['rgb'][2]:.2f}")


def map_world(map_path, roll_half=True, divide_exposure=0.0, name="MapWorld"):
    """World lit by a Painter map, turned back to Blender's equirect convention.

    The map is the camera panorama (u = 0.5 + atan2(x, y)/2pi) rolled by
    ENV_YAW_DEG (a quarter turn); Blender's world uses u = 0.5 - atan2(y, x)/2pi,
    which is the same thing half a turn away.
    """
    src = bpy.data.images.load(map_path, check_existing=False)
    w, h = src.size
    a = np.empty(w * h * 4, dtype=np.float32)
    src.pixels.foreach_get(a)
    bpy.data.images.remove(src)
    a = a.reshape(h, w, 4)
    if roll_half:
        a = np.roll(a, w // 2, axis=1)
    a[..., :3] /= 2.0 ** divide_exposure
    img = bpy.data.images.new(name, w, h, alpha=True, float_buffer=True)
    img.pixels.foreach_set(a.ravel())
    wd = bpy.data.worlds.new(name)
    wd.use_nodes = True
    nt = wd.node_tree
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.image = img
    env.interpolation = "Linear"
    nt.links.new(env.outputs["Color"], nt.nodes["Background"].inputs["Color"])
    nt.nodes["Background"].inputs["Strength"].default_value = 1.0
    return wd, a


def isolated_scene(sl, src, target, world, cam_src):
    s = sl._new_scene("DiagIso", src)
    s.cycles.samples = SAMPLES
    s.cycles.use_denoising = True
    s.render.film_transparent = True
    s.world = world
    s.collection.objects.link(target)
    cd = cam_src.data
    cam = bpy.data.objects.new("DiagIsoCam", cd)
    cam.matrix_world = cam_src.matrix_world.copy()
    s.collection.objects.link(cam)
    s.camera = cam
    return s, cam


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(mod):
    sl = mod.scene_link
    sc, targets, lights = build()
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    ctx = bpy.context
    print(f"\nRender {RES}px, {SAMPLES} spp denoised; maps {ENV_W}x{ENV_W // 2} as Send to Painter makes them\n")
    print("=== Render lighting: object lit by its baked map vs the real scene (Cycles both) ===")
    keep = {}
    for name, ob in targets.items():
        path = os.path.join(TMP, f"{name}_map.exr")
        info = sl.bake_environment(ctx, ob, path, ENV_W, 64, 2.0, True)
        cam = camera_for(sc, ob, *lights["views"][name])
        sc.camera = cam
        ref = render(sc, os.path.join(TMP, f"{name}_ref.exr"))
        world, env = map_world(path)
        iso, icam = isolated_scene(sl, sc, ob, world, cam)
        test = render(iso, os.path.join(TMP, f"{name}_map_render.exr"))
        mask = test[..., 3] > 0.99
        m = compare(ref, test, mask)
        print(f"  {name:17s} {fmt(m)}  (lamps in map: {info['lights']}, moved: {info['moved']})")
        keep[name] = (ref, test, mask, env, path, cam)
        if name == "SphereOpen":
            # orientation check: same map without the half-turn should be clearly worse
            w2, _ = map_world(path, roll_half=False, name="MapWorldRaw")
            iso.world = w2
            t2 = render(iso, os.path.join(TMP, f"{name}_raw.exr"))
            m2 = compare(ref, t2, mask)
            print(f"  {'  (unturned map)':17s} {fmt(m2)}   <- should be worse: orientation check")
            # where does the brightest part of the map point?
            hgt, wid = env.shape[:2]
            lm = lum(env)
            r, c = np.unravel_index(int(np.argmax(lm)), lm.shape)
            u, v = (c + 0.5) / wid, (r + 0.5) / hgt
            phi = math.pi - 2 * math.pi * u          # Blender world convention
            lat = (v - 0.5) * math.pi
            d = Vector((math.cos(lat) * math.cos(phi), math.cos(lat) * math.sin(phi), math.sin(lat)))
            print(f"  brightest map pixel vs sun direction: dot {d.dot(lights['sun']):.3f} (1 = same)")
        for o in list(iso.collection.objects):
            iso.collection.objects.unlink(o)
        bpy.data.objects.remove(icam)
        bpy.data.scenes.remove(iso)

    # Where the error sits: lit vs shadowed parts of the half-shadowed sphere
    ref, test, mask, *_ = keep["SphereHalfShadow"]
    lr = lum(ref)
    lit = mask & (lr > np.median(lr[mask]))
    dark = mask & (lr <= np.median(lr[mask]))
    print(f"\n  SphereHalfShadow, brighter half: {fmt(compare(ref, test, lit))}")
    print(f"  SphereHalfShadow, darker half:   {fmt(compare(ref, test, dark))}")
    ref, test, mask, *_ = keep["SlabCrossShadow"]
    lr = lum(ref)
    shadow = mask & (lr < 0.5 * np.median(lr[mask]))
    print(f"  SlabCrossShadow, in the crate's shadow ({shadow.sum() / max(mask.sum(), 1) * 100:.0f}% "
          f"of the slab): {fmt(compare(ref, test, shadow))}")

    # Tone mapping: what the same light looks like through each view transform
    print("\n=== Display: view transforms on the same render (SphereOpen reference) ===")
    items = [i.identifier for i in sc.view_settings.bl_rna.properties["view_transform"].enum_items]
    print("  view transforms here:", ", ".join(items))
    ref, _t, mask, *_ = keep["SphereOpen"]
    exr = os.path.join(TMP, "SphereOpen_ref.exr")
    img = bpy.data.images.load(exr, check_existing=False)
    shown = {}
    for vt in [v for v in ("AgX", "Filmic", "Standard", "ACES 1.3", "ACES 2.0",
                           "Khronos PBR Neutral") if v in items]:
        sc.view_settings.view_transform = vt
        sc.view_settings.look = "None"
        out = os.path.join(TMP, f"tone_{vt.replace(' ', '_')}.png")
        sc.render.image_settings.file_format = "PNG"
        sc.render.image_settings.color_depth = "16"
        img.save_render(out, scene=sc)
        p = bpy.data.images.load(out, check_existing=False)
        p.colorspace_settings.name = "Non-Color"
        b = np.empty(p.size[0] * p.size[1] * 4, dtype=np.float32)
        p.pixels.foreach_get(b)
        bpy.data.images.remove(p)
        shown[vt] = b.reshape(p.size[1], p.size[0], 4)[..., :3]
    bpy.data.images.remove(img)
    sc.view_settings.view_transform = "Standard"

    def sat(a):
        return float((a.max(axis=-1) - a.min(axis=-1)).mean())
    base = shown.get("AgX")
    for vt, a in shown.items():
        d = np.abs(a - base)[mask].mean() if base is not None else 0
        print(f"  {vt:20s} mean display value {a[mask].mean():.3f}  saturation {sat(a[mask]):.3f}  "
              f"differs from AgX by {d:.3f} (0..1 scale)")

    # Material Preview: EEVEE with the studio HDRI vs Cycles lit by the preview map
    print("\n=== Material Preview: EEVEE with the studio HDRI vs the preview map ===")
    try:
        st = sl.preview_settings(ctx)
        print(f"  studio light: {st['name']}  rotation {st['rotation']:.2f}  strength {st['strength']}")
        look, _im = sl._lookdev_world(st)
        for ob in sc.objects:
            if ob.type == "LIGHT":
                ob.hide_render = not st["scene_lights"]
        sc.world = look
        for name in ("SphereOpen", "SphereGlossy"):
            ob = targets[name]
            ppath = os.path.join(TMP, f"{name}_preview.exr")
            sl.bake_preview(ctx, ob, ppath, ENV_W, 2.0)
            cam = keep[name][5]
            sc.camera = cam
            refs = {}
            for eng in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "CYCLES"):
                try:
                    sc.render.engine = eng
                except TypeError:
                    continue
                refs["EEVEE" if eng != "CYCLES" else "Cycles room"] = render(
                    sc, os.path.join(TMP, f"{name}_pv_{eng}.exr"))
            sc.render.engine = "CYCLES"
            world, _env = map_world(ppath, name="PreviewWorld")
            iso, icam = isolated_scene(sl, sc, ob, world, cam)
            test = render(iso, os.path.join(TMP, f"{name}_pv_map.exr"))
            mask = test[..., 3] > 0.99
            for k, r in refs.items():
                print(f"  {name:13s} map vs {k:6s}: {fmt(compare(r, test, mask))}")
            if "EEVEE" in refs and "Cycles room" in refs:
                print(f"  {name:13s} EEVEE vs Cycles (same world): "
                      f"{fmt(compare(refs['Cycles room'], refs['EEVEE'], mask))}")
            for o in list(iso.collection.objects):
                iso.collection.objects.unlink(o)
            bpy.data.objects.remove(icam)
            bpy.data.scenes.remove(iso)
    except Exception:
        traceback.print_exc()


def irradiance(env):
    """Cosine-weighted irradiance (luminance) for the 6 axis normals from a
    Blender-convention equirect array (rows from the bottom)."""
    h, w = env.shape[:2]
    v = (np.arange(h) + 0.5) / h
    u = (np.arange(w) + 0.5) / w
    lat = (v - 0.5) * math.pi
    phi = math.pi - 2 * math.pi * u
    cl = np.cos(lat)[:, None]
    d = np.stack([cl * np.cos(phi)[None, :], cl * np.sin(phi)[None, :],
                  np.sin(lat)[:, None] * np.ones((1, w))], axis=-1)
    sa = (2 * math.pi / w) * (math.pi / h) * cl          # pixel solid angle
    L = lum(env)
    out = []
    for n in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        cos = np.maximum(d @ np.array(n, dtype=np.float64), 0.0)
        out.append(float((L * cos * sa).sum()))
    return np.array(out)


def load_array(path):
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    a = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(a)
    bpy.data.images.remove(img)
    return a.reshape(h, w, 4)


def run_part2(mod):
    """Why the Material Preview map is darker/bluer, and the view transforms."""
    sl = mod.scene_link
    sc, targets, _l = build()
    ctx = bpy.context
    ob = targets["SphereOpen"]
    st = sl.preview_settings(ctx)
    hdri = load_array(st["hdri"])
    print(f"\n=== Material Preview map vs its HDRI ({os.path.basename(st['hdri'])} "
          f"{hdri.shape[1]}x{hdri.shape[0]}, peak {lum(hdri).max():.0f}) ===")
    ref = irradiance(hdri)
    print("  irradiance for normals +X -X +Y -Y +Z -Z (map / HDRI):")
    orig = sl._render_to_array
    for label, denoise, spp in (("as now: 4 spp, denoised", None, None),
                                ("4 spp, no denoise", False, 4),
                                ("64 spp, no denoise", False, 64),
                                ("64 spp, denoised", True, 64)):
        def patched(s, path, _d=denoise, _n=spp):
            if _d is not None:
                s.cycles.use_denoising = _d
                s.cycles.samples = _n
            return orig(s, path)
        sl._render_to_array = patched
        try:
            path = os.path.join(TMP, "pv.exr")
            sl.bake_preview(ctx, ob, path, ENV_W, 2.0)
        finally:
            sl._render_to_array = orig
        m = np.roll(load_array(path), ENV_W // 2, axis=1)
        r = irradiance(m) / np.maximum(ref, 1e-9)
        col = [float(m[..., i].sum() / max(hdri[..., i].mean() * m.shape[0] * m.shape[1], 1e-9))
               for i in range(3)]
        print(f"  {label:26s} " + " ".join(f"{x:5.2f}" for x in r)
              + f"   peak {lum(m).max():8.1f}   mean R/G/B vs HDRI {col[0]:.2f}/{col[1]:.2f}/{col[2]:.2f}")

    print("\n=== View transforms on one render (sphere in the lit room) ===")
    sc.cycles.samples = 32
    sc.cycles.use_denoising = True
    cam = camera_for(sc, ob, *_l["views"]["SphereOpen"])
    sc.camera = cam
    exr = os.path.join(TMP, "tone_src.exr")
    render(sc, exr)
    img = bpy.data.images.load(exr, check_existing=False)
    shown = {}
    for vt in ("AgX", "Filmic", "Standard", "Khronos PBR Neutral", "ACES 1.3", "ACES 2.0", "ACES"):
        try:
            sc.view_settings.view_transform = vt
        except TypeError:
            continue
        sc.view_settings.look = "None"
        out = os.path.join(TMP, f"tone_{len(shown)}.png")
        sc.render.image_settings.file_format = "PNG"
        sc.render.image_settings.color_depth = "16"
        img.save_render(out, scene=sc)
        p = bpy.data.images.load(out, check_existing=False)
        p.colorspace_settings.name = "Non-Color"
        b = np.empty(p.size[0] * p.size[1] * 4, dtype=np.float32)
        p.pixels.foreach_get(b)
        shown[vt] = b.reshape(p.size[1], p.size[0], 4)[..., :3]
        bpy.data.images.remove(p)
    bpy.data.images.remove(img)
    lin = load_array(exr)[..., :3]
    # Painter's two choices, applied to the same linear values: Linear (clip) and
    # the common ACES filmic fit (Narkowicz), both then sRGB-encoded
    def srgb(x):
        x = np.clip(x, 0, 1)
        return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    shown["Painter Linear (sim)"] = srgb(lin)
    x = lin * 0.6
    shown["Painter ACES (Narkowicz sim)"] = srgb((x * (a * x + b)) / (x * (c * x + d) + e))
    base = shown.get("AgX")
    sat = lambda q: float((q.max(axis=-1) - q.min(axis=-1)).mean())
    for vt, q in shown.items():
        diff = float(np.abs(q - base).mean()) if base is not None else 0.0
        print(f"  {vt:30s} mean {q.mean():.3f}  saturation {sat(q):.3f}  differs from AgX by {diff:.3f}")


def run_part3(mod):
    """Partial shadow seen from where it is visible, and the Material Preview gap split."""
    sl = mod.scene_link
    sc, targets, l = build()
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    ctx = bpy.context
    h = Vector((l["sun"].x, l["sun"].y, 0)).normalized()
    side = Vector((-h.y, h.x, 0))
    up = Vector((0, 0, 1))

    print("\n=== Partial shadow, camera above the sun-facing side ===")
    ob = targets["SphereHalfShadow"]
    pillar = bpy.data.objects["Pillar"]
    path = os.path.join(TMP, "hs_map.exr")
    sl.bake_environment(ctx, ob, path, ENV_W, 64, 2.0, True)
    cam = camera_for(sc, ob, h * 0.4 - side * 0.2 + up, 6.0)
    sc.camera = cam
    ref = render(sc, os.path.join(TMP, "hs_ref.exr"))
    pillar.hide_render = True
    nopillar = render(sc, os.path.join(TMP, "hs_nopillar.exr"))
    pillar.hide_render = False
    world, _ = map_world(path)
    iso, icam = isolated_scene(sl, sc, ob, world, cam)
    test = render(iso, os.path.join(TMP, "hs_test.exr"))
    mask = test[..., 3] > 0.99
    lr, ln = lum(ref), lum(nopillar)
    shaded = mask & (lr < 0.6 * ln)
    print(f"  pixels the pillar darkens by >40%: {shaded.sum() / max(mask.sum(), 1) * 100:.0f}% of the sphere")
    print(f"  whole sphere:       {fmt(compare(ref, test, mask))}")
    if shaded.any():
        print(f"  in pillar shadow:   {fmt(compare(ref, test, shaded))}")
        print(f"  outside it:         {fmt(compare(ref, test, mask & ~shaded))}")
    for o in list(iso.collection.objects):
        iso.collection.objects.unlink(o)
    bpy.data.objects.remove(icam)
    bpy.data.scenes.remove(iso)

    print("\n=== Material Preview gap, split (EEVEE render = proxy for the viewport) ===")
    st = sl.preview_settings(ctx)
    look, _im = sl._lookdev_world(st)
    for o in sc.objects:
        if o.type == "LIGHT":
            o.hide_render = True
    ob = targets["SphereOpen"]
    cam = camera_for(sc, ob, *l["views"]["SphereOpen"])
    eevee = next((e for e in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT")
                  if e in {i.identifier for i in sc.render.bl_rna.properties["engine"].enum_items}),
                 "BLENDER_EEVEE")
    pv_plain = os.path.join(TMP, "pv_plain.exr")
    sl.bake_preview(ctx, ob, pv_plain, ENV_W, 2.0)
    pv_room = os.path.join(TMP, "pv_room.exr")
    sl.bake_environment(ctx, ob, pv_room, ENV_W, 16, 2.0, surroundings=True, world=look, lamps=False)

    # references: EEVEE / Cycles, sphere alone and in the room, all with the studio HDRI
    def ref_render(engine, alone):
        if alone:
            s, c = isolated_scene(sl, sc, ob, look, cam)
            s.render.film_transparent = False
        else:
            s, c = sc, None
            sc.world = look
            sc.camera = cam
        s.render.engine = engine
        try:
            return render(s, os.path.join(TMP, f"pvref_{engine}_{alone}.exr"))
        finally:
            sc.render.engine = "CYCLES"
            if c is not None:
                for o in list(s.collection.objects):
                    s.collection.objects.unlink(o)
                bpy.data.objects.remove(c)
                bpy.data.scenes.remove(s)

    refs = {("EEVEE", "alone"): ref_render(eevee, True), ("EEVEE", "room"): ref_render(eevee, False),
            ("Cycles", "alone"): ref_render("CYCLES", True), ("Cycles", "room"): ref_render("CYCLES", False)}
    for label, mp in (("map, HDRI only (as now)", pv_plain), ("map with surroundings", pv_room)):
        world, _ = map_world(mp, name="PV")
        iso, icam = isolated_scene(sl, sc, ob, world, cam)
        test = render(iso, os.path.join(TMP, "pv_test.exr"))
        mask = test[..., 3] > 0.99
        for (eng, where), r in refs.items():
            print(f"  {label:24s} vs {eng:6s} {where:5s}: {fmt(compare(r, test, mask))}")
        for o in list(iso.collection.objects):
            iso.collection.objects.unlink(o)
        bpy.data.objects.remove(icam)
        bpy.data.scenes.remove(iso)


def main():
    print(f"Blender {bpy.app.version_string}  lighting match diagnostic")
    code = 0
    try:
        mod = install()
        if os.environ.get("TP_DIAG_PART") == "2":
            run_part2(mod)
        elif os.environ.get("TP_DIAG_PART") == "3":
            run_part3(mod)
        else:
            run(mod)
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.stdout.flush()
    if code:
        sys.exit(code)


main()
