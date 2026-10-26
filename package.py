"""Build deterministic Lambda and standalone source archives."""

import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
RUNTIME_FILES = ("LICENSE", "lambda_function.py")
SOURCE_FILES = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "interaction_models/en-GB.json",
    "interaction_models/it-IT.json",
    "lambda_function.py",
    "package.py",
    "test_bridge.py",
)


def _write_archive(path, files, *, prefix=""):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for filename in sorted(files):
            archive_name = prefix + filename
            info = zipfile.ZipInfo(archive_name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info,
                (ROOT / filename).read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def build(runtime_only=False):
    DIST.mkdir(exist_ok=True)
    _write_archive(DIST / "halexia.zip", RUNTIME_FILES, prefix="alexa_bridge/")
    if not runtime_only:
        missing = [name for name in SOURCE_FILES if not (ROOT / name).is_file()]
        if missing:
            raise FileNotFoundError("Source archive requires: " + ", ".join(missing))
        _write_archive(DIST / "halexia-source.zip", SOURCE_FILES)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--runtime-only"]):
        raise SystemExit("usage: python package.py [--runtime-only]")
    build(runtime_only=sys.argv[1:] == ["--runtime-only"])
