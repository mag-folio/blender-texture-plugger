# SPDX-License-Identifier: GPL-3.0-or-later
"""Unwrap many meshes with Smart Seams at once, in background Blender processes.

Smart Seams works on one object at a time on one processor core, and a whole
scene has hundreds of objects. The pool writes the meshes to a temporary
.blend, starts a few background Blenders (``unwrap_worker.py``) that share
the work, and copies each finished mesh's UVs and seams back as it arrives.

* Blender stays responsive meanwhile: nothing heavy runs in this process.
* A worker's scene holds one object, so Smart Seams doesn't pay for the size
  of the user's scene on every step.
* The result is the same as pressing Smart Seams Unwrap on the object: same
  code, same settings, same mesh and same place in the world.
* Nothing in the user's file changes until a mesh is finished and checked, so
  a failed or stopped unwrap leaves the mesh exactly as it was.

When the pool can't be used (see ``usable``) the caller unwraps in this
process as before.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import bpy
import numpy as np

from . import uv_unwrap

# Below both of these the unwrap is quicker in this process than starting
# background Blenders (about two seconds each).
MIN_TASKS = 6
MIN_FACES = 3000
# Dense meshes run under Smart Seams' time budget, which counts seconds. More
# processes than real cores would slow each one down and cost them shells.
MAX_WORKERS = 16

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unwrap_worker.py")
_SKIP_SETTINGS = {"rna_type", "name", "last_report", "last_message"}


def worker_count(n_tasks):
    cores = os.cpu_count() or 2
    return max(1, min(n_tasks, cores // 2, MAX_WORKERS))


def smart_seams_folder():
    """Folder of the Smart Seams add-on that is enabled in this Blender, or None."""
    try:
        cls = bpy.ops.uv.smart_seams_unwrap.get_rna_type()
        op = getattr(bpy.types, cls.identifier, None)
        mod = sys.modules.get(op.__module__) if op is not None else None
        path = getattr(mod, "__file__", None)
    except Exception:
        return None
    if not path:
        return None
    folder = os.path.dirname(os.path.abspath(path))
    return folder if os.path.isfile(os.path.join(folder, "__init__.py")) else None


def smart_seams_settings(scene):
    """The Smart Seams panel settings as plain values."""
    s = scene.smart_seams
    out = {}
    for prop in s.bl_rna.properties:
        name = prop.identifier
        if name in _SKIP_SETTINGS or prop.is_readonly or prop.type not in {
                "BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}:
            continue
        value = getattr(s, name)
        if isinstance(value, (bool, int, float, str)):
            out[name] = value
    return out


def usable(context, obs):
    """True when unwrapping ``obs`` in background processes is possible and worth it."""
    if len(obs) < MIN_TASKS and sum(len(o.data.polygons) for o in obs) < MIN_FACES:
        return False
    if not bpy.app.binary_path or not os.path.isfile(bpy.app.binary_path):
        return False
    if not os.path.isfile(_WORKER) or smart_seams_folder() is None:
        return False
    # "Scene Camera" as the front needs the camera, which the workers don't have.
    return getattr(context.scene.smart_seams, "front", "NONE") != "CAMERA"


class Pool:
    """Background unwrap of ``obs`` (one object per mesh). Use ``start``."""

    def __init__(self, context, obs, workers=None):
        self.obs = list(obs)
        self.pending = set(range(len(self.obs)))
        self.procs = []
        self.logs = []
        self.folder = tempfile.mkdtemp(prefix="tp_unwrap_")
        try:
            self._launch(context, workers)
        except Exception:
            self.close()
            raise

    def _launch(self, context, workers):
        blend = "meshes.blend"
        bpy.data.libraries.write(os.path.join(self.folder, blend),
                                 {o.data for o in self.obs}, fake_user=True)
        # Dense meshes first: the slowest one sets how long the whole job takes.
        order = sorted(range(len(self.obs)), key=lambda i: -len(self.obs[i].data.polygons))
        job = {
            "seams_dir": smart_seams_folder(),
            "settings": smart_seams_settings(context.scene),
            "blend": blend,
            "tasks": [{"id": i, "mesh": self.obs[i].data.name,
                       "matrix": [list(row) for row in self.obs[i].matrix_world]}
                      for i in order],
        }
        with open(os.path.join(self.folder, "job.json"), "w", encoding="utf-8") as fh:
            json.dump(job, fh)
        flags = 0
        if sys.platform == "win32":
            # no console window; below normal priority keeps Blender itself snappy
            flags = 0x08000000 | 0x00004000
        self.workers = workers or worker_count(len(self.obs))
        for k in range(self.workers):
            log = open(os.path.join(self.folder, f"worker_{k}.log"), "wb")
            self.logs.append(log)
            self.procs.append(subprocess.Popen(
                [bpy.app.binary_path, "--background", "--factory-startup",
                 "--python", _WORKER, "--", self.folder, str(k)],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=flags))

    # ------------------------------------------------------------------ state
    def _stem(self, i):
        return os.path.join(self.folder, f"task_{i:05d}")

    def finished(self):
        return not self.pending

    def running(self):
        """Names of the objects being unwrapped right now."""
        return [self.obs[i].name for i in sorted(self.pending)
                if os.path.exists(self._stem(i) + ".claim")]

    def poll(self):
        """Take the results that arrived: ``[(object, error or None, fallback)]``.

        A finished mesh already has its new UVs and seams when it is returned.
        ``fallback`` is True for meshes no worker ever started (the workers
        could not run at all): the caller may unwrap those in this process.
        """
        out = []
        for i in sorted(self.pending):
            path = self._stem(i) + ".npz"
            if os.path.exists(path):
                self.pending.discard(i)
                out.append((self.obs[i], self._apply(self.obs[i], path), False))
        if self.pending and all(p.poll() is not None for p in self.procs):
            # Every worker has stopped. Look once more (a result may have landed
            # after the loop above); what is still open then never will finish.
            for i in sorted(self.pending):
                path = self._stem(i) + ".npz"
                self.pending.discard(i)
                if os.path.exists(path):
                    out.append((self.obs[i], self._apply(self.obs[i], path), False))
                elif os.path.exists(self._stem(i) + ".claim"):
                    out.append((self.obs[i], "the background unwrap stopped unexpectedly", False))
                else:
                    out.append((self.obs[i], "the background unwrap could not start", True))
        return out

    @staticmethod
    def _apply(ob, path):
        """Copy a worker's result onto ``ob``'s mesh. Returns None or a reason."""
        try:
            with np.load(path) as data:
                err = str(data["error"])
                if err:
                    return err
                uv = np.ascontiguousarray(data["uv"], dtype=np.float32)
                seams = np.ascontiguousarray(data["seams"], dtype=bool)
        except Exception as ex:
            return f"could not read the result ({type(ex).__name__})"
        me = ob.data
        if len(uv) != 2 * len(me.loops) or len(seams) != len(me.edges):
            return "the mesh changed while it was being unwrapped"
        if not np.isfinite(uv).all() or float(np.ptp(uv[0::2])) < 1e-6 or float(np.ptp(uv[1::2])) < 1e-6:
            return "no usable UVs were made"
        layer = me.uv_layers.active or me.uv_layers.new(name="UVMap")
        if layer is None:
            return "could not add a UV map"
        layer.data.foreach_set("uv", uv)
        me.edges.foreach_set("use_seam", seams)
        me.update()
        me[uv_unwrap.STAMP_KEY] = uv_unwrap._signature(me)
        return None

    def close(self):
        """Stop the workers and remove the temporary folder."""
        try:
            open(os.path.join(self.folder, "stop"), "w").close()
        except OSError:
            pass
        for p in self.procs:
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        for log in self.logs:
            try:
                log.close()
            except OSError:
                pass
        self.procs, self.logs = [], []
        shutil.rmtree(self.folder, ignore_errors=True)


def start(context, obs, workers=None):
    """A running ``Pool`` for ``obs``, or None when they should be unwrapped
    in this process (pool not usable, or it failed to start)."""
    if not usable(context, obs):
        return None
    try:
        return Pool(context, obs, workers)
    except Exception as ex:
        print(f"[Texture Plugger] background unwrap not started ({type(ex).__name__}: {ex})")
        return None
