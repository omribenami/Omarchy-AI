"""Render QML helper paths for every plugin, including paths with spaces."""
import json
import sys
from pathlib import Path

directory, executable = sys.argv[1:]
replacement = json.dumps(executable)[1:-1]
for path in Path(directory).rglob('*.qml'):
    text = path.read_text()
    if '@OMARCHY_AI_SETTINGS@' in text:
        path.write_text(text.replace('@OMARCHY_AI_SETTINGS@', replacement))
