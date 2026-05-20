#!/usr/bin/env python3
"""Build the hilmar-daily-tracker Cowork plugin (.plugin file).

The plugin bundles exactly one skill -- hilmar-daily-tracker -- whose CANONICAL
source lives at <repo>/.claude/skills/hilmar-daily-tracker/. It is committed
there so that every Claude Code session on this repo -- local CLI, claude.ai/
code on the web, and the iOS Claude app -- auto-discovers it. To avoid keeping
a second copy of the skill in git (which would drift -- see QC-040), this
script copies the canonical skill into the plugin staging dir at build time.

Layout:
  .claude/skills/hilmar-daily-tracker/        canonical skill source (in git)
  plugin-build/hilmar-daily-tracker/
      .claude-plugin/plugin.json              plugin manifest (in git)
      README.md                               plugin readme (in git)
      skills/hilmar-daily-tracker/            <- copied here at build (gitignored)
  plugin-build/hilmar-daily-tracker.plugin    <- build output (gitignored)

Usage:
  python plugin-build/build_plugin.py

Cross-device install is the repo skill at .claude/skills/ -- it travels with
the repo. This .plugin file is only needed to add the skill to a Cowork
workspace on one specific machine (it does not sync across devices).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_SRC = os.path.join(REPO, ".claude", "skills", "hilmar-daily-tracker")
PLUGIN_DIR = os.path.join(REPO, "plugin-build", "hilmar-daily-tracker")
SKILLS_PARENT = os.path.join(PLUGIN_DIR, "skills")
SKILLS_DEST = os.path.join(SKILLS_PARENT, "hilmar-daily-tracker")
OUTPUT = os.path.join(REPO, "plugin-build", "hilmar-daily-tracker.plugin")


def main() -> int:
    if not os.path.isdir(SKILL_SRC):
        print("ERROR: canonical skill not found at " + SKILL_SRC, file=sys.stderr)
        return 1
    manifest = os.path.join(PLUGIN_DIR, ".claude-plugin", "plugin.json")
    if not os.path.isfile(manifest):
        print("ERROR: plugin manifest not found at " + manifest, file=sys.stderr)
        return 1

    # Refresh the bundled skill copy from the canonical source.
    if os.path.isdir(SKILLS_PARENT):
        shutil.rmtree(SKILLS_PARENT)
    shutil.copytree(SKILL_SRC, SKILLS_DEST)
    print("staged skill: " + SKILL_SRC + " -> " + SKILLS_DEST)

    # Zip the plugin dir into a .plugin file. shutil.make_archive walks the
    # whole tree (hidden .claude-plugin/ included) and writes forward slashes.
    if os.path.exists(OUTPUT):
        os.remove(OUTPUT)
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "hilmar-daily-tracker")
        zip_path = shutil.make_archive(base, "zip", PLUGIN_DIR)
        shutil.move(zip_path, OUTPUT)

    with zipfile.ZipFile(OUTPUT) as z:
        bad = z.testzip()
        entries = z.namelist()
    print("built: " + OUTPUT)
    print("size : %d bytes, %d entries" % (os.path.getsize(OUTPUT), len(entries)))
    print("integrity: " + ("OK" if bad is None else "CORRUPT: " + str(bad)))
    print("install  : open the .plugin file in Claude and accept it.")
    return 0 if bad is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
