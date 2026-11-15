# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene-level state: detected sets, the editable slot list and current options."""

import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import detection, prefs, scaffold

CREATE_RESOLUTION_ITEMS = (
    ("1024", "1K", "1024 x 1024 placeholders"),
    ("2048", "2K", "2048 x 2048 placeholders"),
    ("4096", "4K", "4096 x 4096 placeholders"),
)

SLOT_TYPE_ITEMS = [("NONE", "Unassigned", "Not used")] + [
    (mt.id, mt.label, mt.description or mt.label) for mt in detection.SLOT_TYPES
]

# Keep dynamic enum strings alive (Blender requirement for callback items).
_set_items_cache = []


def _set_items(self, context):
    global _set_items_cache
    items = []
    for i, s in enumerate(self.sets):
        label = f"{s.name}  ({s.count} maps)"
        items.append((s.key or f"__{i}", label, s.key, i))
    if not items:
        items = [("__none__", "No texture set", "", 0)]
    _set_items_cache = items
    return items


def _active_set_update(self, context):
    # Imported lazily to avoid a circular import.
    from . import operators
    try:
        operators.populate_slots_from_files(context, self)
    except Exception as ex:  # never raise inside an update callback
        print("Texture Plugger: could not switch set:", ex)


class TEXTUREPLUGGER_PG_slot(bpy.types.PropertyGroup):
    map_type: EnumProperty(name="Map Type", items=SLOT_TYPE_ITEMS, default="NONE")
    filepath: StringProperty(name="File", subtype="FILE_PATH")
    enabled: BoolProperty(name="Use", default=True)
    udim_tiles: StringProperty(name="UDIM Tiles", default="")
    note: StringProperty(name="Note", default="")

    @property
    def tiles(self):
        out = []
        for t in self.udim_tiles.split(","):
            t = t.strip()
            if t.isdigit():
                out.append(int(t))
        return tuple(out)

    @property
    def basename(self):
        return os.path.basename(bpy.path.abspath(self.filepath)) if self.filepath else ""


class TEXTUREPLUGGER_PG_file(bpy.types.PropertyGroup):
    path: StringProperty(name="Path", subtype="FILE_PATH")


class TEXTUREPLUGGER_PG_set(bpy.types.PropertyGroup):
    key: StringProperty()
    # "name" is inherited from PropertyGroup
    count: IntProperty()


class TEXTUREPLUGGER_PG_settings(bpy.types.PropertyGroup):
    slots: CollectionProperty(type=TEXTUREPLUGGER_PG_slot)
    slot_index: IntProperty(name="Active Slot", default=0)
    files: CollectionProperty(type=TEXTUREPLUGGER_PG_file)
    sets: CollectionProperty(type=TEXTUREPLUGGER_PG_set)
    active_set: EnumProperty(name="Texture Set", items=_set_items, update=_active_set_update)
    set_name: StringProperty(name="Set Name", default="")
    set_key: StringProperty(name="Set Key", default="")
    initialized: BoolProperty(default=False)
    show_options: BoolProperty(name="Options", default=False)

    # Create & Paint (Substance Painter round trip)
    create_root: StringProperty(
        name="Folder", subtype="DIR_PATH", default="",
        description="Parent folder; a subfolder per texture set is created inside it")
    create_name: StringProperty(
        name="Name", default="",
        description="Texture set and material name (empty = use the material or object name)")
    create_resolution: EnumProperty(
        name="Size", items=CREATE_RESOLUTION_ITEMS, default="1024",
        description="Placeholder size; Painter overwrites the files at its own export size")
    scene_unwrap: BoolProperty(
        name="Unwrap Objects Without UVs", default=True,
        description="Create Sets for Scene first unwraps every object that has no UVs yet with "
                    "the Smart Seams add-on (uses its settings). Objects that already have UVs "
                    "are never changed")


for _name, _prop in prefs.option_annotations().items():
    TEXTUREPLUGGER_PG_settings.__annotations__[_name] = _prop
for _spec in scaffold.MAPS:
    TEXTUREPLUGGER_PG_settings.__annotations__["create_" + _spec.id] = BoolProperty(
        name=_spec.label, default=_spec.default,
        description=f"Create a {_spec.label} placeholder ({_spec.suffix})")


def copy_defaults_from_prefs(settings, p):
    if p is None:
        return
    for name in prefs.OPTION_NAMES:
        try:
            setattr(settings, name, getattr(p, name))
        except Exception:
            pass
    settings.initialized = True


classes = (
    TEXTUREPLUGGER_PG_slot,
    TEXTUREPLUGGER_PG_file,
    TEXTUREPLUGGER_PG_set,
    TEXTUREPLUGGER_PG_settings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.texture_plugger = PointerProperty(type=TEXTUREPLUGGER_PG_settings)


def unregister():
    try:
        del bpy.types.Scene.texture_plugger
    except AttributeError:
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
