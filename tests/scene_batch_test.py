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
    check("a small scene is one Painter project",
          {m.get("texture_plugger_parts") for m in mats} == {1}, [m.get("texture_plugger_parts") for m in mats])

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

    big_scene(mod, batch, s)

    # unwritable root: error, no exception, no half state
    s.create_root = os.path.join(TMP, "sets", "x" * 5, "\0bad")
    try:
        res = bpy.ops.texture_plugger.create_scene_sets(scope="SELECTED")
    except RuntimeError:
        res = {"CANCELLED"}
    check("bad folder cancelled cleanly", res == {"CANCELLED"}, res)


def big_scene(mod, batch, s):
    """A scene too big for the computer is sized down and split into Painter parts."""
    import json
    from importlib import import_module
    plan = import_module(mod.__name__ + ".scene_plan")
    wf = import_module(mod.__name__ + ".workflow")
    p = bpy.context.preferences.addons[mod.__name__].preferences
    sc = bpy.context.scene
    office = bpy.data.collections.new("Office")
    sc.collection.children.link(office)
    desk_mat = bpy.data.materials.new("DeskShared")
    for i in range(14):
        o = mesh(f"Desk{i:02d}", (i * 2, 6, 0), coll=office)
        o.data.materials.append(desk_mat)
    s.create_root = os.path.join(TMP, "sets_big")
    s.create_resolution = "2048"
    real_mem, real_launch, real_gpu = plan.system_memory_bytes, wf._launch, plan.gpu_memory_bytes
    saved = (p.max_scene_sets, p.memory_budget, p.painter_path, p.env_samples, p.env_resolution)
    launched = []
    try:
        plan.system_memory_bytes = lambda: 300 * 1024 ** 2      # a "300 MB computer"
        plan.gpu_memory_bytes = lambda: None
        p.max_scene_sets, p.memory_budget = 5, 40
        res = bpy.ops.texture_plugger.create_scene_sets(scope="VISIBLE")
        check("big scene finished (no refusal)", res == {"FINISHED"}, res)
        desks = [o for o in sc.objects if o.name.startswith("Desk")]
        mats = [o.active_material for o in desks]
        check("big scene: every desk has its own material", len({m.name for m in mats}) == len(desks))
        folder = mats[0]["texture_plugger_scene"]
        scene_mats = {m.name: m for m in bpy.data.materials
                      if m.get("texture_plugger_scene") == folder}
        reso = {m.get(batch.RES_KEY) for m in scene_mats.values()}
        check("texture size lowered to fit memory", reso == {256}, reso)
        n_parts = {m.get(batch.PARTS_KEY) for m in scene_mats.values()}
        check("scene split into parts", len(n_parts) == 1 and next(iter(n_parts)) > 1, n_parts)
        count = next(iter(n_parts))
        per_part = {}
        for m in scene_mats.values():
            per_part[m[batch.PART_KEY]] = per_part.get(m[batch.PART_KEY], 0) + 1
        check("no part over the limit", max(per_part.values()) <= 5, per_part)
        check("parts numbered 0..n-1", sorted(per_part) == list(range(count)), per_part)
        fbx = sorted(os.listdir(os.path.join(folder, "mesh")))
        check("one FBX per part", len(fbx) == count and all("_part" in f for f in fbx), fbx)
        img = next(n.image for n in mats[0].node_tree.nodes if n.type == "TEX_IMAGE" and n.image)
        check("placeholders written small", tuple(img.size) == (plan.PLACEHOLDER_MAX,) * 2, tuple(img.size))
        desk_parts = sorted({m[batch.PART_KEY] for m in mats})
        check("a collection's objects sit in consecutive parts",
              desk_parts == list(range(desk_parts[0], desk_parts[-1] + 1)), desk_parts)

        # Edit Scene in Painter opens the part of the active object
        fake = os.path.join(TMP, "Fake Painter.exe")
        open(fake, "wb").close()
        p.painter_path, p.env_samples, p.env_resolution = fake, 4, "512"
        wf._launch = lambda cmd, cwd=None: launched.append(cmd)
        target = desks[-1]
        for o in sc.objects:
            o.select_set(o == target)
        bpy.context.view_layer.objects.active = target
        want = target.active_material[batch.PART_KEY]
        res = bpy.ops.texture_plugger.edit_scene_in_painter()
        check("edit scene finished", res == {"FINISHED"} and launched, (res, launched))
        cmd = launched[-1] if launched else []
        mesh_arg = cmd[cmd.index("--mesh") + 1] if "--mesh" in cmd else ""
        stem = plan.part_name(os.path.basename(folder), want, count)
        check("opened the part of the selected object", os.path.basename(mesh_arg) == stem + ".fbx",
              (mesh_arg, stem))
        link = json.load(open(os.path.join(os.environ["TP_LINK_DIR"], "blender_link.json")))
        sc_info = link.get("scene") or {}
        check("Painter told size, part and project", sc_info.get("resolution") == 256
              and sc_info.get("part") == want + 1 and sc_info.get("parts") == count
              and str(sc_info.get("project", "")).endswith(stem + ".spp"), sc_info)
        # a saved part project is reopened instead of the mesh
        open(os.path.join(folder, stem + ".spp"), "wb").close()
        batch._last_launch = 0.0
        res = bpy.ops.texture_plugger.edit_scene_in_painter()
        cmd = launched[-1]
        check("saved part project reopened", cmd[-1].endswith(stem + ".spp") and "--mesh" not in cmd, cmd)
        batch._last_launch = 0.0
        # active object outside the scene (a brushstroke, an empty...) and nothing selected:
        # the button still finds the scene and opens part 1
        empty = bpy.data.objects.new("Stroke Holder", None)
        sc.collection.objects.link(empty)
        for o in sc.objects:
            o.select_set(False)
        bpy.context.view_layer.objects.active = empty
        res = bpy.ops.texture_plugger.edit_scene_in_painter()
        cmd = launched[-1]
        first = plan.part_name(os.path.basename(folder), 0, count)
        check("scene found without a scene object selected",
              res == {"FINISHED"} and ("--mesh" in cmd and os.path.basename(cmd[cmd.index("--mesh") + 1]) == first + ".fbx"),
              (res, cmd))
        batch._last_launch = 0.0
        bpy.data.objects.remove(empty)

        # on a computer where the scene fits, the whole scene opens as one project,
        # and a leftover part project from the split plan is not reopened by mistake
        plan.system_memory_bytes, plan.gpu_memory_bytes = real_mem, real_gpu
        p.max_scene_sets = 0
        for o in sc.objects:
            o.select_set(o.type == "MESH")
        bpy.context.view_layer.objects.active = desks[0]
        res = bpy.ops.texture_plugger.edit_scene_in_painter()
        cmd = launched[-1]
        mesh_arg = cmd[cmd.index("--mesh") + 1] if "--mesh" in cmd else ""
        check("whole scene opens as one project (old part project ignored)",
              res == {"FINISHED"} and os.path.basename(mesh_arg) == os.path.basename(folder) + ".fbx",
              (res, cmd))
        check("every scene object in that project",
              {m.get(batch.PARTS_KEY) for m in bpy.data.materials if m.get("texture_plugger_scene") == folder} == {1})
        batch._last_launch = 0.0
    finally:
        for o in sc.objects:
            o.select_set(False)
        plan.system_memory_bytes, wf._launch, plan.gpu_memory_bytes = real_mem, real_launch, real_gpu
        (p.max_scene_sets, p.memory_budget, p.painter_path, p.env_samples, p.env_resolution) = saved


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
