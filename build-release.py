#!/usr/bin/env python3
"""Build an immutable signed LabKom Agent release bundle."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent
FILES = ("agent.py", "requirements.txt", "updater.py", "VERSION")


def canonical_manifest(manifest: dict) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build signed LabKom Agent release")
    parser.add_argument("--private-key", type=Path, required=True, help="Ed25519 PEM private key outside repository")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--minimum-updater-version", default="1.0.0")
    args = parser.parse_args()

    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"Missing release files: {', '.join(missing)}")
    key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Private key must be Ed25519")

    release_dir = args.out / version
    release_dir.mkdir(parents=True, exist_ok=False)
    artifact = release_dir / "labkom-agent.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = {
        "artifact": artifact.name,
        "minimumUpdaterVersion": args.minimum_updater_version,
        "sha256": digest,
        "sizeBytes": artifact.stat().st_size,
        "version": version,
    }
    signature = base64.b64encode(key.sign(canonical_manifest(manifest))).decode("ascii")
    manifest["signature"] = signature
    (release_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(release_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
