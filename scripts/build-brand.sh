#!/usr/bin/env bash
# Render the brand mark to the PNG sizes Home Assistant's brand proxy serves.
set -euo pipefail

cd "$(dirname "$0")/.."

SRC=assets/brand.svg
OUT=custom_components/perific/brand

mkdir -p "$OUT"

.venv/bin/python - "$SRC" "$OUT" <<'PY'
import sys

import cairosvg

src, out = sys.argv[1], sys.argv[2]

for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
    cairosvg.svg2png(url=src, write_to=f"{out}/{name}", output_width=size, output_height=size)
    print(f"{out}/{name}  {size}x{size}")
PY
