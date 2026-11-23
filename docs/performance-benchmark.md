# HLS Studio: performance benchmark (Phase 1)

This addition is **benchmark-only**. It does not change the live API, worker, AI upscaler, production encoder defaults, HLS ladder, or S3 storage.

## Prerequisites

- Python 3.10+ and FFmpeg + FFprobe on PATH.
- For `--ai` only: install the same FrameLift, PyTorch and model weights as the GPU worker; run on the actual target machine to get meaningful GPU timings.
- Use test footage that you own or have permission to process. Prefer a consistent 10–30 second source clip for short benchmarks.

## CPU/HLS baseline vs. candidate

From the project root:

```bash
python tools/benchmark_pipeline.py /path/to/480p-sample.mp4 --seconds 10 --quality 480p --presets slow medium --report benchmarks/480p-hls.json
python tools/benchmark_pipeline.py /path/to/720p-sample.mp4 --seconds 10 --quality 720p --presets slow medium --report benchmarks/720p-hls.json
python tools/benchmark_pipeline.py /path/to/1080p-sample.mp4 --seconds 10 --quality 1080p --presets slow medium --report benchmarks/1080p-hls.json
```

On Windows PowerShell, use `python` or `py`, and quote Windows file paths.

The benchmark produces an isolated, lossless H.264 test sample and runs the existing HLS command builder with only the x264 preset changed. Both candidates operate on the same prepared input. Preparation time is reported separately; it is **not** included in the per-candidate encode time. Normal video/audio production code is not modified.

## AI baseline on GPU worker

```bash
python tools/benchmark_pipeline.py /path/to/480p-sample.mp4 --seconds 10 --quality 480p --presets slow medium --ai --ai-device cuda --ai-encoder nvenc --ai-model realesr-animevideov3 --ai-scale 2 --ai-tile 384 --report benchmarks/480p-ai.json
```

The AI stage runs your **existing** `app.ai_upscaler.upscale_video` adapter and measures wall-clock time, output size and FFprobe metadata. It DOES NOT estimate AI visual quality from SSIM; compare enhanced video to your expected results visually, with attention to fine detail, artifacts, motion stability, and audio synchronization (the AI processing sample in this benchmark omits audio to isolate the video workload). For end-to-end timing including audio and network/S3, use a production-like worker staging test.

To compare different AI tile sizes, rerun with `--ai-tile 256`, `--ai-tile 384`, and `--ai-tile 512` under identical conditions. Keep model, source, scale, device, and precision the same. Check GPU memory usage externally with `nvidia-smi` during each test.

## Reading results

- `elapsed_seconds`: elapsed wall-clock time of the isolated stage.
- `output_bytes`: total rendition file size (HLS playlist + segments) or AI MP4 size.
- `ssim_vs_scaled_source`: HLS fidelity relative to the **same source, conventionally resized** to the target dimensions; not an AI quality score. Values near 1 are closer to the resized source, but no single metric guarantees subjective quality.
- `errors`: exceptions for individual stages, written into JSON even when a stage fails.
- `source_probe`, `sample_probe`: metadata to make comparisons reproducible.

Review encoding speed **and** file size **and** SSIM **and** representative visual samples. Do not replace `slow` with `medium` merely because it is faster. Do not change AI model or tile defaults without controlled quality comparisons. For AWS cost, measure instance operating time, storage and transfer separately; this report contains no fabricated cost figures.

## Guardrails

- No source video is modified.
- Test outputs are created outside `streams/`, isolated in a temporary directory; specify `--keep-outputs` to inspect their files.
- Default `--ai` is OFF: AI models are not downloaded or invoked without explicit opt-in.
- Benchmarks are intended to be run on the actual CPU/GPU instance type you plan to deploy.
- The project's current rule limiting AI jobs to source short-edge <= 720 pixels is unchanged. Benchmarking an 1080p source is still possible; it does not imply production AI accepts 1080p input.

## Next decision

Collect three reports for 480p, 720p, and 1080p source videos. Compare `slow` vs `medium` at each size, and run AI tests on the applicable source resolutions with the **actual AWS GPU**. Only then choose which optimized path to implement first.

## Browser benchmark dashboard (new)

The homepage now includes a **Performance Lab**. Choose any uploaded original, the short
2–15 second test duration, the target HLS quality, and one to three CPU x264 presets.
The UI launches a bounded asynchronous benchmark and polls the report for per-preset
encode time, processing speed, output size, and SSIM relative to the conventionally
resized source. Test output is isolated; production outputs remain unchanged.

**Required API environment:**

```env
BENCHMARK_ENABLED=true
BENCHMARK_ADMIN_TOKEN=replace-with-a-long-random-secret
# Optional safety limit for original S3 downloads (default 2 GiB)
BENCHMARK_MAX_SOURCE_BYTES=2147483648
```

Enter the benchmark admin token in the Performance Lab field when you run a test.
The token is sent in an HTTP header over your deployment's HTTPS endpoint; the
frontend does not store it in browser storage. **Do not enable this on a public
unauthenticated instance without access controls**; benchmarking uses real CPU,
network transfer, and S3 reads. Only one benchmark may execute per API process.

The API Dockerfile now includes FFmpeg/FFprobe and the `tools/` script. Rebuild
and redeploy the API container for the new endpoints. The backend runs tests on
the **API node's CPU**; it does not perform AI inference, and results should not
be interpreted as GPU worker performance. In-memory reports are lost on API
restart and are not shared across replicas: run one API replica or use sticky
routing for this evaluation version. A durable benchmark queue and GPU worker
benchmark capability are needed before multi-replica production deployment.

For 1080p sources, 1080p and lower HLS tests can run as usual. A request for a
quality above the source *may* be rejected by the underlying existing HLS
benchmark source ladder (this feature intentionally does not change that
rule); the production AI upscaling pipeline remains unaffected.

This dashboard measures encoder quality/speed only. It does not claim GPU
usage, cost per video, AI perceptual quality, or full upload-to-stream latency.
