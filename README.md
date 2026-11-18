# eBPF Latency Guard

[![License: MIT](https://img.shields.io/badge/License-MIT-emerald.svg)](LICENSE)
[![Engine: eBPF CO-RE + C](https://img.shields.io/badge/Engine-eBPF%20%2B%20C-10b981.svg)](#architecture)
[![UI: GTK4 Cyber-Carbon](https://img.shields.io/badge/UI-GTK4%20%2B%20Cairo-34d399.svg)](#)

**Real-time Linux micro-stutter & scheduling latency detective pairing an eBPF tracepoint collector (`sched_wakeup`/`sched_switch`) with sub-millisecond `/proc/pressure` (PSI), per-task `schedstat` runqueue wait profiling, IRQ burst delta scanning, and a GTK4 Cyber-Carbon Telemetry HUD.**

<p align="center">
  <img src="docs/screenshots/showcase.gif" alt="eBPF Latency Guard Showcase" width="900"/>
</p>

## Why eBPF Latency Guard?

When a Linux desktop or game stutters for 20 ms, `htop` only tells you average CPU %. **eBPF Latency Guard** pinpoints *why* the stall occurred:

| Signal | Source | What It Catches |
| :--- | :--- | :--- |
| **eBPF Scheduler Tracepoints** | `bpf/sched_latency.bpf.c` | Exact nanosecond delta from `sched_wakeup` to `sched_switch` in log2 us buckets |
| **PSI Pressure Counters** | `/proc/pressure/{cpu,io,memory}` | `CPU_RUNQ_CONTENTION`, `NVME_IO_STALL`, `MEMORY_RECLAIM_STALL` verdicts |
| **Per-Task Runqueue Wait** | `/proc/[pid]/schedstat` | Zero-overhead attribution ranking tasks spending ms waiting in runqueues |
| **IRQ Burst Radar** | `/proc/interrupts` | Flags `IRQ_STORM` hardware interrupt floods (`nvme`, `iwlwifi`, `xe`, `xhci`) |

## Build & Run

```bash
make all && make test
./elg-probe --json
PYTHONPATH=src /usr/bin/python3 -m elg.gui
```

## License

MIT License — Copyright (c) 2026 Furkan (**lwrlock**). Built with assistance from Google DeepMind Antigravity.
