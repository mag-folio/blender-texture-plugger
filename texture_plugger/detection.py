# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture map detection from filenames.

Pure Python: this module must never import ``bpy`` so it can be unit-tested
with a plain Python interpreter (see ``tests/test_detection.py``).

Pipeline
--------
1. ``tokenize(stem)``   - split a filename stem on ``_ - . space`` and camelCase,
   tagging resolution (``4k``), format (``png``), LOD, variant (``var1``) and
   bit-depth tokens so they can be ignored for matching and set grouping.
2. ``classify(path)``   - find the map type by matching whole tokens (or runs of
   whole tokens) against keyword lists.  The match that ends right-most wins
   (map suffixes are almost always after the set name); on a tie the longest
   (most specific) keyword wins.  Single-letter keywords (``d``, ``n``, ``r``...)
   only count when they are the last meaningful token.
3. ``analyze(paths)``   - classify everything, confirm UDIM sequences, group
   files into texture sets by their remaining tokens, and pick the best file per
   map type (resolution preference, format priority, variant, bit depth).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Map type table
# --------------------------------------------------------------------------

IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".tga",
    ".bmp", ".webp", ".dds",
)

# Channel roles used by packed maps.
ROLE_AO = "ao"
ROLE_ROUGHNESS = "roughness"
ROLE_METALLIC = "metallic"
ROLE_GLOSS = "gloss"


@dataclass(frozen=True)
class MapType:
    id: str
    label: str
    color: bool                 # True -> sRGB colour data, False -> Non-Color
    keywords: tuple
    channels: tuple = ()        # packed maps: role per channel (R, G, B, A)
    ignore: bool = False        # recognised but never wired (preview, cavity...)
    internal: bool = False      # not offered as a slot type (generic "normal")
    description: str = ""

    @property
    def packed(self) -> bool:
        return bool(self.channels)


MAP_TYPES = (
    MapType("base_color", "Base Color", True, (
        "basecolor", "base color", "basecol", "albedo", "alb", "diffuse", "diff",
        "dif", "diffusecolor", "color", "colour", "col", "bc", "albedotransparency",
        "alphamasked", "d"),
        description="Albedo / diffuse colour (sRGB)"),
    MapType("ao", "Ambient Occlusion", False, (
        "ambientocclusion", "ambient occlusion", "occlusion", "ao", "occ",
        "mixedao", "aomap"),
        description="Multiplied into Base Color (optional)"),
    MapType("metallic", "Metallic", False, (
        "metallic", "metalness", "metalic", "metal", "met", "mtl", "m")),
    MapType("roughness", "Roughness", False, (
        "roughness", "rough", "rgh", "roughnessmap", "r")),
    MapType("gloss", "Glossiness", False, (
        "glossiness", "gloss", "smoothness", "smooth", "gls"),
        description="Inverted into Roughness"),
    MapType("specular", "Specular", False, (
        "specular", "specularlevel", "spec", "spc", "reflection", "refl", "s"),
        description="Specular IOR Level"),
    MapType("normal", "Normal (unspecified)", False, (
        "normal", "normals", "normalmap", "nor", "nrm", "nml", "norm", "n"),
        internal=True,
        description="Generic normal: resolved to OpenGL/DirectX by extra tokens or preference"),
    MapType("normal_gl", "Normal (OpenGL)", False, (
        "normalgl", "norgl", "nrmgl", "nmlgl", "normalopengl", "noropengl",
        "nrmopengl", "normalogl", "norogl", "nrmogl")),
    MapType("normal_dx", "Normal (DirectX)", False, (
        "normaldx", "nordx", "nrmdx", "nmldx", "normaldirectx", "nordirectx",
        "nrmdirectx", "normald3d"),
        description="Green channel is flipped"),
    MapType("bump", "Bump", False, ("bump", "bumpmap")),
    MapType("height", "Height / Displacement", False, (
        "height", "heightmap", "displacement", "displace", "disp", "dsp",
        "dispmap", "h")),
    MapType("emission", "Emission", True, (
        "emission", "emissive", "emit", "emi", "glow", "illumination",
        "selfillumination", "selfillum")),
    MapType("opacity", "Opacity / Alpha", False, (
        "opacity", "alpha", "transparency", "transparent", "opac", "mask",
        "cutout")),
    MapType("transmission", "Transmission", False, (
        "transmission", "transmissive", "transmittance", "refraction")),
    MapType("subsurface", "Subsurface", False, (
        "subsurface", "subsurfacescattering", "sss", "scattering",
        "translucency", "translucent", "transluc")),
    MapType("sheen", "Sheen", False, ("sheen", "fuzz", "sheenweight", "fuzzweight")),
    MapType("sheen_tint", "Sheen Tint", True, (
        "sheencolor", "sheencolour", "sheentint", "fuzzcolor")),
    MapType("coat", "Coat", False, (
        "coat", "clearcoat", "coatweight", "clearcoatweight", "coatamount")),
    MapType("coat_roughness", "Coat Roughness", False, (
        "coatroughness", "clearcoatroughness", "coatrough", "clearcoatrough")),
    MapType("anisotropy", "Anisotropy", False, (
        "anisotropy", "anisotropic", "aniso", "anisotropylevel",
        "anisotropystrength")),
    MapType("anisotropy_rotation", "Anisotropy Rotation", False, (
        "anisotropyrotation", "anisotropyangle", "anisorotation", "anisoangle",
        "anisotropicrotation")),
    MapType("orm", "Packed ORM / ARM", False, (
        "orm", "arm", "aorm", "occlusionroughnessmetallic",
        "occlusionroughnessmetalness", "aoroughnessmetallic", "aoroughmetal"),
        channels=(ROLE_AO, ROLE_ROUGHNESS, ROLE_METALLIC, None),
        description="R=AO, G=Roughness, B=Metallic"),
    MapType("rma", "Packed RMA", False, (
        "rma", "rmao", "roughnessmetallicao", "roughnessmetallicocclusion",
        "roughmetalao"),
        channels=(ROLE_ROUGHNESS, ROLE_METALLIC, ROLE_AO, None),
        description="R=Roughness, G=Metallic, B=AO"),
    MapType("mra", "Packed MRA", False, (
        "mra", "mrao", "metallicroughnessao", "metallicroughnessocclusion",
        "metalroughao"),
        channels=(ROLE_METALLIC, ROLE_ROUGHNESS, ROLE_AO, None),
        description="R=Metallic, G=Roughness, B=AO"),
    MapType("mask_map", "Packed Mask Map (HDRP)", False, ("maskmap",),
        channels=(ROLE_METALLIC, ROLE_AO, None, ROLE_GLOSS),
        description="R=Metallic, G=AO, B=detail (unused), A=Smoothness"),
    MapType("metallic_smoothness", "Packed Metallic+Smoothness", False, (
        "metallicsmoothness", "metalsmoothness"),
        channels=(ROLE_METALLIC, None, None, ROLE_GLOSS),
        description="R=Metallic, A=Smoothness (Unity URP/Built-in)"),
    MapType("cavity", "Cavity (ignored)", False, ("cavity",), ignore=True),
    MapType("curvature", "Curvature (ignored)", False, ("curvature", "curv"), ignore=True),
    MapType("thickness", "Thickness (ignored)", False, ("thickness",), ignore=True),
    MapType("preview", "Preview (ignored)", False, (
        "preview", "thumbnail", "thumb", "sphere"), ignore=True),
)

MAP_TYPES_BY_ID = {mt.id: mt for mt in MAP_TYPES}
MAP_ORDER = {mt.id: i for i, mt in enumerate(MAP_TYPES)}

#: Slot types the user can pick (generic "normal" is resolved before it gets here).
SLOT_TYPES = tuple(mt for mt in MAP_TYPES if not mt.internal)

#: Map types for which the "data" format priority list is used.
DATA_PRIORITY_TYPES = frozenset({"normal", "normal_gl", "normal_dx", "height", "bump"})

DEFAULT_KEYWORDS = {mt.id: mt.keywords for mt in MAP_TYPES}

DEFAULT_FORMAT_PRIORITY = "png, tif, tga, exr, jpg, webp, bmp, dds"
DEFAULT_DATA_FORMAT_PRIORITY = "exr, png, tif, tga, jpg, webp, bmp, dds"

_GL_TOKENS = frozenset({"gl", "opengl", "ogl"})
_DX_TOKENS = frozenset({"dx", "directx", "d3d", "dx11", "dx12"})
_FORMAT_TOKENS = frozenset({
    "png", "jpg", "jpeg", "exr", "tif", "tiff", "tga", "bmp", "webp", "dds",
    "psd", "hdr",
})
_FORMAT_ALIASES = {"jpeg": "jpg", "tiff": "tif"}

_POW2_RES = frozenset({256, 512, 1024, 2048, 4096, 8192, 16384})

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[\s_\-.]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_RES_K_RE = re.compile(r"^(\d{1,2})k$", re.IGNORECASE)
_RES_PX_RE = re.compile(r"^(\d{3,5})(?:px)?$", re.IGNORECASE)
_LOD_RE = re.compile(r"^lod\d+$", re.IGNORECASE)
_VAR_RE = re.compile(r"^(?:var|variation|variant)\d+$", re.IGNORECASE)
_BITS_RE = re.compile(r"^(8|16|32)[-_]?bits?$", re.IGNORECASE)
_UDIM_RE = re.compile(r"^(?P<base>.*?)(?P<sep>[._])(?P<tile>1\d{3})$")


@dataclass
class Token:
    text: str           # lowercase
    orig: str           # original case
    kind: str           # word | num | res | fmt | lod | var | bits
    value: int = 0      # resolution in px / bit depth


def _piece_kind(piece: str):
    """Classify an un-split piece that must stay whole. Returns Token or None."""
    low = piece.lower()
    m = _RES_K_RE.match(low)
    if m:
        return Token(low, piece, "res", int(m.group(1)) * 1024)
    m = _RES_PX_RE.match(low)
    if m and int(m.group(1)) in _POW2_RES:
        return Token(low, piece, "res", int(m.group(1)))
    if low in _FORMAT_TOKENS:
        return Token(low, piece, "fmt")
    if _LOD_RE.match(low):
        return Token(low, piece, "lod")
    if _VAR_RE.match(low):
        return Token(low, piece, "var")
    m = _BITS_RE.match(low)
    if m:
        return Token(low, piece, "bits", int(m.group(1)))
    return None


def tokenize(stem: str) -> list:
    """Split a filename stem into tagged tokens."""
    tokens = []
    for piece in _SPLIT_RE.split(stem):
        if not piece:
            continue
        special = _piece_kind(piece)
        if special is not None:
            tokens.append(special)
            continue
        parts = _CAMEL_RE.findall(piece) if piece.isascii() else [piece]
        if not parts or "".join(parts) != piece:
            # Unicode or unusual characters: keep the piece whole.
            parts = [piece]
        for part in parts:
            special = _piece_kind(part)
            if special is not None:
                tokens.append(special)
            elif part.isdigit():
                tokens.append(Token(part.lower(), part, "num", int(part)))
            else:
                tokens.append(Token(part.lower(), part, "word"))
    return tokens


def normalize_keyword(keyword: str) -> str:
    """'Base Color' -> 'basecolor' (lowercase, alphanumerics only)."""
    return "".join(ch for ch in keyword.lower() if ch.isalnum())


def parse_keyword_list(text: str) -> tuple:
    """'basecolor, albedo ,diff' -> ('basecolor', 'albedo', 'diff')."""
    out = []
    for raw in text.replace(";", ",").split(","):
        kw = raw.strip()
        if kw:
            out.append(kw)
    return tuple(out)


def build_keyword_index(keywords=None) -> dict:
    """Map normalised keyword -> map type id. Earlier map types win conflicts."""
    keywords = DEFAULT_KEYWORDS if keywords is None else keywords
    index = {}
    for mt in MAP_TYPES:
        for kw in keywords.get(mt.id, ()):
            norm = normalize_keyword(kw)
            if norm and not norm.isdigit() and norm not in index:
                index[norm] = mt.id
    return index


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

@dataclass
class Detection:
    path: str
    filename: str
    ext: str                    # lowercase, without dot, aliases folded (jpeg->jpg)
    map_type: str | None
    set_key: str
    set_name: str
    match: str = ""             # the matched keyword (normalised)
    resolution: int | None = None
    udim_tile: int | None = None
    udim_sep: str = ""
    udim_base: str = ""         # stem without the tile number
    variant: str = ""
    bits: int | None = None
    udim_tiles: tuple = ()      # filled in by analyze() for the representative
    stem_key: str = ""          # all name tokens incl. the matched keyword

    @property
    def is_udim(self) -> bool:
        return bool(self.udim_tiles)


_MAX_SPAN = 5


def classify(path: str, keywords=None, *, normal_default: str = "GL",
             allow_udim: bool = True, _index=None) -> Detection:
    """Detect the map type and set key of one file. Never raises for odd names."""
    filename = os.path.basename(path)
    stem, ext = os.path.splitext(filename)
    ext = ext.lower().lstrip(".")
    ext = _FORMAT_ALIASES.get(ext, ext)
    index = _index if _index is not None else build_keyword_index(keywords)

    udim_tile = None
    udim_sep = ""
    udim_base = stem
    if allow_udim:
        m = _UDIM_RE.match(stem)
        if m and m.group("base") and int(m.group("tile")) >= 1001:
            udim_tile = int(m.group("tile"))
            udim_sep = m.group("sep")
            udim_base = m.group("base")
            stem = udim_base

    tokens = tokenize(stem)
    resolution = None
    variant = ""
    bits = None
    cand = []                     # indices into tokens of matchable tokens
    for i, tok in enumerate(tokens):
        if tok.kind == "res":
            resolution = max(resolution or 0, tok.value)
        elif tok.kind == "var":
            variant = tok.text
        elif tok.kind == "bits":
            bits = tok.value
        elif tok.kind in ("word", "num"):
            cand.append(i)

    meaningful = [i for i in cand if tokens[i].kind == "word"]
    last_meaningful = meaningful[-1] if meaningful else -1

    best = None  # (end_pos, length, start_pos_in_cand, end_pos_in_cand, type, kw)
    n = len(cand)
    for a in range(n):
        concat = ""
        for b in range(a, min(n, a + _MAX_SPAN)):
            concat += tokens[cand[b]].text
            type_id = index.get(concat)
            if type_id is None:
                continue
            if len(concat) == 1 and cand[b] != last_meaningful:
                continue  # single letters only as the final suffix
            key = (cand[b], len(concat))
            if best is None or key > best[:2]:
                best = (cand[b], len(concat), a, b, type_id, concat)

    map_type = None
    match = ""
    consumed = set()
    if best is not None:
        _, _, a, b, map_type, match = best
        consumed.update(cand[a:b + 1])
        if map_type == "normal":
            conv = None
            free = [i for i in cand if i not in consumed]
            for pos, i in enumerate(free):
                if i in consumed:
                    continue
                t = tokens[i].text
                pair = None
                if pos + 1 < len(free):
                    pair = t + tokens[free[pos + 1]].text   # "Direct"+"X", "Open"+"GL"
                if t in _GL_TOKENS or pair in _GL_TOKENS:
                    conv = "GL"
                elif t in _DX_TOKENS or pair in _DX_TOKENS:
                    conv = "DX"
                else:
                    continue
                consumed.add(i)
                if pair in _GL_TOKENS or pair in _DX_TOKENS:
                    consumed.add(free[pos + 1])
            conv = conv or ("DX" if str(normal_default).upper() == "DX" else "GL")
            map_type = "normal_dx" if conv == "DX" else "normal_gl"
        # Poliigon style "NRM16" / "DISP16": bit depth right after the map token.
        end = cand[b]
        nxt = end + 1
        if nxt < len(tokens) and tokens[nxt].kind == "num" and tokens[nxt].value in (8, 16, 32):
            bits = bits or tokens[nxt].value
            consumed.add(nxt)

    key_tokens = [tokens[i] for i in cand if i not in consumed]
    set_key = "_".join(t.text for t in key_tokens)
    set_name = "_".join(t.orig for t in key_tokens)

    return Detection(
        path=path, filename=filename, ext=ext, map_type=map_type,
        set_key=set_key, set_name=set_name, match=match,
        resolution=resolution, udim_tile=udim_tile, udim_sep=udim_sep,
        udim_base=udim_base, variant=variant, bits=bits,
        stem_key="_".join(tokens[i].text for i in cand),
    )


# --------------------------------------------------------------------------
# Selection helpers
# --------------------------------------------------------------------------

def parse_format_priority(text: str) -> list:
    out = []
    for raw in str(text).replace(";", ",").split(","):
        fmt = raw.strip().lower().lstrip(".")
        fmt = _FORMAT_ALIASES.get(fmt, fmt)
        if fmt and fmt not in out:
            out.append(fmt)
    return out


def _resolution_rank(res, pref):
    """Smaller is better."""
    if res is None:
        return (1, 0.0)   # unknown resolution ranks after known ones
    if pref == "LOWEST":
        return (0, float(res))
    if pref == "HIGHEST" or not pref:
        return (0, -float(res))
    try:
        target = int(pref)
    except (TypeError, ValueError):
        return (0, -float(res))
    # Closest to target in log2 space, prefer the larger on a tie.
    return (0, abs(math.log2(res) - math.log2(target)) - res * 1e-9)


def _rank_key(det, fmt_list, pref):
    try:
        fmt_rank = fmt_list.index(det.ext)
    except ValueError:
        fmt_rank = len(fmt_list)
    return (
        _resolution_rank(det.resolution, pref),
        fmt_rank,
        (det.variant != "", det.variant),
        -(det.bits or 0),
        -len(det.udim_tiles),
        det.filename.lower(),
    )


@dataclass
class Choice:
    map_type: str
    detection: Detection
    alternates: list = field(default_factory=list)
    enabled: bool = True
    note: str = ""


@dataclass
class TextureSet:
    key: str
    name: str
    detections: list = field(default_factory=list)
    choices: dict = field(default_factory=dict)       # map_type -> Choice
    unassigned: list = field(default_factory=list)    # Detection with map_type None
    notes: list = field(default_factory=list)

    @property
    def map_count(self) -> int:
        return sum(1 for c in self.choices.values() if c.enabled)

    def ordered_choices(self):
        return sorted(self.choices.values(), key=lambda c: MAP_ORDER.get(c.map_type, 999))


@dataclass
class Analysis:
    sets: list = field(default_factory=list)          # best set first
    unassigned: list = field(default_factory=list)
    detections: list = field(default_factory=list)


def _resolve_conflicts(ts: TextureSet):
    ch = ts.choices

    def disable(type_id, reason):
        c = ch.get(type_id)
        if c is not None and c.enabled:
            c.enabled = False
            c.note = reason
            ts.notes.append(f"{MAP_TYPES_BY_ID[type_id].label}: {reason}")

    if "normal_gl" in ch and "normal_dx" in ch:
        disable("normal_dx", "OpenGL normal map present, DirectX copy not used")
    if "roughness" in ch and "gloss" in ch:
        disable("gloss", "Roughness map present, glossiness not used")
    if "base_color" not in ch and "ao" in ch:
        ts.notes.append("Ambient Occlusion has no Base Color to multiply into")
    for mt in MAP_TYPES:
        if mt.ignore and mt.id in ch:
            disable(mt.id, "not used by the shader")


def list_image_files(folder: str, recursive: bool = False) -> list:
    """Return sorted image file paths in a folder (optionally recursive)."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for f in sorted(files):
                if f.lower().endswith(IMAGE_EXTENSIONS):
                    out.append(os.path.join(root, f))
    else:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return out
        for f in names:
            p = os.path.join(folder, f)
            if f.lower().endswith(IMAGE_EXTENSIONS) and os.path.isfile(p):
                out.append(p)
    return out


def analyze(paths, keywords=None, *, normal_default: str = "GL",
            format_priority: str = DEFAULT_FORMAT_PRIORITY,
            data_format_priority: str = DEFAULT_DATA_FORMAT_PRIORITY,
            resolution_pref: str = "HIGHEST") -> Analysis:
    """Classify and group a list of image paths into texture sets."""
    index = build_keyword_index(keywords)
    seen = set()
    unique_paths = []
    for p in paths:
        if not p:
            continue
        norm = os.path.normcase(os.path.abspath(p))
        if norm in seen:
            continue
        seen.add(norm)
        unique_paths.append(p)
    unique_paths = [p for p in unique_paths if p.lower().endswith(IMAGE_EXTENSIONS)]

    dets = [classify(p, normal_default=normal_default, _index=index) for p in unique_paths]

    # Confirm "_1001" style tiles: need a sibling tile with the same base.
    # ".1001." style is accepted on its own (that separator is the UDIM convention).
    base_counts = {}
    for d in dets:
        if d.udim_tile is not None:
            k = (os.path.dirname(d.path).lower(), d.udim_base.lower(), d.ext)
            base_counts.setdefault(k, set()).add(d.udim_tile)
    for i, d in enumerate(dets):
        if d.udim_tile is not None and d.udim_sep == "_":
            k = (os.path.dirname(d.path).lower(), d.udim_base.lower(), d.ext)
            if len(base_counts.get(k, ())) < 2:
                dets[i] = classify(d.path, normal_default=normal_default,
                                   allow_udim=False, _index=index)

    # Collapse UDIM tiles into one representative detection (lowest tile).
    collapsed = []
    udim_groups = {}
    for d in dets:
        if d.udim_tile is None:
            collapsed.append(d)
            continue
        k = (os.path.dirname(d.path).lower(), d.udim_base.lower(), d.udim_sep, d.ext)
        udim_groups.setdefault(k, []).append(d)
    for group in udim_groups.values():
        group.sort(key=lambda x: x.udim_tile)
        rep = group[0]
        rep.udim_tiles = tuple(x.udim_tile for x in group)
        collapsed.append(rep)

    # A file named exactly like another set (e.g. preview "Metal032.png" next to
    # "Metal032_Color.png") is that set's thumbnail, not a one-map set of its own.
    typed_keys = {d.set_key for d in collapsed if d.map_type is not None and d.set_key}
    for d in collapsed:
        if d.map_type is not None and d.stem_key in typed_keys and d.stem_key != d.set_key:
            d.map_type = None
            d.match = ""
            d.set_key = d.stem_key

    fmt_color = parse_format_priority(format_priority)
    fmt_data = parse_format_priority(data_format_priority)

    sets = {}
    unassigned = []
    for d in collapsed:
        if d.map_type is None:
            unassigned.append(d)
            continue
        key = d.set_key
        if not key:
            folder = os.path.basename(os.path.dirname(os.path.abspath(d.path)))
            key = "@" + folder.lower()
            name = folder or "Texture Set"
        else:
            name = d.set_name
        ts = sets.get(key)
        if ts is None:
            ts = sets[key] = TextureSet(key=key, name=name)
        ts.detections.append(d)

    for ts in sets.values():
        by_type = {}
        for d in ts.detections:
            by_type.setdefault(d.map_type, []).append(d)
        for type_id, cands in by_type.items():
            fmts = fmt_data if type_id in DATA_PRIORITY_TYPES else fmt_color
            cands.sort(key=lambda x: _rank_key(x, fmts, resolution_pref))
            ts.choices[type_id] = Choice(type_id, cands[0], alternates=cands[1:])
        _resolve_conflicts(ts)
        # Attach unassigned files that look like they belong to this set.
        for d in unassigned:
            k = d.set_key
            if ts.key.startswith("@"):
                if len(sets) == 1:
                    ts.unassigned.append(d)
            elif k == ts.key or k.startswith(ts.key + "_") or len(sets) == 1:
                ts.unassigned.append(d)

    ordered = sorted(sets.values(), key=lambda s: (-s.map_count, s.key))
    return Analysis(sets=ordered, unassigned=unassigned, detections=dets)


def detect_map_type(path: str, keywords=None, normal_default: str = "GL"):
    """Convenience: map type id (or None) for a single file."""
    return classify(path, keywords, normal_default=normal_default).map_type
