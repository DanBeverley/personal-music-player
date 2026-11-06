"""Generate Brotli sidecars for a compiled Flutter web release."""
from argparse import ArgumentParser
from pathlib import Path

import brotli


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    build = parser.parse_args().build
    entrypoint = build / "main.dart.js"
    if not entrypoint.is_file():
        parser.error("expected a compiled JavaScript Flutter web build")

    for path in [entrypoint, *sorted((build / "canvaskit").rglob("canvaskit.wasm"))]:
        content = path.read_bytes()
        compressed = brotli.compress(content, quality=11)
        path.with_suffix(path.suffix + ".br").write_bytes(compressed)
        print(f"{path.relative_to(build)}: {len(content):,} to {len(compressed):,} bytes")


if __name__ == "__main__":
    main()
