# SPDX-License-Identifier: GPL-3.0-or-later
"""Substance 3D Painter round trip: create a texture set, launch Painter, live reload.

* ``texture_plugger.create_set`` writes placeholder PNGs named the way Painter's
  "Blender (Principled BSDF)" export preset names its output, plugs them into the
  material and exports the mesh for Painter.
* ``texture_plugger.edit_in_painter`` starts Painter on that mesh (or on the
  newest ``.spp`` project in the set folder) with ``--export-path`` pointing at
  the textures folder.
* A persistent timer reloads watched images (``node_builder.WATCH_KEY``) once
  their files changed on disk and the new contents are stable.
* Links made by ``create_set`` are remembered outside Blender's undo system and
  put back after an undo/redo (``heal``); only ``texture_plugger.unlink_set``
  breaks one.
"""

import os
import subprocess
from dataclasses import dataclass

import bpy
from bpy.props import StringProperty

from . import node_builder, operators, prefs, scaffold
from .node_builder import WATCH_KEY
from .operators import _safe

PROJECT_KEY = "texture_plugger_project"
MESH_KEY = "texture_plugger_mesh"
EXPORT_PRESET = "Blender (Principled BSDF)"

# Indirection so tests can replace it and never start Painter.
_launch = subprocess.Popen


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _active_object(context):
    vl = getattr(context, "view_layer", None)
    return vl.objects.active if vl is not None else None


def _unique_material_name(mat, wanted):
    """``wanted`` or ``wanted_2``, ``wanted_3``... not used by another local material."""
    def taken(name):
        for m in bpy.data.materials:
            if m.name == name and m.library is None and m != mat:
                return True
        return False

    name, i = wanted, 2
    while taken(name):
        name = f"{wanted}_{i}"
        i += 1
    return name


def material_project(mat):
    """Absolute set folder stored on ``mat`` (or "")."""
    if mat is None:
        return ""
    try:
        value = mat.get(PROJECT_KEY)
    except Exception:
        return ""
    return str(value) if value else ""


def export_mesh(context, obj, path):
    """Export only ``obj`` for Painter. Returns (path_or_None, message_or_None)."""
    vl = context.view_layer
    prev_active = vl.objects.active
    prev_selected = [o for o in vl.objects if o.select_get()]
    prev_mode = obj.mode
    try:
        if obj.mode == "EDIT":
            obj.update_from_editmode()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for o in prev_selected:
            o.select_set(False)
        obj.select_set(True)
        if not obj.select_get():
            return None, (f"Could not export '{obj.name}': it cannot be selected "
                          "(hidden or disabled in the viewport?)")
        vl.objects.active = obj
        fbx_error = None
        try:
            res = bpy.ops.export_scene.fbx(
                filepath=path, use_selection=True, object_types={"MESH"},
                add_leaf_bones=False, bake_anim=False)
            if "FINISHED" in res and os.path.isfile(path):
                return path, None
            fbx_error = "FBX export did not finish"
        except (AttributeError, RuntimeError, TypeError) as ex:
            fbx_error = str(ex).strip() or type(ex).__name__
        obj_path = os.path.splitext(path)[0] + ".obj"
        try:
            res = bpy.ops.wm.obj_export(filepath=obj_path, export_selected_objects=True,
                                        export_materials=True)
            if "FINISHED" in res and os.path.isfile(obj_path):
                return obj_path, None
            return None, f"Mesh export failed ({fbx_error}; OBJ export did not finish)"
        except (AttributeError, RuntimeError, TypeError) as ex:
            return None, f"Mesh export failed ({fbx_error}; {str(ex).strip()})"
    except RuntimeError as ex:
        return None, f"Could not export '{obj.name}': {str(ex).strip()}"
    finally:
        if obj.mode != prev_mode:   # some exporters switch to Object mode
            try:
                vl.objects.active = obj
                bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass
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
# Live reload
# --------------------------------------------------------------------------

_seen = {}       # image key -> signature last loaded / accepted
_pending = {}    # image key -> new signature seen once (waiting to be stable)


def _image_key(img):
    uid = getattr(img, "session_uid", None)
    return uid if uid is not None else img.name_full


def _image_files(img):
    try:
        path = os.path.abspath(bpy.path.abspath(img.filepath, library=img.library))
    except Exception:
        return []
    if img.source == "TILED" and "<UDIM>" in path:
        return [path.replace("<UDIM>", str(t.number)) for t in img.tiles]
    return [path]


def _signature(img):
    """Tuple of (mtime_ns, size) per file, or None when a file is missing."""
    files = _image_files(img)
    if not files:
        return None
    sig = []
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            return None
        sig.append((st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _watched_images():
    for img in bpy.data.images:
        try:
            if img.get(WATCH_KEY) is None:
                continue
            if img.source not in {"FILE", "TILED"} or img.packed_file is not None:
                continue
            if getattr(img, "packed_files", None) and len(img.packed_files):
                continue
        except ReferenceError:
            continue
        yield img


def remember(images):
    """Record the file state of images seen for the first time as already loaded.

    Images known before keep their old state, so a reused datablock whose file
    changed meanwhile is still reloaded by ``check_reload``.
    """
    for img in images:
        try:
            sig = _signature(img)
            key = _image_key(img)
        except ReferenceError:
            continue
        if sig is not None:
            _seen.setdefault(key, sig)


def _redraw():
    try:
        wm = bpy.context.window_manager
    except Exception:
        wm = None
    if wm is None:
        return
    for win in getattr(wm, "windows", ()):
        screen = getattr(win, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            area.tag_redraw()


def check_reload(force=False):
    """Reload watched images whose files changed. Returns reloaded image names."""
    reloaded = []
    for img in _watched_images():
        try:
            if img.is_dirty:          # unsaved paint in Blender: never discard it
                continue
            sig = _signature(img)
            if sig is None:
                continue
            key = _image_key(img)
            if not force:
                old = _seen.get(key)
                if old is None:
                    _seen[key] = sig
                    _pending.pop(key, None)
                    continue
                if sig == old:
                    _pending.pop(key, None)
                    continue
                if _pending.get(key) != sig or any(size == 0 for _, size in sig):
                    _pending[key] = sig     # wait until the writer is done
                    continue
            img.reload()
            _seen[key] = sig
            _pending.pop(key, None)
            reloaded.append(img.name)
        except ReferenceError:
            continue
    if reloaded:
        try:
            _redraw()
        except Exception:
            pass
    return reloaded


def _timer():
    interval = 1.0
    try:
        p = prefs.get_prefs()
        if p is not None:
            interval = max(0.25, float(p.reload_interval))
        prune_registry()
        if p is None or p.auto_reload:
            check_reload()
    except Exception as ex:
        print("Texture Plugger: auto reload error:", ex)
    return interval


# --------------------------------------------------------------------------
# Linking (shared by the operator and the undo repair)
# --------------------------------------------------------------------------

@dataclass
class _Link:
    folder: str          # absolute set folder
    obj_name: str
    slot: int
    set_name: str
    root: str
    map_ids: tuple
    size: int = 1024


# Links live here, outside Blender's undo system, keyed by normalised folder.
_registry = {}


def _folder_key(folder):
    return os.path.normcase(os.path.abspath(folder)) if folder else ""


def _existing_mesh(paths):
    for cand in (paths.mesh, os.path.splitext(paths.mesh)[0] + ".obj"):
        if os.path.isfile(cand):
            return cand
    return ""


def link_set(context, settings, obj, root, wanted, map_ids, size, export=True):
    """Create missing files and plug the set into ``obj``'s active material.

    Returns (info dict, None) or (None, error message).
    """
    mat, err = node_builder.get_or_create_material(
        obj, "ACTIVE_SLOT", wanted, wanted.lower(), {})
    if mat is None:
        return None, err
    if mat.name != wanted:
        mat.name = _unique_material_name(mat, wanted)
    set_name = mat.name   # Painter's texture set name

    results = scaffold.create_files(root, set_name, map_ids, size)
    paths = scaffold.set_paths(root, set_name)
    created = sum(1 for _, _, c in results if c)

    operators.store_known_set(settings, set_name,
                              [(spec.map_type, p) for spec, p, _ in results])
    maps, msgs = operators.collect_maps(settings)
    res = node_builder.build_material(mat, maps, operators.build_options(settings),
                                      settings.set_name, settings.set_key)
    for w in res.warnings:
        if w not in msgs:
            msgs.append(w)
    remember(node_builder.managed_images(mat.node_tree))
    mat[PROJECT_KEY] = paths.folder

    if not export:
        mesh_path = _existing_mesh(paths)
        if mesh_path:
            mat[MESH_KEY] = mesh_path
    elif obj.type == "MESH":
        if len(obj.data.uv_layers) == 0:
            msgs.append(f"'{obj.name}' has no UV map; Painter can auto-unwrap it on import "
                        "(or add a UV map before painting)")
        mesh_path, mesh_err = export_mesh(context, obj, paths.mesh)
        if mesh_path:
            mat[MESH_KEY] = mesh_path
        else:
            msgs.append(mesh_err)
    if sum(1 for s in obj.material_slots if s.material is not None) > 1:
        msgs.append(f"'{obj.name}' has several materials; Painter makes a texture set "
                    f"per material, only '{set_name}' was plugged in")

    # One link per object slot: forget the folder this slot pointed at before.
    for key, lk in list(_registry.items()):
        if lk.obj_name == obj.name and lk.slot == obj.active_material_index:
            del _registry[key]
    _registry[_folder_key(paths.folder)] = _Link(
        folder=paths.folder, obj_name=obj.name, slot=obj.active_material_index,
        set_name=set_name, root=root, map_ids=tuple(map_ids), size=size)
    return {"material": mat, "folder": paths.folder, "created": created,
            "kept": len(results) - created, "messages": msgs}, None


def _linked_folders():
    return {_folder_key(material_project(m)) for m in bpy.data.materials} - {""}


def prune_registry():
    """Forget links no material carries any more (relinked, purged, deleted).

    Runs on the timer, i.e. after the undo handler had its chance to repair.
    """
    linked = _linked_folders()
    for key in [k for k in _registry if k not in linked]:
        del _registry[key]


def sync_registry():
    """Remember links that exist in the file but are not in the registry yet."""
    for obj in bpy.data.objects:
        if not node_builder.object_supports_material(obj):
            continue
        for i, slot in enumerate(obj.material_slots):
            folder = material_project(slot.material)
            key = _folder_key(folder)
            if not key or key in _registry or not os.path.isdir(folder):
                continue
            set_name = os.path.basename(os.path.normpath(folder))
            root = os.path.dirname(os.path.normpath(folder))
            tex = scaffold.set_paths(root, set_name).textures
            ids = tuple(s.id for s in scaffold.MAPS
                        if os.path.isfile(os.path.join(tex, scaffold.texture_filename(set_name, s))))
            if ids:
                _registry[key] = _Link(folder=folder, obj_name=obj.name, slot=i,
                                       set_name=set_name, root=root, map_ids=ids)


def heal(context=None):
    """Put back links that an undo/redo removed. Returns the relinked set names."""
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "texture_plugger", None)
    healed = []
    if settings is None:
        return healed
    linked = _linked_folders()
    for key, lk in list(_registry.items()):
        if not os.path.isdir(lk.folder):
            del _registry[key]
            continue
        if key in linked:
            continue      # some material still carries the link: nothing was undone
        obj = bpy.data.objects.get(lk.obj_name)
        if obj is None or not node_builder.object_supports_material(obj):
            continue
        if 0 <= lk.slot < len(obj.material_slots):
            obj.active_material_index = lk.slot
        operators.ensure_initialized(context, settings)
        info, err = link_set(context, settings, obj, lk.root, lk.set_name,
                             list(lk.map_ids), lk.size, export=False)
        if info is None:
            print("Texture Plugger: could not restore texture set link:", err)
            continue
        healed.append(lk.set_name)
    sync_registry()
    if not healed and len(settings.slots) == 0:
        _restore_panel(context, settings)
    return healed


def _restore_panel(context, settings):
    """Undo emptied the slot list while the material kept its link: list the set again."""
    obj = _active_object(context)
    mat = obj.active_material if node_builder.object_supports_material(obj) else None
    lk = _registry.get(_folder_key(material_project(mat)))
    if lk is None:
        return
    tex = scaffold.set_paths(lk.root, lk.set_name).textures
    items = []
    for spec in scaffold.MAPS:
        path = os.path.join(tex, scaffold.texture_filename(lk.set_name, spec))
        if spec.id in lk.map_ids and os.path.isfile(path):
            items.append((spec.map_type, path))
    if items:
        operators.store_known_set(settings, lk.set_name, items)


@bpy.app.handlers.persistent
def _on_undo_redo(*_args):
    try:
        names = heal()
        if names:
            print("Texture Plugger: restored texture set link after undo:", ", ".join(names))
    except Exception as ex:
        print("Texture Plugger: undo repair error:", ex)


@bpy.app.handlers.persistent
def _on_load(*_args):
    _registry.clear()
    try:
        sync_registry()
    except Exception as ex:
        print("Texture Plugger: could not read texture set links:", ex)


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class TEXTUREPLUGGER_OT_create_set(bpy.types.Operator):
    """Create a folder with placeholder textures named for Substance Painter, plug them into the material and export the mesh"""
    bl_idname = "texture_plugger.create_set"
    bl_label = "Create Texture Set"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(default="", subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"})
    name: StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = operators.get_settings(context)
        operators.ensure_initialized(context, settings)
        obj = _active_object(context)
        if not node_builder.object_supports_material(obj):
            self.report({"WARNING"}, "Select an object that can have materials (e.g. a mesh)")
            return {"CANCELLED"}

        raw_root = (self.directory or settings.create_root or "").strip()
        if not raw_root or (raw_root.startswith("//") and not bpy.data.filepath):
            self.report({"ERROR"}, "Choose a folder for the texture set first "
                                   "(a relative '//' path needs a saved .blend file)")
            return {"CANCELLED"}
        root = os.path.abspath(bpy.path.abspath(raw_root))

        map_ids = [s.id for s in scaffold.MAPS if getattr(settings, "create_" + s.id, False)]
        if not map_ids:
            self.report({"WARNING"}, "Enable at least one map to create")
            return {"CANCELLED"}

        current = obj.active_material
        wanted = scaffold.sanitize_name(
            self.name or settings.create_name or (current.name if current else obj.name))
        info, err = link_set(context, settings, obj, root, wanted, map_ids,
                             int(settings.create_resolution))
        if info is None:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        operators._report_all(self, info["messages"], "WARNING")
        self.report({"INFO"}, f"Created {info['created']} file(s), kept {info['kept']} "
                              f"existing, in {info['folder']}")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_edit_in_painter(bpy.types.Operator):
    """Open the active material's texture set in Adobe Substance 3D Painter"""
    bl_idname = "texture_plugger.edit_in_painter"
    bl_label = "Edit in Substance Painter"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        obj = _active_object(context)
        mat = obj.active_material if node_builder.object_supports_material(obj) else None
        folder = material_project(mat)
        if not folder or not os.path.isdir(folder):
            self.report({"WARNING"}, "Create a texture set first")
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

        project = scaffold.find_project(folder)
        if project:
            cmd = scaffold.painter_command(exe, textures, project=project)
        else:
            set_name = os.path.basename(os.path.normpath(folder))
            mesh_path = scaffold.set_paths(os.path.dirname(os.path.normpath(folder)),
                                           set_name).mesh
            if obj.type == "MESH":
                mesh, err = export_mesh(context, obj, mesh_path)
                if mesh is None:
                    self.report({"ERROR"}, err)
                    return {"CANCELLED"}
                mat[MESH_KEY] = mesh
            else:
                mesh = str(mat.get(MESH_KEY) or "")
                if not mesh or not os.path.isfile(mesh):
                    self.report({"ERROR"}, f"'{obj.name}' is not a mesh and no exported "
                                           "mesh was found for this texture set")
                    return {"CANCELLED"}
            cmd = scaffold.painter_command(exe, textures, mesh=mesh)
        if mat.name != os.path.basename(os.path.normpath(folder)):
            self.report({"WARNING"}, f"Material was renamed: Painter will export files "
                                     f"named '{mat.name}_...', not the placeholders")
        try:
            _launch(cmd, cwd=os.path.dirname(exe))
        except OSError as ex:
            self.report({"ERROR"}, f"Could not start Substance Painter: {ex}")
            return {"CANCELLED"}
        what = os.path.basename(project) if project else "a new project from the mesh"
        self.report({"INFO"}, f"Substance Painter started with {what}. Export with the "
                              f"'{EXPORT_PRESET}' preset as PNG; Blender reloads the textures")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_reload_images(bpy.types.Operator):
    """Reload every image Texture Plugger plugged in from its file (images with unsaved paint are skipped)"""
    bl_idname = "texture_plugger.reload_images"
    bl_label = "Reload Textures"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        names = check_reload(force=True)
        self.report({"INFO"}, f"Reloaded {len(names)} image(s)")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_open_set_folder(bpy.types.Operator):
    """Open the active material's texture set folder in the file browser"""
    bl_idname = "texture_plugger.open_set_folder"
    bl_label = "Open Folder"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        obj = _active_object(context)
        mat = obj.active_material if node_builder.object_supports_material(obj) else None
        folder = material_project(mat)
        if not folder or not os.path.isdir(folder):
            self.report({"WARNING"}, "Create a texture set first")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=folder)
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_unlink_set(bpy.types.Operator):
    """Break the link between the active material and its texture set folder (nodes and files are kept). Undo cannot break this link, only this button"""
    bl_idname = "texture_plugger.unlink_set"
    bl_label = "Unlink Texture Set"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        obj = _active_object(context)
        mat = obj.active_material if node_builder.object_supports_material(obj) else None
        folder = material_project(mat)
        if not folder:
            self.report({"WARNING"}, "The active material is not linked to a texture set")
            return {"CANCELLED"}
        _registry.pop(_folder_key(folder), None)
        for k in (PROJECT_KEY, MESH_KEY):
            if mat.get(k) is not None:
                del mat[k]
        self.report({"INFO"}, f"Unlinked '{mat.name}' from {folder} (files and nodes kept)")
        return {"FINISHED"}


classes = (
    TEXTUREPLUGGER_OT_create_set,
    TEXTUREPLUGGER_OT_edit_in_painter,
    TEXTUREPLUGGER_OT_reload_images,
    TEXTUREPLUGGER_OT_open_set_folder,
    TEXTUREPLUGGER_OT_unlink_set,
)


def _handlers():
    h = bpy.app.handlers
    return ((h.undo_post, _on_undo_redo), (h.redo_post, _on_undo_redo), (h.load_post, _on_load))


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=1.0, persistent=True)
    for lst, fn in _handlers():
        if fn not in lst:
            lst.append(fn)


def unregister():
    for lst, fn in _handlers():
        if fn in lst:
            lst.remove(fn)
    _registry.clear()
    try:
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
    except Exception:
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
