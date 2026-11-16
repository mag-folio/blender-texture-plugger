"""Headless Blender integration test for Texture Plugger.

Run through tests/run_blender_tests.ps1, or manually:
  blender --background --factory-startup --python-exit-code 1 \
          --python tests/blender_test.py -- --zip dist/texture_plugger-1.1.0.zip
  blender ... --python tests/blender_test.py -- --legacy        (package already in scripts/addons)

Use an isolated BLENDER_USER_RESOURCES directory so your real config is untouched.
"""

import os
import shutil
import sys
import tempfile
import traceback

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  -- {detail}"))
    return bool(cond)


# --------------------------------------------------------------------------
# Install / enable
# --------------------------------------------------------------------------

def install():
    if "--legacy" in ARGS:
        import addon_utils
        mod = addon_utils.enable("texture_plugger", default_set=True, handle_error=None)
        check("legacy add-on enabled", mod is not None)
        return "texture_plugger"
    zip_path = os.path.abspath(ARGS[ARGS.index("--zip") + 1])
    check("zip exists", os.path.isfile(zip_path), zip_path)
    bpy.ops.extensions.package_install_files(
        filepath=zip_path, repo="user_default", enable_on_install=True)
    module = "bl_ext.user_default.texture_plugger"
    check("extension enabled", module in bpy.context.preferences.addons,
          list(bpy.context.preferences.addons.keys()))
    return module


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

TMP = tempfile.mkdtemp(prefix="tp_blender_test_")


def make_image(folder, name, rgba=(0.5, 0.5, 0.5, 1.0)):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    ext = os.path.splitext(name)[1].lower()
    fmt = {".png": "PNG", ".jpg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF", ".tga": "TARGA"}[ext]
    img = bpy.data.images.new("gen_" + name, 4, 4, alpha=True, float_buffer=(fmt == "OPEN_EXR"))
    img.pixels = list(rgba) * 16
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


def new_cube(name, location=(0, 0, 0)):
    mesh = bpy.data.meshes.new(name)
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def select_only(*objs):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0] if objs else None


def settings():
    return bpy.context.scene.texture_plugger


def load(folder, names=None, apply=True):
    if names is None:
        return bpy.ops.texture_plugger.load_set(directory=folder + os.sep, apply_after=apply)
    return bpy.ops.texture_plugger.load_set(
        directory=folder + os.sep, files=[{"name": n} for n in names], apply_after=apply)


# --------------------------------------------------------------------------
# Inspection helpers
# --------------------------------------------------------------------------

TAG = "texture_plugger"


def tagged(nt):
    return [n for n in nt.nodes if n.get(TAG) is not None]


def by_role(nt, role):
    return [n for n in nt.nodes if n.get(TAG) == role]


def principled(nt):
    return [n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"][0]


def src_node(sock):
    return sock.links[0].from_node if sock.is_linked else None


def src_sock(sock):
    return sock.links[0].from_socket if sock.is_linked else None


def snapshot(mat):
    nt = mat.node_tree
    nodes = sorted((n.bl_idname, n.name, n.label, round(n.location.x), round(n.location.y))
                   for n in nt.nodes)
    links = sorted((l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier)
                   for l in nt.links)
    return nodes, links, len(bpy.data.images), len(bpy.data.materials)


def image_cs(node):
    return node.image.colorspace_settings.name if node and node.image else None


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_registration(module):
    print("\n[registration]")
    check("operator load_set", hasattr(bpy.ops.texture_plugger, "load_set"))
    check("operator apply", hasattr(bpy.ops.texture_plugger, "apply"))
    check("operator clear", hasattr(bpy.ops.texture_plugger, "clear"))
    check("FileHandler registered", hasattr(bpy.types, "TEXTUREPLUGGER_FH_drop"))
    check("3D view panel", hasattr(bpy.types, "TEXTUREPLUGGER_PT_view3d"))
    check("shader editor panel", hasattr(bpy.types, "TEXTUREPLUGGER_PT_shader"))
    check("scene settings", hasattr(bpy.context.scene, "texture_plugger"))
    prefs = bpy.context.preferences.addons[module].preferences
    check("preferences have keyword lists", "albedo" in prefs.kw_base_color)
    menu_funcs = [f.__name__ for f in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize()]
    check("File > Import entry", "menu_func_import" in menu_funcs, menu_funcs)


def test_basic_set():
    print("\n[basic ambientCG-style set: colour spaces, links, idempotency, clear]")
    names = [
        "Bricks076C_1K-PNG_Color.png", "Bricks076C_1K-JPG_Color.jpg",
        "Bricks076C_1K-PNG_NormalGL.png", "Bricks076C_1K-PNG_NormalDX.png",
        "Bricks076C_1K-PNG_Roughness.png", "Bricks076C_1K-PNG_Metalness.png",
        "Bricks076C_1K-PNG_AmbientOcclusion.png", "Bricks076C_1K-PNG_Displacement.png",
        "Bricks076C_1K-PNG_Displacement.exr",
        "Bricks076C_1K-PNG_Opacity.png", "Bricks076C_1K-PNG_Emission.png",
        "Bricks076C.png",
    ]
    folder = make_set("basic", names)
    cube = bpy.data.objects["Cube"]
    select_only(cube)
    mat = cube.active_material
    nt = mat.node_tree
    user_node = nt.nodes.new("ShaderNodeValue")
    user_node.name = "UserValueNode"
    orig_disp = mat.displacement_method
    bsdf = principled(nt)
    orig_emit = bsdf.inputs["Emission Strength"].default_value
    nodes_before = len(nt.nodes)

    res = load(folder)  # folder only: scan
    check("load_set finished", res == {"FINISHED"}, res)
    s = settings()
    types = {sl.map_type for sl in s.slots if sl.enabled}
    check("slots detected", {"base_color", "normal_gl", "roughness", "metallic", "ao", "height",
                             "opacity", "emission"} <= types, types)
    dx = [sl for sl in s.slots if sl.map_type == "normal_dx"]
    check("DX normal present but disabled (GL wins)", dx and not dx[0].enabled)
    unassigned = [sl for sl in s.slots if sl.map_type == "NONE"]
    check("preview file listed as unassigned", len(unassigned) == 1)
    hslot = [sl for sl in s.slots if sl.map_type == "height"][0]
    check("EXR chosen for displacement", hslot.filepath.endswith(".exr"), hslot.filepath)
    cslot = [sl for sl in s.slots if sl.map_type == "base_color"][0]
    check("PNG chosen over JPG for colour", cslot.filepath.endswith(".png"), cslot.filepath)

    check("same material kept", cube.active_material == mat)
    bsdf = principled(nt)
    base_from = src_node(bsdf.inputs["Base Color"])
    check("Base Color <- AO multiply mix", base_from is not None and base_from.bl_idname == "ShaderNodeMix"
          and base_from.blend_type == "MULTIPLY", base_from)
    a_sock = [i for i in base_from.inputs if i.identifier == "A_Color"][0]
    b_sock = [i for i in base_from.inputs if i.identifier == "B_Color"][0]
    col_img = src_node(a_sock)
    ao_img = src_node(b_sock)
    check("base colour image is sRGB", image_cs(col_img) == "sRGB", image_cs(col_img))
    check("AO image Non-Color", image_cs(ao_img) == "Non-Color", image_cs(ao_img))
    rough = src_node(bsdf.inputs["Roughness"])
    check("Roughness linked, Non-Color", rough and image_cs(rough) == "Non-Color", image_cs(rough))
    metal = src_node(bsdf.inputs["Metallic"])
    check("Metallic linked, Non-Color", metal and image_cs(metal) == "Non-Color")
    nm = src_node(bsdf.inputs["Normal"])
    check("Normal <- Normal Map node", nm and nm.bl_idname == "ShaderNodeNormalMap", nm)
    nimg = src_node(nm.inputs["Color"]) if nm else None
    check("normal image Non-Color", image_cs(nimg) == "Non-Color", image_cs(nimg))
    check("GL normal not flipped", nimg and nimg.bl_idname == "ShaderNodeTexImage")
    out = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"][0]
    disp = src_node(out.inputs["Displacement"])
    check("Output Displacement <- Displacement node", disp and disp.bl_idname == "ShaderNodeDisplacement")
    check("displacement midlevel/scale", disp and abs(disp.inputs["Midlevel"].default_value - 0.5) < 1e-6
          and abs(disp.inputs["Scale"].default_value - 0.1) < 1e-6)
    himg = src_node(disp.inputs["Height"]) if disp else None
    check("height image Non-Color (EXR)", image_cs(himg) == "Non-Color", image_cs(himg))
    check("displacement_method BOTH", mat.displacement_method == "BOTH", mat.displacement_method)
    alpha = src_node(bsdf.inputs["Alpha"])
    check("Alpha linked", alpha is not None and image_cs(alpha) == "Non-Color")
    check("surface_render_method DITHERED", getattr(mat, "surface_render_method", "DITHERED") == "DITHERED")
    check("transparent shadows on", getattr(mat, "use_transparent_shadow", True))
    em = src_node(bsdf.inputs["Emission Color"])
    check("Emission Color linked sRGB", em is not None and image_cs(em) == "sRGB", image_cs(em))
    check("Emission Strength set to 1", abs(bsdf.inputs["Emission Strength"].default_value - 1.0) < 1e-6)

    texco = by_role(nt, "texcoord")
    mapping = by_role(nt, "mapping")
    check("one shared TexCoord + Mapping", len(texco) == 1 and len(mapping) == 1)
    imgs = [n for n in tagged(nt) if n.bl_idname == "ShaderNodeTexImage"]
    check("every image driven by the shared Mapping",
          imgs and all(src_node(i.inputs["Vector"]) == mapping[0] for i in imgs))
    check("mapping driven by UV", src_sock(mapping[0].inputs["Vector"]).name == "UV")
    frame = by_role(nt, "frame")
    check("frame created", len(frame) == 1)
    check("images parented to frame", all(i.parent == frame[0] for i in imgs))
    if hasattr(imgs[0], "location_absolute"):
        bx = bsdf.location.x
        check("image column left of BSDF (absolute)",
              all(abs(i.location_absolute.x - (bx - 1500)) < 1.0 for i in imgs),
              [round(i.location_absolute.x) for i in imgs])
    check("user node untouched", nt.nodes.get("UserValueNode") is not None)
    check("all images single datablock per file",
          len({i.image.name for i in imgs}) == len(imgs))

    # Idempotency
    snap1 = snapshot(mat)
    bpy.ops.texture_plugger.apply()
    snap2 = snapshot(mat)
    check("apply twice -> identical nodes", snap1[0] == snap2[0],
          (len(snap1[0]), len(snap2[0])))
    check("apply twice -> identical links", snap1[1] == snap2[1])
    check("apply twice -> no new images", snap1[2] == snap2[2], (snap1[2], snap2[2]))
    load(folder)
    snap3 = snapshot(mat)
    check("load again -> identical result", snap1[:3] == snap3[:3], (len(snap1[0]), len(snap3[0])))

    # Clear
    res = bpy.ops.texture_plugger.clear()
    check("clear finished", res == {"FINISHED"}, res)
    check("no tagged nodes after clear", not tagged(nt))
    check("node count restored", len(nt.nodes) == nodes_before, (len(nt.nodes), nodes_before))
    check("user node survives clear", nt.nodes.get("UserValueNode") is not None)
    check("displacement method restored", mat.displacement_method == orig_disp, mat.displacement_method)
    check("emission strength restored",
          abs(principled(nt).inputs["Emission Strength"].default_value - orig_emit) < 1e-6)
    check("BSDF still connected to output", principled(nt).outputs["BSDF"].is_linked)
    res = bpy.ops.texture_plugger.clear()
    check("second clear is a no-op", res == {"CANCELLED"}, res)
    nt.nodes.remove(user_node)


def test_packed_dx_gloss():
    print("\n[packed ORM + DirectX normal + gloss]")
    folder = make_set("packed", ["Rock_BaseColor.png", "Rock_ORM.png", "Rock_Normal_DirectX.png"])
    obj = new_cube("PackedCube", (3, 0, 0))
    select_only(obj)
    res = load(folder)
    check("load packed finished", res == {"FINISHED"}, res)
    mat = obj.active_material
    check("material created for empty object", mat is not None and len(obj.material_slots) == 1)
    nt = mat.node_tree
    bsdf = principled(nt)
    sep = by_role(nt, "separate:orm")
    check("Separate Color for ORM", len(sep) == 1 and sep[0].bl_idname == "ShaderNodeSeparateColor")
    sep = sep[0]
    check("ORM image Non-Color", image_cs(src_node(sep.inputs["Color"])) == "Non-Color")
    check("ORM image alpha channel-packed", src_node(sep.inputs["Color"]).image.alpha_mode == "CHANNEL_PACKED")
    check("Green -> Roughness", src_sock(bsdf.inputs["Roughness"]) == sep.outputs["Green"])
    check("Blue -> Metallic", src_sock(bsdf.inputs["Metallic"]) == sep.outputs["Blue"])
    mix = src_node(bsdf.inputs["Base Color"])
    b_sock = [i for i in mix.inputs if i.identifier == "B_Color"][0]
    check("Red (AO) -> AO multiply", src_sock(b_sock) == sep.outputs["Red"])

    nm = src_node(bsdf.inputs["Normal"])
    comb = src_node(nm.inputs["Color"])
    check("DX: Normal Map <- Combine Color", comb and comb.bl_idname == "ShaderNodeCombineColor", comb)
    flip = src_node(comb.inputs["Green"])
    check("DX: green from 1 - G math", flip and flip.bl_idname == "ShaderNodeMath"
          and flip.operation == "SUBTRACT" and abs(flip.inputs[0].default_value - 1.0) < 1e-6)
    dsep = src_node(flip.inputs[1])
    check("DX: math fed by Separate Green", dsep and src_sock(flip.inputs[1]).name == "Green")
    check("DX: red/blue passed through", src_sock(comb.inputs["Red"]).name == "Red"
          and src_sock(comb.inputs["Blue"]).name == "Blue")

    # Gloss
    folder = make_set("gloss", ["Wood_Albedo.png", "Wood_Gloss.png", "Wood_Normal.png", "Wood_Bump.png"])
    obj2 = new_cube("GlossCube", (6, 0, 0))
    select_only(obj2)
    load(folder)
    nt = obj2.active_material.node_tree
    bsdf = principled(nt)
    inv = src_node(bsdf.inputs["Roughness"])
    check("Gloss -> Invert -> Roughness", inv and inv.bl_idname == "ShaderNodeInvert", inv)
    check("gloss image Non-Color", image_cs(src_node(inv.inputs["Color"])) == "Non-Color")
    bump = src_node(bsdf.inputs["Normal"])
    check("Normal <- Bump", bump and bump.bl_idname == "ShaderNodeBump", bump)
    check("Bump.Normal <- Normal Map", src_node(bump.inputs["Normal"]).bl_idname == "ShaderNodeNormalMap")
    check("Bump.Height <- bump image", src_node(bump.inputs["Height"]).bl_idname == "ShaderNodeTexImage")


def test_udim():
    print("\n[UDIM]")
    folder = make_set("udim", ["Hero_BaseColor.1001.png", "Hero_BaseColor.1002.png",
                               "Hero_Normal.1001.png", "Hero_Normal.1002.png"])
    obj = new_cube("UdimCube", (9, 0, 0))
    select_only(obj)
    res = load(folder)
    check("load udim finished", res == {"FINISHED"}, res)
    nt = obj.active_material.node_tree
    img_node = src_node(principled(nt).inputs["Base Color"])
    img = img_node.image if img_node else None
    check("base colour image is TILED", img is not None and img.source == "TILED",
          img.source if img else None)
    check("tiles 1001+1002", img is not None and sorted(t.number for t in img.tiles) == [1001, 1002],
          [t.number for t in img.tiles] if img else None)
    check("UDIM filepath token", img is not None and "<UDIM>" in img.filepath, img.filepath if img else None)
    n_images = len(bpy.data.images)
    bpy.ops.texture_plugger.apply()
    check("UDIM re-apply reuses tiled image", len(bpy.data.images) == n_images)


def test_multi_object():
    print("\n[multi-object, material modes]")
    folder = make_set("multi", ["Tile_Color.png", "Tile_Roughness.png"])
    a = new_cube("MultiA", (0, 4, 0))
    b = new_cube("MultiB", (2, 4, 0))
    c = new_cube("MultiC", (4, 4, 0))
    mat_c = bpy.data.materials.new("OwnMatC")
    c.data.materials.append(mat_c)
    select_only(a, b, c)
    s = settings()
    s.apply_to = "SELECTED"
    s.material_mode = "ACTIVE_SLOT"
    load(folder)
    check("A and B share one new material", a.active_material is not None
          and a.active_material == b.active_material)
    check("C keeps its own material", c.active_material == mat_c)
    for o in (a, b, c):
        bsdf = principled(o.active_material.node_tree)
        check(f"{o.name} Base Color linked", bsdf.inputs["Base Color"].is_linked)
    n_mats = len(bpy.data.materials)
    bpy.ops.texture_plugger.apply()
    check("re-apply on selection: no new materials", len(bpy.data.materials) == n_mats)

    # NEW material mode: never edits a foreign material, idempotent per set
    d = new_cube("MultiD", (6, 4, 0))
    e = new_cube("MultiE", (8, 4, 0))
    foreign = bpy.data.materials.new("ForeignMat")
    if foreign.node_tree is None:  # Blender < 5.0
        foreign.use_nodes = True
    d.data.materials.append(foreign)
    e.data.materials.append(foreign)
    foreign_nodes = len(foreign.node_tree.nodes)
    select_only(d, e)
    s.material_mode = "NEW"
    bpy.ops.texture_plugger.apply()
    new_mat = d.active_material
    check("NEW mode replaced foreign material in the slot", new_mat != foreign)
    check("NEW mode material named after set", new_mat.name.startswith("Tile"), new_mat.name)
    check("NEW mode: one new material shared by the selection", e.active_material == new_mat)
    check("NEW mode: foreign material untouched",
          len(foreign.node_tree.nodes) == foreign_nodes and not tagged(foreign.node_tree))
    n_after_first_new = len(bpy.data.materials)
    bpy.ops.texture_plugger.apply()
    check("NEW mode re-apply reuses material", len(bpy.data.materials) == n_after_first_new
          and d.active_material == new_mat)
    res = bpy.ops.texture_plugger.clear()
    check("clear on selection", res == {"FINISHED"} and not tagged(new_mat.node_tree))
    s.material_mode = "ACTIVE_SLOT"
    s.apply_to = "ACTIVE"


def test_box_projection_and_options():
    print("\n[box projection, height as bump, base alpha]")
    folder = make_set("box", ["Leaf_Albedo.png", "Leaf_Height.png"])
    obj = new_cube("BoxCube", (0, 8, 0))
    select_only(obj)
    s = settings()
    s.projection = "BOX"
    s.height_mode = "BUMP"
    s.use_base_alpha = True
    s.texture_scale = 2.0
    load(folder)
    nt = obj.active_material.node_tree
    imgs = [n for n in tagged(nt) if n.bl_idname == "ShaderNodeTexImage"]
    check("images use BOX projection", imgs and all(i.projection == "BOX" for i in imgs))
    mapping = by_role(nt, "mapping")[0]
    check("box uses Object coordinates", src_sock(mapping.inputs["Vector"]).name == "Object")
    check("mapping scale applied", abs(mapping.inputs["Scale"].default_value[0] - 2.0) < 1e-6)
    bsdf = principled(nt)
    check("height used as bump", src_node(bsdf.inputs["Normal"]).bl_idname == "ShaderNodeBump")
    out = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"][0]
    check("no displacement link in bump mode", not out.inputs["Displacement"].is_linked)
    check("base colour alpha -> Alpha", src_sock(bsdf.inputs["Alpha"]).name == "Alpha")
    s.projection = "UV"
    s.height_mode = "DISPLACEMENT"
    s.use_base_alpha = False
    s.texture_scale = 1.0


def test_manual_slots_and_robustness(module):
    print("\n[manual slots, robustness]")
    folder = make_set("manual", ["mystery_a.png", "mystery_b.png"])
    obj = new_cube("ManualCube", (0, 12, 0))
    select_only(obj)
    res = load(folder)
    s = settings()
    check("unrecognised files listed as unassigned", len(s.slots) == 2
          and all(sl.map_type == "NONE" for sl in s.slots), [sl.map_type for sl in s.slots])
    s.slots[0].map_type = "base_color"
    s.slots[0].enabled = True
    s.slots[1].map_type = "roughness"
    s.slots[1].enabled = True
    res = bpy.ops.texture_plugger.apply()
    check("manual assignment applied", res == {"FINISHED"}
          and principled(obj.active_material.node_tree).inputs["Roughness"].is_linked)
    # Slot clear removes the link on next apply
    bpy.ops.texture_plugger.slot_clear(index=1)
    bpy.ops.texture_plugger.apply()
    check("cleared slot no longer linked",
          not principled(obj.active_material.node_tree).inputs["Roughness"].is_linked)
    # Browse sets path + auto-detects type for unassigned slot
    rough_path = make_image(folder, "mystery_Roughness.png")
    bpy.ops.texture_plugger.slot_add(map_type="metallic")
    check("slot_add appended", s.slots[-1].map_type == "metallic")
    s.slots[-1].map_type = "NONE"
    bpy.ops.texture_plugger.slot_browse(index=len(s.slots) - 1, filepath=rough_path)
    check("slot_browse auto-detects type", s.slots[-1].map_type == "roughness", s.slots[-1].map_type)
    bpy.ops.texture_plugger.slot_remove()

    # Missing file -> warning, not a traceback
    s.slots[0].filepath = os.path.join(folder, "does_not_exist.png")
    try:
        res = bpy.ops.texture_plugger.apply()
        check("missing file handled without exception", res in ({"CANCELLED"}, {"FINISHED"}), res)
    except Exception as ex:
        check("missing file handled without exception", False, ex)

    # No active object
    bpy.context.view_layer.objects.active = None
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    try:
        res = bpy.ops.texture_plugger.apply()
        check("no active object -> CANCELLED, no exception", res == {"CANCELLED"}, res)
    except Exception as ex:
        check("no active object -> CANCELLED, no exception", False, ex)

    # Non-material object (camera/light)
    cam = bpy.data.objects.get("Camera")
    select_only(cam)
    s.slots[0].filepath = os.path.join(folder, "mystery_a.png")
    try:
        res = bpy.ops.texture_plugger.apply()
        check("camera target -> CANCELLED, no exception", res == {"CANCELLED"}, res)
    except Exception as ex:
        check("camera target -> CANCELLED, no exception", False, ex)

    # Folder without images
    empty = os.path.join(TMP, "empty")
    os.makedirs(empty, exist_ok=True)
    open(os.path.join(empty, "readme.txt"), "w").close()
    try:
        res = load(empty)
        check("empty folder -> CANCELLED, no exception", res == {"CANCELLED"}, res)
    except Exception as ex:
        check("empty folder -> CANCELLED, no exception", False, ex)

    # Relative paths on unsaved file: warns and falls back to absolute
    obj = new_cube("RelCube", (0, 16, 0))
    select_only(obj)
    s.relative_paths = True
    try:
        res = load(make_set("rel", ["Rel_Color.png"]))
        img = src_node(principled(obj.active_material.node_tree).inputs["Base Color"]).image
        check("relative paths on unsaved file -> absolute + no exception",
              res == {"FINISHED"} and os.path.isabs(img.filepath), img.filepath)
    except Exception as ex:
        check("relative paths on unsaved file -> absolute + no exception", False, ex)
    # Saved file -> relative
    blend = os.path.join(TMP, "rel_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.texture_plugger.apply()
    img = src_node(principled(obj.active_material.node_tree).inputs["Base Color"]).image
    check("relative path after save", img.filepath.startswith("//"), img.filepath)
    s.relative_paths = False

    # Custom keywords from preferences
    prefs = bpy.context.preferences.addons[module].preferences
    prefs.kw_base_color = "farbe"
    folder = make_set("custom_kw", ["Stein_Farbe.png", "Stein_Albedo.png"])
    obj = new_cube("KwCube", (0, 20, 0))
    select_only(obj)
    load(folder, apply=False)
    types = {os.path.basename(sl.filepath): sl.map_type for sl in s.slots}
    check("custom keyword used", types.get("Stein_Farbe.png") == "base_color", types)
    check("removed keyword no longer matches", types.get("Stein_Albedo.png") in (None, "NONE"), types)
    bpy.ops.texture_plugger.reset_keywords()
    check("reset keywords", "albedo" in prefs.kw_base_color)


def test_multiple_sets():
    print("\n[multiple sets in one folder]")
    folder = make_set("sets", ["Bricks01_Color.png", "Bricks01_Roughness.png",
                               "Wood02_Color.png", "Wood02_Roughness.png", "Wood02_NormalGL.png"])
    obj = new_cube("SetsCube", (0, 24, 0))
    select_only(obj)
    load(folder, apply=False)
    s = settings()
    check("two sets offered", len(s.sets) == 2, [x.name for x in s.sets])
    check("largest set chosen", s.set_key == "wood_02", s.set_key)
    s.active_set = "bricks_01"
    check("switching set repopulates slots", s.set_key == "bricks_01"
          and all("Bricks01" in os.path.basename(sl.filepath) for sl in s.slots),
          [sl.filepath for sl in s.slots])


def _tex_image_node(mat, role):
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and n.get(TAG) == "image:" + role:
            return n
    return None


_SCAFFOLD = []     # the installed add-on's scaffold module, set by test_create_and_paint


def _write_png(path, size, rgb):
    """Replace ``path`` with a solid colour PNG (uses the add-on's pure-Python writer)."""
    if os.path.exists(path):
        os.remove(path)
    _SCAFFOLD[0].write_png(path, size, size, rgb)


def run_op(fn, **kw):
    """Run an operator; an ERROR report makes bpy.ops raise RuntimeError after CANCELLED.

    Returns ("CANCELLED"/"FINISHED"/..., message).
    """
    try:
        res = fn(**kw)
    except RuntimeError as ex:
        return "CANCELLED", str(ex)
    return ("FINISHED" if "FINISHED" in res else "CANCELLED"), ""


def test_create_unsaved_relative():
    """Must run while the session's .blend is still unsaved (before test_manual_slots...)."""
    print("\n[create_set: unsaved '//' path and empty folder]")
    check("session starts unsaved", not bpy.data.filepath, bpy.data.filepath)
    obj = new_cube("PaintUnsaved", (0, 40, 0))
    select_only(obj)
    n_mats = len(bpy.data.materials)
    res = run_op(bpy.ops.texture_plugger.create_set, directory="//paint_rel_unsaved")
    check("unsaved + '//' folder -> CANCELLED", res[0] == "CANCELLED" and "folder" in res[1], res)
    res = run_op(bpy.ops.texture_plugger.create_set, directory="")
    check("empty folder -> CANCELLED", res[0] == "CANCELLED" and "folder" in res[1], res)
    check("cancelled runs left no material", obj.active_material is None
          and len(bpy.data.materials) == n_mats)
    check("cancelled runs created no folder",
          not os.path.exists(os.path.join(os.getcwd(), "paint_rel_unsaved")))


def test_create_and_paint(module):
    print("\n[Create & Paint: create_set, reload, edit_in_painter]")
    import importlib
    pkg = importlib.import_module(module)
    wf = pkg.workflow
    _SCAFFOLD[:] = [pkg.scaffold]
    prefs = bpy.context.preferences.addons[module].preferences
    s = settings()

    # --- registration
    for op in ("create_set", "edit_in_painter", "reload_images", "open_set_folder"):
        check(f"operator {op}", hasattr(bpy.ops.texture_plugger, op))
    check("3D view paint panel", hasattr(bpy.types, "TEXTUREPLUGGER_PT_view3d_paint"))
    check("shader editor paint panel", hasattr(bpy.types, "TEXTUREPLUGGER_PT_shader_paint"))
    check("reload timer registered", bpy.app.timers.is_registered(wf._timer))
    check("create_* defaults", s.create_base_color and s.create_normal and s.create_metallic
          and s.create_roughness and not s.create_emission and not s.create_alpha
          and not s.create_displacement and s.create_resolution == "1024")

    root = os.path.join(TMP, "paint")
    os.makedirs(root, exist_ok=True)
    other = new_cube("PaintOther", (0, 44, 0))
    obj = new_cube("PaintCrate", (3, 44, 0))
    select_only(obj)
    other.select_set(True)

    saved = {k: getattr(s, k) for k in ("create_root", "create_name", "create_resolution")}
    saved_toggles = {k: getattr(s, k) for k in dir(s) if k.startswith("create_")
                     and isinstance(getattr(s, k), bool)}
    saved_painter = prefs.painter_path
    orig_launch = wf._launch
    try:
        # --- no maps enabled: no side effects at all
        for k in saved_toggles:
            setattr(s, k, False)
        n_mats = len(bpy.data.materials)
        res = run_op(bpy.ops.texture_plugger.create_set, directory=root)
        check("no maps enabled -> CANCELLED", res[0] == "CANCELLED", res)
        check("no maps: no folder", not os.path.exists(os.path.join(root, "PaintCrate")))
        check("no maps: no material", obj.active_material is None
              and len(bpy.data.materials) == n_mats)
        for k, v in saved_toggles.items():
            setattr(s, k, v)

        # --- empty folder (neither argument nor setting)
        s.create_root = ""
        res = run_op(bpy.ops.texture_plugger.create_set, directory="")
        check("empty folder -> CANCELLED (settings empty too)", res[0] == "CANCELLED", res)
        check("empty folder: no material", obj.active_material is None)

        # --- create
        res = bpy.ops.texture_plugger.create_set(directory=root)
        check("create_set finished", res == {"FINISHED"}, res)
        mat = obj.active_material
        check("material named after object", mat is not None and mat.name == "PaintCrate",
              mat and mat.name)
        folder = os.path.join(root, "PaintCrate")
        tex = os.path.join(folder, "textures")
        fbx = os.path.join(folder, "mesh", "PaintCrate.fbx")
        expected = ["PaintCrate_BaseColor.png", "PaintCrate_Metallic.png",
                    "PaintCrate_Normal.png", "PaintCrate_Roughness.png"]
        check("only default maps created", sorted(os.listdir(tex)) == expected,
              sorted(os.listdir(tex)))
        check("FBX exported", os.path.isfile(fbx) and os.path.getsize(fbx) > 0)
        check("PROJECT_KEY stored", mat.get(wf.PROJECT_KEY) == folder, mat.get(wf.PROJECT_KEY))
        check("MESH_KEY stored", mat.get(wf.MESH_KEY) == fbx, mat.get(wf.MESH_KEY))
        check("material_project() helper", wf.material_project(mat) == folder)
        check("selection restored", obj.select_get() and other.select_get()
              and bpy.context.view_layer.objects.active == obj)
        check("create_root setting untouched by directory arg", s.create_root == "")

        bsdf = principled(mat.node_tree)
        for sock in ("Base Color", "Metallic", "Roughness", "Normal"):
            check(f"BSDF {sock} linked", bsdf.inputs[sock].is_linked)
        base = src_node(bsdf.inputs["Base Color"])
        check("Base Color <- image node, sRGB", base is not None
              and base.bl_idname == "ShaderNodeTexImage" and image_cs(base) == "sRGB",
              base and image_cs(base))
        check("Metallic Non-Color", image_cs(src_node(bsdf.inputs["Metallic"])) == "Non-Color")
        check("Roughness Non-Color", image_cs(src_node(bsdf.inputs["Roughness"])) == "Non-Color")
        nm = src_node(bsdf.inputs["Normal"])
        check("Normal <- Normal Map node", nm is not None and nm.bl_idname == "ShaderNodeNormalMap", nm)
        check("normal image Non-Color", nm is not None
              and image_cs(src_node(nm.inputs["Color"])) == "Non-Color")
        for role, fname in (("base_color", expected[0]), ("metallic", expected[1]),
                            ("roughness", expected[3])):
            node = _tex_image_node(mat, role)
            check(f"{role} image points at {fname}", node is not None and node.image is not None
                  and os.path.normcase(os.path.abspath(bpy.path.abspath(node.image.filepath)))
                  == os.path.normcase(os.path.join(tex, fname)),
                  node and node.image and node.image.filepath)
        imgs = [n.image for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeTexImage"]
        check("images are watched", len(imgs) == 4 and all(i.get("texture_plugger_watch") for i in imgs),
              [i.get("texture_plugger_watch") for i in imgs])
        check("placeholder is 1024x1024", tuple(imgs[0].size) == (1024, 1024), tuple(imgs[0].size))
        check("panel state shows the set", s.set_name == "PaintCrate" and len(s.slots) == 4
              and len(s.sets) == 1, (s.set_name, len(s.slots), len(s.sets)))

        # --- re-run never overwrites
        rough = os.path.join(tex, "PaintCrate_Roughness.png")
        _write_png(rough, 16, (10, 20, 30))
        with open(rough, "rb") as fh:
            before = fh.read()
        n_mats = len(bpy.data.materials)
        res = bpy.ops.texture_plugger.create_set(directory=root)
        check("second create_set finished", res == {"FINISHED"}, res)
        with open(rough, "rb") as fh:
            check("modified file not overwritten", fh.read() == before)
        check("re-run keeps the material", obj.active_material == mat and mat.name == "PaintCrate"
              and len(bpy.data.materials) == n_mats, mat.name)
        check("re-run keeps four BSDF links", all(bsdf.inputs[k].is_linked for k in
              ("Base Color", "Metallic", "Roughness", "Normal")))

        # --- live reload
        rimg = _tex_image_node(mat, "roughness").image
        bimg = _tex_image_node(mat, "base_color").image
        for _ in range(3):
            wf.check_reload()
        check("kept modified roughness was reloaded", tuple(rimg.size) == (16, 16), tuple(rimg.size))
        check("untouched image has original size", tuple(bimg.size) == (1024, 1024), tuple(bimg.size))
        check("check_reload is quiet when nothing changed", wf.check_reload() == [])
        base_path = os.path.join(tex, "PaintCrate_BaseColor.png")
        _write_png(base_path, 8, (255, 0, 0))
        calls = [wf.check_reload() for _ in range(3)]
        check("reload happens on the second call after a change",
              calls[0] == [] and bimg.name in calls[1] and calls[2] == [], calls)
        check("reloaded pixels", tuple(bimg.size) == (8, 8), tuple(bimg.size))

        # dirty images are never reloaded
        bimg.pixels[0] = 0.5
        check("image is dirty", bimg.is_dirty)
        _write_png(base_path, 4, (0, 255, 0))
        calls = [wf.check_reload() for _ in range(3)] + [wf.check_reload(force=True)]
        check("dirty image not reloaded", all(bimg.name not in c for c in calls)
              and tuple(bimg.size) == (8, 8), (calls, tuple(bimg.size)))
        bimg.reload()      # drop the unsaved paint so later checks start clean
        check("reload() cleared dirty state", not bimg.is_dirty)

        # reload_images operator forces a reload
        _write_png(rough, 32, (1, 2, 3))
        res = bpy.ops.texture_plugger.reload_images()
        check("reload_images finished", res == {"FINISHED"}, res)
        check("reload_images reloaded at once", tuple(rimg.size) == (32, 32), tuple(rimg.size))

        # --- name collision -> _2
        obj2 = new_cube("PaintCrate2", (6, 44, 0))
        select_only(obj2)
        res = bpy.ops.texture_plugger.create_set(directory=root, name="PaintCrate")
        check("collision create_set finished", res == {"FINISHED"}, res)
        check("collision renamed to PaintCrate_2", obj2.active_material is not None
              and obj2.active_material.name == "PaintCrate_2",
              obj2.active_material and obj2.active_material.name)
        check("PaintCrate_2 files", os.path.isfile(os.path.join(
            root, "PaintCrate_2", "textures", "PaintCrate_2_BaseColor.png")))
        check("first material untouched by collision", obj.active_material == mat
              and mat.name == "PaintCrate")

        # --- edit_in_painter (launch replaced, never starts Painter)
        fake = os.path.join(TMP, "Fake Painter.exe")
        with open(fake, "wb"):
            pass
        prefs.painter_path = fake
        launched = []
        wf._launch = lambda cmd, cwd=None: launched.append((cmd, cwd))
        select_only(obj)
        res = bpy.ops.texture_plugger.edit_in_painter()
        check("edit_in_painter (mesh form)", res == {"FINISHED"} and launched and launched[-1] == (
            [fake, "--mesh", fbx, "--export-path", tex], os.path.dirname(fake)), launched)
        spp = os.path.join(folder, "PaintCrate.spp")
        with open(spp, "wb"):
            pass
        res = bpy.ops.texture_plugger.edit_in_painter()
        check("edit_in_painter (.spp form)", res == {"FINISHED"} and launched[-1] == (
            [fake, "--export-path", tex, spp], os.path.dirname(fake)), launched[-1])
        n_launched = len(launched)
        plain = new_cube("PaintPlain", (9, 44, 0))
        select_only(plain)
        res = run_op(bpy.ops.texture_plugger.edit_in_painter)
        check("edit_in_painter without a set -> CANCELLED, nothing launched",
              res[0] == "CANCELLED" and len(launched) == n_launched, (res, len(launched)))
    finally:
        wf._launch = orig_launch
        prefs.painter_path = saved_painter
        for k, v in saved.items():
            setattr(s, k, v)
        for k, v in saved_toggles.items():
            setattr(s, k, v)


class _FakeOp:
    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)


class FakeLayout:
    """Records layout calls and validates property / operator names."""
    errors = []

    def __init__(self):
        self.use_property_split = False
        self.use_property_decorate = False
        self.scale_y = 1.0
        self.active = True
        self.enabled = True

    def _child(self, *a, **k):
        return FakeLayout()

    column = row = box = grid_flow = split = _child

    def label(self, **k):
        pass

    def separator(self, **k):
        pass

    def prop(self, data, name, **k):
        if name not in data.bl_rna.properties:
            FakeLayout.errors.append(f"missing property {type(data).__name__}.{name}")

    def operator(self, idname, **k):
        mod, _, op = idname.partition(".")
        if not hasattr(getattr(bpy.ops, mod), op):
            FakeLayout.errors.append(f"missing operator {idname}")
        return _FakeOp()

    def template_list(self, list_type, list_id, data, prop, active_data, active_prop, **k):
        if not hasattr(bpy.types, list_type):
            FakeLayout.errors.append(f"missing UIList {list_type}")
        for d, p in ((data, prop), (active_data, active_prop)):
            if p not in d.bl_rna.properties:
                FakeLayout.errors.append(f"missing property {p}")


def test_ui_draw(module):
    print("\n[UI draw smoke test with stub layout]")
    import importlib
    pkg = importlib.import_module(module)
    ui, prefs_mod = pkg.ui, pkg.prefs

    class P:
        pass

    FakeLayout.errors = []
    obj = new_cube("UiCube", (0, 30, 0))
    select_only(obj)
    load(make_set("ui", ["Ui_Color.png", "Ui_Roughness.png", "Other_Color.png",
                         "Other_Roughness.png", "Other_Normal.png"]), apply=False)
    s = settings()
    for projection, height in (("UV", "DISPLACEMENT"), ("BOX", "BUMP")):
        s.projection, s.height_mode = projection, height
        p = P()
        p.layout = FakeLayout()
        try:
            ui._draw_main(p, bpy.context)
            ui._draw_options(p, bpy.context)
        except Exception as ex:
            FakeLayout.errors.append(f"draw raised {ex!r}")
    s.projection, s.height_mode = "UV", "DISPLACEMENT"
    # Create & Paint panel: without a texture set, then with one on the material
    paint_mat = bpy.data.materials.new("UiPaintMat")
    for with_set in (False, True):
        if with_set:
            paint_mat[pkg.workflow.PROJECT_KEY] = TMP
            obj.data.materials.append(paint_mat)
        p = P()
        p.layout = FakeLayout()
        try:
            ui._draw_paint(p, bpy.context)
        except Exception as ex:
            FakeLayout.errors.append(f"_draw_paint raised {ex!r}")
    obj.data.materials.clear()
    bpy.data.materials.remove(paint_mat)
    # UIList row
    try:
        ui.TEXTUREPLUGGER_UL_slots.draw_item(None, bpy.context, FakeLayout(), s, s.slots[0],
                                             0, s, "slot_index", 0)
    except Exception as ex:
        FakeLayout.errors.append(f"UIList draw raised {ex!r}")
    # Preferences
    prefs = bpy.context.preferences.addons[module].preferences
    prefs.show_keywords = True
    try:
        # Call draw with a proxy whose layout is fake but properties come from prefs.
        class Proxy:
            def __init__(self, real):
                object.__setattr__(self, "_real", real)
                object.__setattr__(self, "layout", FakeLayout())

            def __getattr__(self, k):
                return getattr(self._real, k)

            @property
            def bl_rna(self):
                return self._real.bl_rna
        proxy = Proxy(prefs)
        prefs_mod.TexturePluggerPreferences.draw(proxy, bpy.context)
    except Exception as ex:
        FakeLayout.errors.append(f"prefs draw raised {ex!r}")
    prefs.show_keywords = False
    check("panels/list/prefs draw without errors", not FakeLayout.errors, FakeLayout.errors)


def test_undo_cannot_break_link(module):
    print("\n[undo cannot break a texture set link; Unlink does]")
    import importlib
    wf = importlib.import_module(module).workflow
    check("operator unlink_set", hasattr(bpy.ops.texture_plugger, "unlink_set"))
    check("undo/redo/load handlers registered",
          wf._on_undo_redo in bpy.app.handlers.undo_post
          and wf._on_undo_redo in bpy.app.handlers.redo_post
          and wf._on_load in bpy.app.handlers.load_post)
    root = os.path.join(TMP, "undo")
    os.makedirs(root, exist_ok=True)
    obj = new_cube("UndoPot", (0, 50, 0))
    select_only(obj)
    res = bpy.ops.texture_plugger.create_set(directory=root, name="UndoPot")
    mat = obj.active_material
    folder = os.path.join(root, "UndoPot")
    check("set created", res == {"FINISHED"} and wf.material_project(mat) == folder)
    check("nothing to repair while linked", wf.heal() == [])

    def simulate_undo():
        # What Ctrl+Z after Create leaves behind: no material, no panel state.
        m = obj.active_material
        obj.data.materials.clear()
        if m is not None and m.users == 0:
            bpy.data.materials.remove(m)
        s = settings()
        s.slots.clear()
        s.files.clear()
        s.sets.clear()
        s.set_name = ""

    base = os.path.join(folder, "textures", "UndoPot_BaseColor.png")
    with open(base, "rb") as fh:
        before = fh.read()
    simulate_undo()
    check("repair relinks the set", wf.heal() == ["UndoPot"])
    mat = obj.active_material
    check("material back with the same name", mat is not None and mat.name == "UndoPot",
          mat.name if mat else None)
    check("link restored", wf.material_project(mat) == folder)
    check("mesh link restored", os.path.isfile(str(mat.get(wf.MESH_KEY) or "")))
    check("textures plugged in again", principled(mat.node_tree).inputs["Base Color"].is_linked
          and principled(mat.node_tree).inputs["Roughness"].is_linked)
    check("panel state restored", settings().set_name == "UndoPot" and len(settings().slots) == 4)
    with open(base, "rb") as fh:
        check("repair does not rewrite files", fh.read() == before)

    # Swapping the material by hand is not an undo: the old one keeps the link.
    own = bpy.data.materials.new("UndoOwn")
    obj.active_material = own
    check("manual material swap is left alone", wf.heal() == [] and obj.active_material == own)
    obj.active_material = mat

    # load_post rebuilds the registry from the file's materials
    wf._on_load()
    check("registry rebuilt from materials", wf._folder_key(folder) in wf._registry)

    # Unlink is the only way out
    res = bpy.ops.texture_plugger.unlink_set()
    check("unlink finished", res == {"FINISHED"} and not wf.material_project(mat)
          and mat.get(wf.MESH_KEY) is None)
    check("unlink keeps the nodes", principled(mat.node_tree).inputs["Base Color"].is_linked)
    check("unlink keeps the files", os.path.isfile(base))
    simulate_undo()
    check("no repair after unlink", wf.heal() == [] and obj.active_material is None)
    res = run_op(bpy.ops.texture_plugger.unlink_set)
    check("unlink without a link -> CANCELLED", res[0] == "CANCELLED", res)

    # Creating again under another name moves the link; the old one must not come back.
    obj2 = new_cube("Relink", (3, 50, 0))
    select_only(obj2)
    bpy.ops.texture_plugger.create_set(directory=root, name="RelinkA")
    bpy.ops.texture_plugger.create_set(directory=root, name="RelinkB")
    mat2 = obj2.active_material
    check("second create moves the link", mat2.name == "RelinkB"
          and wf.material_project(mat2) == os.path.join(root, "RelinkB"))
    check("old link is not restored", wf.heal() == [] and wf.heal() == []
          and obj2.active_material.name == "RelinkB", obj2.active_material.name)

    # Linked material swapped out and purged: the timer forgets the link.
    swapped = bpy.data.materials.new("RelinkSwapped")
    obj2.active_material = swapped
    bpy.data.materials.remove(mat2)
    wf.prune_registry()
    check("purged link is forgotten", wf.heal() == [] and obj2.active_material == swapped
          and swapped.name == "RelinkSwapped" and not wf.material_project(swapped))


def test_brushstroke_objects_target_surface(module):
    print("\n[Brushstroke Tools objects: Create & Paint works on the surface mesh]")
    import importlib
    wf = importlib.import_module(module).workflow
    root = os.path.join(TMP, "brush")
    os.makedirs(root, exist_ok=True)
    surf = new_cube("BsSurface", (0, 56, 0))
    # The custom properties Brushstroke Tools puts on its objects.
    strokes = bpy.data.objects.new("BsSurface - Brushstrokes", bpy.data.curves.new("bs", "CURVE"))
    flow = bpy.data.objects.new("BsSurface - Brushstrokes-FLOW", bpy.data.curves.new("fl", "CURVE"))
    for o in (strokes, flow):
        bpy.context.scene.collection.objects.link(o)
        o.parent = surf
    strokes["BSBST_surface_object"] = surf
    strokes["BSBST_flow_object"] = flow
    strokes["BSBST_active"] = True
    select_only(strokes)
    check("brushstrokes object -> surface", wf._active_object(bpy.context) == surf)
    select_only(flow)
    check("flow object -> surface", wf._active_object(bpy.context) == surf)
    select_only(surf)
    check("plain mesh -> itself", wf._active_object(bpy.context) == surf)
    select_only(flow)
    res = bpy.ops.texture_plugger.create_set(directory=root, name="")
    mat = surf.active_material
    check("create with flow object active lands on the surface",
          res == {"FINISHED"} and mat is not None and mat.name == "BsSurface"
          and wf.material_project(mat) == os.path.join(root, "BsSurface"), os.listdir(root))
    check("brushstroke objects get no material and no folder",
          len(flow.material_slots) == 0 and len(strokes.material_slots) == 0
          and os.listdir(root) == ["BsSurface"], os.listdir(root))
    check("mesh exported, flow object still active",
          os.path.isfile(str(mat.get(wf.MESH_KEY) or ""))
          and bpy.context.view_layer.objects.active == flow)


def test_unlit_from_painter(module):
    """Painter's Bake Lighting marker switches a set to emission only, and back."""
    print("\n[unlit: lighting baked in Painter]")
    import importlib
    import json
    pkg = importlib.import_module(module)
    nb, un = pkg.node_builder, pkg.unlit
    folder = os.path.join(TMP, "unlit", "Crate")
    tex = os.path.join(folder, "textures")
    make_image(tex, "Crate_BaseColor.png", (0.8, 0.2, 0.1, 1.0))
    make_image(tex, "Crate_Opacity.png", (1.0, 1.0, 1.0, 1.0))
    ob = new_cube("UnlitCube", (6, 0, 0))
    mat = bpy.data.materials.new("Crate")
    ob.data.materials.append(mat)
    maps = {"base_color": nb.MapInput(os.path.join(tex, "Crate_BaseColor.png")),
            "opacity": nb.MapInput(os.path.join(tex, "Crate_Opacity.png"))}
    nb.build_material(mat, maps, nb.BuildOptions(), "Crate")
    mat["texture_plugger_project"] = folder
    vs = bpy.context.scene.view_settings
    vs.view_transform, vs.exposure = "AgX", 1.5
    marker = os.path.join(tex, un.LIT_MARKER)

    def write(lit):
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "sets": {"Crate": {"lit": lit, "base_color": "crate_basecolor"}}}, fh)
        un._markers.clear()        # same-second rewrites keep the same mtime on some disks

    un.sync()
    check("no marker: stays lit", not nb.is_unlit(mat))
    key_before = pkg.scene_link.lighting_key(bpy.context)
    write(True)
    un.sync()
    check("Standard switch doesn't change the lighting sent to Painter",
          pkg.scene_link.lighting_key(bpy.context) == key_before
          and un.user_view(bpy.context.scene)["exposure"] == 1.5
          and un.user_view(bpy.context.scene)["view_transform"] == "AgX")
    out = nb.find_output(mat.node_tree)
    surf = out.inputs["Surface"]
    em = mat.node_tree.nodes.get("TP Unlit (lighting baked in Painter)")
    check("marked lit: emission drives the output", nb.is_unlit(mat) and em is not None)
    check("emission reads the Base Color image",
          em is not None and em.inputs["Color"].is_linked
          and em.inputs["Color"].links[0].from_node == nb.base_color_node(mat.node_tree))
    check("alpha kept through a mix with Transparent",
          surf.is_linked and surf.links[0].from_node.bl_idname == "ShaderNodeMixShader")
    check("view transform Standard, exposure 0",
          vs.view_transform == "Standard" and abs(vs.exposure) < 1e-6, (vs.view_transform, vs.exposure))
    check("unlit status for the panel", "1 material" in un.last_status, un.last_status)
    check("second sync changes nothing", un.sync() == [])

    nb.build_material(mat, maps, nb.BuildOptions(), "Crate")      # e.g. Apply again
    bsdf = nb.find_principled(mat.node_tree, nb.find_output(mat.node_tree))
    check("rebuild puts the Principled back first",
          not nb.is_unlit(mat) and surf.links[0].from_node == bsdf)
    un.sync()
    check("sync makes it unlit again after a rebuild", nb.is_unlit(mat))

    write(False)
    un.sync()
    check("exported without lighting: Principled back on the output",
          not nb.is_unlit(mat) and surf.is_linked and surf.links[0].from_node == bsdf
          and not any(str(n.get("texture_plugger", "")).startswith("unlit") for n in mat.node_tree.nodes))
    check("view transform restored", vs.view_transform == "AgX" and abs(vs.exposure - 1.5) < 1e-6,
          (vs.view_transform, vs.exposure))

    write(True)
    un.sync()
    nb.clear_material(mat)
    check("clear while unlit leaves a valid surface",
          surf.is_linked and surf.links[0].from_node.bl_idname == "ShaderNodeBsdfPrincipled")
    un.sync()
    check("view restored once nothing is unlit", vs.view_transform == "AgX", vs.view_transform)
    bpy.data.objects.remove(ob)
    bpy.data.materials.remove(mat)


def test_uv_light_bake(module):
    """Bake Lighting to UVs: the lightmap has the cast shadow and matches a Cycles render."""
    print("\n[Bake Lighting to UVs (Cycles lightmap)]")
    import importlib
    import json
    import math
    import numpy as np
    pkg = importlib.import_module(module)
    nb, lb, pl = pkg.node_builder, pkg.light_bake, pkg.painter_link
    sc = bpy.context.scene
    for o in list(sc.objects):
        o.hide_render = True          # earlier tests' objects stay out of the light
    sc.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in {
        i.identifier for i in sc.render.bl_rna.properties["engine"].enum_items} else sc.render.engine
    engine_before = sc.render.engine
    sc.view_settings.view_transform, sc.view_settings.exposure = "Standard", 0.0
    sc.world = None
    folder = os.path.join(TMP, "uvbake", "Ground")
    tex = os.path.join(folder, "textures")
    make_image(tex, "Ground_BaseColor.png", (0.8, 0.8, 0.8, 1.0))
    bpy.ops.mesh.primitive_plane_add(size=2.0, location=(0, 0, 0))
    plane = bpy.context.active_object
    plane.name = "BakeGround"
    mat = bpy.data.materials.new("Ground")
    plane.data.materials.append(mat)
    nb.build_material(mat, {"base_color": nb.MapInput(os.path.join(tex, "Ground_BaseColor.png"))},
                      nb.BuildOptions(), "Ground")
    mat["texture_plugger_project"] = folder
    bsdf = nb.find_principled(mat.node_tree, nb.find_output(mat.node_tree))
    nb.set_input(bsdf, ("Specular IOR Level", "Specular"), 0.0)
    nb.set_input(bsdf, ("Roughness",), 1.0)
    links_before = len(mat.node_tree.links)
    nodes_before = len(mat.node_tree.nodes)
    blocker = new_cube("BakeBlocker", (0.45, 0.45, 0.5))
    blocker.scale = (0.5, 0.5, 0.5)
    sun_d = bpy.data.lights.new("BakeSun", "SUN")
    sun_d.energy = 2.0
    sun_d.angle = 0.0
    sun = bpy.data.objects.new("BakeSun", sun_d)
    sun.rotation_euler = (math.radians(20), 0.0, 0.0)
    sc.collection.objects.link(sun)
    other = new_cube("BakeOther", (5, 5, 5))      # selected before: must stay selected
    select_only(other)
    link = os.path.join(TMP, "uvbake", "link")
    os.makedirs(link, exist_ok=True)

    entry = lb.bake_set(bpy.context, plane, mat, link, size=64, samples=64, preview=False)
    path = os.path.join(link, entry["render"])
    check("lightmap written", os.path.isfile(path), entry)
    check("scene put back: engine, selection, active",
          sc.render.engine == engine_before and other.select_get() and not plane.select_get()
          and bpy.context.view_layer.objects.active == other, sc.render.engine)
    out = nb.find_output(mat.node_tree)
    check("material put back exactly",
          len(mat.node_tree.links) == links_before and len(mat.node_tree.nodes) == nodes_before
          and out.inputs["Surface"].links[0].from_node == bsdf)

    img = bpy.data.images.load(path, check_existing=False)
    lm = np.empty(64 * 64 * 4, dtype=np.float32)
    img.pixels.foreach_get(lm)
    bpy.data.images.remove(img)
    lm = lm.reshape(64, 64, 4)[..., :3].mean(axis=-1)
    inner = np.zeros((64, 64), bool)
    inner[4:-4, 4:-4] = True
    top = float(np.percentile(lm[inner], 95))
    lit = inner & (lm > 0.8 * top)
    shadow = inner & (lm < 0.3 * top)
    check("cast shadow is in the lightmap", lit.sum() > 200 and shadow.sum() > 40,
          (int(lit.sum()), int(shadow.sum()), top))

    # Cycles render from straight above: render = albedo x lightmap everywhere
    cd = bpy.data.cameras.new("BakeCam")
    cd.type = "ORTHO"
    cd.ortho_scale = 2.0
    cam = bpy.data.objects.new("BakeCam", cd)
    cam.location = (0, 0, 5)
    sc.collection.objects.link(cam)
    blocker.visible_camera = False  # the camera sees the ground; the cube still casts its shadow
    sc.camera = cam
    sc.render.engine = "CYCLES"
    sc.cycles.samples = 64
    sc.render.resolution_x = sc.render.resolution_y = 64
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "OPEN_EXR"
    sc.render.filepath = os.path.join(TMP, "uvbake", "ref.exr")
    bpy.ops.render.render(write_still=True)
    ref_img = bpy.data.images.load(sc.render.filepath, check_existing=False)
    ref = np.empty(64 * 64 * 4, dtype=np.float32)
    ref_img.pixels.foreach_get(ref)
    bpy.data.images.remove(ref_img)
    ref = ref.reshape(64, 64, 4)[..., :3].mean(axis=-1)
    r_lit = float(ref[lit].mean() / lm[lit].mean())
    r_shadow = float(ref[shadow].mean() / max(lm[shadow].mean(), 1e-6))
    print(f"    render / lightmap: lit {r_lit:.3f}, shadow {r_shadow:.3f} (albedo 0.604)")
    check("lightmap matches the Cycles render in light and shadow (same albedo ratio)",
          abs(r_shadow / r_lit - 1.0) < 0.2 and 0.3 < r_lit < 0.9, (round(r_lit, 3), round(r_shadow, 3)))

    # A set exported with baked lighting: its Base Color is albedo x light, never used
    # as albedo (a black patch would divide to nothing)
    make_image(tex, "Ground_BaseColor.png", (0.0, 0.0, 0.0, 1.0))
    nb.base_color_node(mat.node_tree).image.reload()
    with open(os.path.join(tex, "texture_plugger_lit.json"), "w", encoding="utf-8") as fh:
        json.dump({"sets": {"Ground": {"lit": True, "base_color": "ground_basecolor"}}}, fh)
    pkg.unlit._markers.clear()
    sc.render.engine = engine_before
    e2 = lb.bake_set(bpy.context, plane, mat, link, size=64, samples=32, preview=False)
    img = bpy.data.images.load(os.path.join(link, e2["render"]), check_existing=False)
    lm2 = np.empty(64 * 64 * 4, dtype=np.float32)
    img.pixels.foreach_get(lm2)
    bpy.data.images.remove(img)
    lm2 = lm2.reshape(64, 64, 4)[..., :3].mean(axis=-1)
    check("lit-exported set: its Base Color isn't taken as albedo",
          float(lm2[lit].mean()) > 0.8 * float(lm[lit].mean()), (float(lm2[lit].mean()), float(lm[lit].mean())))

    pl.write_lightmaps(link, {"Ground": entry})
    with open(os.path.join(link, pl.LIGHTMAPS_FILE), encoding="utf-8") as fh:
        data = json.load(fh)
    check("lightmaps file lists the set", data["sets"]["Ground"]["render"] == entry["render"])

    for o in (plane, blocker, other, sun, cam):
        bpy.data.objects.remove(o)
    bpy.data.materials.remove(mat)
    sc.render.engine = engine_before


def test_disable_enable(module):
    print("\n[disable / re-enable]")
    try:
        bpy.ops.preferences.addon_disable(module=module)
        check("disabled cleanly", not hasattr(bpy.types, "TEXTUREPLUGGER_PT_view3d"))
        bpy.ops.preferences.addon_enable(module=module)
        check("re-enabled", hasattr(bpy.types, "TEXTUREPLUGGER_PT_view3d"))
    except Exception as ex:
        check("disable/enable without exception", False, ex)


def main():
    print(f"Blender {bpy.app.version_string}  mode={'legacy' if '--legacy' in ARGS else 'extension'}")
    try:
        module = install()
        test_registration(module)
        test_create_unsaved_relative()     # needs the session .blend to be unsaved
        test_basic_set()
        test_packed_dx_gloss()
        test_udim()
        test_multi_object()
        test_box_projection_and_options()
        test_multiple_sets()
        test_manual_slots_and_robustness(module)
        test_ui_draw(module)
        test_create_and_paint(module)
        test_undo_cannot_break_link(module)
        test_brushstroke_objects_target_surface(module)
        test_unlit_from_painter(module)
        test_uv_light_bake(module)
        test_disable_enable(module)
    except Exception:
        traceback.print_exc()
        check("test run completed without exception", False, "see traceback")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for r in RESULTS if r[1])
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed (Blender {bpy.app.version_string})")
    for name, _, detail in failed:
        print(f"  FAILED: {name}: {detail}")
    sys.stdout.flush()
    if failed:
        sys.exit(1)


main()
