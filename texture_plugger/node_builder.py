# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds / removes the Principled BSDF texture node network.

Every node this module creates carries the custom property ``TAG`` with its
role, so a rebuild or clear finds exactly those nodes and never touches the
user's own nodes.  Principled BSDF / Material Output nodes created because the
material had none get ``CORE_TAG`` instead: they survive a clear so the
material stays valid.

Material level settings we change (displacement method, render method, ...)
are remembered in ``material[ORIG_KEY]`` the first time and restored on clear
and before every rebuild, which keeps re-applying deterministic.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field

import bpy

from . import detection

TAG = "texture_plugger"
CORE_TAG = "texture_plugger_core"
ORIG_KEY = "texture_plugger_orig"
SET_KEY = "texture_plugger_set"
#: Image custom property: reload this image when its file changes on disk.
WATCH_KEY = "texture_plugger_watch"

MATERIAL_OBJECT_TYPES = {
    "MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME",
}

# Candidate socket names / identifiers per purpose. First available wins, which
# keeps us working across 4.x / 5.x renames.
BSDF_SOCKETS = {
    "base_color": ("Base Color",),
    "metallic": ("Metallic",),
    "roughness": ("Roughness",),
    "specular": ("Specular IOR Level", "Specular"),
    "normal": ("Normal",),
    "alpha": ("Alpha",),
    "emission": ("Emission Color", "Emission"),
    "emission_strength": ("Emission Strength",),
    "transmission": ("Transmission Weight", "Transmission"),
    "subsurface": ("Subsurface Weight", "Subsurface"),
    "sheen": ("Sheen Weight", "Sheen"),
    "sheen_tint": ("Sheen Tint",),
    "coat": ("Coat Weight", "Clearcoat"),
    "coat_roughness": ("Coat Roughness", "Clearcoat Roughness"),
    "anisotropy": ("Anisotropic", "Anisotropy"),
    "anisotropy_rotation": ("Anisotropic Rotation", "Anisotropy Rotation"),
}

# Map types wired straight from the image Color output into a BSDF input.
DIRECT_MAPS = (
    "metallic", "specular", "transmission", "subsurface", "sheen", "sheen_tint",
    "coat", "coat_roughness", "anisotropy", "anisotropy_rotation",
)

SRGB_NAMES = ("sRGB", "sRGB - Texture", "Utility - sRGB - Texture", "srgb_texture")
LINEAR_NAMES = ("Linear Rec.709", "Linear", "Linear Rec.709 (sRGB)", "scene_linear",
                "Utility - Linear - sRGB")
DATA_NAMES = ("Non-Color", "Raw", "Generic Data", "Data", "Utility - Raw", "data")

FLOAT_EXTS = {".exr", ".hdr"}

# Layout (offsets relative to the Principled BSDF location).
ROW_H = 300.0
X_TEXCOORD = -2000.0
X_MAPPING = -1800.0
X_IMAGE = -1500.0
X_SEP = -1180.0
X_H1 = -1000.0
X_H2 = -820.0
X_H3 = -600.0
X_H4 = -380.0


@dataclass
class MapInput:
    path: str
    udim_tiles: tuple = ()


@dataclass
class BuildOptions:
    ao_multiply: bool = True
    ao_strength: float = 1.0
    projection: str = "UV"          # UV | BOX
    box_blend: float = 0.2
    texture_scale: float = 1.0
    height_mode: str = "DISPLACEMENT"   # DISPLACEMENT | BUMP | NONE
    disp_scale: float = 0.1
    disp_midlevel: float = 0.5
    normal_strength: float = 1.0
    bump_strength: float = 0.5
    bump_distance: float = 0.1
    alpha_mode: str = "DITHERED"    # DITHERED | BLENDED
    use_base_alpha: bool = False
    emission_strength: float = 1.0
    relative_paths: bool = False


@dataclass
class BuildResult:
    linked: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    node_count: int = 0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def is_tagged(node, tag=TAG) -> bool:
    try:
        return node.get(tag) is not None
    except Exception:
        return False


def find_socket(sockets, candidates):
    """Return the first enabled socket matching a candidate identifier or name."""
    if isinstance(candidates, str):
        candidates = (candidates,)
    for cand in candidates:
        for s in sockets:
            if s.identifier == cand and getattr(s, "enabled", True):
                return s
    for cand in candidates:
        for s in sockets:
            if s.name == cand and getattr(s, "enabled", True):
                return s
    return None


def bsdf_input(bsdf, purpose):
    return find_socket(bsdf.inputs, BSDF_SOCKETS.get(purpose, (purpose,)))


def link(nt, out_sock, in_sock) -> bool:
    if out_sock is None or in_sock is None:
        return False
    nt.links.new(out_sock, in_sock)
    return True


def set_input(node, candidates, value):
    sock = find_socket(node.inputs, candidates)
    if sock is not None and hasattr(sock, "default_value"):
        try:
            sock.default_value = value
            return True
        except Exception:
            pass
    return False


def set_colorspace(image, kind: str, ext: str):
    """kind: COLOR or DATA. Float colour images stay linear."""
    if kind == "DATA":
        names = DATA_NAMES
    elif ext.lower() in FLOAT_EXTS:
        names = LINEAR_NAMES
    else:
        names = SRGB_NAMES
    cs = image.colorspace_settings
    if cs.name in names:
        return True
    for name in names:
        try:
            cs.name = name
            return True
        except (TypeError, ValueError):
            continue
    if kind == "DATA" and hasattr(cs, "is_data"):
        try:
            cs.is_data = True
            return True
        except Exception:
            pass
    return False


def _udim_token_path(path: str, tile: int) -> str:
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    tile_s = str(tile)
    if stem.endswith(tile_s):
        stem = stem[: -len(tile_s)] + "<UDIM>"
    return os.path.join(folder, stem + ext)


# Path -> image datablock, while ``image_cache()`` is open. Without it every
# load asks Blender to compare the path with every image in the file: with a
# whole scene (thousands of placeholder images) that was half the run time.
_image_index = None


def _image_path_key(img):
    return os.path.normcase(os.path.abspath(bpy.path.abspath(img.filepath)))


class image_cache:
    """``with cache:`` makes ``load_image`` reuse images through a path index
    instead of a search over all images for every file. The index is built the
    first time it is entered and kept, so one object can be entered again for
    every step of a long job."""

    def __init__(self):
        self._index = None

    def __enter__(self):
        global _image_index
        self._prev = _image_index
        if self._index is None:
            self._index = {}
            for img in bpy.data.images:
                try:
                    if img.library is not None or img.source in {"VIEWER", "GENERATED", "TILED"}:
                        continue
                    if img.filepath:
                        self._index.setdefault(_image_path_key(img), img)
                except Exception:
                    continue
        _image_index = self._index
        return self

    def __exit__(self, *exc):
        global _image_index
        _image_index = self._prev
        return False


def _load_single(abspath):
    if _image_index is None:
        return bpy.data.images.load(abspath, check_existing=True)
    key = os.path.normcase(abspath)
    img = _image_index.get(key)
    if img is not None:
        try:
            if _image_path_key(img) == key:      # still there, still this file
                return img
        except ReferenceError:                   # removed meanwhile
            pass
    img = _image_index[key] = bpy.data.images.load(abspath, check_existing=False)
    return img


def load_image(path: str, udim_tiles=(), relative=False):
    """Load (or reuse) an image datablock. Returns (image, warning_or_None)."""
    abspath = os.path.abspath(bpy.path.abspath(path))
    if not os.path.isfile(abspath):
        return None, f"File not found: {path}"
    warning = None
    if udim_tiles:
        token = _udim_token_path(abspath, udim_tiles[0])
        token_norm = os.path.normcase(token)
        img = None
        for existing in bpy.data.images:
            if existing.source == "TILED":
                try:
                    ep = os.path.normcase(os.path.abspath(bpy.path.abspath(existing.filepath)))
                except Exception:
                    continue
                if ep == token_norm:
                    img = existing
                    break
        if img is None:
            img = bpy.data.images.load(abspath, check_existing=False)
            img.source = "TILED"
            img.filepath = token
        have = {t.number for t in img.tiles}
        for t in udim_tiles:
            if t not in have:
                img.tiles.new(tile_number=t)
        try:
            img.reload()
        except Exception:
            pass
    else:
        img = _load_single(abspath)
    if relative:
        if bpy.data.filepath:
            try:
                img.filepath = bpy.path.relpath(img.filepath)
            except ValueError:
                warning = f"Could not make path relative (different drive?): {os.path.basename(path)}"
        else:
            warning = "Relative paths need a saved .blend file; using absolute paths"
    return img, warning


# --------------------------------------------------------------------------
# Material / core nodes
# --------------------------------------------------------------------------

def ensure_node_tree(mat):
    if mat.node_tree is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                mat.use_nodes = True   # Blender < 5.0; deprecated no-op in 5.x
            except Exception:
                pass
    return mat.node_tree


def find_output(nt):
    outs = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial" and not is_tagged(n)]
    for n in outs:
        if getattr(n, "is_active_output", False):
            return n
    return outs[0] if outs else None


def find_principled(nt, output):
    if output is not None:
        surf = find_socket(output.inputs, ("Surface",))
        if surf is not None and surf.is_linked:
            src = surf.links[0].from_node
            if src.bl_idname == "ShaderNodeBsdfPrincipled":
                return src
    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeBsdfPrincipled" and not is_tagged(n):
            return n
    return None


def ensure_core(mat, result: BuildResult):
    nt = ensure_node_tree(mat)
    if nt is None:
        raise RuntimeError(f"Material '{mat.name}' has no node tree")
    output = find_output(nt)
    bsdf = find_principled(nt, output)
    if output is None:
        output = nt.nodes.new("ShaderNodeOutputMaterial")
        output[CORE_TAG] = 1
        output.location = (bsdf.location.x + 400, bsdf.location.y) if bsdf else (300.0, 300.0)
        output.is_active_output = True
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf[CORE_TAG] = 1
        bsdf.location = (output.location.x - 400, output.location.y)
    surf = find_socket(output.inputs, ("Surface",))
    if surf is not None:
        if not surf.is_linked:
            link(nt, find_socket(bsdf.outputs, ("BSDF",)), surf)
        elif surf.links[0].from_node != bsdf:
            result.warnings.append(
                f"'{mat.name}': Material Output surface is driven by another shader; "
                "textures were wired into the Principled BSDF only")
    return nt, bsdf, output


_MAT_ATTRS = ("displacement_method", "surface_render_method", "use_transparent_shadow")


def _store_originals(mat, bsdf):
    if mat.get(ORIG_KEY) is not None:
        return
    data = {}
    for attr in _MAT_ATTRS:
        if hasattr(mat, attr):
            data[attr] = getattr(mat, attr)
    if not hasattr(mat, "surface_render_method") and hasattr(mat, "blend_method"):
        data["blend_method"] = mat.blend_method
    if bsdf is not None:
        es = bsdf_input(bsdf, "emission_strength")
        if es is not None:
            data["emission_strength"] = float(es.default_value)
        data["bsdf_name"] = bsdf.name
    mat[ORIG_KEY] = data


def _restore_originals(mat):
    data = mat.get(ORIG_KEY)
    if data is None:
        return
    try:
        data = data.to_dict()
    except AttributeError:
        data = dict(data)
    for attr in _MAT_ATTRS + ("blend_method",):
        if attr in data and hasattr(mat, attr):
            try:
                setattr(mat, attr, data[attr])
            except Exception:
                pass
    nt = mat.node_tree
    if nt is not None and "emission_strength" in data:
        bsdf = nt.nodes.get(data.get("bsdf_name", ""))
        if bsdf is not None and bsdf.bl_idname == "ShaderNodeBsdfPrincipled":
            sock = bsdf_input(bsdf, "emission_strength")
            if sock is not None:
                try:
                    sock.default_value = data["emission_strength"]
                except Exception:
                    pass
    del mat[ORIG_KEY]


def managed_images(nt) -> list:
    out = []
    for n in nt.nodes:
        if is_tagged(n) and n.bl_idname == "ShaderNodeTexImage" and n.image is not None:
            if n.image not in out:
                out.append(n.image)
    return out


def purge_unused_images(images):
    """Remove images we loaded that nothing uses any more (keeps unsaved paint)."""
    for img in images:
        try:
            if img.users == 0 and not img.use_fake_user and not img.is_dirty:
                bpy.data.images.remove(img)
        except (ReferenceError, RuntimeError):
            pass


def remove_managed_nodes(nt) -> int:
    doomed = [n for n in nt.nodes if is_tagged(n)]
    # Remove children before frames so nothing gets re-parented on the way.
    doomed.sort(key=lambda n: n.bl_idname == "NodeFrame")
    for n in doomed:
        nt.nodes.remove(n)
    return len(doomed)


def clear_material(mat, purge=True) -> int:
    """Remove our nodes and restore material settings. Returns removed count."""
    if mat is None or mat.node_tree is None:
        return 0
    images = managed_images(mat.node_tree)
    removed = remove_managed_nodes(mat.node_tree)
    _restore_originals(mat)
    if purge:
        purge_unused_images(images)
    return removed


def material_has_managed(mat) -> bool:
    if mat is None or mat.node_tree is None:
        return False
    return mat.get(ORIG_KEY) is not None or any(is_tagged(n) for n in mat.node_tree.nodes)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

class _Builder:
    def __init__(self, mat, maps, opts: BuildOptions, set_name: str, result: BuildResult):
        self.mat = mat
        self.maps = maps
        self.opts = opts
        self.set_name = set_name
        self.result = result
        self.nt = None
        self.bsdf = None
        self.output = None
        self.frame = None
        self.bx = 0.0
        self.by = 0.0
        self.created = []

    # -- node creation -----------------------------------------------------
    def node(self, idname, role, label, col_x, row, framed=True):
        n = self.nt.nodes.new(idname)
        n[TAG] = role
        n.name = f"TP {label}"
        n.label = label
        n.select = False
        n.location = (self.bx + col_x, self.by - row * ROW_H)
        if framed and self.frame is not None:
            self._parent(n)
        self.created.append(n)
        return n

    def _parent(self, n):
        loc = n.location.copy()
        n.parent = self.frame
        # Keep the absolute position regardless of how the Blender version treats
        # child locations (relative in <= 4.3, absolute API in 4.4+).
        if hasattr(n, "location_absolute"):
            try:
                n.location_absolute = loc
                return
            except Exception:
                pass
        n.location = loc

    # -- main ----------------------------------------------------------------
    def build(self):
        opts = self.opts
        self.nt, self.bsdf, self.output = ensure_core(self.mat, self.result)
        nt = self.nt
        _store_originals(self.mat, self.bsdf)
        self.bx, self.by = self.bsdf.location.x, self.bsdf.location.y

        self.frame = nt.nodes.new("NodeFrame")
        self.frame[TAG] = "frame"
        self.frame.name = "TP Frame"
        self.frame.label = f"Texture Plugger: {self.set_name}" if self.set_name else "Texture Plugger"
        self.frame.select = False
        self.frame.location = (self.bx + X_TEXCOORD - 40, self.by + 80)
        self.created.append(self.frame)

        # Shared coordinates.
        texco = self.node("ShaderNodeTexCoord", "texcoord", "Texture Coordinate", X_TEXCOORD, 0)
        mapping = self.node("ShaderNodeMapping", "mapping", "Mapping", X_MAPPING, 0)
        vec_name = "Object" if opts.projection == "BOX" else "UV"
        link(nt, find_socket(texco.outputs, (vec_name,)), find_socket(mapping.inputs, ("Vector",)))
        s = float(opts.texture_scale) if opts.texture_scale else 1.0
        set_input(mapping, ("Scale",), (s, s, s))
        map_vec = find_socket(mapping.outputs, ("Vector",))

        # One row per map, in canonical order.
        order = sorted(self.maps.keys(), key=lambda t: detection.MAP_ORDER.get(t, 999))
        src = {}          # role -> output socket
        packed_src = {}   # role -> output socket (from packed maps, lower priority)
        rows = {}
        for row, type_id in enumerate(order):
            rows[type_id] = row
            mt = detection.MAP_TYPES_BY_ID.get(type_id)
            if mt is None or mt.ignore:
                continue
            mi = self.maps[type_id]
            img, warn = load_image(mi.path, mi.udim_tiles, opts.relative_paths)
            if warn:
                self.result.warnings.append(warn)
            if img is None:
                continue
            try:
                img[WATCH_KEY] = 1
            except Exception:
                pass
            ext = os.path.splitext(mi.path)[1]
            set_colorspace(img, "COLOR" if mt.color else "DATA", ext)
            if mt.packed:
                try:
                    img.alpha_mode = "CHANNEL_PACKED"
                except Exception:
                    pass
            tex = self.node("ShaderNodeTexImage", f"image:{type_id}", mt.label, X_IMAGE, row)
            tex.image = img
            tex.width = 240
            if opts.projection == "BOX":
                tex.projection = "BOX"
                tex.projection_blend = float(opts.box_blend)
            link(nt, map_vec, find_socket(tex.inputs, ("Vector",)))
            color = find_socket(tex.outputs, ("Color",))
            alpha = find_socket(tex.outputs, ("Alpha",))

            if mt.packed:
                sep = self.node("ShaderNodeSeparateColor", f"separate:{type_id}",
                                f"Split {mt.label}", X_SEP, row)
                link(nt, color, find_socket(sep.inputs, ("Color",)))
                chans = (
                    find_socket(sep.outputs, ("Red",)),
                    find_socket(sep.outputs, ("Green",)),
                    find_socket(sep.outputs, ("Blue",)),
                    alpha,
                )
                for role, sock in zip(mt.channels, chans):
                    if role and role not in packed_src and sock is not None:
                        packed_src[role] = sock
            elif type_id in ("normal_gl", "normal_dx"):
                src.setdefault("normal_color", (color, type_id))
            else:
                src.setdefault(type_id, color)
                if type_id == "base_color":
                    src["base_alpha"] = alpha

        for role in (detection.ROLE_AO, detection.ROLE_ROUGHNESS,
                     detection.ROLE_METALLIC, detection.ROLE_GLOSS):
            if role not in src and role in packed_src:
                src[role] = packed_src[role]

        self._wire(src, rows)
        self.result.node_count = len(self.created)

    # -- wiring --------------------------------------------------------------
    def _wire(self, src, rows):
        nt, bsdf, opts, res = self.nt, self.bsdf, self.opts, self.result

        def to_bsdf(sock, purpose, what):
            target = bsdf_input(bsdf, purpose)
            if target is None:
                res.warnings.append(f"Principled BSDF has no input for {what}; skipped")
                return False
            if link(nt, sock, target):
                res.linked.append(what)
                return True
            return False

        # Base colour (+ AO multiply)
        base = src.get("base_color")
        ao = src.get("ao")
        if base is not None:
            out = base
            if ao is not None and opts.ao_multiply:
                row = rows.get("base_color", 0)
                mix = self.node("ShaderNodeMix", "ao_multiply", "AO Multiply", X_H2, row + 0.35)
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                set_input(mix, ("Factor_Float", "Factor"), float(opts.ao_strength))
                link(nt, base, find_socket(mix.inputs, ("A_Color",)))
                link(nt, ao, find_socket(mix.inputs, ("B_Color",)))
                out = find_socket(mix.outputs, ("Result_Color",))
                res.linked.append("Ambient Occlusion")
            to_bsdf(out, "base_color", "Base Color")
        elif ao is not None:
            res.warnings.append("Ambient Occlusion loaded but there is no Base Color to multiply it into")

        # Roughness / gloss
        if src.get("roughness") is not None:
            to_bsdf(src["roughness"], "roughness", "Roughness")
            if src.get("gloss") is not None and "gloss" in self.maps:
                res.warnings.append("Both Roughness and Glossiness given; Glossiness ignored")
        elif src.get("gloss") is not None:
            row = rows.get("gloss", rows.get("mask_map", rows.get("metallic_smoothness", 0)))
            inv = self.node("ShaderNodeInvert", "gloss_invert", "Gloss to Roughness", X_H1, row)
            set_input(inv, ("Fac", "Factor"), 1.0)
            link(nt, src["gloss"], find_socket(inv.inputs, ("Color",)))
            to_bsdf(find_socket(inv.outputs, ("Color",)), "roughness", "Roughness (from Gloss)")

        if src.get("metallic") is not None:
            to_bsdf(src["metallic"], "metallic", "Metallic")

        # Normal / bump
        normal_out = None
        nc = src.get("normal_color")
        if nc is not None:
            color, type_id = nc
            row = rows.get(type_id, 0)
            if type_id == "normal_dx":
                sep = self.node("ShaderNodeSeparateColor", "dx_separate", "DX Split", X_SEP, row)
                flip = self.node("ShaderNodeMath", "dx_flip", "Flip Green (DirectX)", X_H1, row)
                flip.operation = "SUBTRACT"
                comb = self.node("ShaderNodeCombineColor", "dx_combine", "DX Combine", X_H2, row)
                link(nt, color, find_socket(sep.inputs, ("Color",)))
                flip.inputs[0].default_value = 1.0
                link(nt, find_socket(sep.outputs, ("Green",)), flip.inputs[1])
                link(nt, find_socket(sep.outputs, ("Red",)), find_socket(comb.inputs, ("Red",)))
                link(nt, find_socket(flip.outputs, ("Value",)), find_socket(comb.inputs, ("Green",)))
                link(nt, find_socket(sep.outputs, ("Blue",)), find_socket(comb.inputs, ("Blue",)))
                color = find_socket(comb.outputs, ("Color",))
            nm = self.node("ShaderNodeNormalMap", "normal_map", "Normal Map", X_H3, row)
            set_input(nm, ("Strength",), float(opts.normal_strength))
            link(nt, color, find_socket(nm.inputs, ("Color",)))
            normal_out = find_socket(nm.outputs, ("Normal",))
            if opts.projection == "BOX":
                res.warnings.append("Tangent-space normal maps need UVs; Box projection gives approximate normals")

        height = src.get("height")
        bump_src, bump_type = src.get("bump"), "bump"
        if bump_src is None and height is not None and opts.height_mode == "BUMP":
            bump_src, bump_type = height, "height"
        if bump_src is not None:
            row = rows.get(bump_type, 0)
            bump = self.node("ShaderNodeBump", "bump", "Bump", X_H4, row)
            set_input(bump, ("Strength",), float(opts.bump_strength))
            set_input(bump, ("Distance",), float(opts.bump_distance))
            link(nt, bump_src, find_socket(bump.inputs, ("Height",)))
            if normal_out is not None:
                link(nt, normal_out, find_socket(bump.inputs, ("Normal",)))
            normal_out = find_socket(bump.outputs, ("Normal",))
            res.linked.append("Bump" if bump_type == "bump" else "Height (as Bump)")
        if normal_out is not None:
            to_bsdf(normal_out, "normal", "Normal")

        # Displacement
        if height is not None and opts.height_mode == "DISPLACEMENT":
            disp = self.nt.nodes.new("ShaderNodeDisplacement")
            disp[TAG] = "displacement"
            disp.name = "TP Displacement"
            disp.label = "Displacement"
            disp.select = False
            disp.location = (self.output.location.x, self.output.location.y - 220)
            self.created.append(disp)
            set_input(disp, ("Midlevel",), float(opts.disp_midlevel))
            set_input(disp, ("Scale",), float(opts.disp_scale))
            link(nt, height, find_socket(disp.inputs, ("Height",)))
            target = find_socket(self.output.inputs, ("Displacement",))
            if link(nt, find_socket(disp.outputs, ("Displacement",)), target):
                res.linked.append("Displacement")
            if hasattr(self.mat, "displacement_method"):
                try:
                    self.mat.displacement_method = "BOTH"
                except Exception:
                    pass
        elif height is not None and opts.height_mode == "NONE":
            res.warnings.append("Height map loaded but not connected (height mode: None)")

        # Emission
        if src.get("emission") is not None:
            if to_bsdf(src["emission"], "emission", "Emission"):
                es = bsdf_input(bsdf, "emission_strength")
                if es is not None and es.default_value == 0.0:
                    es.default_value = float(opts.emission_strength)

        # Alpha
        alpha = src.get("opacity")
        if alpha is None and opts.use_base_alpha:
            alpha = src.get("base_alpha")
        if alpha is not None:
            if to_bsdf(alpha, "alpha", "Alpha"):
                self._setup_transparency()

        for type_id in DIRECT_MAPS:
            if type_id == "metallic":
                continue
            sock = src.get(type_id)
            if sock is not None:
                to_bsdf(sock, type_id, detection.MAP_TYPES_BY_ID[type_id].label)

    def _setup_transparency(self):
        mat = self.mat
        if hasattr(mat, "surface_render_method"):
            try:
                mat.surface_render_method = self.opts.alpha_mode
            except Exception:
                pass
        elif hasattr(mat, "blend_method"):
            try:
                mat.blend_method = "HASHED" if self.opts.alpha_mode == "DITHERED" else "BLEND"
            except Exception:
                pass
        if hasattr(mat, "use_transparent_shadow"):
            try:
                mat.use_transparent_shadow = True
            except Exception:
                pass


def build_material(mat, maps: dict, opts: BuildOptions, set_name: str = "", set_key: str = "") -> BuildResult:
    """(Re)build our node network on ``mat`` from ``maps`` (type id -> MapInput)."""
    result = BuildResult()
    old_images = managed_images(mat.node_tree) if mat.node_tree is not None else []
    # Keep old images alive during the rebuild so unchanged maps are reused.
    clear_material(mat, purge=False)
    if set_key:
        mat[SET_KEY] = set_key
    builder = _Builder(mat, maps, opts, set_name, result)
    try:
        builder.build()
    except Exception:
        # Leave no half-built network behind.
        try:
            clear_material(mat)
        except Exception:
            pass
        raise
    finally:
        purge_unused_images(old_images)
    return result


# --------------------------------------------------------------------------
# Objects / materials
# --------------------------------------------------------------------------

def object_supports_material(obj) -> bool:
    return (obj is not None and obj.type in MATERIAL_OBJECT_TYPES
            and getattr(obj, "data", None) is not None
            and hasattr(obj.data, "materials"))


def material_is_editable(mat) -> bool:
    # Library overrides count as read-only: node edits on an override are not
    # stored as override properties and would be lost when the file reloads.
    return mat is not None and mat.library is None and mat.override_library is None \
        and not getattr(mat, "is_missing", False)


def get_or_create_material(obj, mode: str, set_name: str, set_key: str, cache: dict):
    """Return (material, message_or_None). ``cache`` shares new materials within one run."""
    if not object_supports_material(obj):
        return None, f"'{obj.name}' cannot have materials"
    if obj.library is not None and obj.override_library is None:
        return None, f"'{obj.name}' is linked from a library"
    current = obj.active_material
    slot_link = obj.material_slots[obj.active_material_index].link \
        if 0 <= obj.active_material_index < len(obj.material_slots) else "DATA"
    data = obj.data
    if slot_link == "DATA" and (data.library is not None or data.override_library is not None) \
            and not (mode != "NEW" and current is not None and material_is_editable(current)):
        return None, f"'{obj.name}' uses linked {obj.type.lower()} data; its materials can't be changed"
    if mode == "NEW":
        if current is not None and current.get(SET_KEY) == set_key and material_is_editable(current):
            return current, None
        mat = cache.get(set_key)
        if mat is None:
            mat = bpy.data.materials.new(set_name or "Texture Set")
            ensure_node_tree(mat)
            cache[set_key] = mat
    else:
        if current is not None:
            if not material_is_editable(current):
                return None, f"Material '{current.name}' on '{obj.name}' is linked/read-only"
            return current, None
        mat = cache.get(set_key)
        if mat is None:
            mat = bpy.data.materials.new(set_name or "Texture Set")
            ensure_node_tree(mat)
            cache[set_key] = mat
    if len(obj.material_slots) == 0:
        obj.data.materials.append(mat)
        obj.active_material_index = 0
    else:
        obj.active_material = mat
    return mat, None
