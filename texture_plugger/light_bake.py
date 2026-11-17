# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake Blender's lighting onto each texture set's UVs with Cycles ("lightmaps").

The environment map sent to Painter is the light seen from one point, so it
cannot carry shadows that other objects cast across a surface (measured with
``tests/lighting_match_diag.py``: 1.9-2.4x too bright in those shadows).  A
Cycles bake of the Diffuse pass (Direct + Indirect, without colour) on the
object's own UVs has them all: cast and contact shadows, bounce light, every
lamp.  Painter's Bake Lighting multiplies it over the Base Color, so the lit
colour is exactly Cycles' diffuse result: albedo x lightmap.

The button runs the bake in a background Blender (``light_bake_worker.py``) on
a copy of the file, on the GPU when the computer has one: baking in Blender
itself froze it for the whole bake (403 sets of a 500k-face office scene took
over 14 minutes on the CPU).  Blender stays usable, shows progress and Esc
stops it; finished sets are recorded after every chunk (a few sets per Cycles
bake call), so nothing done is lost.  Sharing one bake call between sets
doesn't make it faster (measured): the time is Cycles' sampling.

The bake restores everything it touches: render engine and samples, bake
settings, world and lamps (studio HDRI variant), selection and mode, and the
materials (a Diffuse BSDF and the target image are plugged in for the bake
only).  Materials shown unlit are switched back to lit for the bake, so they
don't glow into it.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import bpy
from bpy.props import BoolProperty

from . import node_builder, painter_link, prefs, scene_link, unlit
from .node_builder import TAG
from .operators import _safe

BAKE_ROLE = "lightbake"
MARGIN = 16
CHUNK_TEXELS = 4 * 256 * 256       # texels per bake call: how often progress is saved and shown
CHUNK_TEXELS_GPU = 20 * 256 * 256  # on a GPU every call also pays for uploading the scene
MIN_BACKGROUND_SETS = 3            # fewer sets bake right here (no file copy, no start-up)
_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "light_bake_worker.py")


def reversed_loops(loop_start, loop_total):
    """Loop order with every polygon's corners reversed (numpy index array).

    Polygon ``p`` owns loops ``loop_start[p] .. loop_start[p] + loop_total[p] - 1``;
    the result maps each new loop to the old loop it takes its data from.
    """
    import numpy as np
    ls = np.asarray(loop_start, np.int64)
    lt = np.asarray(loop_total, np.int64)
    n = int(lt.sum())
    start = np.repeat(ls, lt)                                              # face's first loop
    k = np.arange(n, dtype=np.int64) - np.repeat(np.cumsum(lt) - lt, lt)  # corner within face
    out = np.empty(n, np.int64)
    out[start + k] = start + np.repeat(lt, lt) - 1 - k
    return out


def used_materials(obj):
    """Materials that at least one face of ``obj`` uses (an empty slot makes no
    texture set in Painter, so it needs no lightmap)."""
    import numpy as np
    me = getattr(obj, "data", None)
    slots = getattr(obj, "material_slots", ())
    if me is None or not hasattr(me, "polygons") or not len(me.polygons):
        return [s.material for s in slots]
    mi = np.empty(len(me.polygons), np.int32)
    me.polygons.foreach_get("material_index", mi)
    used = set(np.unique(np.clip(mi, 0, max(len(slots) - 1, 0))).tolist())
    return [s.material for i, s in enumerate(slots) if i in used]


def bakeable_materials(obj):
    """Texture Plugger materials on ``obj`` (one lightmap per material = texture set)
    that its faces use."""
    out = []
    for m in used_materials(obj):
        if m is not None and m not in out and m.node_tree is not None and m.get(unlit.PROJECT_KEY):
            out.append(m)
    return out


def scene_resolution():
    """Texture set size of the whole-scene Painter project Blender planned, or 0."""
    try:
        data = painter_link.read_link(painter_link.link_file(scene_link.link_dir())) or {}
        return int((data.get("scene") or {}).get("resolution") or 0)
    except Exception:
        return 0


def set_size(mat, choice="256", scene_res=0):
    """Lightmap size for a set.

    ``choice`` is the *Lightmap Size* preference: a number of pixels (light
    needs far less detail than paint, and bake time grows with the pixel count),
    or "SET" for the texture set's own size: its Base Color texture, unless that
    is a small flat placeholder, then the size Painter was set to.
    """
    if choice != "SET":
        return int(choice)
    size = 0
    node = node_builder.base_color_node(mat.node_tree) if mat.node_tree else None
    try:
        if node is not None and node.image.source == "FILE":
            size = int(max(node.image.size))
    except Exception:
        size = 0
    if size < 256:                       # placeholder (written small)
        size = scene_res or 1024
    return max(64, min(4096, size))


class _Setup:
    """Puts the scene in bake state and back (always, also on errors)."""

    def __init__(self, context, objs, targets, samples, world=None, lamps=True, merge=False):
        """``targets``: [(material, image)] - each set bakes into its own image."""
        self.context, self.objs, self.targets = context, list(objs), list(targets)
        self.obj = self.objs[0]
        self.samples, self.world, self.lamps = samples, world, lamps
        self.undo, self.unlit, self.mats, self.scratch = [], [], [], None
        self.prev_sel, self.prev_active, self.mode = None, None, "OBJECT"
        self.merge, self.merged = merge, None

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
                img = next((i for t, i in self.targets if t == m), None) or self._scratch()
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
        if self.merge and len(self.objs) > 1:
            self._merge()
            return self
        for o in self.objs:          # every object of the sets: each set has one image
            o.select_set(True)
        vl.objects.active = self.obj
        return self

    def _merge(self):
        """Bake one temporary object instead of many.

        Cycles prepares the whole scene again for every object it bakes (about
        3.5 s each on a 500k-face scene, measured on the GPU), so the objects of
        the chunk become one mesh: modifiers applied, world positions, their
        own materials per face, their active UV map and their exact shading
        normals.  The mesh is built here from arrays rather than with
        Mesh.transform or Join: both got some custom normals wrong (sets came
        out 15% too dark or 2x too bright, measured).  The originals are hidden
        from the render meanwhile, so nothing casts its shadow twice.  Used in
        the background bake's copy of the file.
        """
        import numpy as np
        ctx = self.context
        vl, sc = ctx.view_layer, ctx.scene
        dg = ctx.evaluated_depsgraph_get()
        mats, pos, cverts, starts, sizes, mids, uvs, nors = [], [], [], [], [], [], [], []
        nv = nl = 0
        for ob in self.objs:
            ev = ob.evaluated_get(dg)
            me = ev.to_mesh()
            try:
                v, l, f = len(me.vertices), len(me.loops), len(me.polygons)
                co = np.empty(v * 3, np.float32)
                me.vertices.foreach_get("co", co)
                m = np.array(ob.matrix_world, dtype=np.float64)
                co = co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
                vi = np.empty(l, np.int32)
                me.loops.foreach_get("vertex_index", vi)
                ls = np.empty(f, np.int32)
                me.polygons.foreach_get("loop_start", ls)
                lt = np.empty(f, np.int32)
                me.polygons.foreach_get("loop_total", lt)
                mi = np.empty(f, np.int32)
                me.polygons.foreach_get("material_index", mi)
                cn = np.empty(l * 3, np.float32)
                me.corner_normals.foreach_get("vector", cn)
                nm = np.linalg.inv(m[:3, :3]).T            # normals: inverse transpose
                cn = cn.reshape(-1, 3) @ nm.T
                cn /= np.maximum(np.linalg.norm(cn, axis=1, keepdims=True), 1e-12)
                uv = np.zeros(l * 2, np.float32)
                layer = me.uv_layers.active
                if layer is not None:
                    layer.data.foreach_get("uv", uv)
                if np.linalg.det(m[:3, :3]) < 0:
                    # Mirrored object (an odd number of negative scale axes): baked
                    # into world positions its faces wind the other way and face
                    # down, so they caught no light (a floor scaled -22 came out
                    # black).  Blender flips them back when rendering the object
                    # itself; here every face's corners are reversed instead, with
                    # their UVs and normals.
                    perm = reversed_loops(ls, lt)
                    vi, cn = vi[perm], cn[perm]
                    uv = uv.reshape(-1, 2)[perm].ravel()
            finally:
                ev.to_mesh_clear()
            # this object's slots -> the merged mesh's material list
            remap = []
            for slot in ob.material_slots:
                if slot.material not in mats:
                    mats.append(slot.material)
                remap.append(mats.index(slot.material))
            remap = np.array(remap or [0], np.int32)
            pos.append(co)
            cverts.append(vi + nv)
            starts.append(ls + nl)
            sizes.append(lt)
            mids.append(remap[np.clip(mi, 0, len(remap) - 1)])
            uvs.append(uv)
            nors.append(cn)
            nv += v
            nl += l
        me = bpy.data.meshes.new("TP_BakeMerge")
        co = np.concatenate(pos).astype(np.float32)
        me.vertices.add(len(co))
        me.vertices.foreach_set("co", co.ravel())
        me.loops.add(nl)
        me.loops.foreach_set("vertex_index", np.concatenate(cverts))
        ls = np.concatenate(starts)
        me.polygons.add(len(ls))
        me.polygons.foreach_set("loop_start", ls)
        me.update(calc_edges=True)
        me.polygons.foreach_set("material_index", np.concatenate(mids))
        me.polygons.foreach_set("use_smooth", np.ones(len(ls), bool))   # corners carry flat too
        layer = me.uv_layers.new(name="TP_BakeUV")
        layer.data.foreach_set("uv", np.concatenate(uvs))
        for m in mats:
            me.materials.append(m)
        me.normals_split_custom_set(np.concatenate(nors).astype(np.float32).reshape(-1, 3).tolist())
        me.update()
        merged = bpy.data.objects.new("TP_BakeMerge", me)
        sc.collection.objects.link(merged)
        for ob in self.objs:
            if not ob.hide_render:
                self._keep(ob, "hide_render")
                ob.hide_render = True
        merged.select_set(True)
        vl.objects.active = merged
        self.merged = merged

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
        if self.merged is not None:
            me = self.merged.data
            bpy.data.objects.remove(self.merged)
            if me.users == 0:
                bpy.data.meshes.remove(me)
            for me in [m for m in bpy.data.meshes if m.users == 0 and m.name.startswith("TP_BakeMerge")]:
                bpy.data.meshes.remove(me)
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


def bake_lightmaps(context, jobs, samples, world=None, lamps=True, exposure=None, merge=False):
    """Bake several texture sets in ONE Cycles bake and write each to an EXR.

    ``jobs``: [(objects, material, out_path, size)].  Values are Cycles' Diffuse
    Direct + Indirect without colour, times the scene's own exposure: albedo x
    value is the diffuse colour a Cycles render shows.  ``world`` replaces the
    scene world (studio HDRI) and ``lamps=False`` leaves the lamps out.
    Returns {material name: {"path", "size", "max"}}.
    """
    import numpy as np
    objs, seen = [], set()
    for job_objs, _m, _p, _s in jobs:
        for o in job_objs:
            if o.type != "MESH" or not o.data.uv_layers:
                raise RuntimeError(f"'{o.name}' has no UV map to bake the lighting onto")
            if o.name_full not in seen:
                seen.add(o.name_full)
                objs.append(o)
    if exposure is None:
        exposure = float(unlit.user_view(context.scene)["exposure"])
    images = []
    try:
        for _o, mat, _p, size in jobs:
            size = int(max(16, size))
            images.append(bpy.data.images.new("TP_Lightmap", size, size, alpha=True,
                                              float_buffer=True))
        with _Setup(context, objs, [(j[1], im) for j, im in zip(jobs, images)],
                    samples, world, lamps, merge):
            bpy.ops.object.bake(type="DIFFUSE", pass_filter={"DIRECT", "INDIRECT"},
                                margin=MARGIN, use_clear=True, target="IMAGE_TEXTURES")
        out = {}
        for (_o, mat, path, _s), img in zip(jobs, images):
            w, h = img.size
            px = np.empty(w * h * 4, dtype=np.float32)
            img.pixels.foreach_get(px)
            px = px.reshape(h, w, 4)
            px[..., :3] *= 2.0 ** exposure
            px[..., 3] = 1.0
            px[~np.isfinite(px)] = 0.0
            try:     # raw linear values; set BEFORE writing them: changing it regenerates
                img.colorspace_settings.is_data = True       # the image (black)
            except (AttributeError, TypeError):
                pass
            img.pixels.foreach_set(px.ravel())
            img.filepath_raw = path
            img.file_format = "OPEN_EXR"
            img.save()
            out[mat.name] = {"path": path, "size": w, "max": float(px[..., :3].max())}
        return out
    finally:
        for img in images:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


def bake_lightmap(context, objs, mat, out_path, size, samples, world=None, lamps=True):
    """One texture set (see ``bake_lightmaps``)."""
    if not isinstance(objs, (list, tuple)):
        objs = [objs]
    return bake_lightmaps(context, [(objs, mat, out_path, size)], samples, world, lamps)[mat.name]


def chunks(jobs, sizes, texels=CHUNK_TEXELS):
    """Split jobs into bake calls of about ``texels`` pixels each (at least one set)."""
    out, cur, n = [], [], 0
    for job, size in zip(jobs, sizes):
        if cur and n + size * size > texels:
            out.append(cur)
            cur, n = [], 0
        cur.append((job, size))
        n += size * size
    if cur:
        out.append(cur)
    return out


def bake_sets(context, jobs, folder, samples=128, preview=None, size_choice="256",
              scene_res=0, lighting_key=None, on_chunk=None, stop=None, chunk_texels=None,
              merge=False):
    """Bake the lightmaps of many texture sets, a chunk of sets per Cycles bake.

    ``jobs``: [(objects, material)].  ``preview``: Material Preview settings
    (``scene_link.preview_settings``) to also bake the studio-HDRI lightmap, or
    None.  ``on_chunk(entries, done, total)`` runs after every chunk with that
    chunk's lightmaps entries; ``stop()`` returning True ends it between chunks.
    Returns (entries, errors).
    """
    sizes = [set_size(m, size_choice, scene_res) for _o, m in jobs]
    entries, errors, done = {}, [], 0
    world = image = None
    if preview is not None and not preview.get("scene_world"):
        if preview.get("hdri") and os.path.isfile(preview["hdri"]):
            had = {im.name_full for im in bpy.data.images}
            world, image = scene_link._lookdev_world(preview)
        else:
            errors.append(f"studio HDRI '{preview.get('name')}' not found: no studio lightmaps")
            preview = None
    try:
        for chunk in chunks(jobs, sizes, chunk_texels or CHUNK_TEXELS):
            if stop is not None and stop():
                break
            stamp = time.time() * 1000
            batch = {}
            for (objs, mat), size in chunk:
                batch[mat.name] = {"size": size, "objects": [o.name for o in objs],
                                   "time": stamp / 1000.0, "lighting_key": lighting_key,
                                   "render": painter_link.lightmap_name(mat.name, "render", stamp)}
            modes = [("render", None, True, samples)]
            if preview is not None:
                modes.append(("preview", world, preview.get("scene_lights", False),
                              max(16, samples // 2)))
            try:
                for mode, w, lamps, spp in modes:
                    names = {m.name: painter_link.lightmap_name(m.name, mode, stamp)
                             for (_o, m), _s in chunk}
                    res = bake_lightmaps(context, [(o, m, os.path.join(folder, names[m.name]), sz)
                                                   for (o, m), sz in chunk], spp, w, lamps,
                                          merge=merge)
                    for name, info in res.items():
                        batch[name][mode] = names[name]
                        if mode == "render":
                            batch[name]["max"] = round(info["max"], 4)
            except Exception as ex:
                errors.append(f"{', '.join(list(batch)[:3])}: {ex}")
                done += len(chunk)
                continue
            entries.update(batch)
            done += len(chunk)
            if on_chunk is not None:
                on_chunk(batch, done, len(jobs))
    finally:
        if world is not None:
            bpy.data.worlds.remove(world)
            if image is not None and image.name_full not in had and image.users == 0:
                bpy.data.images.remove(image)
    return entries, errors


def bake_set(context, objs, mat, folder, size=None, samples=128, preview=False):
    """Lightmaps of one texture set (see ``bake_sets``). Returns its entry."""
    if not isinstance(objs, (list, tuple)):
        objs = [objs]
    pv = scene_link.preview_settings(context) if preview else None
    entries, errors = bake_sets(context, [(objs, mat)], folder, samples, pv,
                                str(size) if size else "SET", scene_resolution(),
                                current_lighting_key(context))
    if mat.name not in entries:
        raise RuntimeError("; ".join(errors) or "bake failed")
    return entries[mat.name]


def current_lighting_key(context):
    p = prefs.get_prefs(context)
    try:
        return scene_link.lighting_key(context, bool(getattr(p, "env_surroundings", True)) if p else True)
    except Exception:
        return None


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


def _gpu_settings():
    """Cycles GPU settings of this Blender, for the background bake (or None)."""
    try:
        cp = bpy.context.preferences.addons["cycles"].preferences
        kind = cp.compute_device_type
        if kind in {"", "NONE"}:
            return None
        cp.refresh_devices()
        ids = [d.id for d in cp.devices if d.use and d.type != "CPU"]
        return {"type": kind, "devices": ids} if ids else None
    except Exception:
        return None


class TEXTUREPLUGGER_OT_bake_lighting_uv(bpy.types.Operator):
    """Bake the Blender lighting onto the selected objects' UVs with Cycles (cast and contact shadows, bounce light). Runs in the background: Blender stays usable, Esc stops it. Painter's Bake Lighting button then multiplies it over the Base Color"""
    bl_idname = "texture_plugger.bake_lighting_uv"
    bl_label = "Bake Lighting to UVs"
    bl_options = {"REGISTER"}

    preview: BoolProperty(
        name="Studio HDRI Too", default=False,
        description="Also bake a lightmap lit by the Material Preview studio HDRI (with Cycles, "
                    "so shadows and bounce included: not the same as EEVEE's Material "
                    "Preview). Doubles the bake time")

    # ------------------------------------------------------------- common
    def _jobs(self, context):
        jobs, skipped = jobs_for(context)
        for n in skipped:
            self.report({"WARNING"}, f"'{n}' has no UV map: skipped")
        if not jobs:
            self.report({"WARNING"}, "Select meshes with a Texture Plugger texture set "
                                     "(Create Set or Create Sets for Scene first)")
            return None
        try:
            _path, changed = scene_link.install_plugin()
            if changed:
                self.report({"INFO"}, "Installed the Texture Plugger plugin for Painter "
                                      "(restart Painter if it is open)")
        except (OSError, RuntimeError) as ex:
            self.report({"WARNING"}, f"Could not install the Painter plugin: {ex}")
        p = prefs.get_prefs(context)
        self.samples = int(getattr(p, "bake_samples", 128)) if p else 128
        self.size_choice = str(getattr(p, "lightmap_size", "256")) if p else "256"
        self.folder = scene_link.link_dir()
        os.makedirs(self.folder, exist_ok=True)
        return jobs

    def _finish(self, n, total, errors, seconds, stopped=False):
        for e in errors[:5]:
            self.report({"WARNING"}, f"Could not bake the lighting of {e}")
        if not n:
            self.report({"WARNING"}, "No lighting was baked")
            return {"CANCELLED"}
        what = f"{n} of {total}" if n != total else f"{n}"
        self.report({"INFO"}, f"{'Stopped: kept' if stopped else 'Baked'} the lighting of {what} "
                              f"texture set(s) in {seconds:.0f} s. In Painter press Bake "
                              "Lighting (Blender Link panel) to use it")
        return {"FINISHED"}

    # -------------------------------------------- here (few sets, scripts, tests)
    def execute(self, context):
        return _safe(self, self._execute, context)

    def _execute(self, context):
        jobs = self._jobs(context)
        if jobs is None:
            return {"CANCELLED"}
        t0 = time.time()
        pv = scene_link.preview_settings(context) if self.preview else None

        def save(batch, done, total):
            painter_link.write_lightmaps(self.folder, batch, prune=False)
        entries, errors = bake_sets(context, jobs, self.folder, self.samples, pv,
                                    self.size_choice, scene_resolution(),
                                    current_lighting_key(context), on_chunk=save)
        painter_link.write_lightmaps(self.folder, {})       # prune maps nothing uses
        return self._finish(len(entries), len(jobs), errors, time.time() - t0)

    # ------------------------------------------------ background (the button)
    def invoke(self, context, event):
        try:
            jobs = self._jobs(context)
            if jobs is None:
                return {"CANCELLED"}
            if (len(jobs) < MIN_BACKGROUND_SETS or not bpy.app.binary_path
                    or not os.path.isfile(_WORKER)):
                return self._execute(context)
            return self._start(context, jobs)
        except Exception as ex:          # same safety net as _safe
            self.report({"ERROR"}, f"Texture Plugger: {ex}")
            return {"CANCELLED"}

    def _start(self, context, jobs):
        self.tmp = tempfile.mkdtemp(prefix="tp_lightbake_")
        blend = os.path.join(self.tmp, "scene.blend")
        # the scene as it is now (unsaved changes too); the open file is untouched
        bpy.ops.wm.save_as_mainfile(filepath=blend, copy=True, relative_remap=True,
                                    compress=False, check_existing=False)
        job = {
            "package": os.path.dirname(os.path.abspath(__file__)),
            "scene": context.scene.name,
            "view_layer": context.view_layer.name,
            "folder": self.folder,
            "samples": self.samples,
            "size": self.size_choice,
            "scene_res": scene_resolution(),
            "lighting_key": current_lighting_key(context),
            "preview": scene_link.preview_settings(context) if self.preview else None,
            "gpu": _gpu_settings(),
            "jobs": [{"material": m.name, "objects": [o.name for o in objs]} for objs, m in jobs],
        }
        with open(os.path.join(self.tmp, "job.json"), "w", encoding="utf-8") as fh:
            json.dump(job, fh)
        flags = 0x08000000 | 0x00004000 if sys.platform == "win32" else 0   # no console, below normal
        self.log = open(os.path.join(self.tmp, "worker.log"), "wb")
        self.proc = subprocess.Popen(
            [bpy.app.binary_path, "--background", "--factory-startup", blend,
             "--python", _WORKER, "--", self.tmp],
            stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=flags)
        self.total, self.t0 = len(jobs), time.time()
        self._wm = context.window_manager
        self._timer = self._wm.event_timer_add(0.5, window=context.window)
        self._wm.modal_handler_add(self)
        self._wm.progress_begin(0, self.total)
        self._status(context, f"Texture Plugger: baking lighting 0 / {self.total} sets "
                              "(starting, Esc to stop)")
        return {"RUNNING_MODAL"}

    def _progress(self):
        try:
            with open(os.path.join(self.tmp, "progress.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _status(self, context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass

    def _end(self, context, kill=False):
        try:
            self._wm.event_timer_remove(self._timer)
        except Exception:
            pass
        try:
            self._wm.progress_end()
        except Exception:
            pass
        self._status(context, None)
        if kill and self.proc.poll() is None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
            except Exception:
                pass
        try:
            self.log.close()
        except OSError:
            pass
        pr = self._progress()
        log_tail = ""
        try:
            with open(os.path.join(self.tmp, "worker.log"), "rb") as fh:
                log_tail = fh.read()[-2000:].decode("utf-8", "replace")
        except OSError:
            pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        try:
            painter_link.write_lightmaps(self.folder, {})   # prune maps of an unfinished chunk
        except Exception:
            pass
        return pr, log_tail

    def modal(self, context, event):
        if event.type == "ESC":
            pr, _log = self._end(context, kill=True)
            return self._finish(int(pr.get("done_sets", 0)), self.total, pr.get("errors", []),
                                time.time() - self.t0, stopped=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}          # keep working while it bakes
        pr = self._progress()
        done = int(pr.get("done", 0))
        try:
            self._wm.progress_update(done)
        except Exception:
            pass
        eta = ""
        if done and pr.get("started"):
            rate = (time.time() - float(pr["started"])) / done
            eta = f", about {max(1, round(rate * (self.total - done) / 60))} min left"
        self._status(context, f"Texture Plugger: baking lighting {done} / {self.total} sets"
                              f"{eta} (Esc to stop)")
        if self.proc.poll() is None:
            return {"RUNNING_MODAL"}
        pr, log_tail = self._end(context)
        if not pr.get("finished") and not pr.get("done_sets"):
            print("Texture Plugger: background lighting bake failed:\n" + log_tail)
            self.report({"ERROR"}, "The background bake stopped unexpectedly (details in the "
                                   "system console)")
            return {"CANCELLED"}
        return self._finish(int(pr.get("done_sets", 0)), self.total, pr.get("errors", []),
                            time.time() - self.t0, stopped=not pr.get("finished"))


classes = (TEXTUREPLUGGER_OT_bake_lighting_uv,)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
