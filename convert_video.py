#python-hls-streaming\convert_video.py
#!/usr/bin/env python3
"""Optional local CLI for testing FFmpeg against files in ./videos."""
from __future__ import annotations
import argparse
import sys
from app.ffmpeg_service import convert, find_all_videos, find_video_by_slug, slugify, streams_ready


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert local videos into adaptive HLS streams")
    parser.add_argument("--slug", help="Convert only the local video matching this slug")
    parser.add_argument("--all", action="store_true", help="Re-encode every local video")
    parser.add_argument("--mode", choices=["auto", "gpu", "cpu", "hybrid"], default="auto")
    parser.add_argument("--resume", action="store_true", help="Skip completed rendition folders")
    args = parser.parse_args()
    try:
        if args.slug:
            source = find_video_by_slug(args.slug)
            if source is None:
                print(f"No local video found for slug {args.slug!r}", file=sys.stderr)
                return 1
            convert(source, slug=args.slug, clean=not args.resume, resume=args.resume, mode=args.mode)
            return 0 if streams_ready(args.slug) else 1
        videos = find_all_videos()
        if not videos:
            print("No local videos found in ./videos")
            return 0
        if not args.all:
            videos = [video for video in videos if not streams_ready(slugify(video.name))]
        for video in videos:
            convert(video, slug=slugify(video.name), clean=not args.resume, resume=args.resume, mode=args.mode)
        return 0
    except Exception as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
