#python-hls-streaming\run.py
#!/usr/bin/env python3
"""Start the FastAPI application server."""
from __future__ import annotations
import argparse
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the HLS streaming API server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    print(f"\nHLS Streaming API: http://localhost:{args.port}")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
