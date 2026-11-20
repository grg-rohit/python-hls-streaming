#python-hls-streaming\app\ai_upscaler.py
"""FrameLift adapter used by the standalone processing worker."""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("hls.ai_upscaler")
SUPPORTED_MODELS = {"realesr-animevideov3", "realesrgan-x4plus", "realesrnet-x4plus", "realesrgan-x4plus-anime-6B"}
SUPPORTED_SCALES = {2, 3, 4}
SUPPORTED_DEVICES = {"auto", "cpu", "cuda"}
SUPPORTED_PROFILES = {"none", "minimal", "soft_camera", "old_tv", "heavy_noise"}
SUPPORTED_ENCODERS = {"cpu", "nvenc"}


def _parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def upscale_video(source: str | Path, destination: str | Path, *, scale: int = 2, tile: int | None = None,
                  model: str | None = None, device: str = "auto", half: bool | None = None,
                  encoder: str | None = None, ai_strength: float | None = None,
                  profile: str | None = None, interpolate: bool | None = None) -> Path:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Original video not found: {source}")
    if scale not in SUPPORTED_SCALES:
        raise ValueError(f"scale must be one of {sorted(SUPPORTED_SCALES)}")
    model = model or os.getenv("AI_MODEL_NAME", "realesr-animevideov3")
    if model not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported FrameLift model '{model}'")
    device = (device or os.getenv("AI_DEVICE", "auto")).lower()
    if device not in SUPPORTED_DEVICES:
        raise ValueError(f"AI device must be one of {sorted(SUPPORTED_DEVICES)}")
    if tile is None:
        tile = int(os.getenv("AI_TILE_SIZE", "384"))
    if tile != 0 and not 32 <= tile <= 2048:
        raise ValueError("AI_TILE_SIZE must be 0 (automatic) or between 32 and 2048")
    if half is None:
        half = _parse_bool(os.getenv("AI_HALF"), default=(device != "cpu"))
    if encoder is None:
        encoder = (os.getenv("AI_ENCODER") or ("nvenc" if device != "cpu" else "cpu")).lower()
    if encoder not in SUPPORTED_ENCODERS:
        raise ValueError(f"AI encoder must be one of {sorted(SUPPORTED_ENCODERS)}")
    if ai_strength is None:
        ai_strength = float(os.getenv("AI_STRENGTH", "0.45"))
    if not 0 <= float(ai_strength) <= 1:
        raise ValueError("AI_STRENGTH must be between 0 and 1")
    profile = profile or os.getenv("AI_PROFILE", "none")
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"AI profile must be one of {sorted(SUPPORTED_PROFILES)}")
    if interpolate is None:
        interpolate = _parse_bool(os.getenv("AI_INTERPOLATE"), default=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    weights_dir = os.getenv("FRAMELIFT_WEIGHTS")
    if weights_dir:
        Path(weights_dir).expanduser().mkdir(parents=True, exist_ok=True)
    try:
        from framelift import enhance_video
    except ImportError as exc:
        raise RuntimeError("FrameLift is not installed in this worker. Install requirements-worker.txt and the matching CPU/CUDA PyTorch build.") from exc

    last_log, last_fraction = 0.0, -1.0
    def on_progress(progress: Any) -> None:
        nonlocal last_log, last_fraction
        now = time.monotonic()
        fraction = float(getattr(progress, "fraction", 0.0) or 0.0)
        if now - last_log < 15 and fraction < 1.0 and fraction - last_fraction < 0.05:
            return
        last_log, last_fraction = now, fraction
        eta = getattr(progress, "eta_seconds", None)
        eta_text = f", ETA {float(eta):.0f}s" if eta is not None else ""
        log.info("FrameLift %s: %s/%s frames (%.1f%%)%s", getattr(progress, "stage", "processing"), getattr(progress, "frames_done", 0), getattr(progress, "frames_total", 0), fraction * 100, eta_text)

    kwargs = {"scale": scale, "model": model, "device": device, "half": bool(half), "tile": tile,
              "encoder": encoder, "ai_strength": float(ai_strength), "profile": profile,
              "interpolate": bool(interpolate), "show_progress": False, "on_progress": on_progress}
    if device == "cuda":
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("CUDA mode requested but PyTorch is not installed") from exc
        if not torch.cuda.is_available():
            raise RuntimeError("FrameLift was configured for CUDA, but torch.cuda.is_available() is false")
        log.info("FrameLift CUDA device: %s", torch.cuda.get_device_name(0))
    log.info("FrameLift starting: model=%s scale=%dx device=%s half=%s tile=%s encoder=%s profile=%s", model, scale, device, bool(half), tile, encoder, profile)
    started = time.monotonic()
    try:
        result = enhance_video(str(source), str(destination), **kwargs)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if getattr(result, "interrupted", False):
        raise RuntimeError(f"FrameLift was interrupted after {getattr(result, 'frames_written', 0)} frames")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("FrameLift returned without producing a non-empty output MP4")
    log.info("FrameLift completed: %s; frames=%s; elapsed=%.1fs; audio=%s", destination.name, getattr(result, "frames_written", "unknown"), time.monotonic() - started, getattr(result, "audio", "unknown"))
    return destination
