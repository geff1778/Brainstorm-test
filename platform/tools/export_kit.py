#!/usr/bin/env python3
"""Export a portable copy of the platform.

Produces a self-contained directory (and optional tar.gz) that can be dropped
into any OpenHands runtime and bootstrapped. Excludes the database, caches and
any file that could hold a secret.

Usage:
    python tools/export_kit.py --out dist/ohp-kit
    python tools/export_kit.py --out dist/ohp-kit --archive
"""
from __future__ import annotations

import argparse
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INCLUDE = [
    "ohp",
    "tests",
    "docs",
    "tools",
    "config",
    ".agents",
    "README.md",
    "ROADMAP.md",
    "pyproject.toml",
    "bootstrap.sh",
    ".env.example",
    "LICENSE",
]

# Never ship these, even if they appear inside an included directory.
EXCLUDE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "data",
    "scratch",
    ".env",
}
EXCLUDE_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc"}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in EXCLUDE_NAMES or Path(name).suffix in EXCLUDE_SUFFIXES:
            ignored.add(name)
    return ignored


def export(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for item in INCLUDE:
        src = ROOT / item
        if not src.exists():
            continue
        dst = out / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_ignore)
        else:
            shutil.copy2(src, dst)
    return out


def archive(path: Path) -> Path:
    tar_path = path.with_suffix(".tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(path, arcname=path.name)
    return tar_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the portable OHP kit")
    parser.add_argument("--out", default="dist/ohp-kit", help="output directory")
    parser.add_argument("--archive", action="store_true", help="also write a .tar.gz")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    export(out)
    print(f"exported kit -> {out}")
    if args.archive:
        tar = archive(out)
        print(f"archive      -> {tar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
