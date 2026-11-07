#!/usr/bin/env python3
"""Build minimal Windows migration bootstrap ZIP."""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = Path(__file__).resolve().parent
PACKAGE_FILES = (
    "Install-LabKom-Agent.bat",
    "install-windows.ps1",
    "agent.py",
    "requirements.txt",
    "updater.py",
    "VERSION",
    "WINDOWS-BOOTSTRAP-README.md",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LabKom Windows migration bootstrap")
    parser.add_argument("--public-key", required=True, type=Path, help="Existing Ed25519 public PEM")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    missing = [name for name in PACKAGE_FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"Missing package files: {', '.join(missing)}")
    if not args.public_key.is_file():
        raise SystemExit(f"Public key not found: {args.public_key}")
    key = serialization.load_pem_public_key(args.public_key.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise SystemExit("Public key must be Ed25519")

    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    args.out.mkdir(parents=True, exist_ok=True)
    archive_path = args.out / f"LabKom-Agent-Windows-Upgrade-v{version}.zip"
    if archive_path.exists():
        raise SystemExit(f"Refusing to overwrite existing output: {archive_path}")

    with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in PACKAGE_FILES:
            archive.write(ROOT / name, name)
        archive.write(args.public_key, "release-public-key.pem")

    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum_path.write_text(f"{sha256(archive_path)}  {archive_path.name}\n", encoding="ascii")
    print(archive_path)
    print(checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
