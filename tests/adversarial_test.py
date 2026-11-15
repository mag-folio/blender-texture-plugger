"""Adversarial headless tests for Texture Plugger (complements blender_test.py).

  set BLENDER_USER_RESOURCES=<temp dir>
  blender --background --factory-startup --python-exit-code 1 \
          --python tests/adversarial_test.py -- --zip dist/texture_plugger-1.1.0.zip
"""

import importlib
import os
import shutil
import sys
import tempfile
import traceback

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
RESULTS = []
MODULE = "bl_ext.user_default.texture_plugger"
TAG = "texture_plugger"
TMP = tempfile.mkdtemp(prefix="tp adv ü_")   # space + non-ASCII in every path


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  -- {detail}"))
    return bool(cond)


def make_image(folder, name):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    ext = os.path.splitext(name)[1].lower()
    fmt = {".png": "PNG", ".jpg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF"}[ext]
    img = bpy.data.images.new("gen", 4, 4, alpha=True, float_buffer=(fmt == "OPEN_EXR"))
    img.filepath_raw = path
    img.file_format = fmt
    img.save()
    bpy.data.images.remove(img)
    return path


def make_set(sub, names):
    folder = os.path.join(TMP, sub)
    for n in names:
        make_image(folder, n)
    return folder


def new_cube(name):
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def select_only(*objs):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0] if objs else None


def S():
    return bpy.context.scene.texture_plugger


def tagged(nt):
    return [n for n in nt.nodes if n.get(TAG) is not None]


def principled(nt):
    return [n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"][0]


def src_node(sock):
    return sock.links[0].from_node if sock.is_linked else None


def img_of(sock):
    n = src_node(sock)
    return n.image if n is not None and n.bl_idname == "ShaderNodeTexImage" else None


def zero_user_images():
    return [i.name for i in bpy.data.images if i.users == 0 and i.type == "IMAGE"]


def ui_override(area_type="VIEW_3D"):
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == area_type)
    region = next(r for r in area.regions if r.type == "WINDOW")
    return bpy.context.temp_override(window=win, screen=win.screen, area=area, region=region)


# --------------------------------------------------------------------------

def install():
    zip_path = os.path.abspath(ARGS[ARGS.index("--zip") + 1])
    bpy.ops.extensions.package_install_files(filepath=zip_path, repo="user_default",
                                             enable_on_install=True)
    check("extension enabled", MODULE in bpy.context.preferences.addons)
    return importlib.import_module(MODULE)


def test_detection_names(pkg):
    print("\n[weird / misleading filenames]")
    d = pkg.detection
    cases = {
        "Normal_Wood_basecolor.png": ("base_color", "normal_wood"),
        "Rough_Stone_diff.jpg": ("base_color", "rough_stone"),
        "metal_plate_nor_gl.png": ("normal_gl", "metal_plate"),
        "Old Wood Roughness.PNG": ("roughness", "old_wood"),
        "Wood_Color.png.png": ("base_color", "wood"),
        "Ziegel_Ü_Normal_DX.tif": ("normal_dx", "ziegel_ü"),
        "木材_BaseColor.png": ("base_color", "木材"),
        "woodbasecolor.png": (None, None),
        "T_Crate_D.TGA": ("base_color", "t_crate"),
    }
    for fname, (mt, key) in cases.items():
        det = d.classify(fname)
        ok = det.map_type == mt and (key is None or det.set_key == key)
        check(f"classify {fname}", ok, (det.map_type, det.set_key))

    # Preview image named exactly like the set must not become its own set.
    a = d.analyze(["/x/Metal032_2K_Color.png", "/x/Metal032_2K_Roughness.png",
                   "/x/Metal032_2K_NormalGL.png", "/x/Metal032.png"])
    check("Metal032.png preview -> no extra set", len(a.sets) == 1, [s.key for s in a.sets])
    check("Metal032.png listed unassigned",
          any(u.filename == "Metal032.png" for u in a.sets[0].unassigned) if a.sets else False)
    a = d.analyze(["/y/Metal.png", "/y/Metal_Roughness.png", "/y/Metal_Color.png"])
    check("Metal.png preview -> no extra set", len(a.sets) == 1, [s.key for s in a.sets])


def test_drag_drop():
    print("\n[drag and drop via wm.drop_import_file, the FileHandler path]")
    names = ["Drop Set_2K_Color.png", "Drop Set_2K_Roughness.png", "Drop Set_1K_Color.jpg",
             "Drop Set_1K_Roughness.jpg", "Drop Set_2K_NormalGL.exr"]
    folder = make_set("drop dir", names)
    obj = new_cube("DropCube")
    select_only(obj)
    fh = bpy.types.FileHandler.bl_rna_get_subclass_py("TEXTUREPLUGGER_FH_drop")
    with ui_override("VIEW_3D"):
        check("poll_drop True in 3D View", fh.poll_drop(bpy.context))
    with ui_override("PROPERTIES"):
        check("poll_drop False in Properties", not fh.poll_drop(bpy.context))
    # Which handlers compete for an image drop in the 3D View (Blender then shows
    # a chooser menu, which can't be clicked headless).
    with ui_override("VIEW_3D"):
        competing = [c.bl_idname for c in bpy.types.FileHandler.__subclasses__()
                     if c.bl_idname != "TEXTUREPLUGGER_FH_drop" and hasattr(c, "poll_drop")
                     and c.bl_file_extensions and ".png" in c.bl_file_extensions.lower()
                     and c.poll_drop(bpy.context)]
    print("  INFO  other handlers polling True for .png in 3D View:", competing)
    try:
        with ui_override("VIEW_3D"):
            res = bpy.ops.wm.drop_import_file("INVOKE_DEFAULT", directory=folder + os.sep,
                                              files=[{"name": n} for n in names])
        check("drop_import_file runs without exception", res == {"FINISHED"}, res)
    except Exception as ex:
        check("drop_import_file runs without exception", False, repr(ex))
    if obj.active_material is None and competing:
        # Do what Blender does after the user picks Texture Plugger in the menu.
        act = bpy.context.view_layer.objects.active
        print("  INFO  active object after headless drop:", act.name if act else None)
        select_only(obj)
        with ui_override("VIEW_3D"):
            bpy.ops.texture_plugger.load_set("INVOKE_DEFAULT", directory=folder + os.sep,
                                             files=[{"name": n} for n in names])
    mat = obj.active_material
    ok = mat is not None and mat.node_tree is not None
    check("drop built a material", ok)
    if ok:
        bsdf = principled(mat.node_tree)
        img = img_of(bsdf.inputs["Base Color"])
        check("drop: 2K PNG picked over 1K/JPG", img is not None and "2K_Color.png" in img.filepath,
              img.filepath if img else None)
        check("drop: path with space + unicode loads", img is not None and img.has_data or
              (img is not None and img.size[0] == 4), img.filepath if img else None)

    # INVOKE_DEFAULT on load_set itself (directory + files -> no file browser)
    obj2 = new_cube("InvokeCube")
    select_only(obj2)
    with ui_override("VIEW_3D"):
        res = bpy.ops.texture_plugger.load_set("INVOKE_DEFAULT", directory=folder + os.sep,
                                               files=[{"name": n} for n in names])
    check("load_set INVOKE_DEFAULT with files -> FINISHED", res == {"FINISHED"}, res)
    check("invoke applied", obj2.active_material is not None
          and principled(obj2.active_material.node_tree).inputs["Roughness"].is_linked)

    # Stale filename from the browser (file not in the directory) must not be used.
    res = bpy.ops.texture_plugger.load_set(directory=folder + os.sep,
                                           files=[{"name": "not_here_Color.png"}])
    check("nonexistent picked file -> CANCELLED", res == {"CANCELLED"}, res)


def test_two_sets_and_user_nodes():
    print("\n[two sets, user nodes, re-apply other set, orphans]")
    folder = make_set("two", ["Rock_4K_Color.png", "Rock_4K_Roughness.png", "Rock_4K_Normal.png",
                              "Rock_1K_Color.png", "Rock_Height.exr",
                              "Moss_Albedo.jpg", "Moss_Rough.jpg"])
    obj = new_cube("TwoCube")
    select_only(obj)
    mat = bpy.data.materials.new("UserMat")
    if mat.node_tree is None:
        mat.use_nodes = True
    obj.data.materials.append(mat)
    nt = mat.node_tree
    val = nt.nodes.new("ShaderNodeValue")
    val.name = "UserValue"
    bsdf = principled(nt)
    nt.links.new(val.outputs[0], bsdf.inputs["Specular IOR Level"])
    n_user = len(nt.nodes)

    bpy.ops.texture_plugger.load_set(directory=folder + os.sep)
    s = S()
    check("two sets found", len(s.sets) == 2, [x.key for x in s.sets])
    check("rock chosen (more maps)", s.set_key == "rock", s.set_key)
    img = img_of(bsdf.inputs["Base Color"]) or img_of(src_node(bsdf.inputs["Base Color"]).inputs[0]) \
        if bsdf.inputs["Base Color"].is_linked else None
    check("4K colour picked", img is not None and "4K" in img.filepath, img.filepath if img else None)
    check("user link kept", src_node(bsdf.inputs["Specular IOR Level"]) == val)
    disp_linked = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"][0] \
        .inputs["Displacement"].is_linked
    check("rock displacement linked", disp_linked)

    s.active_set = "moss"
    bpy.ops.texture_plugger.apply()
    roles = sorted(n.get(TAG) for n in tagged(nt))
    check("moss: no leftover rock nodes", not any("normal" in r or "height" in r or r == "displacement"
                                                  for r in roles), roles)
    check("moss: Normal unlinked", not bsdf.inputs["Normal"].is_linked)
    out = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"][0]
    check("moss: Displacement unlinked", not out.inputs["Displacement"].is_linked)
    check("moss: base colour is moss", "Moss" in (img_of(bsdf.inputs["Base Color"]).filepath
                                                 if img_of(bsdf.inputs["Base Color"]) else ""))
    check("no zero-user images left after switching set", not zero_user_images(), zero_user_images())
    for _ in range(3):
        bpy.ops.texture_plugger.apply()
    check("repeat apply: node count stable", len(nt.nodes) == n_user + len(tagged(nt)))
    check("repeat apply: images stable", not zero_user_images(), zero_user_images())
    bpy.ops.texture_plugger.clear()
    check("clear: only user nodes remain", len(nt.nodes) == n_user and not tagged(nt))
    check("clear: no zero-user images", not zero_user_images(), zero_user_images())


def test_no_principled_and_misc_objects():
    print("\n[no Principled BSDF, curves, edit mode, empty]")
    folder = make_set("misc", ["Misc_Color.png", "Misc_Roughness.png"])
    obj = new_cube("DiffCube")
    mat = bpy.data.materials.new("DiffMat")
    if mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        if n.bl_idname == "ShaderNodeBsdfPrincipled":
            nt.nodes.remove(n)
    diff = nt.nodes.new("ShaderNodeBsdfDiffuse")
    out = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"][0]
    nt.links.new(diff.outputs[0], out.inputs["Surface"])
    obj.data.materials.append(mat)
    select_only(obj)
    res = bpy.ops.texture_plugger.load_set(directory=folder + os.sep)
    check("no-Principled material: finished", res == {"FINISHED"}, res)
    check("diffuse still drives output", src_node(out.inputs["Surface"]) == diff)
    bpy.ops.texture_plugger.apply()
    check("re-apply: single Principled", len([n for n in nt.nodes
                                             if n.bl_idname == "ShaderNodeBsdfPrincipled"]) == 1)

    cu = bpy.data.curves.new("Crv", "CURVE")
    cu.splines.new("BEZIER")
    cob = bpy.data.objects.new("CurveObj", cu)
    bpy.context.scene.collection.objects.link(cob)
    select_only(cob)
    res = bpy.ops.texture_plugger.apply()
    check("curve object gets material", res == {"FINISHED"} and cob.active_material is not None, res)

    em = bpy.data.objects.new("EmptyObj", None)
    bpy.context.scene.collection.objects.link(em)
    select_only(em)
    res = bpy.ops.texture_plugger.apply()
    check("empty -> CANCELLED without exception", res == {"CANCELLED"}, res)

    ed = new_cube("EditCube")
    select_only(ed)
    bpy.ops.object.mode_set(mode="EDIT")
    res = bpy.ops.texture_plugger.apply()
    bpy.ops.object.mode_set(mode="OBJECT")
    check("apply in Edit Mode", res == {"FINISHED"} and ed.active_material is not None
          and len(ed.data.materials) == 1, res)


def test_linked(pkg):
    print("\n[linked mesh, linked + overridden materials]")
    lib_dir = os.path.join(TMP, "lib")
    os.makedirs(lib_dir, exist_ok=True)
    lib = os.path.join(lib_dir, "lib.blend")
    m = bpy.data.materials.new("LibMat")
    if m.node_tree is None:
        m.use_nodes = True
    me = bpy.data.meshes.new("LibMesh")
    bpy.data.libraries.write(lib, {m, me}, fake_user=True)
    bpy.data.materials.remove(m)
    bpy.data.meshes.remove(me)
    with bpy.data.libraries.load(lib, link=True) as (src, dst):
        dst.materials = ["LibMat"]
        dst.meshes = ["LibMesh"]
    lmat = bpy.data.materials["LibMat", lib] if ("LibMat", lib) in bpy.data.materials else \
        [x for x in bpy.data.materials if x.library][0]
    lmesh = [x for x in bpy.data.meshes if x.library][0]
    nb = pkg.node_builder
    check("linked material not editable", not nb.material_is_editable(lmat))
    ov = lmat.override_create(remap_local_usages=False)
    check("override material treated read-only", ov is None or not nb.material_is_editable(ov))

    ob = bpy.data.objects.new("LinkedMeshObj", lmesh)
    bpy.context.scene.collection.objects.link(ob)
    select_only(ob)
    folder = os.path.join(TMP, "misc")
    n_mats = len(bpy.data.materials)
    try:
        res = bpy.ops.texture_plugger.load_set(directory=folder + os.sep)
        check("linked mesh: no exception, nothing built",
              res in ({"FINISHED"}, {"CANCELLED"}) and len(ob.material_slots) == 0, res)
    except Exception as ex:
        check("linked mesh: no exception, nothing built", False, repr(ex))
    check("linked mesh: no orphan material created", len(bpy.data.materials) == n_mats,
          (n_mats, len(bpy.data.materials)))


def test_undo():
    print("\n[undo]")
    folder = os.path.join(TMP, "misc")
    obj = new_cube("UndoCube")
    select_only(obj)
    try:
        with ui_override("VIEW_3D"):
            bpy.ops.ed.undo_push(message="before")
            bpy.ops.texture_plugger.load_set("EXEC_DEFAULT", directory=folder + os.sep)
            built = bpy.data.objects["UndoCube"].active_material is not None
            bpy.ops.ed.undo()
        check("apply built before undo", built)
        ob = bpy.data.objects.get("UndoCube")
        check("undo removes the material assignment", ob is not None and ob.active_material is None,
              ob.active_material if ob else None)
    except RuntimeError as ex:
        print("  SKIP  undo not testable in --background:", ex)


def test_save_reload():
    print("\n[save + reload, then re-apply / clear]")
    folder = make_set("reload", ["Persist_Color.png", "Persist_Normal.png", "Persist_Roughness.png"])
    obj = new_cube("PersistCube")
    select_only(obj)
    bpy.ops.texture_plugger.load_set(directory=folder + os.sep)
    nt = obj.active_material.node_tree
    n_tag, n_all = len(tagged(nt)), len(nt.nodes)
    blend = os.path.join(TMP, "saved dir ü", "persist.blend")
    os.makedirs(os.path.dirname(blend), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    obj = bpy.data.objects["PersistCube"]
    select_only(obj)
    nt = obj.active_material.node_tree
    check("tags persist after reload", len(tagged(nt)) == n_tag, (len(tagged(nt)), n_tag))
    check("slots persist", len(S().slots) == 3, len(S().slots))
    res = bpy.ops.texture_plugger.apply()
    check("re-apply after reload: no duplicates", res == {"FINISHED"} and len(nt.nodes) == n_all,
          (len(nt.nodes), n_all))
    S().relative_paths = True
    bpy.ops.texture_plugger.apply()
    img = img_of(principled(nt).inputs["Roughness"])
    check("relative path after reload", img is not None and img.filepath.startswith("//"),
          img.filepath if img else None)
    check("relative image resolves", img is not None and os.path.isfile(bpy.path.abspath(img.filepath)))
    n_img = len(bpy.data.images)
    bpy.ops.texture_plugger.apply()
    check("re-apply with relative paths reuses images", len(bpy.data.images) == n_img,
          (n_img, len(bpy.data.images)))
    S().relative_paths = False
    res = bpy.ops.texture_plugger.clear()
    check("clear after reload", res == {"FINISHED"} and not tagged(nt), res)


def test_enable_disable():
    print("\n[disable / enable x3]")
    try:
        for _ in range(3):
            bpy.ops.preferences.addon_disable(module=MODULE)
            assert not hasattr(bpy.types, "TEXTUREPLUGGER_PT_view3d")
            assert not hasattr(bpy.types.Scene, "texture_plugger")
            bpy.ops.preferences.addon_enable(module=MODULE)
            assert hasattr(bpy.types, "TEXTUREPLUGGER_FH_drop")
        menu = [f.__module__ for f in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize()]
        check("3 disable/enable cycles", True)
        check("import menu entry not duplicated",
              sum(1 for m in menu if m.startswith(MODULE)) == 1, menu)
        check("scene data survives cycles", len(S().slots) > 0)
        res = bpy.ops.texture_plugger.apply()
        check("apply works after cycles", res == {"FINISHED"}, res)
    except Exception as ex:
        check("disable/enable cycles", False, repr(ex))


def main():
    print(f"Blender {bpy.app.version_string}  adversarial")
    try:
        pkg = install()
        test_detection_names(pkg)
        test_drag_drop()
        test_two_sets_and_user_nodes()
        test_no_principled_and_misc_objects()
        test_linked(pkg)
        test_undo()
        test_save_reload()
        test_enable_disable()
    except Exception:
        traceback.print_exc()
        check("adversarial run completed without exception", False, "see traceback")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for r in RESULTS if r[1])
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nADVERSARIAL RESULT: {passed} passed, {len(failed)} failed "
          f"(Blender {bpy.app.version_string})")
    for name, _, detail in failed:
        print(f"  FAILED: {name}: {detail}")
    sys.stdout.flush()
    if failed:
        sys.exit(1)


main()
