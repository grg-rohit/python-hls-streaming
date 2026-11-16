"""
FFmpeg service — multi-video, slug-based adaptive HLS encoding.

Each video gets its own namespace under streams/:
    streams/<slug>/master.m3u8
    streams/<slug>/240p/
    streams/<slug>/720p/
    ...

The slug is derived from the source filename:
    "Sniper Ghost Warrior 2026.mp4"  →  "sniper-ghost-warrior-2026"
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
VIDEOS_DIR:   Path = PROJECT_ROOT / "videos"
STREAMS_DIR:  Path = PROJECT_ROOT / "streams"

VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".mov", ".avi", ".webm")

# ---------------------------------------------------------------------------
# Quality ladder
# ---------------------------------------------------------------------------
FULL_LADDER: list[dict] = [
    {"name": "240p",  "width":  426, "height":  240, "video_bitrate":   "600k", "audio_bitrate": "128k", "bandwidth":    728_000},
    {"name": "360p",  "width":  640, "height":  360, "video_bitrate":  "1200k", "audio_bitrate": "128k", "bandwidth":  1_328_000},
    {"name": "480p",  "width":  854, "height":  480, "video_bitrate":  "2500k", "audio_bitrate": "192k", "bandwidth":  2_692_000},
    {"name": "720p",  "width": 1280, "height":  720, "video_bitrate":  "5000k", "audio_bitrate": "192k", "bandwidth":  5_192_000},
    {"name": "1080p", "width": 1920, "height": 1080, "video_bitrate":  "9000k", "audio_bitrate": "192k", "bandwidth":  9_192_000},
    {"name": "1440p", "width": 2560, "height": 1440, "video_bitrate": "16000k", "audio_bitrate": "192k", "bandwidth": 16_192_000},
    {"name": "2160p", "width": 3840, "height": 2160, "video_bitrate": "28000k", "audio_bitrate": "192k", "bandwidth": 28_192_000},
]

HYBRID_CPU_MAX_HEIGHT: int = 360
SEGMENT_DURATION:      int = 6


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """
    Convert a filename (without extension) to a safe directory slug.
    "Sniper Ghost Warrior 2026.07.22 - 20.52.54" → "sniper-ghost-warrior-2026-07-22-20-52-54"
    """
    stem = Path(name).stem if "." in name else name
    slug = stem.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)       # remove non-word chars except - and space
    slug = re.sub(r"[\s_]+", "-", slug)         # spaces/underscores → hyphens
    slug = re.sub(r"-{2,}", "-", slug)          # collapse multiple hyphens
    slug = slug.strip("-")
    return slug or "video"


# ---------------------------------------------------------------------------
# Video discovery
# ---------------------------------------------------------------------------

def find_all_videos() -> list[Path]:
    """Return all video files in videos/, sorted by name."""
    if not VIDEOS_DIR.exists():
        return []
    files: list[Path] = []
    for ext in VIDEO_EXTENSIONS:
        files.extend(VIDEOS_DIR.glob(f"*{ext}"))
    return sorted(files, key=lambda p: p.name.lower())


def find_video_by_slug(slug: str) -> Path | None:
    """Find the video file whose slug matches the given slug."""
    for video in find_all_videos():
        if slugify(video.name) == slug:
            return video
    return None


# ---------------------------------------------------------------------------
# Per-video stream paths
# ---------------------------------------------------------------------------

def stream_dir(slug: str) -> Path:
    """Return the streams/<slug>/ directory path."""
    return STREAMS_DIR / slug


def streams_ready(slug: str) -> bool:
    """True when streams/<slug>/master.m3u8 and at least one quality playlist exist."""
    master = stream_dir(slug) / "master.m3u8"
    if not master.exists():
        return False
    for item in stream_dir(slug).iterdir():
        if item.is_dir() and (item / "playlist.m3u8").exists():
            return True
    return False


def available_qualities(slug: str) -> list[str]:
    """Quality names for a video, sorted ascending by height."""
    sd = stream_dir(slug)
    if not sd.exists():
        return []
    return sorted(
        [d.name for d in sd.iterdir() if d.is_dir() and (d / "playlist.m3u8").exists()],
        key=lambda n: int(n.rstrip("p")) if n.rstrip("p").isdigit() else 0,
    )


def list_all_stream_slugs() -> list[str]:
    """Return all slugs that have a master.m3u8 (i.e. conversion is complete)."""
    if not STREAMS_DIR.exists():
        return []
    return [
        d.name for d in sorted(STREAMS_DIR.iterdir())
        if d.is_dir() and (d / "master.m3u8").exists()
    ]


# ---------------------------------------------------------------------------
# FFmpeg binaries
# ---------------------------------------------------------------------------

def _ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FileNotFoundError(
            "ffmpeg not found on PATH.\n"
            "  Windows : winget install ffmpeg\n"
            "  Ubuntu  : sudo apt install ffmpeg\n"
            "  macOS   : brew install ffmpeg"
        )
    return binary


def _ffprobe_binary() -> str:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise FileNotFoundError("ffprobe not found. It ships with FFmpeg — reinstall FFmpeg.")
    return binary


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

def detect_gpu_encoder() -> str | None:
    """Return the best available hardware H.264 encoder name, or None."""
    ffmpeg = _ffmpeg_binary()
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout + result.stderr
    except Exception:
        return None

    for encoder, label in [
        ("h264_nvenc", "NVENC (Nvidia)"),
        ("h264_qsv",   "QuickSync (Intel)"),
        ("h264_amf",   "AMF (AMD)"),
    ]:
        if encoder in output:
            print(f"  GPU encoder detected : {label}  ({encoder})")
            return encoder
    return None


def _verify_gpu_works(ffmpeg: str, source: Path, encoder: str) -> bool:
    """1-second test encode to confirm the encoder is actually usable."""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-t", "1", "-i", str(source),
        "-c:v", encoder, "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Source probing
# ---------------------------------------------------------------------------

def probe_source(source: Path) -> tuple[int, int]:
    """Return (width, height) of the source video, corrected for rotation."""
    ffprobe = _ffprobe_binary()
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "v:0",
        str(source),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data   = json.loads(result.stdout)
        stream = data["streams"][0]
        w, h   = int(stream["width"]), int(stream["height"])

        # Rotation can be stored in tags.rotate (old) or side_data_list (new)
        rotation = 0
        tags = stream.get("tags", {})
        if "rotate" in tags:
            rotation = int(tags["rotate"])
        else:
            for side in stream.get("side_data_list", []):
                if "rotation" in side:
                    rotation = int(side["rotation"])
                    break

        if abs(rotation) in (90, 270):
            w, h = h, w

        return w, h
    except Exception as exc:
        print(f"  [WARN] Could not probe ({exc}). Assuming 1920x1080.")
        return 1920, 1080


# ---------------------------------------------------------------------------
# Quality ladder builder
# ---------------------------------------------------------------------------

def build_quality_ladder(src_w: int, src_h: int) -> list[dict]:
    src_max = max(src_w, src_h)
    ladder  = [q for q in FULL_LADDER if q["height"] <= src_max]
    if src_max > FULL_LADDER[-1]["height"]:
        ladder.append({
            "name": f"{src_max}p", "width": src_w, "height": src_h,
            "video_bitrate": "20000k", "audio_bitrate": "192k", "bandwidth": 20_192_000,
        })
    return ladder or [FULL_LADDER[0]]


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------

def _cpu_flags(quality: dict) -> list[str]:
    h = quality["height"]
    # H.264 level: use the minimum level that supports the resolution
    if h <= 240:
        h264_level, profile = "3.0", "baseline"
    elif h <= 480:
        h264_level, profile = "3.1", "main"
    elif h <= 720:
        h264_level, profile = "3.2", "main"
    elif h <= 1080:
        h264_level, profile = "4.0", "high"
    else:
        h264_level, profile = "4.2", "high"

    bufsize = str(int(quality["video_bitrate"].rstrip("k")) * 2) + "k"

    return [
        "-c:v",       "libx264",
        # CRF drives quality; maxrate acts as a ceiling — do NOT set -b:v with CRF
        "-crf",       "18",
        "-maxrate",   quality["video_bitrate"],
        "-bufsize",   bufsize,
        "-preset",    "slow",
        "-profile:v", profile,
        "-level",     h264_level,
    ]


def _gpu_flags(quality: dict, encoder: str) -> list[str]:
    h = quality["height"]
    bufsize = str(int(quality["video_bitrate"].rstrip("k")) * 2) + "k"

    if encoder == "h264_nvenc":
        # NVENC: use CQ for quality, maxrate as ceiling — no -b:v with CQ
        return [
            "-c:v",       "h264_nvenc",
            "-preset",    "p7",
            "-tune",      "hq",
            "-rc",        "vbr",
            "-cq",        "18",
            "-maxrate",   quality["video_bitrate"],
            "-bufsize",   bufsize,
            "-profile:v", "baseline" if h <= 240 else ("main" if h <= 720 else "high"),
        ]
    elif encoder == "h264_qsv":
        return [
            "-c:v",            "h264_qsv",
            "-preset",         "slow",
            "-global_quality", "18",
            "-maxrate",        quality["video_bitrate"],
            "-bufsize",        bufsize,
        ]
    else:  # h264_amf
        return [
            "-c:v",     "h264_amf",
            "-quality", "quality",
            "-rc",      "vbr_latency",
            "-qmin",    "18",
            "-maxrate", quality["video_bitrate"],
            "-bufsize",  bufsize,
        ]


def _build_cmd(
    ffmpeg: str, quality: dict, source: Path,
    slug: str, use_gpu: bool, encoder: str | None,
) -> list[str]:
    w, h    = quality["width"], quality["height"]
    out_dir = stream_dir(slug) / quality["name"]

    # scale + pad preserves aspect ratio and pads to exact dimensions.
    # format=yuv420p converts 10-bit (yuv420p10le) and HDR sources to
    # standard 8-bit 4:2:0 which libx264 / NVENC require.
    # transpose is handled automatically by the display matrix in the
    # container — no explicit rotate filter needed.
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
        f"format=yuv420p"
    )

    video_flags = _gpu_flags(quality, encoder) if (use_gpu and encoder) else _cpu_flags(quality)

    return [
        ffmpeg, "-y",
        "-i", str(source),
        # apply display rotation metadata so portrait videos are upright
        "-vf", vf,
        *video_flags,
        "-c:a", "aac", "-b:a", quality["audio_bitrate"], "-ar", "44100", "-ac", "2",
        "-g", str(SEGMENT_DURATION * 30), "-keyint_min", str(SEGMENT_DURATION * 30),
        "-sc_threshold", "0",
        "-f", "hls", "-hls_time", str(SEGMENT_DURATION),
        "-hls_list_size", "0", "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "segment_%03d.ts"),
        str(out_dir / "playlist.m3u8"),
    ]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _prepare_dirs(slug: str, qualities: list[dict], clean: bool) -> None:
    sd = stream_dir(slug)
    if clean and sd.exists():
        for item in sd.iterdir():
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    sd.mkdir(parents=True, exist_ok=True)
    for q in qualities:
        (sd / q["name"]).mkdir(parents=True, exist_ok=True)


def _quality_complete(slug: str, quality: dict) -> bool:
    d = stream_dir(slug) / quality["name"]
    return (d / "playlist.m3u8").exists() and bool(list(d.glob("segment_*.ts")))


def _write_master(slug: str, qualities: list[dict]) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]
    for q in qualities:
        lines += [
            f'#EXT-X-STREAM-INF:BANDWIDTH={q["bandwidth"]},'
            f'RESOLUTION={q["width"]}x{q["height"]},NAME="{q["name"]}"',
            f'{q["name"]}/playlist.m3u8', "",
        ]
    (stream_dir(slug) / "master.m3u8").write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓  master.m3u8 written → {stream_dir(slug)}")


# ---------------------------------------------------------------------------
# Single-quality encode
# ---------------------------------------------------------------------------

def _encode_one(
    ffmpeg: str, quality: dict, source: Path,
    slug: str, use_gpu: bool, encoder: str | None,
) -> None:
    name = quality["name"]
    mode = encoder if (use_gpu and encoder) else "libx264"
    print(f"\n{'='*60}\n  {slug}  {name}  ({quality['width']}x{quality['height']}, {mode})\n{'='*60}")
    cmd    = _build_cmd(ffmpeg, quality, source, slug, use_gpu, encoder)
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed for {slug}/{name} (exit {result.returncode})")
    print(f"  ✓  {slug}/{name} done")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert(
    source: Path,
    slug:   str  | None = None,
    clean:  bool        = True,
    resume: bool        = False,
    mode:   str         = "auto",
) -> None:
    """
    Convert a single video file into adaptive HLS streams.

    Args:
        source: Path to the source video file.
        slug:   Output folder name under streams/. Auto-derived from filename if None.
        clean:  Wipe existing streams for this video before encoding.
        resume: Skip quality tiers that already have complete segments.
        mode:   "auto" | "gpu" | "cpu" | "hybrid"
    """
    if slug is None:
        slug = slugify(source.name)

    print(f"\n=== HLS Conversion: {source.name} → streams/{slug}/ ===\n")

    ffmpeg = _ffmpeg_binary()
    src_w, src_h = probe_source(source)
    print(f"  resolution : {src_w}x{src_h}")

    qualities = build_quality_ladder(src_w, src_h)
    print(f"  qualities  : {', '.join(q['name'] for q in qualities)}")
    print(f"  mode       : {mode}")

    # GPU detection
    gpu_encoder: str | None = None
    if mode in ("auto", "gpu", "hybrid"):
        gpu_encoder = detect_gpu_encoder()
        if gpu_encoder:
            print(f"  Verifying {gpu_encoder}…")
            if _verify_gpu_works(ffmpeg, source, gpu_encoder):
                print(f"  ✓ {gpu_encoder} working")
            else:
                print(f"  ✗ {gpu_encoder} failed runtime test — falling back to CPU")
                gpu_encoder = None
        if mode == "gpu" and not gpu_encoder:
            raise RuntimeError("GPU mode requested but no working hardware encoder found.")

    _prepare_dirs(slug, qualities, clean=(clean and not resume))

    if mode == "hybrid" and gpu_encoder:
        cpu_q = [q for q in qualities if q["height"] <= HYBRID_CPU_MAX_HEIGHT]
        gpu_q = [q for q in qualities if q["height"] >  HYBRID_CPU_MAX_HEIGHT]
        if not gpu_q:
            gpu_q, cpu_q = qualities, []

        print(f"\n  Hybrid — CPU: {[q['name'] for q in cpu_q]}  GPU: {[q['name'] for q in gpu_q]}")

        tasks = [(q, False) for q in cpu_q] + [(q, True) for q in gpu_q]
        errors: list[str] = []

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = {
                pool.submit(
                    _encode_one, ffmpeg, q, source, slug,
                    use_gpu, gpu_encoder if use_gpu else None,
                ): q["name"]
                for q, use_gpu in tasks
                if not (resume and _quality_complete(slug, q))
            }
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    errors.append(str(exc))

        if errors:
            raise RuntimeError("Hybrid encoding errors:\n  " + "\n  ".join(errors))

    else:
        use_gpu = bool(gpu_encoder) and mode != "cpu"
        for q in qualities:
            if resume and _quality_complete(slug, q):
                print(f"  ✓  {q['name']} already complete — skipping")
                continue
            _encode_one(ffmpeg, q, source, slug, use_gpu, gpu_encoder)

    _write_master(slug, qualities)
    print(f"\n=== Done: streams/{slug}/ ===\n")


def convert_all(mode: str = "auto", resume: bool = False) -> None:
    """Convert every video in videos/ that does not yet have complete streams."""
    videos = find_all_videos()
    if not videos:
        print("[WARN] No video files found in videos/")
        return
    for video in videos:
        slug = slugify(video.name)
        if resume or not streams_ready(slug):
            convert(source=video, slug=slug, clean=False, resume=resume, mode=mode)
        else:
            print(f"  ✓  {slug} already ready — skipping")
