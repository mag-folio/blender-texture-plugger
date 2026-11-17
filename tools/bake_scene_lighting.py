# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake Lighting to UVs for every mesh of a .blend, without opening Blender's window,
then (optionally) Send to Painter.  The same steps as pressing the buttons, so the
lightmaps land in Painter's shared link folder exactly as the button puts them.

    blender -b "C:\\path\\Scene.blend" --python tools\\bake_scene_lighting.py -- [--send]
            [--size AUTO|SET|256|512|1024] [--samples N] [--work DIR]

* Runs with YOUR Blender preferences and the INSTALLED Texture Plugger add-on (do
  not pass --factory-startup): lightmap size, samples, GPU and lighting settings
  are the ones you chose.
* Bakes in a second background Blender on a copy of the file (like the button), on
  the GPU when there is one.  Your .blend is never saved or changed.
* ``--size`` / ``--samples`` override the add-on preferences for this run.
* ``--send``: afterwards Send to Painter with the file's active object (lighting
  and the Material Preview lighting; the last viewport view sent is kept).
* Run it once per .blend whose Painter project you want lit.  Lightmaps of several
  .blend files live side by side in the link folder (they are named after the
  texture sets), so an older project keeps working.

Close the same .blend in Blender first if you changed it there: this reads the
saved file.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import bpy


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    send = "--send" in args
    work = args[args.index("--work") + 1] if "--work" in args else tempfile.mkdtemp(prefix="tp_bake_")
    os.makedirs(work, exist_ok=True)

    mod = next((m for m in bpy.context.preferences.addons.keys() if m.endswith("texture_plugger")),
               None)
    if mod is None:
        raise SystemExit("Texture Plugger is not enabled in this Blender (run without "
                         "--factory-startup, with the add-on installed)")
    pkg = sys.modules[mod]
    lb, sl, pl = pkg.light_bake, pkg.scene_link, pkg.painter_link
    ctx = bpy.context
    active = ctx.view_layer.objects.active
    meshes = [o for o in ctx.scene.objects if o.type == "MESH"]
    for o in ctx.view_layer.objects:
        o.select_set(o in meshes)
    jobs, skipped = lb.jobs_for(ctx)
    if skipped:
        print(f"[bake] no UV map, skipped: {', '.join(skipped[:10])}")
    if not jobs:
        raise SystemExit("No Texture Plugger texture sets in this file (Create Sets for Scene first)")
    p = pkg.prefs.get_prefs(ctx)
    samples = int(getattr(p, "bake_samples", 256)) if p else 256
    size = str(getattr(p, "lightmap_size", "AUTO")) if p else "AUTO"
    if "--samples" in args:
        samples = int(args[args.index("--samples") + 1])
    if "--size" in args:              # AUTO, SET, or pixels (128 / 256 / 512 / 1024)
        size = args[args.index("--size") + 1].upper()
    folder = sl.link_dir()
    key = lb.current_lighting_key(ctx)
    print(f"[bake] {os.path.basename(bpy.data.filepath)}: {len(jobs)} texture sets, "
          f"{samples} samples, size {size}, into {folder}")

    copy = os.path.join(work, "scene.blend")
    bpy.ops.wm.save_as_mainfile(filepath=copy, copy=True, relative_remap=True, compress=False,
                                check_existing=False)
    job = {"package": os.path.dirname(lb.__file__), "scene": ctx.scene.name,
           "view_layer": ctx.view_layer.name, "folder": folder, "samples": samples,
           "size": size, "scene_res": lb.scene_resolution(), "lighting_key": key,
           "preview": None, "gpu": lb._gpu_settings(),
           "jobs": [{"material": m.name, "objects": [o.name for o in objs]} for objs, m in jobs]}
    with open(os.path.join(work, "job.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    t0 = time.time()
    log_path = os.path.join(work, "worker.log")
    with open(log_path, "wb") as log:
        code = subprocess.call([bpy.app.binary_path, "--background", "--factory-startup", copy,
                                "--python", lb._WORKER, "--", work],
                               stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        with open(os.path.join(work, "progress.json"), encoding="utf-8") as fh:
            pr = json.load(fh)
    except (OSError, ValueError):
        pr = {"done_sets": 0, "total": len(jobs), "errors": ["no progress file"]}
    print(f"[bake] worker exit {code} after {time.time() - t0:.0f} s: {pr.get('done_sets')}/"
          f"{pr.get('total')} sets" + (f", errors: {pr['errors'][:3]}" if pr.get("errors") else ""))
    if code != 0:
        print(f"[bake] worker log: {log_path}")
    pl.write_lightmaps(folder, {})            # drop maps nothing lists (as the button does)

    if send:
        target = active if active is not None and active.type == "MESH" else jobs[0][0][0]
        old = pl.read_link(pl.link_file(folder)) or {}
        msg, warnings = sl.send(ctx, target, lighting=True, view=False, scene=old.get("scene"))
        print(f"[bake] {msg}" + (f" ({'; '.join(warnings)})" if warnings else ""))


main()
