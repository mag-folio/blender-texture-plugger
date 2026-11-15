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

from . import node_builder, operators, prefs, scaffold, scene_plan, workflow
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


PART_KEY = "texture_plugger_part"          # 0-based part index of a scene material
PARTS_KEY = "texture_plugger_parts"        # number of parts in its scene
RES_KEY = "texture_plugger_resolution"     # texture size Painter should use for it
STEP_SECONDS = 0.12                        # work per UI tick of the modal operator


def _limits(context):
    """(max texture sets per Painter part, memory budget in bytes) from the preferences."""
    p = prefs.get_prefs(context)
    max_sets = int(getattr(p, "max_scene_sets", 50)) if p else 50
    pct = int(getattr(p, "memory_budget", 40)) if p else 40
    return max(1, max_sets), int(scene_plan.system_memory_bytes() * max(5, min(75, pct)) / 100)


def _part_sort_key(ob):
    """Keep things that belong together (collection, then group/parent) in the same part."""
    coll = ob.users_collection[0].name if ob.users_collection else ""
    root = ob
    while root.parent is not None:
        root = root.parent
    return (coll, root.name, ob.name)


def scene_materials(ob, folder):
    key = workflow._folder_key(folder)
    return [s.material for s in ob.material_slots
            if s.material is not None and workflow._folder_key(material_scene(s.material)) == key]


def iter_create_scene_sets(context, settings, objs, root, map_ids, size, limits=None):
    """Generator doing the work of Create Sets for Scene in small steps.

    Yields ``(done, total, what)`` after each small piece of work so the caller
    can keep Blender responsive; returns ``(info, None)`` or ``(None, error)``
    as the generator's value.
    """
    max_sets, budget = limits or _limits(context)
    folder = os.path.join(root, scene_folder_name(bpy.data.filepath))
    tex_dir = os.path.join(folder, "textures")
    mesh_dir = os.path.join(folder, "mesh")
    try:
        os.makedirs(tex_dir, exist_ok=True)
        os.makedirs(mesh_dir, exist_ok=True)
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
    total = len(plan) + len(objs) + 2
    done = 0

    created_mats = []
    try:
        for (ob, i), name in zip(plan, names):
            slot = ob.material_slots[i]
            replaced = slot.material
            mat = bpy.data.materials.new(name)
            node_builder.ensure_node_tree(mat)
            if mat.name != name:
                mat.name = name
            slot.material = mat
            mat[SCENE_KEY] = folder
            if replaced is not None and replaced.users > 0 and replaced.name != name:
                messages.append(f"'{ob.name}': replaced material '{replaced.name}'")
            created_mats.append((ob, mat))
            done += 1
            if done % 25 == 0:
                yield done, total, "Making materials"
    except Exception:
        for _, mat in created_mats:
            try:
                bpy.data.materials.remove(mat)
            except Exception:
                pass
        raise

    # Size the job to this computer: every texture set of the whole scene has to
    # fit in memory once Blender shows the painted textures.
    built_objs = [o for o in objs if o.name not in skipped]
    everyone = {o.name_full: o for o in objects_in_scene(folder)}
    for o in built_objs:
        everyone[o.name_full] = o
    n_sets = sum(max(1, len(scene_materials(o, folder))) for o in everyone.values())
    resolution = scene_plan.choose_resolution(n_sets, len(map_ids), size, budget)
    ph = scene_plan.placeholder_size(resolution)
    if resolution < size:
        messages.append(f"{n_sets} texture sets at {size} px would need "
                        f"{scene_plan.describe_bytes(n_sets * scene_plan.texture_bytes(size, len(map_ids)))}"
                        f" of memory: using {resolution} px so the scene fits on this computer")

    opts = operators.build_options(settings)
    all_ids = tuple(map_ids)
    built = 0
    for ob in built_objs:
        for slot in ob.material_slots:
            mat = slot.material
            if mat is None or workflow._folder_key(material_scene(mat)) not in (
                    "", workflow._folder_key(folder)):
                continue
            set_name = mat.name
            try:
                results = scaffold.create_files(root, set_name, all_ids, ph, textures_dir=tex_dir,
                                               shrink=True)
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
        done += 1
        yield done, total, "Creating texture sets"

    for ob in built_objs:
        if len(ob.data.uv_layers) == 0:
            messages.append(f"'{ob.name}' has no UV map; Painter can auto-unwrap it on import")

    # Parts Painter can handle, keeping collections and groups together.
    ordered = sorted(everyone.values(), key=_part_sort_key)
    weights = [max(1, len(scene_materials(o, folder))) for o in ordered]
    groups = scene_plan.pack_parts(weights, max_sets)
    stem = os.path.basename(folder)
    parts = []
    for p, idx in enumerate(groups):
        members = [ordered[i] for i in idx]
        parts.append(members)
        for o in members:
            for mat in scene_materials(o, folder):
                mat[PART_KEY] = p
                mat[PARTS_KEY] = len(groups)
                mat[RES_KEY] = resolution
    total = done + len(parts)
    exported_paths = []
    for p, members in enumerate(parts):
        path = os.path.join(mesh_dir, scene_plan.part_name(stem, p, len(parts)) + ".fbx")
        exported, err = export_meshes(context, members, path)
        if exported:
            exported_paths.append(exported)
            for o in members:
                for mat in scene_materials(o, folder):
                    mat[workflow.MESH_KEY] = exported
        else:
            messages.append(err)
        done += 1
        yield done, total, "Exporting meshes for Painter"
    if skipped:
        messages.append("Skipped linked objects: " + ", ".join(skipped))
    return {"folder": folder, "textures": tex_dir, "built": built, "objects": len(objs),
            "mesh": exported_paths[0] if len(exported_paths) == 1 else (exported_paths or None),
            "parts": len(parts), "resolution": resolution, "sets": n_sets,
            "messages": messages}, None


def create_scene_sets(context, settings, objs, root, map_ids, size, limits=None):
    """Run the whole job at once. Returns (info dict, None) or (None, error)."""
    gen = iter_create_scene_sets(context, settings, objs, root, map_ids, size, limits)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _finish_message(info):
    msg = f"{info['built']} texture set(s) for {info['objects']} object(s)"
    if info.get("parts", 1) > 1:
        msg += f", in {info['parts']} Painter parts"
    return msg + f" at {info['resolution']} px in {info['folder']}"


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class TEXTUREPLUGGER_OT_create_scene_sets(bpy.types.Operator):
    """Give every mesh its own material and texture set (placeholders in one shared folder) and export them all as one Painter scene"""
    bl_idname = "texture_plugger.create_scene_sets"
    bl_label = "Create Sets for Scene"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(name="Objects", items=_SCOPES, default="SELECTED", options={"SKIP_SAVE"})

    def _prepare(self, context):
        """Checks shared by execute and invoke. Returns the generator, or a result set."""
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
        return iter_create_scene_sets(context, settings, objs, root, map_ids,
                                      int(settings.create_resolution))

    def _report_result(self, result):
        info, err = result
        if info is None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        operators._report_all(self, info["messages"], "WARNING")
        self.report({"INFO"}, _finish_message(info))
        return {"FINISHED"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        gen = self._prepare(context)
        if isinstance(gen, set):
            return gen
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            return self._report_result(stop.value)

    # Interactive use: the same job in small slices, so Blender keeps drawing,
    # shows progress and never looks frozen, however big the scene is.
    def invoke(self, context, event):
        try:
            gen = self._prepare(context)
        except Exception as ex:          # same safety net as _safe
            self.report({"ERROR"}, f"Texture Plugger: {ex}")
            return {"CANCELLED"}
        if isinstance(gen, set):
            return gen
        self._gen = gen
        self._wm = context.window_manager
        self._timer = self._wm.event_timer_add(0.01, window=context.window)
        self._progress = False
        self._wm.modal_handler_add(self)
        self._status(context, "Texture Plugger: starting...")
        return {"RUNNING_MODAL"}

    def _status(self, context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass

    def _end(self, context):
        try:
            self._wm.event_timer_remove(self._timer)
        except Exception:
            pass
        if self._progress:
            try:
                self._wm.progress_end()
            except Exception:
                pass
        self._status(context, None)

    def modal(self, context, event):
        if event.type == "ESC":
            self._end(context)
            self.report({"WARNING"}, "Stopped. Press the button again to finish: what was "
                                     "already made is kept and reused")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}       # keep the scene still while the job runs
        deadline = time.perf_counter() + STEP_SECONDS
        try:
            while time.perf_counter() < deadline:
                done, total, what = next(self._gen)
        except StopIteration as stop:
            self._end(context)
            return self._report_result(stop.value)
        except Exception as ex:
            self._end(context)
            self.report({"ERROR"}, f"Texture Plugger: {ex}")
            return {"CANCELLED"}
        if not self._progress:
            self._wm.progress_begin(0, max(1, total))
            self._progress = True
        self._wm.progress_update(done)
        self._status(context, f"Texture Plugger: {what} {done} / {total}  (Esc to stop)")
        return {"RUNNING_MODAL"}


def part_of(ob, folder):
    """(part index, part count) of an object in its scene."""
    for mat in scene_materials(ob, folder):
        try:
            return int(mat.get(PART_KEY, 0)), int(mat.get(PARTS_KEY, 1))
        except (TypeError, ValueError):
            pass
    return 0, 1


def find_scene_folder(context):
    """The whole-scene folder this .blend uses, when the selection doesn't say.

    Prefers the scene under the current *Folder* setting, then the one with the
    most objects. Only folders that still exist on disk count.
    """
    counts = {}
    for ob in context.scene.objects:
        if ob.type != "MESH":
            continue
        for slot in ob.material_slots:
            f = material_scene(slot.material)
            if f:
                counts[f] = counts.get(f, 0) + 1
                break
    folders = [f for f in counts if os.path.isdir(f)]
    if not folders:
        return ""
    try:
        settings = operators.get_settings(context)
        root = os.path.abspath(bpy.path.abspath((settings.create_root or "").strip()))
    except Exception:
        root = ""
    if root:
        under = [f for f in folders
                 if workflow._folder_key(os.path.dirname(f)) == workflow._folder_key(root)]
        if under:
            return max(under, key=lambda f: counts[f])
    return max(folders, key=lambda f: counts[f])


def scene_resolution(objs, folder):
    for ob in objs:
        for mat in scene_materials(ob, folder):
            try:
                return int(mat.get(RES_KEY, 0)) or None
            except (TypeError, ValueError):
                pass
    return None


class TEXTUREPLUGGER_OT_edit_scene_in_painter(bpy.types.Operator):
    """Open the whole scene (every object that has a scene texture set) in Substance 3D Painter. A big scene opens one part at a time: the part holding the selected object"""
    bl_idname = "texture_plugger.edit_scene_in_painter"
    bl_label = "Edit Scene in Painter"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        global _last_launch
        folder = ""
        picked = None
        for ob in [workflow._active_object(context)] + list(context.selected_objects):
            if ob is None or not node_builder.object_supports_material(ob):
                continue
            for slot in ob.material_slots:
                folder = material_scene(slot.material)
                if folder:
                    break
            if folder:
                picked = ob
                break
        if not folder or not os.path.isdir(folder):
            # The active object isn't part of a scene (a brushstroke, an empty, the
            # floor...): use the scene this file already has.
            folder = find_scene_folder(context)
            picked = None
        if not folder:
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
        everyone = objects_in_scene(folder)
        if picked is not None:
            part, count = part_of(picked, folder)
        else:            # nothing of the scene selected: start with part 1
            part, count = 0, (part_of(everyone[0], folder)[1] if everyone else 1)
        objs = [o for o in everyone if part_of(o, folder)[0] == part] if count > 1 else everyone
        if not objs:
            objs, part, count = everyone, 0, 1
        stem = scene_plan.part_name(os.path.basename(folder), part, count)
        spp = os.path.join(folder, stem + ".spp")
        project = spp if os.path.isfile(spp) else (scaffold.find_project(folder) if count <= 1 else None)
        if project:
            cmd = scaffold.painter_command(exe, textures, project=project)
        else:
            mesh = os.path.join(folder, "mesh", stem + ".fbx")
            mesh, err = export_meshes(context, objs, mesh)
            if mesh is None:
                self.report({"ERROR"}, err)
                return {"CANCELLED"}
            cmd = scaffold.painter_command(exe, textures, mesh=mesh)
        target = picked if picked in objs else (objs[0] if objs else None)
        if target is not None and (p is None or p.send_on_launch):
            from . import scene_link
            scene = {"resolution": scene_resolution(objs, folder), "project": spp,
                     "part": part + 1, "parts": count}
            try:
                scene_link.install_plugin()
                _m, warns = scene_link.send(context, target, scene=scene)
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
        where = f"part {part + 1} of {count} ({len(objs)} objects)" if count > 1 else f"{len(objs)} object(s)"
        if count > 1 and picked is None:
            where += "; select an object first to open the part it is in"
        self.report({"INFO"}, f"Substance Painter started with {where}. Export with "
                              f"the '{workflow.EXPORT_PRESET}' preset as PNG; Blender reloads")
        return {"FINISHED"}


classes = (TEXTUREPLUGGER_OT_create_scene_sets, TEXTUREPLUGGER_OT_edit_scene_in_painter)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
