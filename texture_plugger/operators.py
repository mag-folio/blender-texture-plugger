# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators: load set, apply, clear, slot editing, drag-and-drop handler."""

import os
import traceback

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from . import detection, node_builder, prefs, properties

_IMAGE_GLOB = ";".join("*" + e for e in detection.IMAGE_EXTENSIONS)
_suppress_set_update = False


# --------------------------------------------------------------------------
# Shared logic (also used by the tests)
# --------------------------------------------------------------------------

def get_settings(context):
    scene = getattr(context, "scene", None) or bpy.context.scene
    return scene.texture_plugger


def ensure_initialized(context, settings):
    if not settings.initialized:
        properties.copy_defaults_from_prefs(settings, prefs.get_prefs(context))
        settings.initialized = True


def run_analysis(context, paths):
    p = prefs.get_prefs(context)
    return detection.analyze(paths, prefs.get_keywords(p), **prefs.detection_kwargs(p))


def _fill_slots(settings, ts):
    settings.slots.clear()
    settings.slot_index = 0
    if ts is None:
        settings.set_name = ""
        settings.set_key = ""
        return
    settings.set_name = ts.name
    settings.set_key = ts.key
    for choice in ts.ordered_choices():
        d = choice.detection
        slot = settings.slots.add()
        slot.map_type = choice.map_type
        slot.filepath = d.path
        slot.enabled = choice.enabled
        slot.udim_tiles = ",".join(str(t) for t in d.udim_tiles)
        notes = [choice.note] if choice.note else []
        if choice.alternates:
            notes.append(f"{len(choice.alternates)} alternative file(s) skipped")
        if d.udim_tiles:
            notes.append(f"UDIM {len(d.udim_tiles)} tiles")
        slot.note = "; ".join(notes)
    for d in ts.unassigned:
        slot = settings.slots.add()
        slot.map_type = "NONE"
        slot.filepath = d.path
        slot.enabled = False
        slot.note = "Map type not recognised"


def store_analysis(context, settings, paths, analysis):
    """Store files + sets in the scene and fill slots from the best set."""
    global _suppress_set_update
    settings.files.clear()
    for pth in paths:
        settings.files.add().path = pth
    settings.sets.clear()
    for i, ts in enumerate(analysis.sets):
        item = settings.sets.add()
        item.key = ts.key or f"__{i}"
        item.name = ts.name
        item.count = ts.map_count
    best = analysis.sets[0] if analysis.sets else None
    if best is not None:
        _suppress_set_update = True
        try:
            settings.active_set = settings.sets[0].key
        except Exception:
            pass
        finally:
            _suppress_set_update = False
    _fill_slots(settings, best)
    if best is None and analysis.unassigned:
        for d in analysis.unassigned:
            slot = settings.slots.add()
            slot.map_type = "NONE"
            slot.filepath = d.path
            slot.enabled = False
            slot.note = "Map type not recognised"
    return best


def store_known_set(settings, set_name, items, set_key=""):
    """Store one texture set whose map types are already known.

    ``items`` is an iterable of ``(map_type, path)``; no filename detection runs.
    """
    global _suppress_set_update
    items = [(mt, p) for mt, p in items]
    set_key = set_key or set_name.lower()
    settings.files.clear()
    for _, pth in items:
        settings.files.add().path = pth
    settings.sets.clear()
    entry = settings.sets.add()
    entry.key = set_key
    entry.name = set_name
    entry.count = len(items)
    _suppress_set_update = True
    try:
        settings.active_set = set_key
    except Exception:
        pass
    finally:
        _suppress_set_update = False
    settings.slots.clear()
    settings.slot_index = 0
    settings.set_name = set_name
    settings.set_key = set_key
    for map_type, pth in items:
        slot = settings.slots.add()
        slot.map_type = map_type
        slot.filepath = pth
        slot.enabled = True
        slot.udim_tiles = ""
        slot.note = ""


def populate_slots_from_files(context, settings):
    """Re-run detection on the stored files and fill slots for the active set."""
    if _suppress_set_update:
        return
    paths = [f.path for f in settings.files]
    analysis = run_analysis(context, paths)
    wanted = settings.active_set
    chosen = None
    for i, ts in enumerate(analysis.sets):
        if (ts.key or f"__{i}") == wanted:
            chosen = ts
            break
    if chosen is None and analysis.sets:
        chosen = analysis.sets[0]
    _fill_slots(settings, chosen)


def collect_maps(settings):
    """Slots -> ({type: MapInput}, warnings)."""
    maps, warns = {}, []
    for slot in settings.slots:
        if not slot.enabled or slot.map_type == "NONE" or not slot.filepath:
            continue
        mt = detection.MAP_TYPES_BY_ID.get(slot.map_type)
        if mt is None or mt.ignore:
            continue
        path = bpy.path.abspath(slot.filepath)
        if not os.path.isfile(path):
            warns.append(f"Missing file skipped: {slot.filepath}")
            continue
        if slot.map_type in maps:
            warns.append(f"Several slots set to {mt.label}; using the first")
            continue
        maps[slot.map_type] = node_builder.MapInput(path, slot.tiles)
    # Same conflict rules as detection, for slots the user enabled manually.
    if "normal_gl" in maps and "normal_dx" in maps:
        del maps["normal_dx"]
        warns.append("Both OpenGL and DirectX normals enabled; using OpenGL")
    return maps, warns


def build_options(settings):
    o = node_builder.BuildOptions()
    for name in o.__dataclass_fields__:
        if hasattr(settings, name):
            setattr(o, name, getattr(settings, name))
    return o


def target_objects(context, apply_to):
    vl = context.view_layer
    active = vl.objects.active if vl else None
    if apply_to == "SELECTED":
        objs = [o for o in context.selected_objects]
        if active is not None and active not in objs and active.select_get():
            objs.insert(0, active)
        return objs
    return [active] if active is not None else []


def apply_settings(context, settings):
    """Build materials for target objects. Returns (built_count, messages, level)."""
    maps, msgs = collect_maps(settings)
    if not maps:
        return 0, msgs + ["No usable texture slots to apply"], "WARNING"
    objs = target_objects(context, settings.apply_to)
    if not objs:
        return 0, msgs + ["No target object: select a mesh first"], "WARNING"
    opts = build_options(settings)
    set_key = settings.set_key or settings.set_name or "texture_set"
    cache, done = {}, {}
    built = 0
    for obj in objs:
        mat, err = node_builder.get_or_create_material(
            obj, settings.material_mode, settings.set_name, set_key, cache)
        if mat is None:
            msgs.append(err)
            continue
        if mat.name_full in done:
            continue
        res = node_builder.build_material(mat, maps, opts, settings.set_name, set_key)
        done[mat.name_full] = res
        built += 1
        for w in res.warnings:
            if w not in msgs:
                msgs.append(w)
    level = "INFO" if built else "WARNING"
    return built, msgs, level


def _report_all(op, msgs, level="WARNING"):
    for m in msgs:
        op.report({level}, m)


def _safe(op, fn, *args):
    try:
        return fn(*args)
    except Exception as ex:
        traceback.print_exc()
        op.report({"ERROR"}, f"Texture Plugger: {type(ex).__name__}: {ex}")
        return {"CANCELLED"}


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class TEXTUREPLUGGER_OT_load_set(bpy.types.Operator):
    """Pick image files and/or a folder; map types are detected from the filenames"""
    bl_idname = "texture_plugger.load_set"
    bl_label = "Load Texture Set"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"SKIP_SAVE", "HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE", "HIDDEN"})
    filter_image: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_glob: StringProperty(default=_IMAGE_GLOB, options={"HIDDEN", "SKIP_SAVE"})
    apply_after: BoolProperty(name="Apply After Loading", default=True,
                        description="Build the material right away (uses the scene options)")
    recursive: BoolProperty(name="Scan Subfolders", default=False,
                            description="When no files are selected, also scan subfolders")

    def _gather_paths(self):
        directory = bpy.path.abspath(self.directory) if self.directory else ""
        names = [f.name for f in self.files if f.name]
        paths = []
        if names:
            for n in names:
                p = n if os.path.isabs(n) else os.path.join(directory, n)
                if os.path.isdir(p):
                    paths.extend(detection.list_image_files(p, self.recursive))
                else:
                    paths.append(p)
        elif self.filepath and os.path.isfile(bpy.path.abspath(self.filepath)):
            paths.append(bpy.path.abspath(self.filepath))
        elif directory:
            paths = detection.list_image_files(directory, self.recursive)
        # A stale name left in the browser's filename field may not exist here.
        return [p for p in paths
                if p.lower().endswith(detection.IMAGE_EXTENSIONS) and os.path.isfile(p)]

    def invoke(self, context, event):
        try:
            settings = get_settings(context)
            ensure_initialized(context, settings)
            self.apply_after = settings.auto_apply
        except Exception:
            pass
        p = prefs.get_prefs(context)
        if p is not None:
            self.recursive = p.recursive
        # Drag and drop (FileHandler) passes directory + files: skip the browser.
        if self.directory and any(f.name for f in self.files):
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = get_settings(context)
        ensure_initialized(context, settings)
        paths = self._gather_paths()
        if not paths:
            self.report({"WARNING"}, "No image files found in the selection")
            return {"CANCELLED"}
        analysis = run_analysis(context, paths)
        best = store_analysis(context, settings, paths, analysis)
        if best is None:
            self.report({"WARNING"}, f"No known map types recognised in {len(paths)} file(s); "
                                     "assign them manually in the panel")
            return {"FINISHED"}
        for note in best.notes:
            self.report({"INFO"}, note)
        summary = f"'{best.name}': {best.map_count} map(s) detected"
        if len(analysis.sets) > 1:
            summary += f" ({len(analysis.sets)} texture sets found, choose in the panel)"
        if self.apply_after:
            built, msgs, level = apply_settings(context, settings)
            _report_all(self, msgs, "WARNING")
            if built:
                summary += f", applied to {built} material(s)"
        self.report({"INFO"}, summary)
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_apply(bpy.types.Operator):
    """Build the material node network from the slot list"""
    bl_idname = "texture_plugger.apply"
    bl_label = "Apply"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = get_settings(context)
        ensure_initialized(context, settings)
        built, msgs, level = apply_settings(context, settings)
        _report_all(self, msgs, "WARNING")
        if not built:
            return {"CANCELLED"}
        self.report({"INFO"}, f"Texture set applied to {built} material(s)")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_clear(bpy.types.Operator):
    """Remove the nodes Texture Plugger created (your own nodes are left alone)"""
    bl_idname = "texture_plugger.clear"
    bl_label = "Remove Textures"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = get_settings(context)
        objs = target_objects(context, settings.apply_to)
        mats = []
        for obj in objs:
            if not node_builder.object_supports_material(obj):
                continue
            for slot in obj.material_slots:
                m = slot.material
                if m is not None and m not in mats and node_builder.material_is_editable(m):
                    mats.append(m)
        removed = 0
        cleared = 0
        for m in mats:
            if node_builder.material_has_managed(m):
                removed += node_builder.clear_material(m)
                cleared += 1
        if not cleared:
            self.report({"INFO"}, "Nothing to remove")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Removed {removed} node(s) from {cleared} material(s)")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_rescan(bpy.types.Operator):
    """Run detection again on the loaded files (e.g. after editing keywords)"""
    bl_idname = "texture_plugger.rescan"
    bl_label = "Re-detect"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = get_settings(context)
        paths = [f.path for f in settings.files]
        if not paths:
            self.report({"WARNING"}, "No files loaded")
            return {"CANCELLED"}
        analysis = run_analysis(context, paths)
        best = store_analysis(context, settings, paths, analysis)
        self.report({"INFO"}, f"{len(analysis.sets)} set(s); '{best.name if best else '-'}' active")
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_slot_browse(bpy.types.Operator):
    """Pick the image file for this slot"""
    bl_idname = "texture_plugger.slot_browse"
    bl_label = "Browse Slot Image"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: IntProperty(default=-1, options={"SKIP_SAVE"})
    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_image: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_glob: StringProperty(default=_IMAGE_GLOB, options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        settings = get_settings(context)
        idx = self.index if self.index >= 0 else settings.slot_index
        if not (0 <= idx < len(settings.slots)):
            self.report({"WARNING"}, "No slot selected")
            return {"CANCELLED"}
        path = bpy.path.abspath(self.filepath)
        if not os.path.isfile(path):
            self.report({"WARNING"}, "Please pick an image file")
            return {"CANCELLED"}
        slot = settings.slots[idx]
        slot.filepath = path
        slot.udim_tiles = ""
        slot.enabled = True
        slot.note = ""
        if slot.map_type == "NONE":
            p = prefs.get_prefs(context)
            det = detection.classify(path, prefs.get_keywords(p),
                                     normal_default=(p.normal_default if p else "GL"),
                                     allow_udim=False)
            mt = detection.MAP_TYPES_BY_ID.get(det.map_type) if det.map_type else None
            if mt is not None and not mt.internal:
                slot.map_type = det.map_type
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_slot_clear(bpy.types.Operator):
    """Clear this slot's file"""
    bl_idname = "texture_plugger.slot_clear"
    bl_label = "Clear Slot"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: IntProperty(default=-1, options={"SKIP_SAVE"})

    def execute(self, context):
        settings = get_settings(context)
        idx = self.index if self.index >= 0 else settings.slot_index
        if not (0 <= idx < len(settings.slots)):
            return {"CANCELLED"}
        slot = settings.slots[idx]
        slot.filepath = ""
        slot.udim_tiles = ""
        slot.note = ""
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_slot_add(bpy.types.Operator):
    """Add a slot for a map type and pick its file"""
    bl_idname = "texture_plugger.slot_add"
    bl_label = "Add Slot"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "map_type"

    map_type: EnumProperty(name="Map Type", items=properties.SLOT_TYPE_ITEMS[1:])

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        settings = get_settings(context)
        slot = settings.slots.add()
        slot.map_type = self.map_type
        slot.enabled = True
        settings.slot_index = len(settings.slots) - 1
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_slot_remove(bpy.types.Operator):
    """Remove the active slot from the list"""
    bl_idname = "texture_plugger.slot_remove"
    bl_label = "Remove Slot"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_settings(context)
        idx = settings.slot_index
        if not (0 <= idx < len(settings.slots)):
            return {"CANCELLED"}
        settings.slots.remove(idx)
        settings.slot_index = max(0, min(idx, len(settings.slots) - 1))
        return {"FINISHED"}


class TEXTUREPLUGGER_OT_reset_options(bpy.types.Operator):
    """Copy the default options from the add-on preferences into this scene"""
    bl_idname = "texture_plugger.reset_options"
    bl_label = "Reset Options to Preferences"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        p = prefs.get_prefs(context)
        if p is None:
            self.report({"WARNING"}, "Preferences not available")
            return {"CANCELLED"}
        properties.copy_defaults_from_prefs(get_settings(context), p)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Drag and drop
# --------------------------------------------------------------------------

class TEXTUREPLUGGER_FH_drop(bpy.types.FileHandler):
    bl_idname = "TEXTUREPLUGGER_FH_drop"
    bl_label = "Texture Plugger: PBR texture set"
    bl_import_operator = TEXTUREPLUGGER_OT_load_set.bl_idname
    bl_file_extensions = ";".join(detection.IMAGE_EXTENSIONS)

    @classmethod
    def poll_drop(cls, context):
        area = context.area
        if area is None:
            return False
        if area.type == "VIEW_3D":
            return True
        if area.type == "NODE_EDITOR":
            space = context.space_data
            return getattr(space, "tree_type", "") == "ShaderNodeTree"
        return False


def menu_func_import(self, context):
    self.layout.operator(TEXTUREPLUGGER_OT_load_set.bl_idname,
                         text="PBR Texture Set (Texture Plugger)", icon="TEXTURE")


classes = (
    TEXTUREPLUGGER_OT_load_set,
    TEXTUREPLUGGER_OT_apply,
    TEXTUREPLUGGER_OT_clear,
    TEXTUREPLUGGER_OT_rescan,
    TEXTUREPLUGGER_OT_slot_browse,
    TEXTUREPLUGGER_OT_slot_clear,
    TEXTUREPLUGGER_OT_slot_add,
    TEXTUREPLUGGER_OT_slot_remove,
    TEXTUREPLUGGER_OT_reset_options,
    TEXTUREPLUGGER_FH_drop,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    except Exception:
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
