from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from collections import deque

os.environ.setdefault("GSK_RENDERER", "gl")

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from .analyzer import bucket_histogram_from_samples, compute_percentiles, sample_latency

ROOT = pathlib.Path(__file__).resolve().parents[2]

CARBON_CSS = b"""
window, window.background, AdwApplicationWindow {
    background-color: #040806;
    background-image: radial-gradient(circle at 85% 12%, rgba(16, 185, 129, 0.11), transparent 46%),
                      radial-gradient(circle at 10% 90%, rgba(245, 158, 11, 0.07), transparent 44%);
    color: #ecfdf5;
}
headerbar, AdwHeaderBar {
    background: #070e0b;
    border-bottom: 1px solid rgba(16, 185, 129, 0.25);
    color: #ecfdf5;
    min-height: 44px;
}
windowcontrols button {
    min-width: 18px;
    min-height: 18px;
    padding: 2px;
    margin: 0 2px;
    border-radius: 9999px;
}
windowcontrols button image {
    -gtk-icon-size: 12px;
}
stackswitcher button {
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 12px;
    font-weight: 700;
}
stackswitcher button:checked {
    background: rgba(16, 185, 129, 0.24);
    border: 1px solid rgba(52, 211, 153, 0.45);
    color: #6ee7b7;
}
.scope-panel {
    background: #08100d;
    border: 1px solid rgba(16, 185, 129, 0.26);
    border-radius: 7px;
    padding: 12px 14px;
}
.rail-panel {
    background: #070d0a;
    border: 1px solid rgba(16, 185, 129, 0.22);
    border-radius: 7px;
    padding: 12px 14px;
}
.readout-cell {
    background: #060b09;
    border: 1px solid rgba(52, 211, 153, 0.20);
    border-radius: 5px;
    padding: 8px 10px;
}
.ro-k { font-size: 10px; font-weight: 700; color: #34d399; letter-spacing: 0.8px; }
.ro-v { font-family: monospace; font-size: 17px; font-weight: 800; color: #f8fafc; }
.ro-s { font-size: 10.5px; color: #94a3b8; }
.sec-t { font-size: 13px; font-weight: 700; color: #d1fae5; letter-spacing: 0.3px; }
.sec-s { font-size: 11px; color: #94a3b8; }
.mono { font-family: monospace; font-size: 12px; color: #e2e8f0; }
.kv-row { padding: 6px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.06); }
.pill-btn {
    border-radius: 6px;
    padding: 5px 11px;
    font-size: 11.5px;
    font-weight: 700;
    background: rgba(16, 185, 129, 0.14);
    border: 1px solid rgba(52, 211, 153, 0.32);
}
.pill-btn:hover { background: rgba(16, 185, 129, 0.28); }
.seg-active {
    background: rgba(16, 185, 129, 0.34);
    border: 1px solid #34d399;
    color: #ecfdf5;
}
.badge-ok {
    background: rgba(16, 185, 129, 0.18);
    color: #6ee7b7;
    border: 1px solid rgba(16, 185, 129, 0.40);
    border-radius: 5px;
    padding: 6px 10px;
    font-weight: 800;
    font-size: 13px;
}
.badge-warn {
    background: rgba(245, 158, 11, 0.20);
    color: #fcd34d;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: 5px;
    padding: 6px 10px;
    font-weight: 800;
    font-size: 13px;
}
.status-strip {
    background: #060b09;
    border-top: 1px solid rgba(16, 185, 129, 0.22);
    padding: 6px 16px;
    font-family: monospace;
    font-size: 11px;
    color: #94a3b8;
}
progressbar trough { background: rgba(255, 255, 255, 0.08); border-radius: 4px; min-height: 6px; }
progressbar progress { background: #10b981; border-radius: 4px; min-height: 6px; }
"""


class DualScopeCanvas(Gtk.DrawingArea):
    """Dual-channel oscilloscope: Channel A Runqueue Latency (ms) + Channel B CPU/IO PSI (%)."""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 255)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._delays: list[float] = []
        self._cpu_psi: list[float] = []
        self._io_psi: list[float] = []
        self._p95 = 0.0
        self._p99 = 0.0
        self.set_draw_func(self._on_draw)

    def set_data(self, delays: list[float], cpu_psi: list[float], io_psi: list[float], p95: float, p99: float) -> None:
        self._delays = list(delays)[-60:]
        self._cpu_psi = list(cpu_psi)[-60:]
        self._io_psi = list(io_psi)[-60:]
        self._p95 = p95
        self._p99 = p99
        self.queue_draw()

    def _on_draw(self, _area, cr: cairo.Context, w: int, h: int) -> None:
        cr.set_source_rgba(0.02, 0.04, 0.03, 0.96)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        pad_l, pad_r, pad_t, gap = 50.0, 14.0, 14.0, 16.0
        ch_a_h = max(60.0, (h - pad_t - gap - 18.0) * 0.64)
        ch_b_t = pad_t + ch_a_h + gap
        ch_b_h = max(36.0, h - ch_b_t - 18.0)
        cw = max(40.0, w - pad_l - pad_r)

        # Oscilloscope graticule lines (vertical divisions)
        cr.set_source_rgba(0.10, 0.45, 0.30, 0.14)
        cr.set_line_width(1.0)
        for i in range(1, 10):
            gx = pad_l + cw * (i / 10.0)
            cr.move_to(gx, pad_t)
            cr.line_to(gx, ch_b_t + ch_b_h)
        cr.stroke()

        # Channel A: Runqueue Latency
        max_d = max(5.0, max(self._delays) * 1.25 if self._delays else 5.0)
        for frac in (0.0, 0.5, 1.0):
            y = pad_t + ch_a_h * (1.0 - frac)
            cr.set_source_rgba(0.16, 0.72, 0.50, 0.18)
            cr.move_to(pad_l, y)
            cr.line_to(w - pad_r, y)
            cr.stroke()
            cr.set_source_rgba(0.62, 0.85, 0.75, 0.80)
            cr.set_font_size(9.5)
            cr.move_to(4, y + 3)
            cr.show_text(f"{max_d * frac:.1f}ms")

        cr.set_source_rgba(0.20, 0.83, 0.60, 0.85)
        cr.set_font_size(10.0)
        cr.move_to(pad_l + 6, pad_t + 12)
        cr.show_text("CH-A  SCHED RUNQ DELAY (ms)")

        # Dashed P99 reference
        if self._p99 > 0.0:
            y99 = pad_t + ch_a_h * (1.0 - min(1.0, self._p99 / max_d))
            cr.save()
            cr.set_dash([5.0, 4.0])
            cr.set_source_rgba(0.96, 0.38, 0.42, 0.75)
            cr.set_line_width(1.2)
            cr.move_to(pad_l, y99)
            cr.line_to(w - pad_r, y99)
            cr.stroke()
            cr.restore()
            cr.set_source_rgba(0.98, 0.60, 0.62, 0.90)
            cr.set_font_size(9.0)
            cr.move_to(w - pad_r - 58, max(pad_t + 10, y99 - 3))
            cr.show_text(f"P99 {self._p99:.1f}ms")

        if len(self._delays) >= 2:
            n = len(self._delays)
            pts = [
                (pad_l + (i / (n - 1)) * cw, pad_t + ch_a_h * (1.0 - min(1.0, val / max_d)))
                for i, val in enumerate(self._delays)
            ]
            grad = cairo.LinearGradient(0, pad_t, 0, pad_t + ch_a_h)
            grad.add_color_stop_rgba(0.0, 0.06, 0.72, 0.51, 0.38)
            grad.add_color_stop_rgba(1.0, 0.06, 0.72, 0.51, 0.02)
            cr.set_source(grad)
            cr.move_to(pts[0][0], pad_t + ch_a_h)
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], pad_t + ch_a_h)
            cr.close_path()
            cr.fill()

            cr.set_source_rgba(0.20, 0.90, 0.62, 0.98)
            cr.set_line_width(2.2)
            cr.move_to(pts[0][0], pts[0][1])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.stroke()

            px, py = pts[-1]
            cr.set_source_rgba(0.43, 0.96, 0.75, 1.0)
            cr.new_sub_path()
            cr.arc(px, py, 3.6, 0, 6.28318)
            cr.fill()

        # Channel B: PSI Dual Trace
        max_p = max(10.0, max(max(self._cpu_psi, default=0.0), max(self._io_psi, default=0.0)) * 1.25)
        for frac in (0.0, 1.0):
            y = ch_b_t + ch_b_h * (1.0 - frac)
            cr.set_source_rgba(0.96, 0.62, 0.04, 0.18)
            cr.move_to(pad_l, y)
            cr.line_to(w - pad_r, y)
            cr.stroke()
            cr.set_source_rgba(0.75, 0.75, 0.75, 0.75)
            cr.set_font_size(9.0)
            cr.move_to(4, y + 3)
            cr.show_text(f"{max_p * frac:.0f}%")

        cr.set_source_rgba(0.98, 0.75, 0.14, 0.85)
        cr.set_font_size(10.0)
        cr.move_to(pad_l + 6, ch_b_t + 11)
        cr.show_text("CH-B  PSI PRESSURE avg10% (Cyan: CPU • Amber: IO)")

        for series, rgba in ((self._cpu_psi, (0.22, 0.82, 0.92, 0.92)), (self._io_psi, (0.98, 0.75, 0.14, 0.92))):
            if len(series) >= 2:
                cr.set_source_rgba(*rgba)
                cr.set_line_width(1.7)
                m = len(series)
                for i, v in enumerate(series):
                    x = pad_l + (i / (m - 1)) * cw
                    y = ch_b_t + ch_b_h * (1.0 - min(1.0, v / max_p))
                    cr.move_to(x, y) if i == 0 else cr.line_to(x, y)
                cr.stroke()


class Log2HistogramCanvas(Gtk.DrawingArea):
    """Vertical bucket bar histogram matching scheduler latency buckets."""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 250)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._buckets: list[tuple[str, int]] = [("<0.5ms", 0), ("0.5-2ms", 0), ("2-5ms", 0), ("5-15ms", 0), ("15ms+", 0)]
        self.set_draw_func(self._on_draw)

    def set_buckets(self, buckets: list[tuple[str, int]]) -> None:
        self._buckets = list(buckets)
        self.queue_draw()

    def _on_draw(self, _a, cr: cairo.Context, w: int, h: int) -> None:
        cr.set_source_rgba(0.02, 0.04, 0.03, 0.96)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        pad_l, pad_r, pad_t, pad_b = 42.0, 20.0, 28.0, 34.0
        cw = max(40.0, w - pad_l - pad_r)
        ch = max(40.0, h - pad_t - pad_b)
        tot = sum(c for _, c in self._buckets)
        peak = max(1, max((c for _, c in self._buckets), default=1))

        for frac in (0.0, 0.5, 1.0):
            y = pad_t + ch * (1.0 - frac)
            cr.set_source_rgba(0.16, 0.72, 0.50, 0.15)
            cr.move_to(pad_l, y)
            cr.line_to(w - pad_r, y)
            cr.stroke()

        n = max(1, len(self._buckets))
        slot = cw / n
        bw = min(92.0, slot * 0.58)
        colors = [
            (0.06, 0.72, 0.51),
            (0.20, 0.83, 0.60),
            (0.96, 0.75, 0.14),
            (0.98, 0.57, 0.24),
            (0.96, 0.25, 0.37),
        ]
        for i, (lbl, cnt) in enumerate(self._buckets):
            bx = pad_l + i * slot + (slot - bw) * 0.5
            bh = (cnt / peak) * ch
            by = pad_t + ch - bh
            rgb = colors[i % len(colors)]

            cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.22)
            cr.rectangle(bx, pad_t, bw, ch)
            cr.fill()

            cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.92)
            cr.rectangle(bx, by, bw, max(2.0 if cnt > 0 else 0.0, bh))
            cr.fill()

            pct = (cnt / tot * 100.0) if tot > 0 else 0.0
            cr.set_source_rgba(0.95, 0.98, 0.96, 0.95)
            cr.set_font_size(11.0)
            cr.move_to(bx + 4, max(pad_t - 6, by - 6))
            cr.show_text(f"{cnt} ({pct:.0f}%)")

            cr.set_source_rgba(0.75, 0.86, 0.80, 0.90)
            cr.set_font_size(11.0)
            cr.move_to(bx + 4, h - 12)
            cr.show_text(lbl)


class LatencyWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="eBPF Latency Guard", default_width=1120, default_height=730)
        self._delays: deque[float] = deque(maxlen=60)
        self._cpu_psis: deque[float] = deque(maxlen=60)
        self._io_psis: deque[float] = deque(maxlen=60)
        self._busy = False
        self._paused = False
        self._sample_ms = 180
        self._last_report: dict = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=180)
        switcher = Gtk.StackSwitcher(stack=self.stack)

        hb = Adw.HeaderBar(title_widget=switcher)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        app_lbl = Gtk.Label(label="eBPF Latency Guard")
        app_lbl.add_css_class("sec-t")
        title_box.append(app_lbl)
        hb.pack_start(title_box)

        ctrl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._win_btns: dict[int, Gtk.Button] = {}
        for ms in (80, 180, 400):
            b = Gtk.Button(label=f"{ms}ms")
            b.add_css_class("pill-btn")
            if ms == self._sample_ms:
                b.add_css_class("seg-active")
            b.connect("clicked", self._set_window_ms, ms)
            ctrl_box.append(b)
            self._win_btns[ms] = b

        self.pause_btn = Gtk.ToggleButton(label="Pause")
        self.pause_btn.add_css_class("pill-btn")
        self.pause_btn.connect("toggled", self._on_pause)
        ctrl_box.append(self.pause_btn)

        exp_btn = Gtk.Button(label="Export JSON")
        exp_btn.add_css_class("pill-btn")
        exp_btn.connect("clicked", self._export_trace)
        ctrl_box.append(exp_btn)
        hb.pack_end(ctrl_box)
        root.append(hb)

        # Page 1: Oscilloscope + Right Diagnostic Rail
        scope_page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin_top=12, margin_bottom=10, margin_start=14, margin_end=14)
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        scope_card = self._panel("scope-panel", "Dual-Channel Kernel Oscilloscope", "Live runqueue delay waveform & PSI contention monitor")
        self.chart = DualScopeCanvas()
        scope_card.append(self.chart)
        left_col.append(scope_card)

        # 5-column digital readout bar (replaces generic top hero row)
        readouts = Gtk.Grid(column_spacing=8, column_homogeneous=True)
        self.ro_vals: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for idx, (key, title, sub) in enumerate([
            ("p50", "P50 MEDIAN", "Typical wakeup"),
            ("p95", "P95 TAIL", "Interactive edge"),
            ("p99", "P99 PEAK", "Stutter bound"),
            ("max", "LAST SAMPLE", "Max runq delay"),
            ("stall", "PSI CPU STALL", "Accumulated ms"),
        ]):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            cell.add_css_class("readout-cell")
            kl = Gtk.Label(label=title, xalign=0.0); kl.add_css_class("ro-k")
            vl = Gtk.Label(label="--", xalign=0.0); vl.add_css_class("ro-v")
            sl = Gtk.Label(label=sub, xalign=0.0, ellipsize=3); sl.add_css_class("ro-s")
            cell.append(kl); cell.append(vl); cell.append(sl)
            readouts.attach(cell, idx, 0, 1, 1)
            self.ro_vals[key] = (vl, sl)
        left_col.append(readouts)

        # Right Diagnostic Verdict Rail (fixed 320px width)
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        rail.set_size_request(320, -1)

        verdict_box = self._panel("rail-panel", "Stutter Root-Cause Verdict", "Threshold classification")
        self.verdict_badge = Gtk.Label(label="SAMPLING...", xalign=0.5)
        self.verdict_badge.add_css_class("badge-ok")
        verdict_box.append(self.verdict_badge)
        self.verdict_desc = Gtk.Label(label="Collecting scheduler runqueue deltas", xalign=0.0, wrap=True)
        self.verdict_desc.add_css_class("sec-s")
        verdict_box.append(self.verdict_desc)
        rail.append(verdict_box)

        bpf_box = self._panel("rail-panel", "eBPF Object & Tracepoints", "Compiled clang -target bpf ELF")
        self.bpf_lbl = Gtk.Label(label="Inspecting bpf/sched_latency.bpf.o...", xalign=0.0, wrap=True)
        self.bpf_lbl.add_css_class("mono")
        bpf_box.append(self.bpf_lbl)
        self.tp_lbl = Gtk.Label(label="tracepoint/sched/sched_wakeup\ntracepoint/sched/sched_switch", xalign=0.0)
        self.tp_lbl.add_css_class("sec-s")
        bpf_box.append(self.tp_lbl)
        rail.append(bpf_box)

        psi_box = self._panel("rail-panel", "Pressure Stall Breakdown", "/proc/pressure/{cpu,io,memory}")
        self.psi_bars: dict[str, tuple[Gtk.Label, Gtk.ProgressBar]] = {}
        for k, name in [("cpu", "CPU Runqueue"), ("io", "Block / NVMe IO"), ("mem", "Memory Reclaim")]:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            nl = Gtk.Label(label=name, xalign=0.0, hexpand=True); nl.add_css_class("sec-s")
            vl = Gtk.Label(label="0.00%", xalign=1.0); vl.add_css_class("mono")
            pb = Gtk.ProgressBar()
            top.append(nl); top.append(vl)
            row.append(top); row.append(pb)
            psi_box.append(row)
            self.psi_bars[k] = (vl, pb)
        rail.append(psi_box)

        scope_page.append(left_col)
        scope_page.append(rail)

        # Page 2: Log2 Histogram
        hist_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=10, margin_start=14, margin_end=14)
        hist_card = self._panel("scope-panel", "Scheduler Delay Distribution (Log2 Buckets)", "Cumulative distribution across observed sampling windows")
        self.hist_canvas = Log2HistogramCanvas()
        hist_card.append(self.hist_canvas)
        self.hist_summary = Gtk.Label(label="Waiting for samples...", xalign=0.0)
        self.hist_summary.add_css_class("mono")
        hist_card.append(self.hist_summary)
        hist_page.append(hist_card)

        # Page 3: Task & IRQ Attribution
        tasks_page = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True, margin_top=12, margin_bottom=10, margin_start=14, margin_end=14)
        task_card = self._panel("scope-panel", "Top Scheduler-Delayed Processes", "Ranked by /proc/[pid]/schedstat wait deltas")
        self.task_rows = []
        for i in range(6):
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            r.add_css_class("kv-row")
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            tl = Gtk.Label(label=f"#{i + 1} Sampling...", xalign=0.0, hexpand=True); tl.add_css_class("mono")
            ml = Gtk.Label(label="0.00 ms", xalign=1.0); ml.add_css_class("sec-s")
            pb = Gtk.ProgressBar()
            h.append(tl); h.append(ml)
            r.append(h); r.append(pb)
            task_card.append(r)
            self.task_rows.append((tl, ml, pb))

        irq_card = self._panel("scope-panel", "Active Hardware Interrupts (/proc/interrupts)", "Sorted by interrupt rate per second")
        self.irq_rows = []
        for i in range(6):
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            r.add_css_class("kv-row")
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            il = Gtk.Label(label=f"IRQ {i + 1}", xalign=0.0, hexpand=True, ellipsize=3); il.add_css_class("mono")
            rl = Gtk.Label(label="0 /s", xalign=1.0); rl.add_css_class("sec-s")
            pb = Gtk.ProgressBar()
            h.append(il); h.append(rl)
            r.append(h); r.append(pb)
            irq_card.append(r)
            self.irq_rows.append((il, rl, pb))

        tasks_page.attach(task_card, 0, 0, 1, 1)
        tasks_page.attach(irq_card, 1, 0, 1, 1)

        self.stack.add_titled(scope_page, "scope", "Oscilloscope")
        self.stack.add_titled(hist_page, "hist", "Log2 Histogram")
        self.stack.add_titled(tasks_page, "tasks", "Task & IRQ")
        root.append(self.stack)

        self.status_bar = Gtk.Label(label="Window: 180 ms  •  Samples: 0  •  Source: elg-probe (procfs)", xalign=0.0)
        self.status_bar.add_css_class("status-strip")
        root.append(self.status_bar)

        self._bind_keys()
        GLib.timeout_add(140, self._warmup_tick)
        GLib.timeout_add(450, self._warmup_tick)
        GLib.timeout_add(850, self._warmup_tick)
        GLib.timeout_add(1500, self._tick)

    def _bind_keys(self) -> None:
        ctrl = Gtk.EventControllerKey()
        pages = ["scope", "hist", "tasks"]

        def _on_key(_c, keyval, _code, state):
            if state & Gdk.ModifierType.ALT_MASK:
                for idx, name in enumerate(pages):
                    if keyval == getattr(Gdk, f"KEY_{idx + 1}"):
                        self.stack.set_visible_child_name(name)
                        return True
            return False

        ctrl.connect("key-pressed", _on_key)
        self.add_controller(ctrl)

    def _panel(self, css: str, title: str, sub: str) -> Gtk.Box:
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        b.add_css_class(css)
        t = Gtk.Label(label=title, xalign=0.0); t.add_css_class("sec-t")
        s = Gtk.Label(label=sub, xalign=0.0); s.add_css_class("sec-s")
        b.append(t); b.append(s)
        return b

    def _set_window_ms(self, _btn, ms: int) -> None:
        self._sample_ms = ms
        for k, b in self._win_btns.items():
            b.remove_css_class("seg-active")
            if k == ms:
                b.add_css_class("seg-active")
        self._sample_once()

    def _on_pause(self, btn: Gtk.ToggleButton) -> None:
        self._paused = btn.get_active()
        btn.set_label("Resume" if self._paused else "Pause")

    def _warmup_tick(self) -> bool:
        self._sample_once()
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        if not self._paused:
            self._sample_once()
        return GLib.SOURCE_CONTINUE

    def _sample_once(self) -> None:
        if self._busy:
            return
        self._busy = True
        win_ms = self._sample_ms

        def _worker():
            try:
                data = sample_latency(win_ms)
            except Exception:
                data = None

            def _apply():
                self._busy = False
                if not data:
                    return GLib.SOURCE_REMOVE
                self._last_report = data
                max_d = float(data.get("max_runq_delay_ms", 0.0))
                self._delays.append(max_d)
                psi = data.get("psi", {})
                cpu_a = float(psi.get("cpu_avg10", 0.0))
                io_a = float(psi.get("io_avg10", 0.0))
                mem_a = float(psi.get("mem_avg10", 0.0))
                self._cpu_psis.append(cpu_a)
                self._io_psis.append(io_a)

                pcts = compute_percentiles(list(self._delays))
                self.chart.set_data(list(self._delays), list(self._cpu_psis), list(self._io_psis), pcts["p95"], pcts["p99"])

                self.ro_vals["p50"][0].set_label(f"{pcts['p50']:.2f} ms")
                self.ro_vals["p95"][0].set_label(f"{pcts['p95']:.2f} ms")
                self.ro_vals["p99"][0].set_label(f"{pcts['p99']:.2f} ms")
                self.ro_vals["max"][0].set_label(f"{max_d:.2f} ms")
                self.ro_vals["stall"][0].set_label(f"{psi.get('cpu_stall_ms', 0.0):.2f} ms")

                vrd = data.get("verdict", "HEALTHY")
                self.verdict_badge.set_label(vrd)
                self.verdict_badge.remove_css_class("badge-ok")
                self.verdict_badge.remove_css_class("badge-warn")
                self.verdict_badge.add_css_class("badge-ok" if vrd == "HEALTHY" else "badge-warn")
                self.verdict_desc.set_label(
                    "No runqueue contention or stall thresholds exceeded."
                    if vrd == "HEALTHY"
                    else f"Threshold exceeded — check Task & IRQ tab ({vrd})."
                )

                bpf_path = ROOT / "bpf" / "sched_latency.bpf.o"
                if bpf_path.exists():
                    kb = bpf_path.stat().st_size / 1024.0
                    self.bpf_lbl.set_label(f"READY  bpf/sched_latency.bpf.o ({kb:.1f} KB)")
                else:
                    self.bpf_lbl.set_label("NOT BUILT  run `make bpf`")

                for k, val in [("cpu", cpu_a), ("io", io_a), ("mem", mem_a)]:
                    vl, pb = self.psi_bars[k]
                    vl.set_label(f"{val:.2f}%")
                    pb.set_fraction(min(1.0, val / 20.0))

                bks = bucket_histogram_from_samples(list(self._delays))
                self.hist_canvas.set_buckets(bks)
                self.hist_summary.set_label("Buckets: " + "  |  ".join(f"{k}: {v}" for k, v in bks))

                tasks = data.get("top_delayed", [])
                for idx, (tl, ml, pb) in enumerate(self.task_rows):
                    if idx < len(tasks):
                        tk = tasks[idx]
                        dms = float(tk.get("runq_delay_ms", 0.0))
                        tl.set_label(f"#{idx + 1} {tk.get('name', '?')} (PID {tk.get('pid', 0)})")
                        ml.set_label(f"{dms:.2f} ms • {tk.get('switches', 0)} sw")
                        pb.set_fraction(min(1.0, max(0.03, dms / max(5.0, max_d))))
                    else:
                        tl.set_label(f"#{idx + 1} Idle")
                        ml.set_label("0.00 ms")
                        pb.set_fraction(0.0)

                irqs = data.get("top_irqs", [])
                top_rate = max(100, max((int(x.get("per_sec", 0)) for x in irqs), default=100))
                for idx, (il, rl, pb) in enumerate(self.irq_rows):
                    if idx < len(irqs):
                        iq = irqs[idx]
                        rate = int(iq.get("per_sec", 0))
                        il.set_label(f"IRQ {iq.get('irq', '?')} — {iq.get('source', '')}")
                        rl.set_label(f"{rate} /s")
                        pb.set_fraction(min(1.0, max(0.02, rate / top_rate)))
                    else:
                        il.set_label("—")
                        rl.set_label("0 /s")
                        pb.set_fraction(0.0)

                self.status_bar.set_label(
                    f"Window: {win_ms} ms  •  Samples: {len(self._delays)}  •  Verdict: {vrd}  •  Shortcuts: Alt+1 Scope | Alt+2 Hist | Alt+3 Tasks/IRQ"
                )
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    def _export_trace(self, _btn) -> None:
        out = pathlib.Path.home() / f"elg-trace-{int(time.time())}.json"
        out.write_text(json.dumps(self._last_report, indent=2))
        self.toast_overlay.add_toast(Adw.Toast(title=f"Saved {out.name}"))


class LatencyApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.lwrlock.EbpfLatencyGuard")

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CARBON_CSS)
        d = Gdk.Display.get_default()
        if d:
            Gtk.StyleContext.add_provider_for_display(d, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.props.active_window or LatencyWindow(self)
        win.present()


def main() -> int:
    return LatencyApp().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
