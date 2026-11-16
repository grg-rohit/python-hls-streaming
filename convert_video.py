#python-hls-streaming\convert_video.py

#!/usr/bin/env python3
"""
convert_video.py
────────────────
CLI tool to convert videos in videos/ into adaptive HLS streams.

Usage
─────
    python convert_video.py                        # convert all videos not yet ready (auto GPU)
    python convert_video.py --all                  # re-convert every video (wipes existing)
    python convert_video.py --slug my-video        # convert one specific video
    python convert_video.py --mode cpu             # force CPU (libx264)
    python convert_video.py --mode gpu             # force GPU (NVENC/QSV/AMF)
    python convert_video.py --mode hybrid          # CPU for low qualities + GPU for high, parallel
    python convert_video.py --slug my-video --resume   # resume interrupted conversion

Docker usage
────────────
    docker compose run --rm converter                                  # CPU, all videos
    docker compose run --rm converter-gpu                              # GPU, all videos
    docker compose run --rm converter-gpu python convert_video.py --mode hybrid
    docker compose run --rm converter-gpu python convert_video.py --slug my-video --mode gpu
"""

from __future__ import annotations

import argparse
import sys

from app.ffmpeg_service import (
    convert,
    convert_all,
    find_all_videos,
    find_video_by_slug,
    slugify,
    streams_ready,
    detect_gpu_encoder,
)


def _print_gpu_status() -> None:
    encoder = detect_gpu_encoder()
    if encoder:
        print(f"  [GPU] Hardware encoder available: {encoder}")
    else:
        print("  [GPU] No hardware encoder found — CPU (libx264) will be used")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert video(s) in videos/ to adaptive HLS streams",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--slug",
        metavar="SLUG",
        help="Convert only the video matching this slug.\n"
             "Run without --slug to convert all videos.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-convert all videos even if streams already exist.",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "gpu", "cpu", "hybrid"],
        default="auto",
        help=(
            "Encoding mode:\n"
            "  auto   – try GPU first, fall back to CPU  (default)\n"
            "  gpu    – force hardware encoder\n"
            "  cpu    – always use libx264\n"
            "  hybrid – CPU for 240p/360p + GPU for 480p+ in parallel"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip quality tiers that already have complete segments.",
    )

    args = parser.parse_args()
    _print_gpu_status()

    try:
        if args.slug:
            # ── Single video by slug ──────────────────────────────────────
            source = find_video_by_slug(args.slug)
            if source is None:
                # Try treating --slug as a raw filename stem
                videos = find_all_videos()
                matches = [v for v in videos if slugify(v.name) == args.slug]
                if not matches:
                    print(f"\n[ERROR] No video found with slug '{args.slug}'", file=sys.stderr)
                    print("Available videos:", file=sys.stderr)
                    for v in find_all_videos():
                        print(f"  {slugify(v.name)}  ←  {v.name}", file=sys.stderr)
                    return 1
                source = matches[0]

            convert(
                source=source,
                slug=args.slug,
                clean=not args.resume,
                resume=args.resume,
                mode=args.mode,
            )

            if streams_ready(args.slug):
                print(f"[OK] streams/{args.slug}/ is ready.")
                return 0
            else:
                print(f"[WARN] Conversion finished but streams/{args.slug}/ is incomplete.", file=sys.stderr)
                return 1

        else:
            # ── All videos ────────────────────────────────────────────────
            videos = find_all_videos()
            if not videos:
                print("[WARN] No video files found in videos/.", file=sys.stderr)
                return 1

            print(f"\nFound {len(videos)} video(s):")
            for v in videos:
                slug = slugify(v.name)
                status = "ready" if streams_ready(slug) else "not converted"
                print(f"  {slug:<40}  {v.name}  [{status}]")
            print()

            convert_all(mode=args.mode, resume=args.resume)

            # Summary
            print("\n=== Summary ===")
            all_ok = True
            for v in videos:
                slug = slugify(v.name)
                ok   = streams_ready(slug)
                mark = "✓" if ok else "✗"
                print(f"  {mark}  streams/{slug}/")
                if not ok:
                    all_ok = False

            if all_ok:
                print("\n[OK] All streams ready.")
                print("     Run  docker compose up server  to start the player.")
                return 0
            else:
                print("\n[WARN] Some conversions failed.", file=sys.stderr)
                return 1

    except FileNotFoundError as exc:
        print(f"\n[ERROR] {exc}\n", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}\n", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"\n[ERROR] {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
