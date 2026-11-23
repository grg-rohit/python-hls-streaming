# AI optimization comparison update

This is a complete project package, based on the previous optimization comparison release. It **does not** add TensorRT or change the neural-network computation itself.

## New: optionally repeat tests in ABBA order

Performance Lab > AI GPU enhancement > Baseline vs candidate > Comparison runs:
- `A → B`: quick, two passes, no order-bias control
- `A → B → B → A`: four passes, compare median time for each tile

All runs use the same source sample, model, scale, FP16 request and NVENC settings. Both outputs remain temporary. The default is still the quick two-run mode. At current measured speed a repeated test may take tens of minutes. You can keep baseline tile 384, candidate 256. Neither is automatically promoted to production.

**Important:** This release is an improved comparison tool, **not a demonstrated speed-up**. The existing results showed about 1.9% difference, within plausible run-to-run variation. To deliver a substantial inference speed-up requires modifying or replacing FrameLift's model execution backend, which cannot be validated without a CUDA-compatible development/test environment and equivalent model exports. This package deliberately does not claim a TensorRT implementation.

## Deploy

Keep existing `.env`, model weights, storage and secrets. Extract over the project, then:

`docker compose --profile gpu up -d --build server worker-gpu`

Hard-refresh the browser. Benchmark runs are isolated and do not modify uploaded or published videos. The benchmark queue remains in-process and requires one API replica.
