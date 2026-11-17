# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture Plugger: hook PBR texture sets up to a Principled BSDF material.

Packaged as a Blender Extension (see blender_manifest.toml).  ``bl_info`` is
kept only so the same folder also works as a legacy add-on; Blender ignores it
for extensions.
"""

bl_info = {
    "name": "Texture Plugger",
    "author": "Texture Plugger contributors",
    "version": (1, 6, 4),
    "blender": (4, 5, 0),
    "location": "3D Viewport / Shader Editor > Sidebar > Texture Plugger; File > Import",
    "description": "Detect PBR texture maps from filenames and wire them into a Principled BSDF",
    "category": "Material",
}

if "bpy" in locals():
    import importlib
    for _mod in (detection, scaffold, painter_link, prefs, properties, node_builder,  # noqa: F821
                 operators, workflow, unlit, scene_link, light_bake, scene_batch, ui):  # noqa: F821
        importlib.reload(_mod)
else:
    from . import (detection, scaffold, painter_link, prefs, properties, node_builder,
                   operators, workflow, unlit, scene_link, light_bake, scene_batch, ui)

import bpy  # noqa: E402

_modules = (prefs, properties, operators, workflow, unlit, scene_link, light_bake, scene_batch, ui)


def register():
    for mod in _modules:
        mod.register()


def unregister():
    for mod in reversed(_modules):
        try:
            mod.unregister()
        except Exception as ex:  # keep unregistering the rest
            print("Texture Plugger: unregister error in", mod.__name__, ex)
