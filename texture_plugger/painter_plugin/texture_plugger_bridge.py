# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture Plugger link for Adobe Substance 3D Painter.

Installed by the Texture Plugger Blender add-on into Painter's
``Documents/Adobe/Adobe Substance 3D Painter/python/startup`` folder, so it
loads with Painter.  It adds a "Blender Link" panel (Window > Views) and a
"Texture Plugger" menu:

* Lighting: Blender bakes its world, lamps and surroundings into an HDR seen
  from the object; this plugin makes it Painter's environment map, and swaps
  to the right object's lighting when you solo another texture set.
* Solo: shows only one texture set (Painter's own Focus Mode) and frames the
  camera on it.  While soloing, picking another texture set in Painter's
  Texture Set List frames that one.  Orbit with Alt + left-drag on the object:
  Painter orbits around the point under the cursor.
* Match Blender View: puts Painter's camera where your Blender viewport is.

Everything comes from ``../../texture_plugger_link/blender_link.json`` (next
to the ``python`` folder), written by Blender's "Send to Painter" button.

The maths at the top has no Painter imports so it can be tested with plain
Python.
"""

import json
import math
import os

PLUGIN_VERSION = "1.2.1"
LINK_FILE = "blender_link.json"
LINK_DIR_NAME = "texture_plugger_link"
FRAME_MARGIN = 1.15
POLL_MS = 700


# ==========================================================================
# Pure maths (no Painter imports)
# ==========================================================================

def _norm(v):
    n = math.sqrt(sum(c * c for c in v))
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return [c / n for c in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def basis_from_euler(rot):
    """(forward, up) of a Painter camera with rotation ``rot`` (degrees).

    Painter's camera looks down -Z with +Y up at (0, 0, 0) and composes
    R = Rx * Ry * Rz (measured in Painter 12.2: [45, -90, 0] reads back as
    [0, -90, -45], [-30, 45, 0] as [150, 135, 180]).
    """
    a, b, c = (math.radians(x) for x in rot)
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)

    def rx(v):
        return [v[0], ca * v[1] - sa * v[2], sa * v[1] + ca * v[2]]

    def ry(v):
        return [cb * v[0] + sb * v[2], v[1], -sb * v[0] + cb * v[2]]

    def rz(v):
        return [cc * v[0] - sc * v[1], sc * v[0] + cc * v[1], v[2]]

    return rx(ry(rz([0.0, 0.0, -1.0]))), rx(ry(rz([0.0, 1.0, 0.0])))


def euler_from_basis(forward, up=(0.0, 1.0, 0.0)):
    """Painter rotation (degrees) for a camera looking along ``forward``."""
    f = _norm(forward)
    u = list(up)
    r = _cross(f, u)
    if math.sqrt(sum(x * x for x in r)) < 1e-6:          # up parallel to forward
        u = [0.0, 0.0, -1.0] if f[1] > 0 else [0.0, 0.0, 1.0]
        r = _cross(f, u)
    r = _norm(r)
    u = _cross(r, f)
    z = [-f[0], -f[1], -f[2]]                              # camera +Z
    # R = Rx(a) Ry(b) Rz(c):  R*ez = (sin b, -sin a cos b, cos a cos b)
    b = math.asin(max(-1.0, min(1.0, z[0])))
    if abs(math.cos(b)) > 1e-6:
        a = math.atan2(-z[1], z[2])
        c = math.atan2(-u[0], r[0])
    else:                                                  # gimbal: fold roll into a
        a = math.atan2(u[2], u[1])
        c = 0.0
    return [math.degrees(a), math.degrees(b), math.degrees(c)]


def box_center_radius(lo, hi):
    c = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
    r = math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))) / 2.0
    return c, max(r, 1e-6)


def orbit_around(pos, center, yaw_deg, pitch_deg, max_pitch=85.0):
    """Camera position and forward after orbiting ``yaw``/``pitch`` degrees about ``center``.

    Painter's API has no orbit pivot (its mouse orbit turns about the point under
    the cursor), so the plugin orbits explicitly: the distance to ``center`` is
    kept, world up is +Y and the pitch is clamped so the view never flips.
    """
    v = [pos[i] - center[i] for i in range(3)]
    d = math.sqrt(sum(c * c for c in v))
    if d < 1e-9:
        d, v = 1.0, [0.0, 0.0, 1.0]
    az = math.atan2(v[0], v[2]) + math.radians(yaw_deg)
    lim = math.radians(max_pitch)
    el = max(-lim, min(lim, math.asin(max(-1.0, min(1.0, v[1] / d))) + math.radians(pitch_deg)))
    new = [center[0] + d * math.cos(el) * math.sin(az),
           center[1] + d * math.sin(el),
           center[2] + d * math.cos(el) * math.cos(az)]
    return new, _norm([center[i] - new[i] for i in range(3)])


def zoom_about(pos, center, factor, min_dist=1e-3):
    """Move ``pos`` towards (factor < 1) or away from (> 1) ``center``."""
    v = [pos[i] - center[i] for i in range(3)]
    d = math.sqrt(sum(c * c for c in v))
    if d < 1e-9:
        return list(pos)
    k = max(min_dist, d * max(factor, 1e-3)) / d
    return [center[i] + v[i] * k for i in range(3)]


def fit_transform(blender_lo, blender_hi, painter_center, painter_dims):
    """(scale, b_center, p_center) mapping link coordinates onto Painter's scene.

    The link stores Painter axes in Blender units; Painter works in its own
    units (centimetres for a metre scene) around its own bounding box.
    """
    bc = [(blender_lo[i] + blender_hi[i]) / 2.0 for i in range(3)]
    bd = math.sqrt(sum((blender_hi[i] - blender_lo[i]) ** 2 for i in range(3)))
    pd = math.sqrt(sum(d * d for d in painter_dims))
    scale = pd / bd if bd > 1e-9 and pd > 1e-9 else 100.0
    return scale, bc, list(painter_center)


def apply_fit(fit, p):
    scale, bc, pc = fit
    return [(p[i] - bc[i]) * scale + pc[i] for i in range(3)]


def frame_position(center, radius, forward, fov_v_deg, aspect, margin=FRAME_MARGIN):
    """Camera position looking along ``forward`` so a sphere fills the view."""
    fv = math.radians(max(3.0, min(179.0, fov_v_deg)))
    fh = 2.0 * math.atan(math.tan(fv / 2.0) * max(aspect, 1e-3))
    half = min(fv, fh) / 2.0
    dist = radius * margin / math.sin(half)
    f = _norm(forward)
    return [center[i] - f[i] * dist for i in range(3)], dist


def union_box(sets, names):
    lo = hi = None
    for n in names:
        info = sets.get(n)
        if not isinstance(info, dict) or info.get("min") is None:
            continue
        if lo is None:
            lo, hi = list(info["min"]), list(info["max"])
        else:
            lo = [min(lo[i], info["min"][i]) for i in range(3)]
            hi = [max(hi[i], info["max"][i]) for i in range(3)]
    return lo, hi


def _name_key(name):
    """Loose form of a texture set name: Painter may change punctuation and case."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def match_sets(painter_names, link_sets):
    """Map each Painter texture set name to its entry key in the link (or leave it out).

    Exact names win; otherwise names that only differ in case or punctuation
    (``Material.001`` vs ``Material_001``) match, when that is unambiguous.
    """
    out = {}
    loose = {}
    for key, info in link_sets.items():
        if isinstance(info, dict):
            loose.setdefault(_name_key(key), []).append(key)
    for n in painter_names:
        info = link_sets.get(n)
        if isinstance(info, dict):
            out[n] = n
            continue
        cands = loose.get(_name_key(n), [])
        if len(cands) == 1:
            out[n] = cands[0]
    return out


def ts_name(texture_set):
    """A Texture Set's name on any Painter version.

    Painter 12 turned ``name()`` into a property; calling it still works but logs a
    deprecation warning every time (thousands a second with a big scene).
    """
    n = texture_set.name
    return str(n) if isinstance(n, str) else n()


def default_link_dir(plugin_file=__file__):
    """<Painter user folder>/texture_plugger_link (plugin sits in python/startup)."""
    env = os.environ.get("TP_LINK_DIR")
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(plugin_file))))
    return os.path.join(root, LINK_DIR_NAME)


def read_link(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ==========================================================================
# Painter side
# ==========================================================================

class Bridge:
    """All state of the plugin; one instance while the plugin is started."""

    def __init__(self):
        from PySide6 import QtCore
        import substance_painter.event as ev
        self.link_dir = default_link_dir()
        self.link_path = os.path.join(self.link_dir, LINK_FILE)
        self.data = None
        self.mtime = None
        self.env_cache = {}          # file path -> resource url
        self.applied_env = None      # file name last applied
        self.soloing = False
        self.last_active_set = None
        self.last_set_names = ()
        self.auto_light = True
        self.follow = True
        self.widget = None
        self.menu = None
        self._handlers = []
        self.timer = QtCore.QTimer()
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._tick)
        for event, fn in ((ev.ProjectEditionEntered, self._on_project_ready),
                          (ev.ProjectAboutToClose, self._on_project_closing)):
            ev.DISPATCHER.connect(event, fn)
            self._handlers.append((event, fn))

    # ---------------------------------------------------------------- helpers
    def log(self, msg, warn=False):
        import substance_painter.logging as lg
        (lg.warning if warn else lg.info)("Texture Plugger: " + msg)
        if self.widget is not None:
            self.widget.set_status(msg)

    @staticmethod
    def project_ready():
        import substance_painter.project as pj
        try:
            return pj.is_open() and pj.is_in_edition_state()
        except Exception:
            return False

    def texture_set_names(self):
        import substance_painter.textureset as ts
        try:
            return [ts_name(t) for t in ts.all_texture_sets()]
        except Exception:
            return []

    def active_set_name(self):
        import substance_painter.textureset as ts
        try:
            return ts_name(ts.get_active_stack().material())
        except Exception:
            return None

    def focus_button(self):
        """Painter's Focus Mode button (Texture Set List toolbar), or None.

        Measured in Painter 12.2: clicking this button isolates the active
        texture set and reflects its state; triggering the "FocusMode" QAction
        from Python does nothing (Painter keeps two, the first a dead wrapper).
        Looked up fresh every time because Painter rebuilds that toolbar.
        """
        from PySide6 import QtWidgets
        found = []
        for w in QtWidgets.QApplication.allWidgets():
            try:
                if not isinstance(w, QtWidgets.QToolButton):
                    continue
                a = w.defaultAction()
                if a is None or a.objectName() != "FocusMode":
                    continue
                p = w.parent()
                found.append((w.isVisible(), p is not None and p.objectName() == "TextureSetsView", w))
            except RuntimeError:
                continue
        found.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return found[0][2] if found else None

    def set_focus_mode(self, on):
        """Turn Painter's Focus Mode on/off. Returns True on success, None if not found."""
        b = self.focus_button()
        if b is None:
            return None
        try:
            if b.isChecked() != on:
                b.click()
            return True
        except RuntimeError:
            return None

    def viewport_aspect(self):
        from PySide6 import QtWidgets
        best = None
        for w in QtWidgets.QApplication.allWidgets():
            try:
                if w.objectName() == "Viewer3D" and w.isVisible():
                    if best is None or w.width() * w.height() > best[0] * best[1]:
                        best = (w.width(), w.height())
            except RuntimeError:
                continue
        return best[0] / max(best[1], 1) if best else 1.0

    def sets(self):
        s = (self.data or {}).get("sets")
        return s if isinstance(s, dict) else {}

    def set_info(self, set_name):
        """Link entry for a Painter texture set (tolerates renamed punctuation)."""
        if not set_name:
            return None
        key = match_sets([set_name], self.sets()).get(set_name)
        return self.sets().get(key) if key else None

    def fit(self):
        """Mapping from link coordinates to Painter's scene, or None."""
        import substance_painter.project as pj
        keys = list(match_sets(self.texture_set_names(), self.sets()).values())
        if not keys:
            return None
        lo, hi = union_box(self.sets(), keys)
        if lo is None:
            return None
        bb = pj.get_scene_bounding_box()
        return fit_transform(lo, hi, bb.center, bb.dimensions)

    # --------------------------------------------------------------- link file
    def reload_link(self, force=False):
        try:
            mtime = os.path.getmtime(self.link_path)
        except OSError:
            if self.data is not None:
                self.data = None
                self.refresh_ui()
            return False
        if not force and mtime == self.mtime:
            return False
        data = read_link(self.link_path)
        if data is None:
            return False                    # being written: try again next tick
        self.mtime = mtime
        self.data = data
        self.refresh_ui()
        return True

    def _tick(self):
        try:
            before = (self.data or {}).get("environment")
            changed = self.reload_link()
            if not self.project_ready():
                return
            if changed and self.auto_light:
                if (self.data or {}).get("environment") != before:
                    self.apply_lighting(self.sent_set())     # new lighting: the object just sent
                else:                                        # e.g. a view-only send: stay on
                    self.apply_lighting()                    # the set being worked on
            names = tuple(self.texture_set_names())
            if names != self.last_set_names:      # mesh reimported / sets added
                self.last_set_names = names
                self.refresh_ui()
            active = self.active_set_name()
            if active != self.last_active_set:
                self.last_active_set = active
                if self.soloing and self.follow and active:
                    self.frame_set(active)
                if self.auto_light:
                    self.apply_lighting()
                if self.widget is not None:
                    self.widget.select_set(active)
        except Exception as ex:      # never let the timer die
            self.log(f"error: {ex}", warn=True)

    # --------------------------------------------------------------- lighting
    def sent_set(self):
        """Painter name of the texture set Blender last sent (the one its lighting
        was baked around), or None."""
        want = (self.data or {}).get("active")
        if not want:
            return None
        for painter_name, key in match_sets(self.texture_set_names(), self.sets()).items():
            if key == want:
                return painter_name
        return None

    def environment_for(self, set_name):
        d = self.data or {}
        info = self.set_info(set_name)
        if info and info.get("environment"):
            return info["environment"]
        return d.get("environment")

    def apply_lighting(self, set_name=None, force=False):
        import substance_painter.display as dp
        import substance_painter.resource as rs
        if not self.project_ready() or not self.data:
            return False
        if not match_sets(self.texture_set_names(), self.sets()):
            return False        # an unrelated project: never replace its lighting
        name = self.environment_for(set_name or self.active_set_name())
        if not name:
            return False
        if name == self.applied_env and not force:
            self.apply_tone_mapping()      # tone mapping may change on its own
            return True
        path = os.path.join(self.link_dir, name)
        if not os.path.isfile(path):
            self.log(f"lighting file missing: {name}", warn=True)
            return False
        url = self.env_cache.get(path)
        if url is None:
            res = None
            try:
                res = rs.import_session_resource(path, rs.Usage.ENVIRONMENT,
                                                 name="Blender " + os.path.splitext(name)[0],
                                                 group="Texture Plugger")
            except Exception:
                res = rs.import_project_resource(path, rs.Usage.ENVIRONMENT,
                                                 name="Blender " + os.path.splitext(name)[0],
                                                 group="Texture Plugger")
            url = res.identifier().url()
            self.env_cache[path] = url
        dp.set_environment_resource(rs.ResourceID.from_url(url))
        self.apply_tone_mapping()
        self.applied_env = name
        owner = next((n for n, i in self.sets().items()
                      if isinstance(i, dict) and i.get("environment") == name), None)
        self.log("Blender lighting applied" + (f" (baked around '{owner}')" if owner else ""))
        return True

    # ------------------------------------------------------------------ camera
    def frame_set(self, set_name):
        """Point the camera at one texture set (keeps the viewing direction)."""
        import substance_painter.display as dp
        import substance_painter.project as pj
        cam = dp.Camera.get_default_camera()
        fwd, _up = basis_from_euler(cam.rotation)
        info = self.set_info(set_name)
        fit = self.fit()
        if info and fit and info.get("min") is not None:
            lo = apply_fit(fit, info["min"])
            hi = apply_fit(fit, info["max"])
            center, radius = box_center_radius(lo, hi)
        else:
            bb = pj.get_scene_bounding_box()
            center, radius = list(bb.center), bb.radius
            if set_name:
                self.log(f"no Blender bounds for '{set_name}': framed the whole scene "
                         "(press Send to Painter in Blender)", warn=True)
        pos, dist = frame_position(center, radius, fwd, cam.field_of_view, self.viewport_aspect())
        cam.position = pos
        try:
            cam.focus_distance = dist
        except Exception:
            pass
        if cam.projection_type == dp.CameraProjectionType.Orthographic:
            cam.orthographic_height = radius * 2 * FRAME_MARGIN
        return True

    def focus_center(self, set_name=None):
        """(centre, radius) of the texture set being worked on, in Painter's scene."""
        import substance_painter.project as pj
        info = self.set_info(set_name or self.active_set_name())
        fit = self.fit()
        if info and fit and info.get("min") is not None:
            return box_center_radius(apply_fit(fit, info["min"]), apply_fit(fit, info["max"]))
        bb = pj.get_scene_bounding_box()
        return list(bb.center), bb.radius

    def orbit(self, yaw=0.0, pitch=0.0, zoom=1.0, set_name=None):
        """Orbit and/or zoom about the focused object's centre (not the cursor)."""
        import substance_painter.display as dp
        if not self.project_ready():
            return False
        cam = dp.Camera.get_default_camera()
        center, _r = self.focus_center(set_name)
        pos, _f = orbit_around(cam.position, center, yaw, pitch)
        if zoom != 1.0:
            pos = zoom_about(pos, center, zoom)
            if cam.projection_type == dp.CameraProjectionType.Orthographic:
                cam.orthographic_height = max(1e-3, cam.orthographic_height * zoom)
        fwd = _norm([center[i] - pos[i] for i in range(3)])
        cam.position = pos
        cam.rotation = euler_from_basis(fwd)
        try:
            cam.focus_distance = math.sqrt(sum((pos[i] - center[i]) ** 2 for i in range(3)))
        except Exception:
            pass
        return True

    def match_view(self):
        import substance_painter.display as dp
        view = (self.data or {}).get("view")
        fit = self.fit()
        if not view or not fit:
            self.log("no Blender view yet: press Send to Painter in Blender", warn=True)
            return False
        cam = dp.Camera.get_default_camera()
        cam.projection_type = (dp.CameraProjectionType.Orthographic if view.get("ortho")
                               else dp.CameraProjectionType.Perspective)
        cam.field_of_view = float(view.get("fov_v", 40.0))      # Painter's FOV is vertical
        cam.position = apply_fit(fit, view["position"])
        cam.rotation = euler_from_basis(view["forward"], view["up"])
        if view.get("ortho"):
            cam.orthographic_height = float(view.get("ortho_height", 1.0)) * fit[0]
        self.log("camera matched to the Blender view")
        return True

    # -------------------------------------------------------------------- solo
    def solo(self, set_name=None):
        import substance_painter.textureset as ts
        if not self.project_ready():
            return False
        set_name = set_name or self.active_set_name()
        if not set_name:
            return False
        try:
            ts.set_active_stack(ts.Stack.from_name(set_name))
        except Exception as ex:
            self.log(f"cannot select '{set_name}': {ex}", warn=True)
            return False
        from PySide6 import QtCore

        def isolate():       # one event-loop turn after the set change has landed
            if self.soloing and self.set_focus_mode(True) is None:
                self.log("Painter's Focus Mode button was not found: click Focus Mode in "
                         "the Texture Set List to hide the other sets", warn=True)
        QtCore.QTimer.singleShot(0, isolate)
        self.soloing = True
        self.last_active_set = set_name
        self.frame_set(set_name)
        if self.auto_light:
            self.apply_lighting(set_name)
        self.log(f"solo '{set_name}' (Alt + drag on it to orbit)")
        self.refresh_ui()
        return True

    def unsolo(self, frame_all=True):
        import substance_painter.display as dp
        import substance_painter.project as pj
        self.set_focus_mode(False)
        self.soloing = False
        self.log("showing all texture sets")
        if frame_all and self.project_ready():
            cam = dp.Camera.get_default_camera()
            fwd, _ = basis_from_euler(cam.rotation)
            bb = pj.get_scene_bounding_box()
            cam.position, _d = frame_position(bb.center, bb.radius, fwd, cam.field_of_view,
                                              self.viewport_aspect())
        self.refresh_ui()

    # ------------------------------------------------------------------ events
    def _on_project_ready(self, _e):
        self.env_cache.clear()
        self.applied_env = None
        self.soloing = False
        # already "seen": the first tick must not swap to the first set's lighting
        self.last_active_set = self.active_set_name()
        self.reload_link(force=True)
        if self.auto_light:
            try:
                # Painter opens on its first texture set; light the one Blender sent
                self.apply_lighting(self.sent_set())
            except Exception as ex:
                self.log(f"could not apply lighting: {ex}", warn=True)
        self.refresh_ui()
        from PySide6 import QtCore

        def setup():
            try:
                self.apply_scene_setup()
            except Exception as ex:
                self.log(f"scene setup failed: {ex}", warn=True)
        QtCore.QTimer.singleShot(1500, setup)      # let Painter finish loading the mesh

    def apply_tone_mapping(self):
        """Use the Painter tone mapping that matches Blender's view transform.

        Blender's AgX/Filmic roll bright light off; Painter's default "Linear" clips
        it, so a room lit by strong lamps would look blown out. Not available in
        colour-managed (OCIO/ACEScg) projects, which handle this themselves.
        """
        import substance_painter.display as dp
        want = (self.data or {}).get("tone_mapping")
        if not want:
            return False
        try:
            fn = getattr(dp.ToneMappingFunction, str(want))
            if dp.get_tone_mapping() != fn:
                dp.set_tone_mapping(fn)
            return True
        except Exception:      # e.g. colour-managed projects: never break the lighting
            return False

    def apply_scene_setup(self):
        """New whole-scene project from Blender: size its texture sets and save it.

        Blender picks a texture size that keeps the whole scene within the
        computer's memory and a project file per part; both are applied once,
        to a project that has never been saved, so later choices made in
        Painter are never overridden.
        """
        import substance_painter.project as pj
        import substance_painter.textureset as ts
        sc = (self.data or {}).get("scene")
        if not isinstance(sc, dict) or not self.project_ready():
            return False
        try:
            if pj.file_path():
                return False
        except Exception:
            return False
        names = self.texture_set_names()
        linked = match_sets(names, self.sets())
        if not linked:
            return False
        done = []
        try:
            res = int(sc.get("resolution") or 0)
        except (TypeError, ValueError):
            res = 0
        if res >= 128:
            try:
                todo = [t for t in ts.all_texture_sets()
                        if ts_name(t) in linked and t.get_resolution().width != res]
                if todo:
                    ts.set_resolutions(todo, ts.Resolution(res, res))
                done.append(f"{len(linked)} texture sets at {res} px")
            except Exception as ex:
                self.log(f"could not set the texture size: {ex}", warn=True)
        proj = sc.get("project")
        if proj and not os.path.exists(proj):
            try:
                pj.save_as(proj)
                done.append(f"saved as {os.path.basename(proj)}")
            except Exception as ex:
                self.log(f"could not save the project: {ex}", warn=True)
        if sc.get("parts", 1) and int(sc.get("parts", 1)) > 1:
            done.insert(0, f"part {sc.get('part')} of {sc.get('parts')}")
        if done:
            self.log("scene ready: " + ", ".join(done))
        return True

    def _on_project_closing(self, _e):
        self.soloing = False
        self.applied_env = None

    def refresh_ui(self):
        if self.widget is not None:
            try:
                self.widget.refresh()
            except RuntimeError:
                pass

    def close(self):
        import substance_painter.event as ev
        self.timer.stop()
        for event, fn in self._handlers:
            try:
                ev.DISPATCHER.disconnect(event, fn)
            except Exception:
                pass


def _make_panel(bridge):
    from PySide6 import QtCore, QtWidgets

    class Panel(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("TexturePluggerBlenderLink")
            self.setWindowTitle("Blender Link")
            lay = QtWidgets.QVBoxLayout(self)
            self.info = QtWidgets.QLabel()
            self.info.setWordWrap(True)
            lay.addWidget(self.info)

            self.list = QtWidgets.QListWidget()
            self.list.setToolTip("Double-click to solo a texture set and frame it")
            self.list.itemDoubleClicked.connect(lambda it: bridge.solo(it.data(QtCore.Qt.UserRole)))
            lay.addWidget(self.list, 1)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Solo")
            b.setToolTip("Show only the selected texture set and frame it")
            b.clicked.connect(lambda: bridge.solo(self.selected()))
            row.addWidget(b)
            b = QtWidgets.QPushButton("Frame")
            b.setToolTip("Frame the selected texture set without hiding the others")
            b.clicked.connect(lambda: bridge.frame_set(self.selected()))
            row.addWidget(b)
            b = QtWidgets.QPushButton("Show All")
            b.setToolTip("Leave solo and frame the whole scene")
            b.clicked.connect(lambda: bridge.unsolo())
            row.addWidget(b)
            lay.addLayout(row)

            row = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("Match Blender View")
            b.clicked.connect(bridge.match_view)
            row.addWidget(b)
            b = QtWidgets.QPushButton("Apply Lighting")
            b.setToolTip("Use the lighting baked in Blender as the environment map")
            b.clicked.connect(lambda: bridge.apply_lighting(self.selected(), force=True))
            row.addWidget(b)
            lay.addLayout(row)

            grid = QtWidgets.QGridLayout()
            for text, tip, col, args in (
                    ("◀", "Orbit left around the object", 0, dict(yaw=-15.0)),
                    ("▲", "Orbit up around the object", 1, dict(pitch=10.0)),
                    ("▼", "Orbit down around the object", 2, dict(pitch=-10.0)),
                    ("▶", "Orbit right around the object", 3, dict(yaw=15.0)),
                    ("Zoom +", "Move closer to the object", 4, dict(zoom=0.8)),
                    ("Zoom -", "Move away from the object", 5, dict(zoom=1.25))):
                b = QtWidgets.QPushButton(text)
                b.setToolTip(tip)
                b.clicked.connect(lambda _=False, a=args: bridge.orbit(set_name=self.selected(), **a))
                grid.addWidget(b, 0, col)
            lay.addLayout(grid)

            self.auto = QtWidgets.QCheckBox("Use Blender lighting automatically")
            self.auto.setChecked(bridge.auto_light)
            self.auto.toggled.connect(self._auto)
            lay.addWidget(self.auto)
            self.follow = QtWidgets.QCheckBox("While soloing, frame the texture set I pick")
            self.follow.setChecked(bridge.follow)
            self.follow.toggled.connect(lambda v: setattr(bridge, "follow", v))
            lay.addWidget(self.follow)
            hint = QtWidgets.QLabel("Arrow buttons orbit around the selected object's centre at the "
                                    "current zoom. Alt + left-drag orbits around the point under "
                                    "the cursor instead.")
            hint.setWordWrap(True)
            hint.setEnabled(False)
            lay.addWidget(hint)
            self.status = QtWidgets.QLabel()
            self.status.setWordWrap(True)
            lay.addWidget(self.status)
            self.refresh()

        def _auto(self, v):
            bridge.auto_light = v
            if v:
                bridge.apply_lighting(force=True)

        def selected(self):
            it = self.list.currentItem()
            return it.data(QtCore.Qt.UserRole) if it is not None else bridge.active_set_name()

        def select_set(self, name):
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.data(QtCore.Qt.UserRole) == name:
                    self.list.setCurrentItem(it)
                    return

        def set_status(self, text):
            self.status.setText(text)

        def refresh(self):
            d = bridge.data
            if not d:
                self.info.setText("No link from Blender yet. In Blender select your object "
                                  "and press Send to Painter (Texture Plugger panel).")
            else:
                env = d.get("environment") or "none"
                blend = d.get("blend") or "unsaved .blend"
                self.info.setText(f"Linked to {blend}. Lighting: {'yes' if env != 'none' else 'not sent'}"
                                  f"{' (soloing)' if bridge.soloing else ''}")
            current = self.selected() if self.list.count() else None
            self.list.clear()
            names = bridge.texture_set_names() if bridge.project_ready() else []
            linked = bridge.sets()
            keys = match_sets(names, linked)
            for n in names:
                tag = []
                if n in keys:
                    tag.append("bounds")
                    if linked[keys[n]].get("environment"):
                        tag.append("own lighting")
                it = QtWidgets.QListWidgetItem(n + (f"   [{', '.join(tag)}]" if tag else "   [not in Blender link]"))
                it.setData(QtCore.Qt.UserRole, n)
                self.list.addItem(it)
            self.select_set(current or bridge.active_set_name())

    return Panel()


_bridge = None
_ui_elements = []


def start_plugin():
    global _bridge
    from PySide6 import QtGui, QtWidgets
    import substance_painter.ui as ui
    _bridge = Bridge()
    panel = _make_panel(_bridge)
    _bridge.widget = panel
    dock = ui.add_dock_widget(panel)
    _ui_elements.append(dock)

    menu = QtWidgets.QMenu("Texture Plugger", ui.get_main_window())
    for text, fn in (("Solo Active Texture Set", lambda: _bridge.solo()),
                     ("Show All", lambda: _bridge.unsolo()),
                     ("Frame Active Texture Set", lambda: _bridge.frame_set(_bridge.active_set_name())),
                     ("Orbit Left", lambda: _bridge.orbit(yaw=-15.0)),
                     ("Orbit Right", lambda: _bridge.orbit(yaw=15.0)),
                     ("Orbit Up", lambda: _bridge.orbit(pitch=10.0)),
                     ("Orbit Down", lambda: _bridge.orbit(pitch=-10.0)),
                     ("Zoom In", lambda: _bridge.orbit(zoom=0.8)),
                     ("Zoom Out", lambda: _bridge.orbit(zoom=1.25)),
                     ("Match Blender View", lambda: _bridge.match_view()),
                     ("Apply Blender Lighting", lambda: _bridge.apply_lighting(force=True))):
        act = QtGui.QAction(text, menu)
        act.triggered.connect(fn)
        menu.addAction(act)
    ui.add_menu(menu)
    _ui_elements.append(menu)
    _bridge.menu = menu
    _bridge.reload_link(force=True)
    _bridge.timer.start()
    if _bridge.project_ready():
        _bridge._on_project_ready(None)


def close_plugin():
    global _bridge
    import substance_painter.ui as ui
    if _bridge is not None:
        _bridge.close()
    for el in _ui_elements:
        try:
            ui.delete_ui_element(el)
        except Exception:
            pass
    _ui_elements.clear()
    _bridge = None


if __name__ == "__main__":
    start_plugin()
