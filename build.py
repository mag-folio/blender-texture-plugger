"""Build the installable zip into dist/.

Usage:  python build.py [--blender PATH]   (validates with Blender when given/found)

The zip contains a single top-level ``texture_plugger/`` folder holding
``blender_manifest.toml``.  Blender's extension installer accepts that layout
(manifest in one sub-directory) and so does the legacy add-on installer.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = "texture_plugger"
SRC = os.path.join(ROOT, PKG)
DIST = os.path.join(ROOT, "dist")

EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache"}
EXCLUDE_EXT = {".pyc", ".pyo", ".zip", ".blend1"}


def manifest_version():
    with open(os.path.join(SRC, "blender_manifest.toml"), encoding="utf-8") as fh:
        m = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
    if not m:
        sys.exit("version not found in blender_manifest.toml")
    return m.group(1)


def find_blender(explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = shutil.which("blender")
    if found:
        return found
    base = r"C:\Program Files\Blender Foundation"
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, name, "blender.exe")
            if os.path.isfile(exe):
                return exe
    return None


def build():
    version = manifest_version()
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"{PKG}-{version}.zip")
    if os.path.exists(out):
        os.remove(out)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in EXCLUDE_EXT:
                    continue
                full = os.path.join(root, f)
                arc = os.path.join(PKG, os.path.relpath(full, SRC)).replace(os.sep, "/")
                zf.write(full, arc)
                count += 1
    print(f"Built {out} ({count} files)")
    return out


def validate(zip_path, blender):
    cmd = [blender, "--factory-startup", "--command", "extension", "validate", zip_path]
    print("Validating:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print((proc.stdout or "") + (proc.stderr or ""))
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender", help="Path to blender executable used for validation")
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()
    zip_path = build()
    if args.no_validate:
        return 0
    blender = find_blender(args.blender)
    if not blender:
        print("Blender not found: skipped manifest validation")
        return 0
    return 0 if validate(zip_path, blender) else 1


if __name__ == "__main__":
    sys.exit(main())
