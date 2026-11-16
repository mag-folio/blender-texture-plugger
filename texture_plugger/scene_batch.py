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

from . import (node_builder, operators, prefs, scaffold, scene_plan, unwrap_pool, uv_unwrap,
               workflow)
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
            try:
                o.select_set(True)
            except RuntimeError:
                continue                  # not in this view layer (in no collection)
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
UNWRAP_STEP = "Unwrapping"                 # status of a slow step: show it before it runs


def _limits(context):
    """This computer's memory and graphics memory, and the limits from the preferences."""
    p = prefs.get_prefs(context)
    max_sets = int(getattr(p, "max_scene_sets", 0)) if p else 0
    pct = int(getattr(p, "memory_budget", 40)) if p else 40
    return {"ram": scene_plan.system_memory_bytes(), "gpu": scene_plan.gpu_memory_bytes(),
            "share": max(5, min(75, pct)) / 100.0, "max_sets": max_sets or None}


def plan_parts(context, folder, size, n_maps, extra=(), limits=None):
    """Texture size and Painter parts for every object of the scene in ``folder``.

    Sized to this computer (memory and graphics memory, measured Painter costs):
    the whole scene opens as one Painter project whenever it can. Stamps the
    result on the scene's materials and returns ``(size, [[objects of part 1], ...])``.
    """
    lim = limits or _limits(context)
    everyone = {o.name_full: o for o in objects_in_scene(folder)}
    for o in extra:
        everyone[o.name_full] = o
    ordered = sorted(everyone.values(), key=_part_sort_key)
    weights = [max(1, len(scene_materials(o, folder))) for o in ordered]
    resolution, groups = scene_plan.plan_scene(weights, n_maps, size, lim["ram"], lim["gpu"],
                                               lim["share"], lim["max_sets"])
    parts = [[ordered[i] for i in idx] for idx in groups]
    for p, members in enumerate(parts):
        for o in members:
            for mat in scene_materials(o, folder):
                mat[PART_KEY] = p
                mat[PARTS_KEY] = len(parts)
                mat[RES_KEY] = resolution
    return resolution, parts


def _part_sort_key(ob):
    """Keep things that belong together (collection, then group/parent) in the same part."""
    coll = ob.users_collection[0].name if ob.users_collection else ""
    root = ob
    while root.parent is not None:
        root = root.parent
    return (coll, root.name, ob.name)


def painted_sets(ob, folder, tex_dir):
    """Names of the object's scene texture sets that have a painted (not placeholder) map."""
    out = []
    for mat in scene_materials(ob, folder):
        for spec in scaffold.MAPS:
            path = os.path.join(tex_dir, scaffold.texture_filename(mat.name, spec))
            if os.path.isfile(path) and not scaffold.is_untouched_placeholder(path, spec.rgb):
                out.append(mat.name)
                break
    return out


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

    # Objects are unwrapped with Smart Seams (when it is installed) before
    # anything is exported, so Painter gets clean UVs. "All Objects" redoes
    # existing UVs too, except where textures are already painted (new UVs would
    # scramble them) or Smart Seams already did this mesh. Shared meshes once.
    mode = getattr(settings, "scene_unwrap_mode", "ALL")
    want_unwrap = mode != "NONE"
    have_seams = want_unwrap and uv_unwrap.smart_seams_available()
    to_unwrap, seen_data, kept_painted = [], set(), []
    if have_seams:
        users = {}
        for o in bpy.data.objects:
            if o.type == "MESH" and o.data is not None:
                users.setdefault(o.data.name_full, []).append(o)
        for ob in objs:
            key = ob.data.name_full
            if (ob.name in skipped or key in seen_data or len(ob.data.polygons) == 0
                    or not uv_unwrap.unwrappable(ob)):
                continue
            if not uv_unwrap.needs_unwrap(ob):
                if mode != "ALL" or uv_unwrap.is_optimized(ob):
                    continue
                if any(painted_sets(u, folder, tex_dir) for u in users.get(key, [ob])):
                    kept_painted.append(ob.name)
                    seen_data.add(key)
                    continue
            seen_data.add(key)
            to_unwrap.append(ob)
    if kept_painted:
        shown = ", ".join(kept_painted[:8]) + (" ..." if len(kept_painted) > 8 else "")
        messages.append(f"Kept the UVs of {len(kept_painted)} object(s) whose textures are "
                        f"already painted (new UVs would scramble them): {shown}")

    # Meshes with the same geometry (copies that were made single user) are
    # solved once and get the same UVs. Light meshes first: dense ones take
    # minutes, and Esc between objects keeps everything done so far.
    max_faces = int(getattr(settings, "scene_unwrap_max_faces", 0) or 0)
    groups, too_dense = {}, []
    for ob in to_unwrap:
        if max_faces and len(ob.data.polygons) > max_faces:
            too_dense.append(ob.name)
            continue
        groups.setdefault(uv_unwrap.geometry_key(ob.data), []).append(ob)
    if too_dense:
        shown = ", ".join(too_dense[:8]) + (" ..." if len(too_dense) > 8 else "")
        messages.append(f"Not unwrapped: {len(too_dense)} mesh(es) over {max_faces} faces "
                        f"(Max Faces): {shown}")
    batches = sorted(groups.values(), key=lambda g: len(g[0].data.polygons))

    total = len(batches) + len(plan) + len(objs) + 2
    done = 0
    unwrapped, unwrap_failed = 0, set()

    def unwrap_done(group, err):
        """Book one solved (or failed) mesh and give its copies the same UVs."""
        nonlocal unwrapped
        ob = group[0]
        if err:
            for o in group:
                unwrap_failed.add(o.data.name_full)
            more = f" and {len(group) - 1} copies" if len(group) > 1 else ""
            messages.append(f"'{ob.name}'{more}: Smart Seams could not unwrap it ({err})")
            return
        unwrapped += 1
        for o in group[1:]:
            try:
                uv_unwrap.copy_unwrap(ob, o)
                unwrapped += 1
            except Exception as ex:
                unwrap_failed.add(o.data.name_full)
                messages.append(f"'{o.name}': could not copy the UVs of '{ob.name}' ({ex})")

    # Many or dense meshes: Smart Seams runs in background Blenders, several
    # meshes at once, and this Blender stays responsive. Otherwise, or for
    # whatever the background could not start, one by one right here.
    pool = unwrap_pool.start(context, [g[0] for g in batches]) if batches else None
    if pool is not None:
        group_of = {g[0].name_full: g for g in batches}
        batches = []
        try:
            while not pool.finished():
                arrived = pool.poll()
                for ob, err, fallback in arrived:
                    if fallback:
                        batches.append(group_of[ob.name_full])
                        continue
                    unwrap_done(group_of[ob.name_full], err)
                    done += 1
                if not arrived:
                    time.sleep(0.05)
                now = pool.running()
                names = ", ".join(now[:3]) + (" ..." if len(now) > 3 else "")
                yield done, total, f"Smart Seams is unwrapping {names or '...'} ({len(now)} at once),"
        finally:
            pool.close()
    for i, group in enumerate(batches):
        ob = group[0]
        yield done, total, (f"{UNWRAP_STEP} {ob.name} with Smart Seams ({i + 1}/{len(batches)}, "
                            f"{len(ob.data.polygons)} faces)")
        unwrap_done(group, uv_unwrap.unwrap_one(context, ob))
        done += 1
        yield done, total, "Unwrapped with Smart Seams"

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

    # Placeholders are flat colours: small files whatever the final size.
    built_objs = [o for o in objs if o.name not in skipped]
    ph = scene_plan.placeholder_size(size)

    opts = operators.build_options(settings)
    all_ids = tuple(map_ids)
    images = node_builder.image_cache()     # thousands of images: no search per file
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
            with images:
                res = node_builder.build_material(mat, maps, opts, settings.set_name,
                                                  settings.set_key)
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
        if not uv_unwrap.needs_unwrap(ob) or ob.data.name_full in unwrap_failed:
            continue
        if want_unwrap and not have_seams:
            messages.append(f"'{ob.name}' has no UV map; enable the Smart Seams add-on to "
                            f"unwrap it automatically (or let Painter auto-unwrap it on import)")
        else:
            messages.append(f"'{ob.name}' has no UV map; Painter can auto-unwrap it on import")

    # Size the scene to this computer and cut it into Painter parts (one when it
    # fits), keeping collections and groups together.
    resolution, parts = plan_parts(context, folder, size, len(map_ids), built_objs, limits)
    n_sets = sum(len(scene_materials(o, folder)) for part in parts for o in part)
    if resolution < size:
        messages.append(f"Texture size {resolution} px (asked {size}) so all {n_sets} texture "
                        f"sets fit in this computer's memory and graphics memory")
    stem = os.path.basename(folder)
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
            "unwrapped": unwrapped,
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
    if info.get("unwrapped"):
        msg += f", {info['unwrapped']} unwrapped with Smart Seams"
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
                if what.startswith(UNWRAP_STEP):
                    break                  # show the status before the slow unwrap runs
        except StopIteration as stop:
            self._end(context)
            return self._report_result(stop.value)
        except Exception as ex:
            self._end(context)
            self.report({"ERROR"}, f"Texture Plugger: {ex}")
            return {"CANCELLED"}
        # Begin again every tick: Smart Seams ends the progress bar it shares with us.
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


def is_part_project(path, folder):
    """True for ``<scene>_partNN.spp``: a project of one part of a split scene."""
    stem = os.path.splitext(os.path.basename(path))[0]
    base = os.path.basename(folder)
    rest = stem[len(base):] if stem.lower().startswith(base.lower()) else ""
    return rest.lower().startswith("_part") and rest[5:].isdigit()


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
        # Plan again for this computer as it is now: the whole scene in one Painter
        # project when it fits, otherwise the part holding the selected object.
        settings = operators.get_settings(context)
        n_maps = sum(1 for spec in scaffold.MAPS if getattr(settings, "create_" + spec.id, False)) or 4
        _res, plan = plan_parts(context, folder, int(settings.create_resolution), n_maps)
        everyone = [o for part in plan for o in part]
        count = max(1, len(plan))
        part = 0
        if picked is not None:
            part = next((i for i, members in enumerate(plan) if picked in members), 0)
        objs = plan[part] if plan else []
        if not objs:
            objs, part, count = everyone, 0, 1
        stem = scene_plan.part_name(os.path.basename(folder), part, count)
        spp = os.path.join(folder, stem + ".spp")
        project = spp if os.path.isfile(spp) else None
        if project is None and count <= 1:
            # A project saved by hand in the folder (any name) is reopened, but never
            # a part project from an earlier, split-up plan of this scene.
            found = scaffold.find_project(folder)
            if found and not is_part_project(found, folder):
                project = found
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
        self.report({"INFO"}, f"Substance Painter started with {where}. Press 'Export to "
                              f"Blender' in Painter's Blender Link panel (or export with the "
                              f"'{workflow.EXPORT_PRESET}' preset as PNG); Blender reloads")
        return {"FINISHED"}


classes = (TEXTUREPLUGGER_OT_create_scene_sets, TEXTUREPLUGGER_OT_edit_scene_in_painter)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
