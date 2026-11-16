# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake Blender's lighting onto each texture set's UVs with Cycles ("lightmaps").

The environment map sent to Painter is the light seen from one point, so it
cannot carry shadows that other objects cast across a surface (measured with
``tests/lighting_match_diag.py``: 1.9-2.4x too bright in those shadows).  A
Cycles bake of the Diffuse pass (Direct + Indirect, without colour) on the
object's own UVs has them all: cast and contact shadows, bounce light, every
lamp.  Painter's Bake Lighting multiplies it over the Base Color, so the lit
colour is exactly Cycles' diffuse result: albedo x lightmap.

The bake runs in the user's scene with everything restored afterwards: render
engine and samples, bake settings, world and lamps (Material Preview variant),
selection and mode, and the materials (a white Diffuse BSDF and the target
image are plugged in for the bake only).  Materials shown unlit are switched
back to lit for the bake, so they don't glow into it.
"""

import os
import time

import bpy
from bpy.props import BoolProperty

from . import node_builder, painter_link, prefs, scene_link, unlit
from .node_builder import TAG
from .operators import _safe

BAKE_ROLE = "lightbake"
MARGIN = 16


def bakeable_materials(obj):
    """Texture Plugger materials on ``obj`` (one lightmap per material = texture set)."""
    out = []
    for slot in getattr(obj, "material_slots", ()):
        m = slot.material
        if m is not None and m not in out and m.node_tree is not None and m.get(unlit.PROJECT_KEY):
            out.append(m)
    return out


def set_size(mat, default=1024):
    """Lightmap size: the Base Color texture's size (the texture set resolution)."""
    node = node_builder.base_color_node(mat.node_tree) if mat.node_tree else None
    try:
        if node is not None and node.image.source == "FILE":
            w, h = node.image.size
            if w > 0 and h > 0:
                return int(max(w, h))
    except Exception:
        pass
    return default


class _Setup:
    """Puts the scene in bake state and back (always, also on errors)."""

    def __init__(self, context, objs, target, image, samples, world=None, lamps=True):
        self.context, self.objs, self.target, self.image = context, list(objs), target, image
        self.obj = self.objs[0]
        self.samples, self.world, self.lamps = samples, world, lamps
        self.undo, self.unlit, self.mats, self.scratch = [], [], [], None
        self.prev_sel, self.prev_active, self.mode = None, None, "OBJECT"

    def __enter__(self):
        try:
            return self._enter()
        except BaseException:
            self.__exit__(None, None, None)      # put back whatever was changed
            raise

    def _keep(self, owner, attr):
        try:
            self.undo.append((owner, attr, getattr(owner, attr)))
        except AttributeError:
            pass

    def _enter(self):
        ctx, sc = self.context, self.context.scene
        vl = ctx.view_layer
        for owner, attr in ((sc.render, "engine"), (sc.cycles, "samples"),
                            (sc.cycles, "use_denoising"), (sc, "world"),
                            (sc.render.bake, "margin"), (sc.render.bake, "use_clear"),
                            (sc.render.bake, "use_selected_to_active"), (sc.render.bake, "target")):
            self._keep(owner, attr)
        try:
            self._keep(sc.render.bake, "margin_type")
        except Exception:
            pass
        sc.render.engine = "CYCLES"
        sc.cycles.samples = int(self.samples)
        b = sc.render.bake
        b.margin = MARGIN
        b.use_clear = True
        b.use_selected_to_active = False
        b.target = "IMAGE_TEXTURES"
        try:
            b.margin_type = "EXTEND"
        except Exception:
            pass
        if self.world is not None:
            sc.world = self.world
        if not self.lamps:
            for ob in sc.objects:
                if ob.type == "LIGHT" and not ob.hide_render:
                    self._keep(ob, "hide_render")
                    ob.hide_render = True
        # unlit (emission) sets would glow into the bake: light them for now
        for m in bpy.data.materials:
            if m.library is None and node_builder.is_unlit(m):
                node_builder.set_unlit(m, False)
                self.unlit.append(m)
        # materials: diffuse + the image to bake into, active
        for ob in self.objs:
            for slot in ob.material_slots:
                m = slot.material
                if m is None or m.node_tree is None or any(m == x[0] for x in self.mats):
                    continue
                img = self.image if m == self.target else self._scratch()
                # a set exported with its lighting has albedo x light in its Base
                # Color: never use that as albedo (shadows would divide by ~0)
                lit = any(m == u for u in self.unlit) or unlit.wants_unlit(m)
                self.mats.append((m,) + self._plug(m, img, flat=lit))
        # selection and mode
        self.mode = self.obj.mode
        self.prev_active = vl.objects.active
        self.prev_sel = [o for o in vl.objects if o.select_get()]
        if ctx.object is not None and ctx.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for o in self.prev_sel:
            o.select_set(False)
        for o in self.objs:          # every object of the texture set: one shared image
            o.select_set(True)
        vl.objects.active = self.obj
        return self

    def _scratch(self):
        if self.scratch is None:
            self.scratch = bpy.data.images.new("TP_LightBakeScratch", 16, 16, float_buffer=True)
        return self.scratch

    @staticmethod
    def _plug(mat, img, flat=False):
        nt = mat.node_tree
        out = node_builder.find_output(nt)
        made_out = out is None
        if made_out:
            out = nt.nodes.new("ShaderNodeOutputMaterial")
            out[TAG] = BAKE_ROLE
        surf = node_builder.find_socket(out.inputs, ("Surface",))
        prev = surf.links[0].from_socket if surf.is_linked else None
        # A plain Diffuse BSDF with the material's own Base Color: the bake divides
        # the colour out again, but light bouncing off the object (onto itself and
        # back from its surroundings) keeps the real albedo.  A white stand-in made
        # shadowed areas 1.7x too bright (measured).  No metal/specular: the
        # lightmap is the diffuse light, which a metallic surface would zero.
        diff = nt.nodes.new("ShaderNodeBsdfDiffuse")
        diff[TAG] = BAKE_ROLE
        diff.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        bsdf = node_builder.find_principled(nt, out)
        base = node_builder.bsdf_input(bsdf, "base_color") if bsdf is not None else None
        if flat:
            # its real albedo isn't in Blender any more: bounce off it is approximate
            diff.inputs["Color"].default_value = (0.5, 0.5, 0.5, 1.0)
        elif base is not None:
            if base.is_linked:
                nt.links.new(base.links[0].from_socket, diff.inputs["Color"])
            else:
                diff.inputs["Color"].default_value = tuple(base.default_value)
        nt.links.new(diff.outputs[0], surf)
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex[TAG] = BAKE_ROLE
        tex.image = img
        prev_active = nt.nodes.active
        nt.nodes.active = tex
        return prev, prev_active, made_out

    def __exit__(self, *exc):
        ctx, vl = self.context, self.context.view_layer
        for m, prev, prev_active, _made in self.mats:
            nt = m.node_tree
            for n in [n for n in nt.nodes if n.get(TAG) == BAKE_ROLE]:
                nt.nodes.remove(n)
            out = node_builder.find_output(nt)
            surf = node_builder.find_socket(out.inputs, ("Surface",)) if out is not None else None
            if prev is not None and surf is not None:
                try:
                    nt.links.new(prev, surf)
                except Exception:
                    pass
            try:
                nt.nodes.active = prev_active
            except Exception:
                pass
        if self.scratch is not None:
            bpy.data.images.remove(self.scratch)
        for m in self.unlit:
            node_builder.set_unlit(m, True)
        for owner, attr, value in reversed(self.undo):
            try:
                setattr(owner, attr, value)
            except Exception:
                pass
        if self.prev_sel is None:
            return False
        for o in vl.objects:
            try:
                o.select_set(o in self.prev_sel)
            except RuntimeError:
                pass
        try:
            vl.objects.active = self.prev_active
            if self.mode != "OBJECT" and self.prev_active == self.obj:
                bpy.ops.object.mode_set(mode=self.mode)
        except Exception:
            pass
        return False


def bake_lightmap(context, objs, mat, out_path, size, samples, world=None, lamps=True):
    """Bake the light ``objs`` receive onto their UVs (faces using ``mat``) into an EXR.

    Values are Cycles' Diffuse Direct + Indirect without colour, times the
    scene's own exposure: albedo x value is the diffuse colour a Cycles render
    shows.  ``world`` replaces the scene world (Material Preview) and
    ``lamps=False`` leaves the lamps out.
    """
    import numpy as np
    if not isinstance(objs, (list, tuple)):
        objs = [objs]
    for obj in objs:
        if obj.type != "MESH" or not obj.data.uv_layers:
            raise RuntimeError(f"'{obj.name}' has no UV map to bake the lighting onto")
    size = int(max(16, size))
    img = bpy.data.images.new("TP_Lightmap", size, size, alpha=True, float_buffer=True)
    try:
        with _Setup(context, objs, mat, img, samples, world, lamps):
            bpy.ops.object.bake(type="DIFFUSE", pass_filter={"DIRECT", "INDIRECT"},
                                margin=MARGIN, use_clear=True, target="IMAGE_TEXTURES")
        px = np.empty(size * size * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(size, size, 4)
        px[..., :3] *= 2.0 ** float(unlit.user_view(context.scene)["exposure"])
        px[..., 3] = 1.0
        px[~np.isfinite(px)] = 0.0
        try:     # raw linear values; set BEFORE writing them: changing it regenerates
            img.colorspace_settings.is_data = True       # the image (black)
        except (AttributeError, TypeError):
            pass
        img.pixels.foreach_set(px.ravel())
        img.filepath_raw = out_path
        img.file_format = "OPEN_EXR"
        img.save()
        lit = px[..., :3].mean(axis=-1)
        return {"path": out_path, "size": size, "mean": float(lit.mean()), "max": float(lit.max())}
    finally:
        bpy.data.images.remove(img)


def bake_set(context, objs, mat, folder, size=None, samples=128, preview=False):
    """Render (and Cycles-with-studio-HDRI) lightmaps of one texture set into ``folder``.

    ``objs``: every object that uses ``mat`` (they share the texture set's UVs).
    Returns the lightmaps entry: {"render": file, "preview": file or None, ...};
    ``lighting_key`` and ``time`` let Painter tell when the lighting changed since.
    """
    if not isinstance(objs, (list, tuple)):
        objs = [objs]
    size = size or set_size(mat)
    stamp = time.time() * 1000
    p = prefs.get_prefs(context)
    try:
        key = scene_link.lighting_key(context, bool(getattr(p, "env_surroundings", True)) if p else True)
    except Exception:
        key = None
    entry = {"size": size, "objects": [o.name for o in objs], "time": stamp / 1000.0,
             "lighting_key": key}
    name = painter_link.lightmap_name(mat.name, "render", stamp)
    info = bake_lightmap(context, objs, mat, os.path.join(folder, name), size, samples)
    entry["render"] = name
    entry["max"] = round(info["max"], 4)
    entry["preview"] = None
    if preview:
        st = scene_link.preview_settings(context)
        world, image = None, None
        if not st["scene_world"]:
            if not st["hdri"] or not os.path.isfile(st["hdri"]):
                return entry
            had = {im.name_full for im in bpy.data.images}
            world, image = scene_link._lookdev_world(st)
        try:
            name = painter_link.lightmap_name(mat.name, "preview", stamp)
            bake_lightmap(context, objs, mat, os.path.join(folder, name), size,
                          max(16, samples // 2), world=world, lamps=st["scene_lights"])
            entry["preview"] = name
        finally:
            if world is not None:
                bpy.data.worlds.remove(world)
                if image is not None and image.name_full not in had and image.users == 0:
                    bpy.data.images.remove(image)
    return entry


def set_users(context, mat):
    """Meshes in the scene that render and use ``mat``: one texture set in Painter."""
    vl = context.view_layer
    rendered = scene_link._render_collections(vl)
    return [o for o in context.scene.objects
            if o.type == "MESH" and scene_link._render_visible(o, vl, rendered)
            and any(s.material == mat for s in o.material_slots)]


def jobs_for(context):
    """([objects], material) per texture set on the selected meshes (or the active
    one).  All objects using a set are baked together, so none comes out black."""
    from . import workflow
    objs = [o for o in context.selected_objects if o.type == "MESH"]
    act = workflow._active_object(context)
    if not objs and act is not None and act.type == "MESH":
        objs = [act]
    jobs, seen, skipped = [], set(), []
    for o in objs:
        for m in bakeable_materials(o):
            if m.name_full in seen:
                continue
            seen.add(m.name_full)
            users = set_users(context, m) or [o]
            if o in users:              # the selected one first (active in the bake)
                users.remove(o)
            users.insert(0, o)
            no_uv = [u.name for u in users if not u.data.uv_layers]
            if no_uv:
                skipped.extend(no_uv)
                users = [u for u in users if u.data.uv_layers]
            if users:
                jobs.append((users, m))
    return jobs, sorted(set(skipped))


class TEXTUREPLUGGER_OT_bake_lighting_uv(bpy.types.Operator):
    """Bake the Blender lighting onto the selected objects' UVs with Cycles (cast and contact shadows, bounce light). Painter's Bake Lighting button then multiplies it over the Base Color instead of approximating it from the environment map"""
    bl_idname = "texture_plugger.bake_lighting_uv"
    bl_label = "Bake Lighting to UVs"
    bl_options = {"REGISTER"}

    preview: BoolProperty(
        name="Studio HDRI Too", default=False,
        description="Also bake a lightmap lit by the Material Preview studio HDRI (with Cycles, "
                    "so shadows and bounce included: not the same as EEVEE's Material "
                    "Preview). Doubles the bake time")

    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        jobs, skipped = jobs_for(context)
        for n in skipped:
            self.report({"WARNING"}, f"'{n}' has no UV map: skipped")
        if not jobs:
            self.report({"WARNING"}, "Select meshes with a Texture Plugger texture set "
                                     "(Create Set or Create Sets for Scene first)")
            return {"CANCELLED"}
        p = prefs.get_prefs(context)
        samples = int(getattr(p, "bake_samples", 128)) if p else 128
        preview = self.preview
        try:
            path, changed = scene_link.install_plugin()
            if changed:
                self.report({"INFO"}, "Installed the Texture Plugger plugin for Painter "
                                      "(restart Painter if it is open)")
        except (OSError, RuntimeError) as ex:
            self.report({"WARNING"}, f"Could not install the Painter plugin: {ex}")
        folder = scene_link.link_dir()
        os.makedirs(folder, exist_ok=True)
        wm = context.window_manager
        wm.progress_begin(0, len(jobs))
        updates, t0 = {}, time.time()
        try:
            for i, (objs, mat) in enumerate(jobs):
                wm.progress_update(i)
                print(f"Texture Plugger: baking lighting of '{mat.name}' ({i + 1}/{len(jobs)}, "
                      f"{len(objs)} object(s))")
                try:
                    updates[mat.name] = bake_set(context, objs, mat, folder, samples=samples,
                                                 preview=preview)
                except Exception as ex:
                    self.report({"WARNING"}, f"Could not bake the lighting of '{mat.name}': {ex}")
        finally:
            wm.progress_end()
            if updates:
                painter_link.write_lightmaps(folder, updates)
        if not updates:
            return {"CANCELLED"}
        self.report({"INFO"}, f"Baked the lighting of {len(updates)} texture set(s) in "
                              f"{time.time() - t0:.0f} s. In Painter press Bake Lighting "
                              "(Blender Link panel) to use it")
        return {"FINISHED"}


classes = (TEXTUREPLUGGER_OT_bake_lighting_uv,)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
