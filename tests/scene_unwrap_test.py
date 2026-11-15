"""Headless Blender test: Create Sets for Scene unwraps UV-less objects with Smart Seams.

Run via:  .\\tests\\run_blender_tests.ps1 -Script scene_unwrap_test.py
Smart Seams is loaded from its source folder (default: ..\\smart-seams next to this
repo; override with --smart-seams <folder that contains smart_seams/>).
"""

import array
import importlib
import os
import shutil
import sys
import tempfile

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
MODULE = "bl_ext.user_default.texture_plugger"
HERE = os.path.dirname(os.path.abspath(__file__))
SEAMS_ROOT = (os.path.abspath(ARGS[ARGS.index("--smart-seams") + 1]) if "--smart-seams" in ARGS
              else os.path.normpath(os.path.join(HERE, "..", "..", "smart-seams")))
TMP = tempfile.mkdtemp(prefix="tp unwrap ü_")
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


def uvs(ob):
    me = ob.data
    if not me.uv_layers:
        return None
    co = array.array("f", bytes(8 * len(me.loops)))
    me.uv_layers.active.data.foreach_get("uv", co)
    return list(co)


def cube(name, loc):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.data.name = name + "_mesh"
    return ob


def build_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for m in list(bpy.data.materials):
        bpy.data.materials.remove(m)
    bare = cube("Bare", (0, 0, 0))                       # no UV map at all
    while bare.data.uv_layers:
        bare.data.uv_layers.remove(bare.data.uv_layers[0])
    dup = bpy.data.objects.new("BareDup", bare.data)     # shares Bare's mesh
    dup.location = (2, 0, 0)
    bpy.context.scene.collection.objects.link(dup)
    kept = cube("Kept", (4, 0, 0))                       # hand-made UVs: never touched
    flat = cube("Flat", (6, 0, 0))                       # UV map collapsed to one point
    n = len(flat.data.loops)
    flat.data.uv_layers.active.data.foreach_set("uv", array.array("f", [0.5] * (2 * n)))
    for o in bpy.context.view_layer.objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = kept
    return bare, dup, kept, flat


def run(batch, s, objs):
    map_ids = ["base_color", "normal", "metallic", "roughness"]
    return batch.create_scene_sets(bpy.context, s, objs, s.create_root, map_ids, 256)


def test(mod):
    batch = importlib.import_module(mod.__name__ + ".scene_batch")
    uvu = importlib.import_module(mod.__name__ + ".uv_unwrap")
    s = bpy.context.scene.texture_plugger
    s.create_root = os.path.join(TMP, "sets")

    # 1. Smart Seams not installed: nothing is invented, the user gets a hint.
    check("smart seams not detected before install", not uvu.smart_seams_available())
    bare, dup, kept, flat = build_scene()
    kept_before = uvs(kept)
    objs = batch.collect_targets(bpy.context, "SELECTED")
    info, err = run(batch, s, objs)
    check("batch without smart seams ok", info is not None, err)
    check("no UVs invented without smart seams", not bare.data.uv_layers)
    check("hint to enable smart seams",
          any("Smart Seams" in m and "Bare" in m for m in info["messages"]), info["messages"])
    check("needs_unwrap: bare / flat / kept",
          uvu.needs_unwrap(bare) and uvu.needs_unwrap(flat) and not uvu.needs_unwrap(kept))

    # 2. Smart Seams installed: UV-less objects are unwrapped before export.
    sys.path.insert(0, SEAMS_ROOT)
    seams = importlib.import_module("smart_seams")
    seams.register()
    try:
        check("smart seams detected", uvu.smart_seams_available())
        check("default mode is All Objects", s.scene_unwrap_mode == "ALL", s.scene_unwrap_mode)
        s.scene_unwrap_mode = "MISSING"
        bare, dup, kept, flat = build_scene()
        kept_before = uvs(kept)
        sel_before = sorted(o.name for o in bpy.context.selected_objects)
        objs = batch.collect_targets(bpy.context, "SELECTED")
        info, err = run(batch, s, objs)
        check("batch with smart seams ok", info is not None, err)
        if info is None:
            return
        check("two meshes unwrapped (shared mesh once)", info.get("unwrapped") == 2, info)
        check("bare object got UVs", bare.data.uv_layers and not uvu.needs_unwrap(bare))
        check("collapsed UVs unwrapped", not uvu.needs_unwrap(flat))
        check("existing UVs untouched", uvs(kept) == kept_before)
        check("no 'no UV map' warning left",
              not any("no UV map" in m for m in info["messages"]), info["messages"])
        check("selection restored",
              sorted(o.name for o in bpy.context.selected_objects) == sel_before)
        check("active restored", bpy.context.view_layer.objects.active == kept)
        check("still object mode", bpy.context.mode == "OBJECT", bpy.context.mode)
        check("every object got its own material",
              len({o.active_material.name for o in (bare, dup, kept, flat)}) == 4)

        # The FBX Painter opens must carry the new UVs.
        mesh_path = info["mesh"]
        check("one FBX exported", isinstance(mesh_path, str) and os.path.isfile(mesh_path), mesh_path)
        before = set(bpy.data.objects)
        bpy.ops.import_scene.fbx(filepath=mesh_path)
        imported = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
        bare_in = [o for o in imported if o.name.startswith("Bare") and not o.name.startswith("BareDup")]
        check("exported Bare has UVs",
              bare_in and bare_in[0].data.uv_layers and not uvu.needs_unwrap(bare_in[0]),
              [(o.name, len(o.data.uv_layers)) for o in imported])
        for o in imported:
            bpy.data.objects.remove(o, do_unlink=True)

        # 3. All Objects: existing UVs are re-unwrapped too, once.
        s.scene_unwrap_mode = "ALL"
        bare, dup, kept, flat = build_scene()
        kept_before = uvs(kept)
        info, err = run(batch, s, batch.collect_targets(bpy.context, "SELECTED"))
        check("ALL: batch ok", info is not None, err)
        check("ALL: three meshes unwrapped", info and info.get("unwrapped") == 3,
              info and info.get("unwrapped"))
        check("ALL: existing UVs re-unwrapped", uvs(kept) != kept_before)
        check("ALL: meshes stamped", all(uvu.is_optimized(o) for o in (bare, kept, flat)))
        kept_after = uvs(kept)
        info, err = run(batch, s, batch.collect_targets(bpy.context, "SELECTED"))
        check("ALL: second run doesn't redo them", info and info.get("unwrapped") == 0,
              info and info.get("unwrapped"))
        check("ALL: second run keeps UVs", uvs(kept) == kept_after)

        # Smart Seams fails on an object: reported, no half-made UV map left, others go on.
        pipeline = importlib.import_module("smart_seams.core.pipeline")
        real_solve = pipeline.solve_seams

        def boom(obj, opts):
            if obj.name in ("Bare", "Kept"):
                raise ValueError("solver blew up")
            return real_solve(obj, opts)

        pipeline.solve_seams = boom
        try:
            bare, dup, kept, flat = build_scene()
            kept_before = uvs(kept)
            info, err = run(batch, s, batch.collect_targets(bpy.context, "SELECTED"))
        finally:
            pipeline.solve_seams = real_solve
        check("failed unwrap: batch still finishes", info is not None, err)
        check("failed re-unwrap keeps the old UVs", uvs(kept) == kept_before)
        check("failed re-unwrap not stamped", not kept.data.get(uvu.STAMP_KEY))
        if info is not None:
            check("failed unwrap not counted", info.get("unwrapped") == 1, info.get("unwrapped"))
            check("failed unwrap reported",
                  any("could not unwrap" in m and "Bare" in m for m in info["messages"]),
                  info["messages"])
        check("failed unwrap leaves no UV map", len(bare.data.uv_layers) == 0,
              len(bare.data.uv_layers))
        check("other object still unwrapped", not uvu.needs_unwrap(flat))
        check("still object mode after failure", bpy.context.mode == "OBJECT", bpy.context.mode)

        # All Objects never re-unwraps an object whose textures are painted.
        s.scene_unwrap_mode = "MISSING"
        bare, dup, kept, flat = build_scene()
        info, err = run(batch, s, batch.collect_targets(bpy.context, "SELECTED"))
        scaffold = importlib.import_module(mod.__name__ + ".scaffold")
        spec = scaffold.MAPS_BY_ID["base_color"]
        painted = os.path.join(info["textures"], scaffold.texture_filename(kept.active_material.name, spec))
        os.remove(painted)
        scaffold.write_png(painted, 8, 8, (200, 30, 30))          # "painted" in Painter
        kept_before = uvs(kept)
        s.scene_unwrap_mode = "ALL"
        info, err = run(batch, s, batch.collect_targets(bpy.context, "SELECTED"))
        check("painted: UVs kept", uvs(kept) == kept_before)
        check("painted: reported", info and any("already painted" in m and "Kept" in m
                                                 for m in info["messages"]), info and info["messages"])
        check("painted: others already done, not redone", info and info.get("unwrapped") == 0
              and uvu.is_optimized(bare) and uvu.is_optimized(flat),
              info and info.get("unwrapped"))

        # Off: no unwrapping even with Smart Seams installed.
        bare, dup, kept, flat = build_scene()
        s.scene_unwrap_mode = "NONE"
        info, err = run(batch, s, batch.collect_targets(bpy.context, "SELECTED"))
        s.scene_unwrap_mode = "ALL"
        check("toggle off leaves UVs alone", info and not info.get("unwrapped")
              and not bare.data.uv_layers, info and info.get("unwrapped"))
    finally:
        seams.unregister()


def main():
    print(f"Blender {bpy.app.version_string}  scene unwrap (Smart Seams at {SEAMS_ROOT})")
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
