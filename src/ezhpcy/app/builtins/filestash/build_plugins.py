"""
Build Filestash's office plugins, `plugins/application_<name>.zip`, from
`plugin_src/application_<name>/` and their pinned libraries: downloaded, each
checked against its SHA-256, or bundled from `plugin_build/<name>/` with its
`package-lock.json` (`npm ci`, then esbuild; this needs Node.js). The zips are
the same bytes on every build.

    uv run python src/ezhpcy/app/builtins/filestash/build_plugins.py
"""

import hashlib
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
PLUGINS = ("application_docx", "application_xlsx", "application_pptx")
# Libraries to download: where they go in the zip, where they come from, and
# their SHA-256.
VENDORED = {
    "application_docx": [
        (
            "lib/vendor/jszip.min.js",
            "https://cdn.jsdelivr.net/npm/jszip@3.10.2/dist/jszip.min.js",
            "7f839b2d4688b845c105ebf5d2f9803075f91ea0fe72bdaac176c3a04dd3d2c1",
        ),
        (
            # Under the name Filestash's loader asks for.
            "lib/vendor/docx-preview.js",
            "https://cdn.jsdelivr.net/npm/docx-preview@0.4.1/dist/docx-preview.min.js",
            "c4a133c65a112799e35b143c572dfff9e923a3bf39b5fd3b07a06aa0f8f530c2",
        ),
    ],
    "application_xlsx": [
        (
            "lib/vendor/xlsx.full.min.js",
            "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js",
            "cc015130aa8521e7f088f88898eba949ccdcbfb38df0bd129b44b7273c3a6f41",
        ),
    ],
}
# Libraries to bundle: where they go in the zip, the `plugin_build/` directory
# with their `package.json`, `package-lock.json` and `entry.js`, and the global
# the bundle sets.
BUNDLED = {
    "application_pptx": [
        ("lib/vendor/pptx-glimpse.min.js", "pptx-glimpse", "PptxGlimpse"),
    ],
}
# A fixed time for every entry, so that a rebuild changes no asset's identity.
_DATE_TIME = (2026, 1, 1, 0, 0, 0)


def download(url: str, sha256: str) -> bytes:
    # cdn.sheetjs.com refuses urllib's own User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": "ezhpcy-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if (digest := hashlib.sha256(data).hexdigest()) != sha256:
        raise SystemExit(f"{url} has SHA-256 {digest}, not {sha256}")
    return data


def bundle(name: str, global_name: str) -> bytes:
    """Bundle `plugin_build/<name>/entry.js` for the browser, as one minified script."""
    npm = shutil.which("npm")
    npx = shutil.which("npx")
    if npm is None or npx is None:
        raise SystemExit(f"bundling {name} needs Node.js (npm and npx)")
    # Outside the package, so that no `node_modules/` lands in it.
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        for file in ("package.json", "package-lock.json", "entry.js"):
            shutil.copy(HERE / "plugin_build" / name / file, work / file)
        subprocess.run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=work,
            check=True,
        )
        subprocess.run(
            [npx, "--no-install", "esbuild", "entry.js", "--bundle"]
            + ["--platform=browser", "--format=iife", f"--global-name={global_name}"]
            + ["--minify", "--legal-comments=eof", "--outfile=bundle.js"],
            cwd=work,
            check=True,
        )
        return (work / "bundle.js").read_bytes()


def build(name: str) -> Path:
    source = HERE / "plugin_src" / name
    files = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    for path, url, sha256 in VENDORED.get(name, []):
        files[path] = download(url, sha256)
    for path, directory, global_name in BUNDLED.get(name, []):
        files[path] = bundle(directory, global_name)
    target = HERE / "plugins" / f"{name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(files.items()):
            entry = zipfile.ZipInfo(path, _DATE_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    return target


if __name__ == "__main__":
    for name in PLUGINS:
        target = build(name)
        print(f"{target.relative_to(HERE)}: {target.stat().st_size} bytes")
