"""
Fetch this folder's own data/ (~1.3G) from a Google Drive archive, so this folder is runnable
standalone - clone/unzip it anywhere, `make setup && make download-data && make webpage`, no
other project needed.

Usage: .venv/bin/python download_data.py --file-id <GDRIVE_FILE_ID> [--force]

Standalone copy of the same generic script used by the main synthetic-grids-claude-agents
project and by impedance-estimation/ - only PROJECT_ROOT/DEFAULT_MARKERS differ. This one and
impedance-estimation/download_data.py are pointed at the SAME Drive archive (see that project's
docstring) - they read ~99% the same data, no reason to maintain two near-identical archives.

Provenance: the main project's `make shared-data-archive` - see README.md.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_MARKERS = [
    "data/grid_topology",
    "data/power_flow_results",
    "data/load_power",
]


def extract_file_id(raw: str) -> str:
    m = re.search(r"/d/([\w-]+)", raw) or re.search(r"[?&]id=([\w-]+)", raw)
    return m.group(1) if m else raw


def already_populated(project_root: Path, markers: list[str]) -> bool:
    return all((project_root / m).exists() for m in markers)


def download_archive(file_id: str, archive_path: Path) -> None:
    import gdown

    print(f"Downloading archive from Google Drive (file id {file_id}) -> {archive_path}")
    gdown.download(id=file_id, output=str(archive_path), quiet=False)
    if not archive_path.exists():
        sys.exit(f"gdown reported success but {archive_path} does not exist - aborting.")


def extract_archive(archive_path: Path, dest: Path) -> None:
    print(f"Extracting {archive_path} into {dest} ...")
    if shutil.which("tar"):
        subprocess.run(["tar", "-xf", str(archive_path), "-C", str(dest)], check=True)
    else:
        with tarfile.open(archive_path) as tf:
            tf.extractall(dest)  # noqa: S202 - trusted, self-produced archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-id", required=True, help="Drive file id or share URL of the archive.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT))
    parser.add_argument("--marker", action="append", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-archive", action="store_true")
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    markers = args.marker or DEFAULT_MARKERS

    if already_populated(dest, markers) and not args.force:
        print("Already populated (found all marker paths) - skipping download. Pass --force to redo.")
        return

    dest.mkdir(parents=True, exist_ok=True)
    archive_path = dest / "data-archive-download.tar.gz"

    download_archive(extract_file_id(args.file_id), archive_path)
    extract_archive(archive_path, dest)

    if not already_populated(dest, markers):
        sys.exit("Extraction finished but expected marker paths are still missing - check the archive.")

    if args.keep_archive:
        print(f"Keeping downloaded archive at {archive_path}.")
    else:
        archive_path.unlink()

    print("Done.")


if __name__ == "__main__":
    main()
