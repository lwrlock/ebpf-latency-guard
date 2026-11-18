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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .analyzer import bucket_histogram_from_samples, compute_percentiles, sample_latency

CARBON_CSS = b"""
window, window.background, AdwApplicationWindow {
    background-color: #070b0f;
    background-image: radial-gradient(circle at 80% 10%, rgba(16, 185, 129, 0.14), transparent 48%),
                      radial-gradient(circle at 12% 88%, rgba(245, 158, 11, 0.09), transparent 45%);
    color: #ecfdf5;
}
headerbar, AdwHeaderBar {
    background: rgba(9, 14, 19, 0.90);
    border-bottom: 1px solid rgba(16, 185, 129, 0.22);
    color: #ecfdf5;
}
.carbon-card {
    background: rgba(13, 19, 26, 0.94);
    border: 1px solid rgba(16, 185, 129, 0.22);
    border-radius: 16px;
    padding: 15px 18px;
}
.carbon-hero {
    background: rgba(15, 23, 30, 0.95);
    border: 1px solid rgba(52, 211, 153, 0.26);
    border-radius: 15px;
    padding: 14px 16px;
}
.hero-k { font-size: 10.5px; font-weight: 700; color: #34d399; letter-spacing: 0.7px; }
.hero-v { font-size: 24px; font-weight: 800; color: #f8fafc; }
.hero-v-warn { font-size: 24px; font-weight: 800; color: #fbbf24; }
.hero-s { font-size: 11.5px; color: #94a3b8; }
.sec-t { font-size: 14px; font-weight: 700; color: #f1f5f9; }
.sec-s { font-size: 11.5px; color: #94a3b8; }
.kv-row { padding: 6px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.06); }
.pill-btn {
    border-radius: 9999px;
    padding: 6px 14px;
    font-weight: 600;
    background: rgba(16, 185, 129, 0.16);
    border: 1px solid rgba(52, 211, 153, 0.35);
}
.pill-btn:hover { background: rgba(16, 185, 129, 0.30); }
.badge-ok { background: rgba(16, 185, 129, 0.22); color: #6ee7b7; border-radius: 9999px; padding: 4px 11px; font-weight: 700; font-size: 11.5px; }
.badge-warn { background: rgba(245, 158, 11, 0.24); color: #fcd34d; border-radius: 9999px; padding: 4px 11px; font-weight: 700; font-size: 11.5px; }
"""


class LatencyWaveformChart(Gtk.DrawingArea):
    """Live Cairo scheduler runqueue latency waveform + stutter threshold overlay."""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 175)
        self.set_hexpand(True)
        self._delays: list[float] = []
        self._psi: list[float] = []
        self.set_draw_func(self._on_draw)

    def set_data(self, delays: list[float], psi: list[float]) -> None:
        self._delays = list(delays)[-60:]
        self._psi = list(psi)[-60:]
        self.queue_draw()

    def _on_draw(self, _area, cr: cairo.Context, w: int, h: int) -> None:
        pad_l, pad_r, pad_t, pad_b = 42.0, 14.0, 14.0, 26.0
        cw = max(20.0, w - pad_l - pad_r)
        ch = max(20.0, h - pad_t - pad_b)

        cr.set_source_rgba(0.04, 0.07, 0.10, 0.90)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        max_d = max(10.0, max(self._delays) * 1.2 if self._delays else 10.0)
        for frac in (0.0, 0.5, 1.0):
            y = pad_t + ch * (1.0 - frac)
            cr.set_source_rgba(0.20, 0.83, 0.60, 0.14)
            cr.set_line_width(1.0)
            cr.move_to(pad_l, y)
            cr.line_to(w - pad_r, y)
            cr.stroke()
            cr.set_source_rgba(0.65, 0.78, 0.72, 0.75)
            cr.set_font_size(9.5)
            cr.move_to(4, y + 3)
            cr.show_text(f"{max_d * frac:.1f}ms")

        if len(self._delays) < 2:
            return
        n = len(self._delays)
        pts = [
            (pad_l + (i / (n - 1)) * cw, pad_t + ch * (1.0 - min(1.0, val / max_d)))
            for i, val in enumerate(self._delays)
        ]

        grad = cairo.LinearGradient(0, pad_t, 0, pad_t + ch)
        grad.add_color_stop_rgba(0.0, 0.06, 0.72, 0.51, 0.40)
        grad.add_color_stop_rgba(1.0, 0.06, 0.72, 0.51, 0.02)
        cr.set_source(grad)
        cr.move_to(pts[0][0], pad_t + ch)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], pad_t + ch)
        cr.close_path()
        cr.fill()

        cr.set_source_rgba(0.20, 0.83, 0.60, 0.98)
        cr.set_line_width(2.2)
        cr.move_to(pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.stroke()

        if len(self._psi) >= 2:
            cr.save()
            cr.set_dash([4.0, 3.0])
            cr.set_source_rgba(0.98, 0.75, 0.14, 0.90)
            cr.set_line_width(1.6)
            m = len(self._psi)
            for i, p in enumerate(self._psi):
                x = pad_l + (i / (m - 1)) * cw
                y = pad_t + ch * (1.0 - min(1.0, p / 25.0))
                if i == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.stroke()
            cr.restore()


class LatencyWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="eBPF Latency Guard", default_width=1080, default_height=740)
        self._delays: deque[float] = deque(maxlen=60)
        self._psis: deque[float] = deque(maxlen=60)
        self._busy = False
        self._last_report: dict = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)

        self._title = Adw.WindowTitle(title="eBPF Latency Guard", subtitle="Kernel Runqueue, PSI & IRQ Stutter Detective")
        hb = Adw.HeaderBar(title_widget=self._title)
        exp_btn = Gtk.Button(label="Export Trace")
        exp_btn.add_css_class("pill-btn")
        exp_btn.connect("clicked", self._export_trace)
        hb.pack_end(exp_btn)
        root.append(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=14, margin_bottom=16, margin_start=18, margin_end=18)
        root.append(body)

        hero = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        c1, self.h_p99, self.h_p99_s = self._hero("P99 RUNQ DELAY", "-- ms", "eBPF + schedstat")
        c2, self.h_cpu, self.h_cpu_s = self._hero("CPU PSI AVG10", "--%", "Some / Stall ms")
        c3, self.h_io, self.h_io_s = self._hero("NVME / IO PSI", "--%", "Block queue stalls")
        c4, self.h_vrd, self.h_vrd_s = self._hero("STUTTER VERDICT", "SAMPLING", "Kernel eBPF CO-RE")
        for i, c in enumerate((c1, c2, c3, c4)):
            hero.attach(c, i, 0, 1, 1)
        body.append(hero)

        mid = Gtk.Grid(column_spacing=14, row_spacing=14, column_homogeneous=True)
        chart_card = self._card("Real-Time Runqueue Latency & CPU Pressure", "Emerald: Max Wait (ms) • Dashed Amber: PSI Avg10")
        self.chart = LatencyWaveformChart()
        chart_card.append(self.chart)
        self.hist_lbl = Gtk.Label(label="Buckets: waiting...", xalign=0.0)
        self.hist_lbl.add_css_class("sec-s")
        chart_card.append(self.hist_lbl)

        tasks_card = self._card("Top Scheduler-Delayed Tasks & Active IRQs", "Per-task kernel schedstat wait attribution")
        self.task_rows = []
        for i in range(5):
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            r.add_css_class("kv-row")
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            tl = Gtk.Label(label=f"#{i + 1} Sampling...", xalign=0.0, hexpand=True)
            ml = Gtk.Label(label="0.00 ms", xalign=1.0)
            ml.add_css_class("sec-s")
            h.append(tl)
            h.append(ml)
            pb = Gtk.ProgressBar()
            r.append(h)
            r.append(pb)
            tasks_card.append(r)
            self.task_rows.append((tl, ml, pb))
        self.irq_lbl = Gtk.Label(label="Top IRQs: probing...", xalign=0.0, wrap=True, margin_top=6)
        self.irq_lbl.add_css_class("sec-s")
        tasks_card.append(self.irq_lbl)

        mid.attach(chart_card, 0, 0, 3, 1)
        mid.attach(tasks_card, 3, 0, 2, 1)
        body.append(mid)

        GLib.timeout_add(1600, self._tick)
        self._sample_once()

    def _hero(self, k: str, v: str, s: str):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("carbon-hero")
        kl = Gtk.Label(label=k, xalign=0.0); kl.add_css_class("hero-k")
        vl = Gtk.Label(label=v, xalign=0.0); vl.add_css_class("hero-v")
        sl = Gtk.Label(label=s, xalign=0.0, ellipsize=3); sl.add_css_class("hero-s")
        box.append(kl); box.append(vl); box.append(sl)
        return box, vl, sl

    def _card(self, title: str, sub: str) -> Gtk.Box:
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        b.add_css_class("carbon-card")
        t = Gtk.Label(label=title, xalign=0.0); t.add_css_class("sec-t")
        s = Gtk.Label(label=sub, xalign=0.0); s.add_css_class("sec-s")
        b.append(t); b.append(s)
        return b

    def _tick(self) -> bool:
        self._sample_once()
        return GLib.SOURCE_CONTINUE

    def _sample_once(self) -> None:
        if self._busy:
            return
        self._busy = True

        def _worker():
            try:
                data = sample_latency(180)
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
                self._psis.append(cpu_a)

                pcts = compute_percentiles(list(self._delays))
                self.h_p99.set_label(f"{pcts['p99']:.2f} ms")
                self.h_p99_s.set_label(f"P50: {pcts['p50']:.2f}ms • P95: {pcts['p95']:.2f}ms")
                self.h_cpu.set_label(f"{cpu_a:.2f}%")
                self.h_cpu_s.set_label(f"Stall: {psi.get('cpu_stall_ms', 0.0):.1f} ms")
                self.h_io.set_label(f"{io_a:.2f}%")
                self.h_io_s.set_label(f"Mem PSI: {psi.get('mem_avg10', 0.0):.2f}%")
                vrd = data.get("verdict", "HEALTHY")
                self.h_vrd.set_label(vrd.replace("_", " ").title())
                self.h_vrd_s.set_label("BPF CO-RE Ready" if data.get("bpf_object_ready") else "Schedstat Mode")

                self.chart.set_data(list(self._delays), list(self._psis))
                bks = bucket_histogram_from_samples(list(self._delays))
                self.hist_lbl.set_label("Buckets: " + "  |  ".join(f"{k}: {v}" for k, v in bks))

                tasks = data.get("top_delayed", [])
                for idx, (tl, ml, pb) in enumerate(self.task_rows):
                    if idx < len(tasks):
                        tk = tasks[idx]
                        dms = float(tk.get("runq_delay_ms", 0.0))
                        tl.set_label(f"#{idx + 1} {tk.get('name', '?')} ({tk.get('pid', 0)})")
                        ml.set_label(f"{dms:.2f} ms • {tk.get('switches', 0)} sw")
                        pb.set_fraction(min(1.0, max(0.03, dms / max(5.0, max_d))))
                    else:
                        tl.set_label(f"#{idx + 1} Idle"); ml.set_label("0.00 ms"); pb.set_fraction(0.0)

                irqs = data.get("top_irqs", [])[:3]
                self.irq_lbl.set_label("Top IRQs: " + (", ".join(f"{x['irq']} {x['source']} ({x['per_sec']}/s)" for x in irqs) or "Quiet"))
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
