"""Opt-in diagnostic hooks for FrameLift. Not used in normal publishing jobs.

Measurements are observational and explicitly scoped. Hooks necessarily add overhead;
GPU synchronization is used to make forward/transfer wall timings meaningful.
"""
from __future__ import annotations

from contextlib import contextmanager
import subprocess
import threading
import time
from statistics import mean


class GPUSampler:
    def __init__(self, interval=0.5):
        self.interval = interval
        self.rows = []
        self._stop = threading.Event()
        self._thread = None
        self.error = None

    def _sample(self):
        while not self._stop.is_set():
            try:
                p = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
                if p.returncode == 0 and p.stdout.strip():
                    values = p.stdout.splitlines()[0].split(",")
                    self.rows.append([float(s.strip()) if s.strip() not in {"[N/A]", "N/A"} else None for s in values[:4]])
                else:
                    self.error = p.stderr.strip()[:200] or "nvidia-smi unavailable"
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                self.error = str(exc)[:200]
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread = threading.Thread(target=self._sample, name="benchmark-gpu-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=4)

    def results(self):
        def stats(index):
            values = [r[index] for r in self.rows if r[index] is not None]
            return {"mean": round(mean(values), 1), "peak": round(max(values), 1)} if values else None
        return {"samples": len(self.rows), "gpu_utilization_percent": stats(0),
                "device_memory_mb": stats(1), "device_total_memory_mb": stats(2),
                "gpu_power_watts": stats(3), "sampling_error": self.error}


class ModelForwardTrace:
    """Measure outermost torch Module forward calls (not arbitrary CUDA kernels).

    Root Module hooks execute only on the *first* module entered in a forward tree.
    Synchronization makes elapsed time interpretable but increases runtime.
    """
    def __init__(self):
        self.timings = []
        self.forward_calls = 0
        self._handles = []
        self._tls = threading.local()
        self.error = None

    def __enter__(self):
        try:
            import torch
            from torch.nn.modules.module import register_module_forward_pre_hook, register_module_forward_hook
            self.torch = torch
            def pre(module, args):
                depth = getattr(self._tls, "depth", 0)
                self._tls.depth = depth + 1
                if depth == 0:
                    if torch.cuda.is_available(): torch.cuda.synchronize()
                    self._tls.started = time.perf_counter()
            def post(module, args, output):
                depth = getattr(self._tls, "depth", 1) - 1
                self._tls.depth = max(depth, 0)
                if depth == 0 and hasattr(self._tls, "started"):
                    if torch.cuda.is_available(): torch.cuda.synchronize()
                    self.timings.append(time.perf_counter() - self._tls.started)
                    self.forward_calls += 1
            self._handles = [register_module_forward_pre_hook(pre), register_module_forward_hook(post, always_call=True)]
        except Exception as exc:
            self.error = str(exc)
            self.__exit__(None, None, None)
        return self

    def __exit__(self, *_):
        for handle in self._handles: handle.remove()
        self._handles = []

    def results(self):
        if not self.timings:
            return {"module_forward_seconds": None, "module_forward_calls": 0,
                    "note": self.error or "No torch Module forward calls were observed; inference timing unavailable"}
        return {"module_forward_seconds": round(sum(self.timings), 3),
                "module_forward_calls": self.forward_calls,
                "mean_forward_ms": round(1000 * mean(self.timings), 2),
                "note": "GPU-synchronized outermost torch Module forward wall time; diagnostic hooks add overhead. Includes model-side operations, not necessarily every inference operation."}


class TransferTrace:
    """Count explicit Tensor.to/.cpu/.cuda methods; native implicit transfers are not seen."""
    def __init__(self):
        self.calls = 0
        self.seconds = 0.0
        self._original = {}
        self.error = None
        self._tls = threading.local()

    def __enter__(self):
        try:
            import torch
            for name in ("to", "cpu", "cuda"):
                original = getattr(torch.Tensor, name)
                self._original[name] = original
                def wrapped(tensor, *args, _original=original, **kwargs):
                    # Prevent recursive measurement if the method invokes another wrapped method.
                    if getattr(self._tls, "inside", False):
                        return _original(tensor, *args, **kwargs)
                    self._tls.inside = True
                    try:
                        if torch.cuda.is_available(): torch.cuda.synchronize()
                        start = time.perf_counter()
                        result = _original(tensor, *args, **kwargs)
                        if torch.cuda.is_available(): torch.cuda.synchronize()
                        elapsed = time.perf_counter() - start
                        if tensor.device != result.device:
                            self.calls += 1
                            self.seconds += elapsed
                        return result
                    finally:
                        self._tls.inside = False
                setattr(torch.Tensor, name, wrapped)
        except Exception as exc:
            self.error = str(exc)
            self.__exit__(None, None, None)
        return self

    def __exit__(self, *_):
        try:
            import torch
            for name, original in self._original.items(): setattr(torch.Tensor, name, original)
        except ImportError:
            pass
        self._original.clear()

    def results(self):
        return {"explicit_transfer_seconds": round(self.seconds, 3) if not self.error else None,
                "explicit_transfer_calls": self.calls,
                "note": self.error or "Only explicit PyTorch Tensor.to/cpu/cuda device changes are measured; OpenCV/NumPy/native transfers and allocations are not included."}


@contextmanager
def diagnostic_hooks(enabled=True):
    if not enabled:
        yield None
        return
    with ModelForwardTrace() as forward, TransferTrace() as transfer:
        yield {"forward": forward, "transfer": transfer}
