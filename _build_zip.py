"""Build the App Hub bundles.

One install per person: App Hub keys apps by `manifest.json`'s `name` and uses it
as the directory, so each gets its own venv, port and database. The name in the
checked-in manifest is a placeholder — it is always rewritten from INSTANCES
below, so the working tree keeps one canonical manifest and the bundles cannot
drift apart in anything but the name.

Writes forward-slash paths and sets 0755 on .sh files so the bundles work when
extracted on Linux. PowerShell's Compress-Archive cannot do either.

Run it with the repo's venv:

    .venv\\Scripts\\python.exe _build_zip.py                 all instances
    .venv\\Scripts\\python.exe _build_zip.py finance-jordi   just one

then upload the zips at http://<pi-host>:8000.
"""

import os
from pathlib import Path
import sys
import zipfile

# Individually listed, because the root also holds a great deal that must never
# ship — the live database, the session signing key, credentials notes, the
# whole Windows build chain. An allow-list makes shipping those structurally
# impossible instead of relying on remembering an exclude pattern. Add new
# top-level deployables here.
ROOT_FILES = [
    "manifest.json",
    "icon.png",
    "setup.sh",
    "run.sh",
    "requirements.txt",
    "app.py",
    "db.py",
    "llm_categorize.py",
]

# Shipped whole, so new templates or static assets are picked up automatically
# and never get left out of the zip.
ASSET_DIRS = ["templates", "static"]

# Skipped anywhere under ASSET_DIRS. `uploads` is listed pre-emptively: nothing
# writes there today, but the day something does, an overlay update would
# otherwise clobber the live copies with whatever was on the build machine.
SKIP_DIRS = {"__pycache__", "uploads"}
SKIP_EXTS = {".pyc", ".pyo"}

# Belt and braces behind the allow-list. An overlay update writes the zip over
# the live install, so a database in here would replace real financial data with
# a stale snapshot — the one mistake that cannot be undone by re-uploading.
FORBIDDEN_EXTS = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"}

# One entry per person with an install on the Pi. Adding someone is a line here
# plus an upload; nothing else in the app is per-user.
INSTANCES = [
    ("finance-meinte", "Meinte"),
    ("finance-jordi", "Jordi"),
]


def rewrite_manifest(app_name, owner=None):
    """Return manifest.json bytes with `name` (and the card label) replaced."""
    import json
    import re

    manifest = json.loads(Path("manifest.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", app_name):
        raise SystemExit(
            f"Invalid app name {app_name!r}: lowercase kebab-case, 2-63 chars, "
            "must start with a letter."
        )
    manifest["name"] = app_name
    if owner:
        # Shown under the name on the App Hub grid, where two identical tiles
        # would otherwise be indistinguishable.
        manifest["description"] = f"Bankafschriften van {owner}."
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def collect_files():
    """Return the sorted list of repo-relative paths to bundle."""
    files = list(ROOT_FILES)
    for root_dir in ASSET_DIRS:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.startswith(".") or os.path.splitext(name)[1] in SKIP_EXTS:
                    continue
                files.append(os.path.join(dirpath, name).replace(os.sep, "/"))
    return sorted(files)


def build_one(app_name, owner, files):
    out = f"{app_name}.zip"
    manifest = rewrite_manifest(app_name, owner)

    if os.path.exists(out):
        os.remove(out)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(f.replace(os.sep, "/"))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if f.endswith(".sh") else 0o644) << 16
            if f == "manifest.json":
                z.writestr(info, manifest)
            else:
                with open(f, "rb") as fh:
                    z.writestr(info, fh.read())

    # Plain ASCII: this is read in cmd.exe, which mangles anything else.
    label = f"  ({owner})" if owner else ""
    print(f"  {out:34} {os.path.getsize(out):>9,} bytes{label}")
    return out


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # No argument builds every instance; a name builds just that one.
    if len(sys.argv) > 1:
        wanted = sys.argv[1].strip()
        owner = dict(INSTANCES).get(wanted)
        targets = [(wanted, owner)]
    else:
        targets = list(INSTANCES)

    missing = [f for f in ROOT_FILES if not os.path.isfile(f)]
    if missing:
        print("Missing files:", *missing, sep="\n  ")
        return 1

    files = collect_files()

    dangerous = [f for f in files if os.path.splitext(f)[1] in FORBIDDEN_EXTS]
    if dangerous:
        print("Refusing to bundle database files:", *dangerous, sep="\n  ")
        return 1

    print(f"{len(files)} files per bundle:")
    for f in files:
        print("  +", f)
    print()

    for app_name, owner in targets:
        build_one(app_name, owner, files)

    print("\nUpload at http://<pi-host>:8000. Each install gets its own")
    print("directory, port and database; nothing is shared between them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
