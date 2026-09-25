#!/usr/bin/env python3
"""Start the SLA pause service.

    python3 run_server.py --db sla.db --host 127.0.0.1 --port 8080
"""

import argparse

from sla.api import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rectification SLA pause service")
    parser.add_argument("--db", default="sla.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.host, args.port, args.db)
