"""Headless Blender test of whole-scene texturing (scene_batch).

Run via:  .\\tests\\run_blender_tests.ps1 -Script scene_batch_test.py
"""

import importlib
import os
import shutil
import sys
import tempfile

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
MODULE = "bl_ext.user_default.texture_plugger"
TMP = tempfile.mkdtemp(prefix="tp batch ü_")
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


def mesh(name, loc, coll=None, parent=None, data=None):
    if data is None:
        bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
        ob = bpy.context.active_object
        ob.name = name
    else:
        ob = bpy.data.objects.new(name, data)
        ob.location = loc
        bpy.context.scene.collection.objects.link(ob)
    if coll is not None:
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        coll.objects.link(ob)
    if parent is not None:
        ob.parent = parent
    return ob


def test(mod):
    from importlib import import_module
    batch = import_module(mod.__name__ + ".scene_batch")
    names = batch.unique_names(["Rock", "rock", "Rock", "a b", "a_b", ""], ["Taken"])
    check("unique_names ignores case", len({n.lower() for n in names}) == 6, names)
    check("unique_names sanitized", all(n.replace("_", "").replace("-", "").isalnum() for n in names), names)

    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    sc = bpy.context.scene
    s = sc.texture_plugger
    s.create_root = os.path.join(TMP, "sets")
    shared = bpy.data.materials.new("Shared")
    coll = bpy.data.collections.new("Group")
    sc.collection.children.link(coll)
    root_ob = mesh("Root", (0, 0, 0))
    child = mesh("Child", (2, 0, 0), coll=coll, parent=root_ob)
    rock_a = mesh("Rock", (4, 0, 0))
    rock_b = mesh("rock", (6, 0, 0))                       # case collision
    dup = mesh("Dup", (8, 0, 0), data=rock_a.data)          # shared mesh data
    for o in (root_ob, child, rock_a, rock_b, dup):
        o.data.materials.clear() if o.data.users == 1 else None
    root_ob.data.materials.append(shared)
    child.data.materials.append(shared)                    # same material on two objects
    rock_b.data.materials.append(None)
    hidden = mesh("Hidden", (10, 0, 0))
    hidden.hide_set(True)
    allobs = [root_ob, child, rock_a, rock_b, dup]
    for o in sc.objects:
        o.select_set(o in allobs)
    bpy.context.view_layer.objects.active = root_ob

    res = bpy.ops.texture_plugger.create_scene_sets(scope="SELECTED")
    check("operator finished", res == {"FINISHED"}, res)
    mats = []
    for o in allobs:
        check(f"{o.name} has a material", o.active_material is not None)
        mats.append(o.active_material)
    check("every object has a unique material", len({m.name_full for m in mats if m}) == len(allobs),
          [m.name if m else None for m in mats])
    check("names unique ignoring case", len({m.name.lower() for m in mats if m}) == len(allobs),
          [m.name for m in mats if m])
    check("shared material no longer shared", shared.users == 0, shared.users)
    check("linked duplicate independent of its twin",
          rock_a.active_material != dup.active_material)
    check("hidden object untouched", len(hidden.material_slots) == 0)
    folders = {m.get("texture_plugger_project") for m in mats}
    check("one shared scene folder", len(folders) == 1, folders)
    folder = next(iter(folders))
    tex = os.path.join(folder, "textures")
    for m in mats:
        check(f"{m.name} placeholders in shared textures folder",
              os.path.isfile(os.path.join(tex, f"{m.name}_BaseColor.png")))
    fbx = [f for f in os.listdir(os.path.join(folder, "mesh"))]
    check("one mesh export", len(fbx) == 1, fbx)
    imgs = [i for i in bpy.data.images if i.get("texture_plugger_watch") is not None or
            os.path.abspath(bpy.path.abspath(i.filepath)).startswith(os.path.abspath(tex))]
    check("images point into the shared folder", len(imgs) >= len(allobs), len(imgs))

    # idempotent: run again, nothing multiplies
    before = sorted(m.name for m in bpy.data.materials)
    res = bpy.ops.texture_plugger.create_scene_sets(scope="SELECTED")
    after = sorted(m.name for m in bpy.data.materials)
    check("second run creates no new materials", before == after, (before, after))

    # nothing selected -> clean cancel
    for o in sc.objects:
        o.select_set(False)
    res = bpy.ops.texture_plugger.create_scene_sets(scope="SELECTED")
    check("empty selection cancelled", res == {"CANCELLED"}, res)

    # visible scope picks up everything visible (not the hidden one)
    res = bpy.ops.texture_plugger.create_scene_sets(scope="VISIBLE")
    check("visible scope finished", res == {"FINISHED"}, res)
    check("hidden still untouched", len(hidden.material_slots) == 0)

    # unwritable root: error, no exception, no half state
    s.create_root = os.path.join(TMP, "sets", "x" * 5, "\0bad")
    try:
        res = bpy.ops.texture_plugger.create_scene_sets(scope="SELECTED")
    except RuntimeError:
        res = {"CANCELLED"}
    check("bad folder cancelled cleanly", res == {"CANCELLED"}, res)


def main():
    print(f"Blender {bpy.app.version_string}  scene batch")
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
