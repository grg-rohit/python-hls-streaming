"""Run an isolated FrameLift sample in a CUDA worker; never publish its output."""
from __future__ import annotations
import json
import base64
import subprocess
import tempfile
import time
from pathlib import Path

from app import s3_storage
from app.ai_upscaler import upscale_video
from app.ai_performance_profiler import GPUSampler, diagnostic_hooks
from contextlib import nullcontext


def _probe(path):
    raw = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_frames", "-of", "json", str(path)], timeout=30)
    stream = json.loads(raw)["streams"][0]
    rate = stream.get("avg_frame_rate", "0/1").split("/")
    fps = float(rate[0])/float(rate[1]) if len(rate)==2 and float(rate[1]) else 0
    return int(stream["width"]),int(stream["height"]),fps


def execute(job):
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("AI benchmark requires CUDA GPU; torch.cuda.is_available() is false")
    with tempfile.TemporaryDirectory(prefix="hls-ai-benchmark-") as directory:
        base=Path(directory)
        source=base/"source.video"
        started=time.perf_counter()
        s3_storage.download_file(s3_storage.originals_bucket(), job["original_key"], source)
        download_s=time.perf_counter()-started
        clip=base/"sample.mp4"
        started=time.perf_counter()
        # Re-encode first N seconds to create a precisely bounded, independently decodable sample.
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-t", str(job["seconds"]), "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "18", str(clip)], check=True, timeout=180)
        prep_s=time.perf_counter()-started
        width,height,fps=_probe(clip)
        output=base/"enhanced.mp4"
        if job.get("compare"):
            return _compare(job, clip, base, source_dimensions=(width, height, fps), download_s=download_s, prep_s=prep_s)
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started=time.perf_counter()
        # Diagnostic tracing is enabled only for isolated benchmarks, not production video jobs.
        with GPUSampler() as sampler, diagnostic_hooks() as hooks:
            upscale_video(clip, output, scale=int(job["scale"]), tile=int(job["tile"]),
                model=job["model"], device="cuda", half=True, encoder="nvenc", interpolate=False)
        torch.cuda.synchronize()
        elapsed=time.perf_counter()-started
        out_w,out_h,out_fps=_probe(output)
        forward = hooks["forward"].results() if hooks else {}
        transfers = hooks["transfer"].results() if hooks else {}
        gpu_samples = sampler.results()
        # These operation boundaries are inside the third-party FrameLift package.
        # Without FrameLift-provided instrumentation, never fabricate their durations.
        stage_trace = {
            "source_decode_seconds": None,
            "source_decode_seconds_per_frame": None,
            "model_load_seconds": None,
            "output_encode_seconds": None,
            "output_encode_seconds_per_frame": None,
            "unattributed_pipeline_seconds": round(max(0, elapsed - (forward.get("module_forward_seconds") or 0)), 3),
            "model_forward": forward,
            "explicit_tensor_transfers": transfers,
            "gpu_sampling": gpu_samples,
            "missing_stage_reason": "FrameLift 2.1.0 does not expose the separate model-loading, decode and encode timers through this adapter. Unattributed time combines those stages, preprocessing, scheduling, rescaling and other work; it is not an encoding estimate.",
        }
        known_x4 = job["model"] in {"realesrgan-x4plus", "realesrnet-x4plus", "realesrgan-x4plus-anime-6B"}
        resize = {"model_native_scale": 4 if known_x4 else None,
                  "requested_scale": int(job["scale"]),
                  "likely_post_model_resizing": bool(known_x4 and int(job["scale"]) != 4),
                  "note": "Potential redundant scaling inferred from model family and requested scale; actual FrameLift resize operations are not individually timed."}
        return dict(stage_trace=stage_trace, resizing_audit=resize, model=job["model"], scale=job["scale"], tile=job["tile"],
            gpu_name=torch.cuda.get_device_name(0), input_width=width,input_height=height,
            output_width=out_w, output_height=out_h, input_fps=round(fps,3),
            output_fps=round(out_fps,3), ai_pipeline_seconds=round(elapsed,3),
            processing_fps=round(fps*job["seconds"]/elapsed,3) if elapsed else None,
            speed_vs_realtime=round(job["seconds"]/elapsed,3) if elapsed else None,
            peak_pytorch_vram_mb=round(torch.cuda.max_memory_reserved()/1048576,1),
            output_bytes=output.stat().st_size,
            source_download_seconds=round(download_s,3),sample_preparation_seconds=round(prep_s,3),
            note="AI runtime includes tracing overhead. Torch forward timing is synchronized and not interchangeable with total neural network kernel time. Decode/model load/encode timings require FrameLift internal stage hooks. No AI fidelity score is inferred.")


def _run_candidate(job, clip, base, tile, model=None, index=0):
    """Uninstrumented identical pipeline settings; tile is the only changed parameter."""
    import torch
    model = model or job["model"]
    output = base / f"compare-{index}-{tile}.mp4"
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    start = time.perf_counter()
    with GPUSampler(interval=1.0) as sampler:
        upscale_video(clip, output, scale=int(job["scale"]), tile=tile,
                     model=model, device="cuda", half=True,
                     encoder="nvenc", interpolate=False)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - start
    width, height, fps = _probe(output)
    return {"tile": tile, "model": model, "elapsed_seconds": round(elapsed, 3),
            "peak_pytorch_vram_mb": round(torch.cuda.max_memory_reserved()/1048576,1),
            "gpu_sampling": sampler.results(), "output_width": width, "output_height": height,
            "output_fps": round(fps,3), "output_bytes": output.stat().st_size}, output


def _output_ssim(first, second):
    """Compare decoded results at matching geometry; this is NOT ground-truth AI quality."""
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(first), "-i", str(second),
               "-filter_complex", "[0:v]scale=640:-2,format=yuv420p[a];[1:v]scale=640:-2,format=yuv420p[b];[a][b]ssim",
               "-an", "-f", "null", "-"]
    try:
        p = subprocess.run(command, text=True, capture_output=True, timeout=180)
        import re
        matches = re.findall(r"All:([0-9.]+)", p.stderr)
        if p.returncode == 0 and matches:
            return float(matches[-1]), None
        return None, (p.stderr or "SSIM computation failed")[-300:]
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, str(exc)[:300]



def _frame_preview(path, *, seconds, crop=False):
    """Capture a real enhanced output frame as a bounded, transient JPEG data URI.

    960px overview plus 512px native-resolution center crop. No files are
    published, and the API token protects benchmark results.
    """
    timestamp = max(0, float(seconds) / 2 - .05)
    vf = ("crop=w='min(512,iw)':h='min(512,ih)':"
          "x='(iw-ow)/2':y='(ih-oh)/2'" if crop
          else "scale=w='min(960,iw)':h=-2")
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", str(timestamp), "-i", str(path), "-frames:v", "1", "-vf", vf,
         "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
         capture_output=True, timeout=45)
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 600_000:
        raise RuntimeError("Preview frame could not be extracted within size limit")
    return "data:image/jpeg;base64," + base64.b64encode(result.stdout).decode("ascii")


def _pan_frame_preview(path, *, seconds):
    """Bounded full-frame JPEG for panning within the quality viewer.

    Preserve native resolution up to 3840 pixels wide. A larger frame is
    downscaled for bounded transport and explicitly labelled in the UI.
    """
    width, height, _ = _probe(path)
    timestamp = max(0, float(seconds) / 2 - .05)
    max_width = 3840
    scale = "scale=w='min(3840,iw)':h=-2"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(timestamp),
               "-i", str(path), "-frames:v", "1", "-vf", scale,
               "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    # Avoid stdout growth from unbounded input via a process timeout and
    # cap the resulting preview before storing it in the benchmark response.
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 3_500_000:
        raise RuntimeError("Pan frame exceeded preview limits or could not be extracted")
    return {"src": "data:image/jpeg;base64," + base64.b64encode(result.stdout).decode("ascii"),
            "native": width <= max_width, "source_width": width, "source_height": height,
            "preview_width": min(width, max_width)}


def _comparison_frames(a, b, seconds):
    try:
        result = {"overview_a": _frame_preview(a, seconds=seconds),
                  "overview_b": _frame_preview(b, seconds=seconds),
                  "detail_a": _frame_preview(a, seconds=seconds, crop=True),
                  "detail_b": _frame_preview(b, seconds=seconds, crop=True)}
        try:
            pan_a = _pan_frame_preview(a, seconds=seconds)
            pan_b = _pan_frame_preview(b, seconds=seconds)
            result.update(pan_a=pan_a["src"], pan_b=pan_b["src"],
                          pan_native=pan_a["native"] and pan_b["native"],
                          pan_source_width=pan_a["source_width"],
                          pan_preview_width=pan_a["preview_width"])
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            result["pan_error"] = str(exc)[:180]
        return result, None
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        return None, str(exc)[:200]

def _compare(job, clip, base, *, source_dimensions, download_s, prep_s):
    """Controlled tile or model comparison. No published video is changed."""
    import statistics
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for optimization comparisons")
    tile_a, tile_b = int(job["tile"]), int(job["candidate_tile"])
    model_a = job["model"]
    model_b = job.get("candidate_model") or model_a
    model_comparison = model_a != model_b
    key_a, key_b = "baseline", "candidate"
    rounds = int(job.get("comparison_rounds", 1))
    if rounds not in {1, 2}:
        raise ValueError("Comparison rounds must be 1 or 2")
    ordering = [key_a, key_b] if rounds == 1 else [key_a, key_b, key_b, key_a]
    measurements = {key_a: [], key_b: []}
    output_paths = {}
    for index, key in enumerate(ordering):
        tile, model = (tile_a, model_a) if key == key_a else (tile_b, model_b)
        report, output = _run_candidate(job, clip, base, tile, model=model, index=index)
        report["run_index"] = index + 1
        measurements[key].append(report)
        output_paths[key] = output
    def summarize(key):
        items = measurements[key]
        return {
            "tile": items[-1]["tile"], "model": items[-1]["model"],
            "elapsed_seconds": round(statistics.median(x["elapsed_seconds"] for x in items), 3),
            "peak_pytorch_vram_mb": round(max(x["peak_pytorch_vram_mb"] for x in items), 1),
            "gpu_sampling": items[-1]["gpu_sampling"],
            "output_width": items[-1]["output_width"],
            "output_height": items[-1]["output_height"],
            "output_fps": items[-1]["output_fps"],
            "output_bytes": items[-1]["output_bytes"],
            "runs": items,
        }
    baseline, candidate = summarize(key_a), summarize(key_b)
    same_shape = all(baseline[k] == candidate[k] for k in ("output_width", "output_height", "output_fps"))
    ssim, error = (_output_ssim(output_paths[key_a], output_paths[key_b])
                   if same_shape else (None, "Outputs have different geometry or frame rates"))
    previews, preview_error = (_comparison_frames(output_paths[key_a], output_paths[key_b], job["seconds"])
                               if same_shape else (None, "Different video dimensions or FPS"))
    improvement = ((baseline["elapsed_seconds"]-candidate["elapsed_seconds"])
                   / baseline["elapsed_seconds"] * 100)
    speed_status = ("inconclusive" if rounds == 1 or abs(improvement) < 5 else
                    "candidate_faster" if improvement > 0 else "baseline_faster")
    return {"comparison": True, "comparison_rounds": rounds,
            "run_order": ordering, "speed_status": speed_status,
            "comparison_kind": "models" if model_comparison else "tiles",
            "preview_frames": previews, "preview_error": preview_error,
            "model": job["model"], "candidate_model": model_b, "scale": job["scale"],
            "gpu_name": torch.cuda.get_device_name(0),
            "input_width": source_dimensions[0], "input_height": source_dimensions[1],
            "input_fps": round(source_dimensions[2], 3),
            "baseline": baseline, "candidate": candidate,
            "candidate_speedup_percent": round(improvement, 2),
            "output_ssim": ssim, "output_ssim_error": error,
            "source_download_seconds": round(download_s, 3),
            "sample_preparation_seconds": round(prep_s, 3),
            "note": "Runtime uses median of each tile's runs. A/B/B/A order helps with warm-up and order bias, "
                    "but GPU thermal and clock changes still affect timing. Peak torch VRAM is not total GPU memory. "
                    "Output SSIM is measured on videos downscaled to 640px width, not against ground truth. "
                    "Comparison previews are temporary real output frames; full-resolution panning is available up to 3840px preview width. "
                    "Small runtime differences are not proof of speed gains. No production settings are modified."}
