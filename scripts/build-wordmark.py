#!/usr/bin/env python3
"""Regenerate the watchdog overlay's Wordmark.js from the wordmark bitmap.

`scripts/assets/omarchy-wordmark.txt` is the Omarchy wordmark sampled from
the real logo raster (252x53, '#' = ink), carried over from the design
project's own omarchy-wordmark.json rather than hand-drawn.

Emits two things the QML needs and should not recompute at runtime:

  runs  — horizontal spans [x, y, length] of the solid mark, so the crisp
          wordmark is ~663 fillRects a frame instead of 13356.
  cells — the coarse 42x9 grid, one flying glyph per cell. A cell is on
          when at least THRESHOLD of its step x step block is ink; 0.3
          reproduces the design project's own `cells` list exactly
          (verified against its rows 0 and 8).
"""
import io
import json
import pathlib

STEP = 6
THRESHOLD = 0.3
ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "assets" / "omarchy-wordmark.txt"
DST = ROOT / "quickshell" / "plugins" / "omarchy-ai.watchdog" / "Wordmark.js"


def main() -> None:
    rows = io.open(SRC, encoding="utf-8").read().rstrip("\n").split("\n")
    h, w = len(rows), len(rows[0])
    if any(len(r) != w for r in rows):
        raise SystemExit("wordmark rows are not all the same width")
    cols, crows = w // STEP, -(-h // STEP)

    cells = []
    for cy in range(crows):
        for cx in range(cols):
            block = [rows[y][x]
                     for y in range(cy * STEP, min(h, cy * STEP + STEP))
                     for x in range(cx * STEP, min(w, cx * STEP + STEP))]
            if block and sum(c == "#" for c in block) / len(block) >= THRESHOLD:
                cells.append([cx, cy])

    runs = []
    for y, row in enumerate(rows):
        x = 0
        while x < w:
            if row[x] == "#":
                start = x
                while x < w and row[x] == "#":
                    x += 1
                runs.append([start, y, x - start])
            else:
                x += 1

    js = (
        "// GENERATED — do not hand-edit. Rebuild with scripts/build-wordmark.py.\n"
        "//\n"
        "// The Omarchy wordmark as a %dx%d bitmap, sampled from the real logo\n"
        "// raster rather than hand-drawn. Used by the wake-up animation in\n"
        "// ActivationEffects.qml.\n"
        "//\n"
        "//   runs  — horizontal spans [x, y, length] of the solid mark. %d rects\n"
        "//           a frame; a per-pixel loop would be %d.\n"
        "//   cells — the coarse %dx%d grid, one flying glyph per cell.\n"
        ".pragma library\n"
        "var w = %d, h = %d, step = %d, cols = %d, rows = %d\n"
        "var runs = %s\n"
        "var cells = %s\n"
    ) % (w, h, len(runs), w * h, cols, crows, w, h, STEP, cols, crows,
         json.dumps(runs, separators=(",", ",")),
         json.dumps(cells, separators=(",", ",")))
    DST.write_text(js, encoding="utf-8")
    print("wrote %s: runs=%d cells=%d" % (DST, len(runs), len(cells)))


if __name__ == "__main__":
    main()
