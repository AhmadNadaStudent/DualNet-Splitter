"""Build an allowlisted Windows release; never copy local settings or logs."""
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if (sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64")
            or struct.calcsize("P") != 8):
        raise SystemExit("This release builder requires Windows x64 and a 64-bit Python runtime.")
    version = (ROOT / "VERSION").read_text().strip()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "DualNet.spec"], cwd=ROOT, check=True)
    output = ROOT / "release"
    output.mkdir(exist_ok=True)
    archive = output / f"DualNet-Splitter-v{version}-windows-x64.zip"
    allowed = {
        "DualNet.exe": ROOT / "dist/DualNet.exe",
        "routing_rules.json": ROOT / "routing_rules.json",
        "README.md": ROOT / "README.md",
        "THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
        "RELEASE_NOTES.md": ROOT / "RELEASE_NOTES.md",
        "SECURITY.md": ROOT / "SECURITY.md",
        "VERSION": ROOT / "VERSION",
    }
    for file in (ROOT / "docs/images").glob("*.png"):
        allowed[file.relative_to(ROOT).as_posix()] = file
    for file in (ROOT / "docs/licenses").glob("*"):
        if file.is_file():
            allowed[file.relative_to(ROOT).as_posix()] = file
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, path in sorted(allowed.items()):
            bundle.write(path, name)
        for package in ("Pillow", "pystray", "colorama", "six", "PyInstaller"):
            dist = importlib.metadata.distribution(package)
            for file in dist.files or []:
                if any(word in file.name.lower() for word in ("license", "copying", "notice")):
                    path = Path(dist.locate_file(file))
                    if path.is_file():
                        bundle.write(path, f"licenses/{package}/{file.as_posix()}")
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
        if python_license.exists():
            bundle.write(python_license, "licenses/Python/LICENSE.txt")
        tcl_root = Path(sys.base_prefix) / "tcl"
        for file in tcl_root.glob("*/license*"):
            if file.is_file():
                bundle.write(file, "licenses/TclTk/" + file.relative_to(tcl_root).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"archive": archive.name, "sha256": digest, "bytes": archive.stat().st_size}))


if __name__ == "__main__":
    main()
