# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-on preferences: default options, format priority, editable keyword lists."""

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty)

from . import detection

ADDON_ID = __package__

APPLY_TO_ITEMS = (
    ("ACTIVE", "Active Object", "Apply to the active object only"),
    ("SELECTED", "Selected Objects", "Apply to all selected objects"),
)
MATERIAL_MODE_ITEMS = (
    ("ACTIVE_SLOT", "Active Slot", "Use the material in the active slot (create one if empty)"),
    ("NEW", "New Material", "Create a new material named after the texture set "
                            "(re-applying reuses the one created before)"),
)
PROJECTION_ITEMS = (
    ("UV", "UV", "Use the UV map"),
    ("BOX", "Box (Triplanar)", "Box projection from object coordinates, no UVs needed"),
)
HEIGHT_MODE_ITEMS = (
    ("DISPLACEMENT", "Displacement", "Displacement node to Material Output (method: Displacement and Bump)"),
    ("BUMP", "Bump", "Use the height map as bump (when there is no dedicated bump map)"),
    ("NONE", "Not Connected", "Load the height map but do not connect it"),
)
ALPHA_MODE_ITEMS = (
    ("DITHERED", "Dithered", "EEVEE dithered transparency (fast, sorts correctly)"),
    ("BLENDED", "Blended", "EEVEE blended transparency (smooth, may sort incorrectly)"),
)
NORMAL_DEFAULT_ITEMS = (
    ("GL", "OpenGL", "Normal maps without GL/DX tokens are OpenGL (Blender's convention)"),
    ("DX", "DirectX", "Normal maps without GL/DX tokens are DirectX (green flipped)"),
)
RESOLUTION_ITEMS = (
    ("HIGHEST", "Highest", "Prefer the highest resolution available"),
    ("LOWEST", "Lowest", "Prefer the lowest resolution available"),
    ("1024", "1K", "Prefer the resolution closest to 1K"),
    ("2048", "2K", "Prefer the resolution closest to 2K"),
    ("4096", "4K", "Prefer the resolution closest to 4K"),
    ("8192", "8K", "Prefer the resolution closest to 8K"),
)


def option_annotations():
    """Build options shared by the preferences (defaults) and the scene settings."""
    return {
        "apply_to": EnumProperty(name="Apply To", items=APPLY_TO_ITEMS, default="ACTIVE"),
        "material_mode": EnumProperty(name="Material", items=MATERIAL_MODE_ITEMS, default="ACTIVE_SLOT"),
        "ao_multiply": BoolProperty(name="Multiply AO into Base Color", default=True),
        "ao_strength": FloatProperty(name="AO Strength", default=1.0, min=0.0, max=1.0, subtype="FACTOR"),
        "projection": EnumProperty(name="Projection", items=PROJECTION_ITEMS, default="UV"),
        "box_blend": FloatProperty(name="Box Blend", default=0.2, min=0.0, max=1.0, subtype="FACTOR"),
        "texture_scale": FloatProperty(name="Texture Scale", default=1.0, min=0.0001, soft_max=100.0,
                                       description="Uniform scale on the shared Mapping node"),
        "height_mode": EnumProperty(name="Height Map", items=HEIGHT_MODE_ITEMS, default="DISPLACEMENT"),
        "disp_scale": FloatProperty(name="Displacement Scale", default=0.1, min=0.0, soft_max=10.0),
        "disp_midlevel": FloatProperty(name="Midlevel", default=0.5, soft_min=0.0, soft_max=1.0),
        "normal_strength": FloatProperty(name="Normal Strength", default=1.0, min=0.0, soft_max=10.0),
        "bump_strength": FloatProperty(name="Bump Strength", default=0.5, min=0.0, max=1.0, subtype="FACTOR"),
        "bump_distance": FloatProperty(name="Bump Distance", default=0.1, min=0.0, soft_max=10.0),
        "alpha_mode": EnumProperty(name="Transparency", items=ALPHA_MODE_ITEMS, default="DITHERED"),
        "use_base_alpha": BoolProperty(
            name="Base Color Alpha as Opacity", default=False,
            description="Use the base colour image's alpha channel when there is no opacity map"),
        "emission_strength": FloatProperty(name="Emission Strength", default=1.0, min=0.0, soft_max=100.0),
        "relative_paths": BoolProperty(name="Relative Paths", default=False,
                                       description="Store image paths relative to the .blend file"),
        "auto_apply": BoolProperty(name="Apply After Loading", default=True,
                                   description="Build the material right after loading a texture set"),
    }


OPTION_NAMES = tuple(option_annotations().keys())


def kw_prop_name(type_id):
    return "kw_" + type_id


class TEXTUREPLUGGER_OT_reset_keywords(bpy.types.Operator):
    """Reset all keyword lists to the built-in defaults"""
    bl_idname = "texture_plugger.reset_keywords"
    bl_label = "Reset Keywords to Defaults"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        prefs = get_prefs(context)
        if prefs is None:
            self.report({"ERROR"}, "Add-on preferences not available")
            return {"CANCELLED"}
        for mt in detection.MAP_TYPES:
            setattr(prefs, kw_prop_name(mt.id), ", ".join(mt.keywords))
        prefs.format_priority = detection.DEFAULT_FORMAT_PRIORITY
        prefs.data_format_priority = detection.DEFAULT_DATA_FORMAT_PRIORITY
        self.report({"INFO"}, "Keyword lists reset to defaults")
        return {"FINISHED"}


class TexturePluggerPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    normal_default: EnumProperty(name="Untagged Normal Maps", items=NORMAL_DEFAULT_ITEMS, default="GL")
    resolution_pref: EnumProperty(name="Preferred Resolution", items=RESOLUTION_ITEMS, default="HIGHEST")
    format_priority: StringProperty(
        name="Format Priority (colour/other)", default=detection.DEFAULT_FORMAT_PRIORITY,
        description="Comma separated, first wins when the same map exists in several formats")
    data_format_priority: StringProperty(
        name="Format Priority (normal/height)", default=detection.DEFAULT_DATA_FORMAT_PRIORITY,
        description="Comma separated priority for normal, bump and height maps")
    recursive: BoolProperty(name="Scan Subfolders", default=False,
                            description="When a folder is picked, also scan its subfolders")
    show_keywords: BoolProperty(name="Show Keyword Lists", default=False)
    painter_path: StringProperty(
        name="Substance Painter", subtype="FILE_PATH", default="",
        description="Adobe Substance 3D Painter executable (leave empty to auto-detect)")
    auto_reload: BoolProperty(
        name="Auto Reload Textures", default=True,
        description="Reload plugged-in images when their files change on disk")
    reload_interval: FloatProperty(
        name="Check Interval", default=1.0, min=0.25, max=60.0,
        description="Seconds between checks for changed texture files")
    send_on_launch: BoolProperty(
        name="Send Lighting When Opening Painter", default=True,
        description="Edit in Substance Painter also bakes and sends the lighting, object "
                    "bounds and view")
    env_resolution: EnumProperty(
        name="Lighting Map Size", default="1024",
        items=(("512", "512 x 256", "Fast"), ("1024", "1024 x 512", "Balanced"),
               ("2048", "2048 x 1024", "Sharper reflections, slower")),
        description="Width of the baked environment map")
    env_samples: IntProperty(
        name="Lighting Samples", default=64, min=4, max=4096,
        description="Cycles samples for the lighting bake (denoised)")
    light_min_size: FloatProperty(
        name="Min Lamp Size", default=2.0, min=0.25, max=25.0,
        description="Smallest apparent size (degrees) a lamp gets in the map. Larger is "
                    "softer and less noisy in Painter")
    max_scene_sets: IntProperty(
        name="Max Texture Sets per Painter Project", default=0, min=0, max=5000,
        description="0 = automatic: as many as this computer's memory and graphics memory "
                    "can hold, so a whole scene opens as one Painter project when it fits. "
                    "A number splits bigger scenes into parts of at most that many sets")
    memory_budget: IntProperty(
        name="Texture Memory Budget (%)", default=40, min=5, max=75, subtype="PERCENTAGE",
        description="Share of the computer's memory the painted textures may use once "
                    "Blender shows them; the texture size is lowered to stay within it")
    env_surroundings: BoolProperty(
        name="Include Surroundings", default=True,
        description="Bake the rest of the scene too (bounce light, reflections, shadows "
                    "from other objects). Off: only the world and the lamps")

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        box = col.box()
        box.label(text="Default Options (copied to a scene on first load)", icon="PREFERENCES")
        grid = box.grid_flow(columns=2, even_columns=True)
        for name in OPTION_NAMES:
            grid.prop(self, name)

        box = col.box()
        box.label(text="Detection", icon="VIEWZOOM")
        box.prop(self, "normal_default")
        box.prop(self, "resolution_pref")
        box.prop(self, "format_priority")
        box.prop(self, "data_format_priority")
        box.prop(self, "recursive")

        box = col.box()
        box.label(text="Substance Painter", icon="TPAINT_HLT")
        box.prop(self, "painter_path")
        row = box.row()
        row.prop(self, "auto_reload")
        sub = row.row()
        sub.active = self.auto_reload
        sub.prop(self, "reload_interval")
        row = box.row()
        row.prop(self, "max_scene_sets")
        row.prop(self, "memory_budget")
        box.prop(self, "send_on_launch")
        grid = box.grid_flow(columns=2, even_columns=True)
        grid.prop(self, "env_resolution")
        grid.prop(self, "env_samples")
        grid.prop(self, "light_min_size")
        grid.prop(self, "env_surroundings")
        box.operator("texture_plugger.install_painter_plugin", icon="IMPORT")

        box = col.box()
        row = box.row()
        row.prop(self, "show_keywords", icon="TRIA_DOWN" if self.show_keywords else "TRIA_RIGHT",
                 emboss=False)
        row.operator(TEXTUREPLUGGER_OT_reset_keywords.bl_idname, icon="LOOP_BACK")
        if self.show_keywords:
            box.label(text="Comma separated; matched against whole filename tokens "
                           "(case-insensitive, separators ignored).")
            for mt in detection.MAP_TYPES:
                box.prop(self, kw_prop_name(mt.id), text=mt.label)


# Inject per-option defaults and keyword lists into the preferences class.
for _name, _prop in option_annotations().items():
    TexturePluggerPreferences.__annotations__[_name] = _prop
for _mt in detection.MAP_TYPES:
    TexturePluggerPreferences.__annotations__[kw_prop_name(_mt.id)] = StringProperty(
        name=_mt.label, default=", ".join(_mt.keywords), description=_mt.description)


def get_prefs(context=None):
    context = context or bpy.context
    try:
        addon = context.preferences.addons.get(ADDON_ID)
        return addon.preferences if addon is not None else None
    except Exception:
        return None


def get_keywords(prefs):
    if prefs is None:
        return dict(detection.DEFAULT_KEYWORDS)
    out = {}
    for mt in detection.MAP_TYPES:
        text = getattr(prefs, kw_prop_name(mt.id), None)
        out[mt.id] = detection.parse_keyword_list(text) if text is not None else mt.keywords
    return out


def detection_kwargs(prefs):
    if prefs is None:
        return {}
    return {
        "normal_default": prefs.normal_default,
        "format_priority": prefs.format_priority,
        "data_format_priority": prefs.data_format_priority,
        "resolution_pref": prefs.resolution_pref,
    }


classes = (TEXTUREPLUGGER_OT_reset_keywords, TexturePluggerPreferences)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
