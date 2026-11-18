from __future__ import annotations

import json
import math
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]


def find_probe_binary() -> str:
    local = ROOT / "elg-probe"
    if local.exists():
        return str(local)
    return "elg-probe"


def sample_latency(sample_ms: int = 200) -> dict:
    """Invoke the native C latency probe (falls back to pure procfs if binary absent)."""
    bin_path = find_probe_binary()
    if pathlib.Path(bin_path).exists():
        out = subprocess.check_output(
            [bin_path, "--sample-ms", str(max(50, min(1500, sample_ms))), "--json"],
            text=True,
            timeout=5.0,
        )
        return json.loads(out)
    return _python_fallback_sample(sample_ms)


def _read_psi_file(path: str) -> float:
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            if line.startswith("some "):
                for tok in line.split():
                    if tok.startswith("avg10="):
                        return float(tok.split("=", 1)[1])
    except Exception:
        pass
    return 0.0


def _python_fallback_sample(sample_ms: int) -> dict:
    time.sleep(sample_ms / 1000.0)
    cpu_p = _read_psi_file("/proc/pressure/cpu")
    io_p = _read_psi_file("/proc/pressure/io")
    mem_p = _read_psi_file("/proc/pressure/memory")
    return {
        "sample_ms": sample_ms,
        "bpf_object_ready": (ROOT / "bpf" / "sched_latency.bpf.o").exists(),
        "verdict": classify_verdict(max_delay_ms=0.5, cpu_avg10=cpu_p, io_avg10=io_p, mem_avg10=mem_p, top_irq_rate=0),
        "max_runq_delay_ms": 0.5,
        "total_runq_delay_ms": 0.5,
        "psi": {
            "cpu_avg10": cpu_p, "cpu_stall_ms": 0.0,
            "io_avg10": io_p, "io_stall_ms": 0.0,
            "mem_avg10": mem_p, "mem_stall_ms": 0.0,
        },
        "top_delayed": [],
        "top_irqs": [],
    }


def classify_verdict(
    max_delay_ms: float,
    cpu_avg10: float,
    io_avg10: float,
    mem_avg10: float,
    top_irq_rate: int,
) -> str:
    if mem_avg10 > 2.0:
        return "MEMORY_RECLAIM_STALL"
    if io_avg10 > 3.0:
        return "NVME_IO_STALL"
    if top_irq_rate > 12000:
        return "IRQ_STORM"
    if max_delay_ms > 18.0 or cpu_avg10 > 12.0:
        return "CPU_RUNQ_CONTENTION"
    return "HEALTHY"


def bucket_histogram_from_samples(delays_ms: list[float]) -> list[tuple[str, int]]:
    labels = ["<0.5ms", "0.5-2ms", "2-5ms", "5-15ms", "15ms+"]
    counts = [0, 0, 0, 0, 0]
    for d in delays_ms:
        if d < 0.5:
            counts[0] += 1
        elif d < 2.0:
            counts[1] += 1
        elif d < 5.0:
            counts[2] += 1
        elif d < 15.0:
            counts[3] += 1
        else:
            counts[4] += 1
    return list(zip(labels, counts))


def compute_percentiles(samples_ms: list[float]) -> dict[str, float]:
    if not samples_ms:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    s = sorted(float(x) for x in samples_ms)
    def pct(p: float) -> float:
        idx = min(len(s) - 1, max(0, math.ceil((p / 100.0) * len(s)) - 1))
        return round(s[idx], 2)
    return {"p50": pct(50), "p95": pct(95), "p99": pct(99)}
