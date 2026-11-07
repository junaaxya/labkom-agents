#!/usr/bin/env python3
"""Build an immutable LabKom Agent release bundle (signed when --private-key is supplied)."""

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
    parser = argparse.ArgumentParser(description="Build LabKom Agent release")
    parser.add_argument("--private-key", type=Path, required=False, help="Ed25519 PEM private key outside repository")
    parser.add_argument("--version", default="", help="Override release version (defaults to VERSION file)")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--output", type=Path, default=None, help="Explicit output ZIP path")
    parser.add_argument("--minimum-updater-version", default="1.0.0")
    args = parser.parse_args()

    version = args.version.strip() or (ROOT / "VERSION").read_text(encoding="ascii").strip()
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"Missing release files: {', '.join(missing)}")

    versioned_artifact = args.output if args.output is not None else (args.out / f"labkom-agent-{version}.zip")
    versioned_artifact.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(versioned_artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)

    if args.private_key is not None:
        key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit("Private key must be Ed25519")

        release_dir = args.out / version
        release_dir.mkdir(parents=True, exist_ok=True)
        artifact = release_dir / "labkom-agent.zip"
        artifact.write_bytes(versioned_artifact.read_bytes())

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

    print(versioned_artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
