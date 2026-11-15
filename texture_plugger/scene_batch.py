# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-scene texturing: one unique material per object, one Painter project.

* ``texture_plugger.create_scene_sets`` gives every selected (or every visible)
  mesh its own material, however it is parented or collected, creates the
  placeholder textures for all of them in ONE shared ``textures`` folder and
  exports every mesh into ONE FBX.  Painter has a single export path, so a
  shared folder is what keeps live reload working for every object.
* ``texture_plugger.edit_scene_in_painter`` opens that FBX (or the saved
  ``.spp``) in Painter, together with the lighting and the per-object bounds,
  so the Painter plugin can solo / frame one object at a time.

Safety rules baked in: materials are never shared between objects (even when
the meshes share data), names are unique ignoring case (Windows file names),
nothing is overwritten, and a failed export leaves the user's scene as it was.
"""

import os
import time

import bpy
from bpy.props import EnumProperty

from . import node_builder, operators, prefs, scaffold, workflow
from .operators import _safe

SCENE_KEY = "texture_plugger_scene"
_SCOPES = (
    ("SELECTED", "Selected", "Every selected mesh (including ones inside groups and collections)"),
    ("VISIBLE", "All Visible", "Every visible mesh in the scene"),
)
_last_launch = 0.0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def scene_folder_name(blend_path):
    stem = os.path.splitext(os.path.basename(blend_path or ""))[0]
    return scaffold.sanitize_name(stem, "Scene") + "_scene"


def unique_names(wanted, taken):
    """Names from ``wanted`` that are unique among themselves and ``taken`` (case-insensitive)."""
    used = {t.lower() for t in taken}
    out = []
    for w in wanted:
        base = scaffold.sanitize_name(w, "Object")
        name, i = base, 2
        while name.lower() in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name.lower())
        out.append(name)
    return out


def _paintable(ob, view_layer):
    if ob.type != "MESH" or ob.data is None:
        return False
    if ob.library is not None and ob.override_library is None:
        return False
    try:
        return bool(ob.visible_get(view_layer=view_layer))
    except (RuntimeError, TypeError):
        return False


def collect_targets(context, scope):
    """Meshes to texture: selected or all visible; no duplicates, stable order."""
    vl = context.view_layer
    pool = context.selected_objects if scope == "SELECTED" else context.scene.objects
    seen, out = set(), []
    for ob in sorted(pool, key=lambda o: o.name):
        if ob.name_full in seen or not _paintable(ob, vl):
            continue
        seen.add(ob.name_full)
        out.append(ob)
    return out


def material_scene(mat):
    try:
        v = mat.get(SCENE_KEY) if mat is not None else None
    except Exception:
        return ""
    return str(v) if v else ""


def objects_in_scene(folder):
    key = workflow._folder_key(folder)
    out = []
    for ob in bpy.data.objects:
        if ob.type != "MESH" or ob.data is None:
            continue
        if any(workflow._folder_key(material_scene(s.material)) == key for s in ob.material_slots):
            out.append(ob)
    return sorted(out, key=lambda o: o.name)


def export_meshes(context, objs, path):
    """Export exactly ``objs`` into one FBX (OBJ fallback). Returns (path|None, message|None)."""
    vl = context.view_layer
    prev_active = vl.objects.active
    prev_selected = [o for o in vl.objects if o.select_get()]
    try:
        try:
            if prev_active is not None and prev_active.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for o in prev_selected:
            o.select_set(False)
        chosen = []
        for o in objs:
            o.select_set(True)
            if o.select_get():
                chosen.append(o)
        if not chosen:
            return None, "None of the objects can be selected for export"
        vl.objects.active = chosen[0]
        err = None
        try:
            res = bpy.ops.export_scene.fbx(filepath=path, use_selection=True,
                                           object_types={"MESH"}, add_leaf_bones=False,
                                           bake_anim=False)
            if "FINISHED" in res and os.path.isfile(path):
                return path, None
            err = "FBX export did not finish"
        except (AttributeError, RuntimeError, TypeError) as ex:
            err = str(ex).strip() or type(ex).__name__
        obj_path = os.path.splitext(path)[0] + ".obj"
        try:
            res = bpy.ops.wm.obj_export(filepath=obj_path, export_selected_objects=True,
                                        export_materials=True)
            if "FINISHED" in res and os.path.isfile(obj_path):
                return obj_path, None
        except (AttributeError, RuntimeError, TypeError) as ex:
            err += f"; {str(ex).strip()}"
        return None, f"Mesh export failed ({err})"
    finally:
        for o in vl.objects:
            try:
                o.select_set(o in prev_selected)
            except RuntimeError:
                pass
        try:
            vl.objects.active = prev_active
        except Exception:
            pass


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------

def _slot_wants_new_material(slot, folder):
    """True unless the slot already holds this scene's own single-user material."""
    mat = slot.material
    if mat is None:
        return True
    return not (workflow._folder_key(material_scene(mat)) == workflow._folder_key(folder)
                and mat.users <= 1 and node_builder.material_is_editable(mat))


def create_scene_sets(context, settings, objs, root, map_ids, size):
    """Returns (info dict, None) or (None, error)."""
    folder = os.path.join(root, scene_folder_name(bpy.data.filepath))
    tex_dir = os.path.join(folder, "textures")
    mesh_path = os.path.join(folder, "mesh", scene_folder_name(bpy.data.filepath) + ".fbx")
    try:
        os.makedirs(tex_dir, exist_ok=True)
        os.makedirs(os.path.dirname(mesh_path), exist_ok=True)
    except OSError as ex:
        return None, f"Cannot create the folder {folder}: {ex}"

    taken = [m.name for m in bpy.data.materials]
    reuse_names = {m.name.lower() for m in bpy.data.materials
                   if workflow._folder_key(material_scene(m)) == workflow._folder_key(folder)
                   and m.users <= 1}
    taken = [t for t in taken if t.lower() not in reuse_names]
    plan, wanted = [], []        # (obj, slot index) needing a fresh material
    skipped, messages = [], []
    for ob in objs:
        if ob.library is not None and ob.override_library is None:
            skipped.append(ob.name)
            continue
        if len(ob.material_slots) == 0:
            ob.data.materials.append(None)
        shared_data = ob.data.users > 1
        for i, slot in enumerate(ob.material_slots):
            if not _slot_wants_new_material(slot, folder):
                continue
            if shared_data and slot.link == "DATA":
                slot.link = "OBJECT"          # keep linked duplicates independent
            n = len(ob.material_slots)
            plan.append((ob, i))
            wanted.append(ob.name if n == 1 else f"{ob.name}_{i + 1}")
    names = unique_names(wanted, taken)

    created_mats = []
    all_ids = tuple(map_ids)
    try:
        for (ob, i), name in zip(plan, names):
            slot = ob.material_slots[i]
            replaced = slot.material
            mat = bpy.data.materials.new(name)
            node_builder.ensure_node_tree(mat)
            if mat.name != name:
                mat.name = name
            slot.material = mat
            if replaced is not None and replaced.users > 0 and replaced.name != name:
                messages.append(f"'{ob.name}': replaced material '{replaced.name}'")
            created_mats.append((ob, mat))
    except Exception:
        for _, mat in created_mats:
            try:
                bpy.data.materials.remove(mat)
            except Exception:
                pass
        raise

    opts = operators.build_options(settings)
    built = 0
    for ob in objs:
        for slot in ob.material_slots:
            mat = slot.material
            if mat is None or workflow._folder_key(material_scene(mat)) not in (
                    "", workflow._folder_key(folder)):
                continue
            set_name = mat.name
            try:
                results = scaffold.create_files(root, set_name, all_ids, size, textures_dir=tex_dir)
            except OSError as ex:
                return None, f"Could not write textures for '{set_name}': {ex}"
            operators.store_known_set(settings, set_name,
                                      [(spec.map_type, p) for spec, p, _ in results])
            maps, msgs = operators.collect_maps(settings)
            res = node_builder.build_material(mat, maps, opts, settings.set_name, settings.set_key)
            for w in list(msgs) + list(res.warnings):
                if w not in messages:
                    messages.append(w)
            workflow.remember(node_builder.managed_images(mat.node_tree))
            mat[workflow.PROJECT_KEY] = folder
            mat[SCENE_KEY] = folder
            built += 1

    for ob in objs:
        if len(ob.data.uv_layers) == 0:
            messages.append(f"'{ob.name}' has no UV map; Painter can auto-unwrap it on import")

    exported, err = export_meshes(context, [o for o in objs if o.name not in skipped], mesh_path)
    if exported:
        for ob in objs:
            for slot in ob.material_slots:
                if slot.material is not None and material_scene(slot.material):
                    slot.material[workflow.MESH_KEY] = exported
    else:
        messages.append(err)
    if skipped:
        messages.append("Skipped linked objects: " + ", ".join(skipped))
    return {"folder": folder, "textures": tex_dir, "built": built, "objects": len(objs),
            "mesh": exported, "messages": messages}, None


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class TEXTUREPLUGGER_OT_create_scene_sets(bpy.types.Operator):
    """Give every mesh its own material and texture set (placeholders in one shared folder) and export them all as one Painter scene"""
    bl_idname = "texture_plugger.create_scene_sets"
    bl_label = "Create Sets for Scene"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(name="Objects", items=_SCOPES, default="SELECTED", options={"SKIP_SAVE"})

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = operators.get_settings(context)
        operators.ensure_initialized(context, settings)
        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                self.report({"WARNING"}, "Switch to Object Mode first")
                return {"CANCELLED"}
        raw_root = (settings.create_root or "").strip()
        if not raw_root or (raw_root.startswith("//") and not bpy.data.filepath):
            self.report({"ERROR"}, "Choose a folder for the texture sets first "
                                   "(a relative '//' path needs a saved .blend file)")
            return {"CANCELLED"}
        root = os.path.abspath(bpy.path.abspath(raw_root))
        map_ids = [s.id for s in scaffold.MAPS if getattr(settings, "create_" + s.id, False)]
        if not map_ids:
            self.report({"WARNING"}, "Enable at least one map to create")
            return {"CANCELLED"}
        objs = collect_targets(context, self.scope)
        if not objs:
            self.report({"WARNING"}, "No paintable meshes found: select the objects first "
                                     "(hidden and linked objects are skipped)")
            return {"CANCELLED"}
        info, err = create_scene_sets(context, settings, objs, root, map_ids,
                                      int(settings.create_resolution))
        if info is None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        operators._report_all(self, info["messages"], "WARNING")
        self.report({"INFO"}, f"{info['built']} texture set(s) for {info['objects']} object(s) "
                              f"in {info['folder']}")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_edit_scene_in_painter(bpy.types.Operator):
    """Open the whole scene (every object that has a scene texture set) in Substance 3D Painter"""
    bl_idname = "texture_plugger.edit_scene_in_painter"
    bl_label = "Edit Scene in Painter"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        global _last_launch
        folder = ""
        for ob in [workflow._active_object(context)] + list(context.selected_objects):
            if ob is None or not node_builder.object_supports_material(ob):
                continue
            for slot in ob.material_slots:
                folder = material_scene(slot.material)
                if folder:
                    break
            if folder:
                break
        if not folder or not os.path.isdir(folder):
            self.report({"WARNING"}, "Create sets for the scene first")
            return {"CANCELLED"}
        if time.time() - _last_launch < 10.0:
            self.report({"INFO"}, "Painter is already starting")
            return {"CANCELLED"}
        p = prefs.get_prefs(context)
        explicit = bpy.path.abspath(p.painter_path) if p is not None and p.painter_path else ""
        exe = scaffold.find_painter(explicit)
        if not exe:
            self.report({"ERROR"}, "Substance Painter not found: set its path in the "
                                   "Texture Plugger add-on preferences")
            return {"CANCELLED"}
        textures = os.path.join(folder, "textures")
        os.makedirs(textures, exist_ok=True)
        objs = objects_in_scene(folder)
        project = scaffold.find_project(folder)
        if project:
            cmd = scaffold.painter_command(exe, textures, project=project)
        else:
            mesh = os.path.join(folder, "mesh", os.path.basename(folder) + ".fbx")
            mesh, err = export_meshes(context, objs, mesh)
            if mesh is None:
                self.report({"ERROR"}, err)
                return {"CANCELLED"}
            cmd = scaffold.painter_command(exe, textures, mesh=mesh)
        target = workflow._active_object(context)
        if target not in objs:
            target = objs[0] if objs else None
        if target is not None and (p is None or p.send_on_launch):
            from . import scene_link
            try:
                scene_link.install_plugin()
                _m, warns = scene_link.send(context, target)
                for w in warns:
                    self.report({"WARNING"}, w)
            except Exception as ex:
                self.report({"WARNING"}, f"Could not send the lighting to Painter: {ex}")
        try:
            workflow._launch(cmd, cwd=os.path.dirname(exe))
        except OSError as ex:
            self.report({"ERROR"}, f"Could not start Substance Painter: {ex}")
            return {"CANCELLED"}
        _last_launch = time.time()
        self.report({"INFO"}, f"Substance Painter started with {len(objs)} object(s). Export with "
                              f"the '{workflow.EXPORT_PRESET}' preset as PNG; Blender reloads")
        return {"FINISHED"}


classes = (TEXTUREPLUGGER_OT_create_scene_sets, TEXTUREPLUGGER_OT_edit_scene_in_painter)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
