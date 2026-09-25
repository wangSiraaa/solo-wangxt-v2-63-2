#!/usr/bin/env python3
"""Regenerate the committed ``openapi.json`` from the route registry."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sla.openapi import build_openapi  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"

if __name__ == "__main__":
    spec = build_openapi()
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(spec['paths'])} paths)")
