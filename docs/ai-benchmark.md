# Browser-based AI benchmark

Enable `BENCHMARK_ENABLED=true`, `BENCHMARK_ADMIN_TOKEN=<strong random secret>` on the API; also set `BENCHMARK_AI_ENABLED=true` on **both** the API and GPU worker, with `WORKER_SHARED_TOKEN` configured. Rebuild/restart `server` and `worker-gpu`. In Performance Lab select **AI GPU enhancement**. No terminal benchmark commands are needed.

AI tests are bounded to 2–10 seconds, only one outstanding job per API process, with a configurable abandoned-claim timeout (default 120 minutes). Runs on GPU worker in a temporary directory; no changes to video or HLS objects in S3. For video sources whose full download is expensive, preprocessing still downloads the entire video. Model weights may download on first use.

Results include FrameLift + output encoding wall time, estimated processed FPS based on source FPS and sample duration, GPU name, peak PyTorch reserved VRAM, output dimensions/size. They do **not** measure GPU utilization percentage, full-system VRAM usage, VMAF/SSIM on AI quality, or cloud billing. Benchmark clips are generated with x264 ultrafast CRF18 and may have different quality characteristics than production sources.

**Limitations:** The AI benchmark queue uses API process memory. Restarting API loses jobs. Multiple API workers/replicas are unsupported; use database or Redis for a deployment with replicas and robust retries/leases. Lost completion callbacks can leave jobs running until the configured timeout. Restrict benchmark access, limit concurrency and S3 consumption; don't expose admin credentials publicly.

## Performance audit mode (GPU benchmark only)

The browser AI result now includes a GPU performance audit. GPU utilization, device memory and power are sampled through `nvidia-smi` every 0.5 seconds (when available). The worker also measures GPU-synchronized outermost PyTorch `Module` forward wall time and explicit `Tensor.to`, `.cpu`, `.cuda` device transfers. The hooks are active only during benchmarks, not production enhancement. Hooks add synchronization overhead, so compare *like-for-like benchmark versions* rather than treating measured times as zero-overhead production estimates.

**Important distinction:** model-forward wall time is not all GPU kernel time, and explicit tensor-transfer times omit transfers performed through other frameworks or native APIs. The remaining pipeline time **cannot** be interpreted as output encoding time. FrameLift's installed package is external to this repository; its exposed adapter has no separate decode-per-frame, model-loading and encode-per-frame timers. These appear as **Not exposed by FrameLift** in the UI rather than fake numbers. To get exact breakdowns, add timers inside the FrameLift implementation and return structured stats, or use a fork with instrumentation at its decoder, model loading, inference, rescale and encoder boundaries.

The model-scale audit flags potential unnecessary resizing when an x4-native RealESRGAN model is requested at x2 or x3; this is an **inference**, not proof of extra interpolations. GPU readings represent `nvidia-smi` samples, not continuous device accounting. No enhanced video or HLS output is written to persistent storage.

For slow AI benchmarks, set `BENCHMARK_AI_TIMEOUT_SECONDS` on the API (default `7200`) so the claim is not expired before the worker finishes. This is not a heartbeat or durable queue; do not run multiple API replicas without replacing the in-memory dispatcher.
