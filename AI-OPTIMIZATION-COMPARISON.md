# AI Performance Lab: controlled tile comparison

This release adds **Baseline vs candidate (no tracing)** to AI GPU enhancement.

- Uses the same original upload, 2–10 second prepared clip, model, output scale, half precision, NVENC output path, and other FrameLift settings for both runs. Only tile size differs.
- Runs the two candidates sequentially on the CUDA worker. The outputs stay inside the worker's temporary directory and are never published.
- Reports total pipeline time, peak PyTorch reserved VRAM, sampled GPU utilization, and a similarity score (SSIM at 640px width) between the enhanced outputs. This is **not** an AI quality metric against ground truth.
- The original single-run audit remains available. Comparison deliberately disables intrusive forward/transfer tracing to avoid synchronization overhead when measuring performance.
- This does not auto-change production AI settings or enable a new inference runtime. Model load/warmup, clock variability and CUDA memory fragmentation can bias results. Repeat the comparison, including reversing the run order if needed, before changing settings.
- Tile 512 can exhaust an 8GB GPU; start with tile 384 baseline and 256 candidate. Monitoring and runtime limits are important. Restarting a benchmark after CUDA OOM may require restarting the worker.
- The benchmark API uses an in-memory queue, unsuitable for multiple API replicas. Enable only with strong BENCHMARK_ADMIN_TOKEN and WORKER_SHARED_TOKEN, and do not expose the GPU worker endpoints publicly.
- Note: larger tiles do not guarantee faster throughput; they may increase VRAM demand significantly. This change optimizes *measurement and comparison*, not necessarily inference speed.

To start: extract over project while preserving app/.env, configure BENCHMARK_ENABLED=true, BENCHMARK_AI_ENABLED=true, BENCHMARK_ADMIN_TOKEN and WORKER_SHARED_TOKEN, then run `docker compose --profile gpu up -d --build server worker-gpu`. In Performance Lab choose AI GPU enhancement, comparison mode, baseline and candidate tile sizes.

Testing: Python compilation and JS syntax validated. CUDA execution on target hardware not performed here.
