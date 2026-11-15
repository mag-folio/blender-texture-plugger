# SPDX-License-Identifier: GPL-3.0-or-later
"""Sidebar panels for the 3D Viewport and the Shader Editor."""

import bpy

from . import detection, node_builder, prefs, scaffold, workflow

CATEGORY = "Texture Plugger"


class TEXTUREPLUGGER_UL_slots(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        sub = row.row(align=True)
        sub.active = item.enabled
        sub.prop(item, "map_type", text="")
        name = item.basename or "<no file>"
        if item.udim_tiles:
            name += "  [UDIM]"
        sub.label(text=name, icon="IMAGE_DATA" if item.filepath else "BLANK1")
        op = row.operator("texture_plugger.slot_browse", text="", icon="FILEBROWSER")
        op.index = index
        op = row.operator("texture_plugger.slot_clear", text="", icon="X")
        op.index = index

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = [self.bitflag_filter_item] * len(items)
        if self.filter_name:
            needle = self.filter_name.lower()
            for i, it in enumerate(items):
                hay = (it.basename + " " + it.map_type).lower()
                if needle not in hay:
                    flt_flags[i] = 0
        return flt_flags, []


class _PanelBase:
    bl_region_type = "UI"
    bl_category = CATEGORY


def _draw_main(panel, context):
    layout = panel.layout
    scene = context.scene
    settings = getattr(scene, "texture_plugger", None)
    if settings is None:
        layout.label(text="Add-on not initialised", icon="ERROR")
        return

    obj = context.view_layer.objects.active if context.view_layer else None
    col = layout.column(align=True)
    col.scale_y = 1.3
    col.operator("texture_plugger.load_set", icon="FILE_FOLDER")

    if obj is None:
        layout.label(text="No active object", icon="INFO")
    elif not node_builder.object_supports_material(obj):
        layout.label(text=f"'{obj.name}' cannot have materials", icon="ERROR")
    else:
        mat = obj.active_material
        layout.label(text=f"{obj.name}  >  {mat.name if mat else '(new material)'}",
                     icon="MATERIAL")

    if len(settings.sets) > 1:
        layout.prop(settings, "active_set", text="Set")
    elif settings.set_name:
        layout.label(text=f"Set: {settings.set_name}", icon="TEXTURE")

    row = layout.row()
    row.template_list("TEXTUREPLUGGER_UL_slots", "", settings, "slots", settings, "slot_index",
                      rows=4)
    side = row.column(align=True)
    side.operator("texture_plugger.slot_add", text="", icon="ADD")
    side.operator("texture_plugger.slot_remove", text="", icon="REMOVE")
    side.separator()
    side.operator("texture_plugger.rescan", text="", icon="FILE_REFRESH")

    if 0 <= settings.slot_index < len(settings.slots):
        slot = settings.slots[settings.slot_index]
        if slot.note:
            layout.label(text=slot.note, icon="INFO")
        mt = detection.MAP_TYPES_BY_ID.get(slot.map_type)
        if mt is not None and mt.description:
            layout.label(text=mt.description)

    row = layout.row(align=True)
    row.scale_y = 1.3
    row.operator("texture_plugger.apply", icon="CHECKMARK")
    row.operator("texture_plugger.clear", icon="TRASH")
    layout.prop(settings, "apply_to", expand=True)


def _draw_options(panel, context):
    layout = panel.layout
    settings = getattr(context.scene, "texture_plugger", None)
    if settings is None:
        return
    layout.use_property_split = True
    layout.use_property_decorate = False
    col = layout.column()
    col.prop(settings, "material_mode")
    col.prop(settings, "auto_apply")
    col.separator()
    col.prop(settings, "projection")
    if settings.projection == "BOX":
        col.prop(settings, "box_blend")
    col.prop(settings, "texture_scale")
    col.separator()
    col.prop(settings, "ao_multiply")
    sub = col.column()
    sub.active = settings.ao_multiply
    sub.prop(settings, "ao_strength")
    col.prop(settings, "normal_strength")
    col.prop(settings, "height_mode")
    if settings.height_mode == "DISPLACEMENT":
        col.prop(settings, "disp_scale")
        col.prop(settings, "disp_midlevel")
    col.prop(settings, "bump_strength")
    col.prop(settings, "bump_distance")
    col.separator()
    col.prop(settings, "alpha_mode")
    col.prop(settings, "use_base_alpha")
    col.prop(settings, "emission_strength")
    col.prop(settings, "relative_paths")
    layout.operator("texture_plugger.reset_options", icon="LOOP_BACK")


def _draw_paint(panel, context):
    layout = panel.layout
    settings = getattr(context.scene, "texture_plugger", None)
    if settings is None:
        return
    col = layout.column()
    col.prop(settings, "create_root")
    col.prop(settings, "create_name")
    grid = col.grid_flow(columns=2, even_columns=True, align=True)
    for spec in scaffold.MAPS:
        grid.prop(settings, "create_" + spec.id)
    col.prop(settings, "create_resolution")
    sub = col.column()
    sub.scale_y = 1.4
    sub.operator("texture_plugger.create_set", icon="NEWFOLDER")

    active = context.view_layer.objects.active if context.view_layer else None
    obj = workflow._active_object(context)
    mat = obj.active_material if node_builder.object_supports_material(obj) else None
    folder = workflow.material_project(mat)
    layout.separator()
    if obj is not None and obj != active:
        layout.label(text=f"Brushstrokes on: {obj.name}", icon="OBJECT_DATA")
    if folder:
        layout.label(text=f"Set: {mat.name}", icon="TEXTURE")
    row = layout.row()
    row.scale_y = 1.2
    row.enabled = bool(folder)
    row.operator("texture_plugger.edit_in_painter", icon="TPAINT_HLT")
    row = layout.row(align=True)
    row.operator("texture_plugger.reload_images", icon="FILE_REFRESH")
    sub = row.row(align=True)
    sub.enabled = bool(folder)
    sub.operator("texture_plugger.open_set_folder", icon="FILE_FOLDER")
    row = layout.row()
    row.enabled = bool(folder)
    row.operator("texture_plugger.unlink_set", icon="UNLINKED")
    p = prefs.get_prefs(context)
    if p is not None:
        layout.prop(p, "auto_reload")

    box = layout.box()
    box.label(text="Whole Scene (one material per object)", icon="SCENE_DATA")
    row = box.row(align=True)
    row.scale_y = 1.3
    op = row.operator("texture_plugger.create_scene_sets", text="Selected", icon="RESTRICT_SELECT_OFF")
    op.scope = "SELECTED"
    op = row.operator("texture_plugger.create_scene_sets", text="All Visible", icon="HIDE_OFF")
    op.scope = "VISIBLE"
    box.operator("texture_plugger.edit_scene_in_painter", icon="TPAINT_HLT")

    box = layout.box()
    box.label(text="Painter Link: lighting, solo, view", icon="LIGHT_SUN")
    row = box.row(align=True)
    row.scale_y = 1.3
    row.enabled = obj is not None and obj.type == "MESH"
    row.operator("texture_plugger.send_to_painter", icon="EXPORT")
    op = row.operator("texture_plugger.send_to_painter", text="", icon="VIEW_CAMERA")
    op.lighting = False
    if p is not None:
        col = box.column(align=True)
        col.prop(p, "send_on_launch", text="Send when opening Painter")
        col.prop(p, "env_surroundings")


def _shader_poll(context):
    space = context.space_data
    return getattr(space, "tree_type", "") == "ShaderNodeTree"


class TEXTUREPLUGGER_PT_view3d(_PanelBase, bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_label = "Texture Plugger"
    bl_idname = "TEXTUREPLUGGER_PT_view3d"

    def draw(self, context):
        _draw_main(self, context)


class TEXTUREPLUGGER_PT_view3d_paint(_PanelBase, bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_label = "Create & Paint"
    bl_parent_id = "TEXTUREPLUGGER_PT_view3d"

    def draw(self, context):
        _draw_paint(self, context)


class TEXTUREPLUGGER_PT_view3d_options(_PanelBase, bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_label = "Options"
    bl_parent_id = "TEXTUREPLUGGER_PT_view3d"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        _draw_options(self, context)


class TEXTUREPLUGGER_PT_shader(_PanelBase, bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_label = "Texture Plugger"
    bl_idname = "TEXTUREPLUGGER_PT_shader"

    @classmethod
    def poll(cls, context):
        return _shader_poll(context)

    def draw(self, context):
        _draw_main(self, context)


class TEXTUREPLUGGER_PT_shader_paint(_PanelBase, bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_label = "Create & Paint"
    bl_parent_id = "TEXTUREPLUGGER_PT_shader"

    @classmethod
    def poll(cls, context):
        return _shader_poll(context)

    def draw(self, context):
        _draw_paint(self, context)


class TEXTUREPLUGGER_PT_shader_options(_PanelBase, bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_label = "Options"
    bl_parent_id = "TEXTUREPLUGGER_PT_shader"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return _shader_poll(context)

    def draw(self, context):
        _draw_options(self, context)


classes = (
    TEXTUREPLUGGER_UL_slots,
    TEXTUREPLUGGER_PT_view3d,
    TEXTUREPLUGGER_PT_view3d_paint,
    TEXTUREPLUGGER_PT_view3d_options,
    TEXTUREPLUGGER_PT_shader,
    TEXTUREPLUGGER_PT_shader_paint,
    TEXTUREPLUGGER_PT_shader_options,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
