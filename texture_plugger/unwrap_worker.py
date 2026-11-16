# SPDX-License-Identifier: GPL-3.0-or-later
"""Background Blender process that unwraps meshes with Smart Seams.

Started by ``unwrap_pool`` as

    blender --background --factory-startup --python unwrap_worker.py -- <job folder> <worker number>

It is a plain script, not part of the add-on package: it must not import
anything from Texture Plugger. Several of these run at once and share the
tasks in ``job.json``: a worker takes a task by creating its ``.claim`` file
(only one process can create a file), loads that one mesh from the job's
.blend, runs Smart Seams on it exactly like the button does, and writes the
UVs and seams to ``<task>.npz``. Nothing but the job folder is written to.
"""

import importlib.util
import json
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix

MODULE_NAME = "tp_worker_smart_seams"


def load_smart_seams(folder):
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, os.path.join(folder, "__init__.py"), submodule_search_locations=[folder])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    mod.register()
    return mod


def apply_settings(scene, values):
    s = scene.smart_seams
    for name, value in values.items():
        try:
            setattr(s, name, value)
        except (AttributeError, TypeError, ValueError):
            pass                          # a setting this Smart Seams doesn't have


def unwrap(scene, view_layer, blend, task):
    """Returns ``(uv, seams, shells, None)`` or ``(None, None, 0, reason)``."""
    with bpy.data.libraries.load(blend, link=False) as (_src, dst):
        dst.meshes = [task["mesh"]]
    me = dst.meshes[0]
    if me is None:
        return None, None, 0, "mesh missing from the job file"
    ob = bpy.data.objects.new("tp_unwrap", me)
    try:
        scene.collection.objects.link(ob)
        ob.matrix_world = Matrix(task["matrix"])
        view_layer.update()
        for o in view_layer.objects:
            o.select_set(o is ob)
        view_layer.objects.active = ob
        err = None
        try:
            res = bpy.ops.uv.smart_seams_unwrap("EXEC_DEFAULT")
            if "FINISHED" not in res:
                err = scene.smart_seams.last_message or "cancelled"
        except RuntimeError as ex:
            text = str(ex).strip()
            err = text.splitlines()[-1] if text else "failed"
        if err is None and me.uv_layers.active is None:
            err = scene.smart_seams.last_message or "no UVs were made"
        if err is not None:
            return None, None, 0, err
        uv = np.zeros(2 * len(me.loops), dtype=np.float32)
        me.uv_layers.active.data.foreach_get("uv", uv)
        seams = np.zeros(len(me.edges), dtype=bool)
        me.edges.foreach_get("use_seam", seams)
        shells = 0
        try:
            report = json.loads(scene.smart_seams.last_report or "{}").get(ob.name, {})
            shells = int(report.get("islands", report.get("charts", 0)) or 0)
        except (ValueError, TypeError):
            pass
        return uv, seams, shells, None
    finally:
        try:
            if ob.mode != "OBJECT":
                view_layer.objects.active = ob
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        bpy.data.objects.remove(ob, do_unlink=True)
        if me.users == 0:
            bpy.data.meshes.remove(me)


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    folder = args[0]
    with open(os.path.join(folder, "job.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    load_smart_seams(job["seams_dir"])
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    for ob in list(bpy.data.objects):     # the factory scene's cube, light and camera
        bpy.data.objects.remove(ob, do_unlink=True)
    apply_settings(scene, job["settings"])
    blend = os.path.join(folder, job["blend"])
    stop = os.path.join(folder, "stop")
    for task in job["tasks"]:
        if os.path.exists(stop):
            break
        stem = os.path.join(folder, f"task_{task['id']:05d}")
        try:
            os.close(os.open(stem + ".claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue                      # another worker has it
        t0 = time.perf_counter()
        try:
            uv, seams, shells, err = unwrap(scene, view_layer, blend, task)
        except Exception as ex:
            traceback.print_exc()
            uv = seams = None
            shells = 0
            err = f"failed ({type(ex).__name__}: {str(ex).strip()[:120]})"
        secs = round(time.perf_counter() - t0, 2)
        print(f"[worker] task {task['id']} {task['mesh']}: {err or 'ok'} in {secs} s", flush=True)
        tmp = stem + ".tmp.npz"
        if err is None:
            np.savez(tmp, uv=uv, seams=seams, shells=shells, seconds=secs, error="")
        else:
            np.savez(tmp, error=err, seconds=secs)
        os.replace(tmp, stem + ".npz")    # appears complete or not at all


main()
