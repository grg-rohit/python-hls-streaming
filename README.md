# python-hls-streaming
Self-hosted adaptive HLS video streaming server. Upload videos via browser, auto-converts to 240p–4K with GPU acceleration (NVENC), streams to any device on LAN or internet via ngrok/Cloudflare Tunnel. Built with FastAPI, FFmpeg, hls.js, and Docker.

### Performance benchmarking

A non-invasive benchmarking utility for FFmpeg presets and optional FrameLift AI inference is available at `tools/benchmark_pipeline.py`. See [docs/performance-benchmark.md](docs/performance-benchmark.md) before changing production encoding settings.

## Frontend Performance Lab

Select an uploaded video in the **Performance Lab** panel to run a 2–15 second
CPU HLS encoding benchmark, compare x264 presets, and view measured speed,
output size and SSIM in the browser. For configuration, safety limitations and
deployment instructions, see [`docs/performance-benchmark.md`](docs/performance-benchmark.md).
The API image now bundles FFmpeg/FFprobe and the benchmark tool. The feature
is disabled unless `BENCHMARK_ENABLED=true` and `BENCHMARK_ADMIN_TOKEN` are set.
This phase does **not** benchmark FrameLift on the GPU workers.
