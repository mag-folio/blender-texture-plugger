# SPDX-License-Identifier: GPL-3.0-or-later
"""Background Blender process that bakes lightmaps for "Bake Lighting to UVs".

Started by ``light_bake.TEXTUREPLUGGER_OT_bake_lighting_uv`` as

    blender --background --factory-startup <copy of the scene>.blend
            --python light_bake_worker.py -- <job folder>

It loads Texture Plugger's own modules from the folder named in ``job.json``
(without registering anything), bakes the listed texture sets with
``light_bake.bake_sets`` and, after every chunk, adds the finished lightmaps
to ``blender_lightmaps.json`` itself and updates ``progress.json``.  So the
work done so far is kept even if Blender is closed or the bake is stopped.
It only writes to the job folder and the Painter link folder.
"""

import importlib.util
import json
import os
import sys
import time
import traceback

import bpy

PACKAGE_NAME = "tp_bake_pkg"


def load_package(folder):
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, os.path.join(folder, "__init__.py"), submodule_search_locations=[folder])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


GPU_KINDS = ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")


def use_gpu(gpu, scene):
    """Bake on a GPU: the one the user's Blender uses for Cycles, else the best one
    this computer has (many people never switch Cycles from "None" to their card,
    and the CPU is several times slower).  Returns the device kind or None.
    Only this background Blender's settings change."""
    try:
        cp = bpy.context.preferences.addons["cycles"].preferences
    except Exception:
        return None
    order = ([gpu["type"]] if gpu and gpu.get("type") in GPU_KINDS else []) + list(GPU_KINDS)
    for kind in order:
        try:
            cp.compute_device_type = kind
            cp.refresh_devices()
            devs = [d for d in cp.devices if d.type == kind]
            if not devs:
                continue
            wanted = set(gpu.get("devices", ())) if gpu and gpu.get("type") == kind else set()
            for d in cp.devices:
                d.use = d.type == kind and (not wanted or d.id in wanted)
            if not any(d.use for d in cp.devices):
                for d in devs:
                    d.use = True
            scene.cycles.device = "GPU"
            return kind
        except Exception:
            continue
    try:
        cp.compute_device_type = "NONE"
    except Exception:
        pass
    return None


def main():
    folder = sys.argv[sys.argv.index("--") + 1]
    with open(os.path.join(folder, "job.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    progress = {"done": 0, "done_sets": 0, "total": len(job["jobs"]), "errors": [],
                "started": time.time(), "finished": False}
    ppath = os.path.join(folder, "progress.json")
    write_json(ppath, progress)
    pkg = load_package(job["package"])
    lb, pl = pkg.light_bake, pkg.painter_link

    scene = bpy.data.scenes.get(job.get("scene")) or bpy.context.scene
    win = bpy.context.window
    if win is not None and win.scene != scene:
        win.scene = scene
    ctx = bpy.context
    jobs = []
    for j in job["jobs"]:
        mat = bpy.data.materials.get(j["material"])
        objs = [o for o in (bpy.data.objects.get(n) for n in j["objects"]) if o is not None]
        if mat is not None and objs:
            jobs.append((objs, mat))
        else:
            progress["errors"].append(f"{j['material']}: not found in the copy of the scene")
    gpu = use_gpu(job.get("gpu"), ctx.scene)
    print(f"[light bake] {len(jobs)} sets, {gpu or 'CPU'}, "
          f"{job['samples']} samples, size {job['size']}", flush=True)

    def on_chunk(batch, done, total):
        pl.write_lightmaps(job["folder"], batch, prune=False)
        progress["done"] = done
        progress["done_sets"] += len(batch)
        write_json(ppath, progress)
        print(f"[light bake] {done}/{total} ({time.time() - progress['started']:.0f} s)", flush=True)

    def stop():
        return os.path.exists(os.path.join(folder, "stop"))

    args = (ctx, jobs, job["folder"], job["samples"], job.get("preview"), job["size"],
            job.get("scene_res", 0), job.get("lighting_key"))
    texels = job.get("chunk_texels") or (lb.CHUNK_TEXELS_GPU if gpu else lb.CHUNK_TEXELS)
    entries, errors = lb.bake_sets(*args, on_chunk=on_chunk, stop=stop, chunk_texels=texels,
                                   merge=job.get("merge", True))
    if gpu and not entries and errors:        # the GPU refused: once more on the CPU
        print("[light bake] GPU bake failed, retrying on the CPU:", errors[:1], flush=True)
        ctx.scene.cycles.device = "CPU"
        entries, errors = lb.bake_sets(*args, on_chunk=on_chunk, stop=stop,
                                       chunk_texels=lb.CHUNK_TEXELS, merge=job.get("merge", True))
    progress["errors"] += errors
    progress["done"] = len(jobs)
    progress["finished"] = True
    write_json(ppath, progress)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.exit(1)
