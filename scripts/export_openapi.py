"""导出当前应用 OpenAPI 规范到仓库根目录 openapi.json：

    python scripts/export_openapi.py
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app


def main() -> None:
    app = create_app(db_url="sqlite://")
    spec = app.openapi()
    target = Path(__file__).resolve().parent.parent / "openapi.json"
    target.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {target} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()
