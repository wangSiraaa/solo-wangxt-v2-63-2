"""迁移命令行入口：

    python -m migrations.run --db sqlite:///./sla.db \\
        --cutoff 2026-09-25T00:00:00Z
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import create_engine

from app.clock import SystemClock, ensure_utc
from migrations.m0001_init_and_legacy import run_migration


def main() -> None:
    parser = argparse.ArgumentParser(description="run schema + legacy pause migration")
    parser.add_argument("--db", default="sqlite:///./sla.db")
    parser.add_argument("--cutoff", default=None, help="ISO8601；缺省取系统时钟")
    args = parser.parse_args()

    engine = create_engine(args.db)
    cutoff = ensure_utc(datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))) if args.cutoff else None
    summary = run_migration(engine, SystemClock(), legacy_cutoff=cutoff)
    print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
