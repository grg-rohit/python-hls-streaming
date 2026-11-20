#python-hls-streaming\app\ffmpeg_service.py
"""FFmpeg HLS encoder with per-rendition progressive completion callbacks."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VIDEOS_DIR = PROJECT_ROOT / "videos"
STREAMS_DIR = PROJECT_ROOT / "streams"
if os.getenv("HLS_STREAMS_DIR"):
    STREAMS_DIR = Path(os.environ["HLS_STREAMS_DIR"]).expanduser().resolve()

VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
SEGMENT_DURATION = 6
HYBRID_CPU_MAX_HEIGHT = 360
FULL_LADDER: list[dict] = [
    {"name": "240p", "width": 426, "height": 240, "video_bitrate": "600k", "audio_bitrate": "128k", "bandwidth": 728_000},
    {"name": "360p", "width": 640, "height": 360, "video_bitrate": "1200k", "audio_bitrate": "128k", "bandwidth": 1_328_000},
    {"name": "480p", "width": 854, "height": 480, "video_bitrate": "2500k", "audio_bitrate": "192k", "bandwidth": 2_692_000},
    {"name": "720p", "width": 1280, "height": 720, "video_bitrate": "5000k", "audio_bitrate": "192k", "bandwidth": 5_192_000},
    {"name": "1080p", "width": 1920, "height": 1080, "video_bitrate": "9000k", "audio_bitrate": "192k", "bandwidth": 9_192_000},
    {"name": "1440p", "width": 2560, "height": 1440, "video_bitrate": "16000k", "audio_bitrate": "192k", "bandwidth": 16_192_000},
    {"name": "2160p", "width": 3840, "height": 2160, "video_bitrate": "28000k", "audio_bitrate": "192k", "bandwidth": 28_192_000},
]
QUALITY_BY_NAME = {item["name"]: item for item in FULL_LADDER}


def slugify(name: str) -> str:
    slug = Path(name).stem.lower()
    slug = re.sub(r"[^\w\s-]", "", slug, flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "video"


def find_all_videos() -> list[Path]:
    if not VIDEOS_DIR.exists():
        return []
    return sorted((p for p in VIDEOS_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS), key=lambda p: p.name.lower())


def find_video_by_slug(slug: str) -> Path | None:
    return next((p for p in find_all_videos() if slugify(p.name) == slug), None)


def stream_dir(slug: str) -> Path:
    return STREAMS_DIR / slug


def streams_ready(slug: str) -> bool:
    master = stream_dir(slug) / "master.m3u8"
    return master.is_file() and any(child.is_dir() and (child / "playlist.m3u8").is_file() for child in stream_dir(slug).iterdir())


def available_qualities(slug: str) -> list[str]:
    directory = stream_dir(slug)
    if not directory.exists():
        return []
    values = [child.name for child in directory.iterdir() if child.is_dir() and (child / "playlist.m3u8").is_file()]
    return sorted(values, key=lambda value: int(value.rstrip("p")) if value.rstrip("p").isdigit() else 0)


def list_all_stream_slugs() -> list[str]:
    if not STREAMS_DIR.exists():
        return []
    return sorted(child.name for child in STREAMS_DIR.iterdir() if child.is_dir() and (child / "master.m3u8").is_file())


def _ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise FileNotFoundError("ffmpeg was not found on PATH")
    return binary


def _ffprobe_binary() -> str:
    binary = shutil.which("ffprobe")
    if not binary:
        raise FileNotFoundError("ffprobe was not found on PATH; install FFmpeg")
    return binary


def detect_gpu_encoder() -> str | None:
    result = subprocess.run([_ffmpeg_binary(), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20, check=False)
    output = result.stdout + result.stderr
    for encoder in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if encoder in output:
            return encoder
    return None


def _verify_gpu_works(source: Path, encoder: str) -> bool:
    cmd = [_ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-t", "1", "-i", str(source), "-an", "-c:v", encoder, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=45, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def probe_source(source: Path) -> tuple[int, int]:
    cmd = [_ffprobe_binary(), "-v", "error", "-print_format", "json", "-show_streams", "-select_streams", "v:0", str(source)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-2000:]}")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        rotation = int(stream.get("tags", {}).get("rotate", 0) or 0)
        if not rotation:
            for side in stream.get("side_data_list", []):
                if "rotation" in side:
                    rotation = int(side["rotation"])
                    break
        if abs(rotation) in (90, 270):
            width, height = height, width
        if width <= 0 or height <= 0:
            raise ValueError("invalid dimensions")
        return width, height
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not determine source video dimensions: {exc}") from exc


def source_short_edge(src_w: int, src_h: int) -> int:
    return min(int(src_w), int(src_h))


def quality_specs_for_source(src_w: int, src_h: int, names: list[str] | None = None) -> list[dict]:
    """Return configured rendition specs oriented to the source's aspect."""
    portrait = src_h > src_w
    selected = set(names) if names is not None else None
    result = []
    for item in FULL_LADDER:
        if selected is not None and item["name"] not in selected:
            continue
        quality = dict(item)
        quality["level_height"] = item["height"]
        if portrait:
            quality["width"], quality["height"] = item["height"], item["width"]
        result.append(quality)
    return result


def build_quality_ladder(src_w: int, src_h: int) -> list[dict]:
    """Create only renditions at or below the source's actual short edge."""
    edge = source_short_edge(src_w, src_h)
    return [q for q in quality_specs_for_source(src_w, src_h) if q["level_height"] <= edge]


def ai_upscale_targets(src_w: int, src_h: int, scale: int = 2) -> list[str]:
    """Configured target renditions above source quality and within FrameLift output size."""
    if scale not in (2, 3, 4):
        raise ValueError("scale must be 2, 3, or 4")
    edge = source_short_edge(src_w, src_h)
    if edge > 720:
        return []
    maximum_output_edge = edge * scale
    return [q["name"] for q in FULL_LADDER if edge < q["height"] <= maximum_output_edge]


def sort_qualities(names: list[str]) -> list[str]:
    order = {item["name"]: index for index, item in enumerate(FULL_LADDER)}
    return sorted(set(names), key=lambda name: order.get(name, 999))


def write_master_playlist(destination: Path, qualities: list[dict]) -> None:
    """Write a master playlist containing only currently published renditions."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]
    for quality in qualities:
        lines.extend([
            f'#EXT-X-STREAM-INF:BANDWIDTH={quality["bandwidth"]},RESOLUTION={quality["width"]}x{quality["height"]},NAME="{quality["name"]}"',
            f'{quality["name"]}/playlist.m3u8',
            "",
        ])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def _cpu_flags(quality: dict) -> list[str]:
    level = quality.get("level_height", quality["height"])
    if level <= 240:
        h264_level, profile = "3.0", "baseline"
    elif level <= 480:
        h264_level, profile = "3.1", "main"
    elif level <= 720:
        h264_level, profile = "3.2", "main"
    elif level <= 1080:
        h264_level, profile = "4.0", "high"
    else:
        h264_level, profile = "5.1", "high"
    bufsize = str(int(quality["video_bitrate"].rstrip("k")) * 2) + "k"
    return ["-c:v", "libx264", "-crf", "18", "-maxrate", quality["video_bitrate"], "-bufsize", bufsize, "-preset", "slow", "-profile:v", profile, "-level", h264_level]


def _gpu_flags(quality: dict, encoder: str) -> list[str]:
    level = quality.get("level_height", quality["height"])
    bufsize = str(int(quality["video_bitrate"].rstrip("k")) * 2) + "k"
    if encoder == "h264_nvenc":
        profile = "baseline" if level <= 240 else ("main" if level <= 720 else "high")
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-maxrate", quality["video_bitrate"], "-bufsize", bufsize, "-profile:v", profile]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "slow", "-global_quality", "19", "-maxrate", quality["video_bitrate"], "-bufsize", bufsize]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "vbr_peak", "-qp_i", "19", "-qp_p", "19", "-maxrate", quality["video_bitrate"], "-bufsize", bufsize]
    raise ValueError(f"Unsupported hardware encoder: {encoder}")


def _build_cmd(ffmpeg: str, quality: dict, source: Path, slug: str, use_gpu: bool, encoder: str | None) -> list[str]:
    width, height = quality["width"], quality["height"]
    output_dir = stream_dir(slug) / quality["name"]
    output_dir.mkdir(parents=True, exist_ok=True)
    video_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    flags = _gpu_flags(quality, encoder) if use_gpu and encoder else _cpu_flags(quality)
    segment_pattern = output_dir / "segment_%05d.ts"
    return [ffmpeg, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf", video_filter, *flags,
            "-c:a", "aac", "-b:a", quality["audio_bitrate"], "-ar", "44100", "-ac", "2",
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_DURATION})", "-f", "hls", "-hls_time", str(SEGMENT_DURATION),
            "-hls_list_size", "0", "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts", "-hls_segment_filename", str(segment_pattern), str(output_dir / "playlist.m3u8")]


def _prepare_dirs(slug: str, qualities: list[dict], clean: bool) -> None:
    destination = stream_dir(slug)
    if clean and destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for quality in qualities:
        (destination / quality["name"]).mkdir(parents=True, exist_ok=True)


def _quality_complete(slug: str, quality: dict) -> bool:
    directory = stream_dir(slug) / quality["name"]
    return (directory / "playlist.m3u8").is_file() and any(directory.glob("segment_*.ts"))


def _encode_one(ffmpeg: str, quality: dict, source: Path, slug: str, use_gpu: bool, encoder: str | None) -> None:
    mode = encoder if use_gpu and encoder else "libx264"
    print(f"[{slug}] Encoding {quality['name']} ({quality['width']}x{quality['height']}, {mode})", flush=True)
    result = subprocess.run(_build_cmd(ffmpeg, quality, source, slug, use_gpu, encoder), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed for {slug}/{quality['name']} (exit {result.returncode})")
    if not _quality_complete(slug, quality):
        raise RuntimeError(f"FFmpeg did not produce a complete rendition for {slug}/{quality['name']}")


def convert(
    source: Path,
    slug: str | None = None,
    clean: bool = True,
    resume: bool = False,
    mode: str = "auto",
    quality_names: list[str] | None = None,
    on_quality_ready: Callable[[dict, list[str]], None] | None = None,
) -> list[str]:
    """Encode renditions and invoke callback immediately after each complete quality.

    `quality_names` is used by optional AI jobs so only newly-needed higher rungs
    are encoded. Existing lower-quality renditions are never re-encoded.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"Source video not found: {source}")
    if mode not in {"auto", "gpu", "cpu", "hybrid"}:
        raise ValueError("mode must be auto, gpu, cpu, or hybrid")
    slug = slug or slugify(source.name)
    ffmpeg = _ffmpeg_binary()
    src_w, src_h = probe_source(source)
    full_ladder = build_quality_ladder(src_w, src_h) if quality_names is None else quality_specs_for_source(src_w, src_h, quality_names)
    if quality_names is not None:
        full_ladder = [item for item in full_ladder if item["name"] in set(quality_names)]
    if not full_ladder:
        raise ValueError("Source is below the lowest configured 240p HLS rendition; the original remains playable without upscaling")
    print(f"[{slug}] Source {src_w}x{src_h}; renditions: {', '.join(q['name'] for q in full_ladder)}", flush=True)

    gpu_encoder = None
    if mode in {"auto", "gpu", "hybrid"}:
        try:
            candidate = detect_gpu_encoder()
            if candidate and _verify_gpu_works(source, candidate):
                gpu_encoder = candidate
                print(f"[{slug}] Hardware encoder ready: {gpu_encoder}", flush=True)
            elif candidate:
                print(f"[{slug}] {candidate} listed but failed test encode; using CPU", flush=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[{slug}] GPU detection unavailable: {exc}", flush=True)
    if mode == "gpu" and not gpu_encoder:
        raise RuntimeError("GPU mode requested, but no working H.264 hardware encoder was found")

    _prepare_dirs(slug, full_ladder, clean=(clean and not resume))
    pending = [q for q in full_ladder if not (resume and _quality_complete(slug, q))]
    completed = [q["name"] for q in full_ladder if _quality_complete(slug, q)]

    def publish(quality: dict) -> None:
        completed.append(quality["name"])
        completed[:] = sort_qualities(completed)
        # Local master is useful for CLI conversions; the worker replaces it
        # with a combined master (including already-published AI/base qualities).
        write_master_playlist(stream_dir(slug) / "master.m3u8", [q for q in full_ladder if q["name"] in completed])
        if on_quality_ready:
            on_quality_ready(quality, list(completed))

    if mode == "hybrid" and gpu_encoder and pending:
        cpu_items = [q for q in pending if q.get("level_height", q["height"]) <= HYBRID_CPU_MAX_HEIGHT]
        gpu_items = [q for q in pending if q not in cpu_items]
        if not gpu_items:
            gpu_items, cpu_items = pending, []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(_encode_one, ffmpeg, q, source, slug, is_gpu, gpu_encoder if is_gpu else None): q for q, is_gpu in [(q, False) for q in cpu_items] + [(q, True) for q in gpu_items]}
            for future in as_completed(futures):
                try:
                    future.result()
                    publish(futures[future])
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise RuntimeError("Hybrid encoding failed: " + " | ".join(errors))
    else:
        use_gpu = bool(gpu_encoder) and mode != "cpu"
        for quality in pending:
            _encode_one(ffmpeg, quality, source, slug, use_gpu, gpu_encoder)
            publish(quality)

    if not all(_quality_complete(slug, q) for q in full_ladder):
        raise RuntimeError("One or more selected HLS renditions are incomplete")
    write_master_playlist(stream_dir(slug) / "master.m3u8", full_ladder)
    return sort_qualities([q["name"] for q in full_ladder])


def convert_all(mode: str = "auto", resume: bool = False) -> None:
    for source in find_all_videos():
        slug = slugify(source.name)
        if resume or not streams_ready(slug):
            convert(source, slug=slug, clean=not resume, resume=resume, mode=mode)
