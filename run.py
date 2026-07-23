#!/usr/bin/env python3
"""
run.py
──────
Starts the FastAPI / Uvicorn server on  http://0.0.0.0:8000

Usage
-----
    python run.py                       # default host/port
    python run.py --port 9000           # custom port
    python run.py --reload              # enable auto-reload (dev mode)

Alternatively, run Uvicorn directly:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import argparse
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the HLS streaming server")
    parser.add_argument("--host",   default="0.0.0.0",  help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",   default=8000, type=int, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true",    help="Enable auto-reload")
    args = parser.parse_args()

    print(f"\n  Starting HLS Streaming Server")
    print(f"  URL  : http://localhost:{args.port}")
    print(f"  Host : {args.host}  (LAN devices can use your laptop IP)")
    print(f"  Reload: {'on' if args.reload else 'off'}\n")

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
