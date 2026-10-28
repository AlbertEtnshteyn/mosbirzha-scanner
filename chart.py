# -*- coding: utf-8 -*-
"""Окно графика: свечи, объём, EMA10 и EMA20, смена таймфрейма, уровни открытой позиции."""
import os
import queue
import threading
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import ttk

import scanner as sc

BG, GRID, TXT = "#131722", "#1f2433", "#b2b5be"
UP, DOWN = "#26a69a", "#ef5350"
EMA10_C, EMA20_C = "#e8e8e8", "#2962ff"
TFS = [(1, "1м"), (5, "5м"), (15, "15м"), (60, "1ч"), (1440, "Д")]
DEPTH = {1: 2, 5: 4, 15: 8, 60: 45, 1440: 400}           # сколько дней истории грузить


def ema(vals, span):
    k, e, out = 2 / (span + 1), None, []
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


class ChartWindow(tk.Toplevel):
    def __init__(self, app, ticker, tf=5):
        super().__init__(app)
        self.app = app
        self.ticker = ticker.upper()
        self.tf = tf
        self.bars = []
        self.n_vis = 120
        self.offset = 0               # сколько последних свечей сдвинуто вправо за край (прокрутка в прошлое)
        self.res = queue.Queue()
        self.loading = False
        self.hover = None
        self.geometry("1000x620")
        self.configure(bg=BG)
        self._build()
        self.load()
        self.after(20000, self._auto)

    # ------------------------------------------------------------------ интерфейс
    def _build(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=6, pady=4)
        self.v_tk = tk.StringVar(value=self.ticker)
        e = ttk.Entry(bar, textvariable=self.v_tk, width=10)
        e.pack(side="left")
        e.bind("<Return>", lambda _: self.set_ticker())
        ttk.Button(bar, text="Открыть", command=self.set_ticker).pack(side="left", padx=(4, 12))
        self.tf_btns = {}
        for tf, name in TFS:
            b = tk.Button(bar, text=name, width=4, relief="flat", bg=GRID, fg=TXT, activebackground="#2a2e39",
                          command=lambda t=tf: self.set_tf(t))
            b.pack(side="left", padx=1)
            self.tf_btns[tf] = b
        ttk.Button(bar, text="Обновить", command=self.load).pack(side="left", padx=12)
        legend = tk.Frame(self, bg=BG)
        legend.pack(fill="x", padx=8)
        self.info = tk.Label(self, text="", bg=BG, fg=TXT, font=("Consolas", 10), anchor="w")
        self.info.pack(fill="x", padx=8)
        tk.Label(legend, text="━ EMA 10", fg=EMA10_C, bg=BG).pack(side="left")
        tk.Label(legend, text="━ EMA 20", fg=EMA20_C, bg=BG).pack(side="left", padx=10)
        tk.Label(legend, text="колесо мыши — масштаб, перетаскивание — прокрутка", fg="#6b7080", bg=BG).pack(side="right")
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda _: self.draw())
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", lambda e: self._zoom(-1))
        self.cv.bind("<Button-5>", lambda e: self._zoom(1))
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", lambda _: (setattr(self, "hover", None), self.draw()))
        self._mark_tf()

    def _mark_tf(self):
        for tf, b in self.tf_btns.items():
            b.configure(bg="#2962ff" if tf == self.tf else GRID, fg="white" if tf == self.tf else TXT)
        name = dict(TFS)[self.tf]
        self.title(f"{self.ticker} · {name} · график")

    def set_tf(self, tf):
        self.tf, self.offset = tf, 0
        self._mark_tf()
        self.load()

    def set_ticker(self):
        t = self.v_tk.get().strip().upper()
        if t:
            self.ticker, self.offset = t, 0
            self._mark_tf()
            self.load()

    # ------------------------------------------------------------------ данные
    def load(self):
        if self.loading:
            return
        tok = os.environ.get("TINVEST_TOKEN")
        if not tok:
            self.info.configure(text="Для графика нужен токен Т-Инвестиций (вкладка «Настройки»).")
            return
        self.loading = True
        self.info.configure(text=f"Загружаю {self.ticker}…")
        env = "sandbox" if self.app.s.get("sandbox_token") else "prod"
        tk_, tf = self.ticker, self.tf

        def job():
            try:
                ti = sc.TInvest(tok, env)
                now = datetime.now(sc.MSK)
                frm = (now - timedelta(days=DEPTH[tf])).replace(hour=0, minute=0, second=0, microsecond=0)
                rows = ti.candles_range(tk_, frm, now + timedelta(minutes=1), tf)
                self.res.put((tk_, tf, rows, None))
            except Exception as e:
                self.res.put((tk_, tf, None, e))
        threading.Thread(target=job, daemon=True).start()
        self.after(200, self._collect)

    def _collect(self):
        try:
            tk_, tf, rows, err = self.res.get_nowait()
        except queue.Empty:
            self.after(200, self._collect)
            return
        self.loading = False
        if tk_ != self.ticker or tf != self.tf:
            self.load()
            return
        if err:
            self.info.configure(text=f"Ошибка загрузки: {err}")
            return
        self.bars = rows or []
        if not self.bars:
            self.info.configure(text=f"{self.ticker}: свечей нет")
        self.draw()

    def _auto(self):
        if not self.winfo_exists():
            return
        if self.offset == 0:
            self.load()
        self.after(20000 if self.tf <= 15 else 60000, self._auto)

    # ------------------------------------------------------------------ мышь
    def _wheel(self, e):
        self._zoom(-1 if e.delta > 0 else 1)

    def _zoom(self, d):
        self.n_vis = int(max(30, min(600, self.n_vis * (1.15 if d > 0 else 1 / 1.15))))
        self.draw()

    def _press(self, e):
        self._px, self._off0 = e.x, self.offset

    def _drag(self, e):
        bw = max(1.0, (self.cv.winfo_width() - 70) / self.n_vis)
        self.offset = int(max(0, min(len(self.bars) - 10, self._off0 + (e.x - self._px) / bw)))
        self.draw()

    def _motion(self, e):
        self.hover = (e.x, e.y)
        self.draw()

    # ------------------------------------------------------------------ рисование
    def _positions(self):
        robot = getattr(self.app, "robot", None)
        poss = list(robot.positions) if robot else []
        return [p for p in poss if p["ticker"] == self.ticker]

    def draw(self):
        cv = self.cv
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 100 or H < 100 or not self.bars:
            return
        closes = [b["c"] for b in self.bars]
        e10, e20 = ema(closes, 10), ema(closes, 20)
        end = len(self.bars) - self.offset
        start = max(0, end - self.n_vis)
        vis = self.bars[start:end]
        if not vis:
            return
        right, bottom = 70, 22
        pw, ph = W - right, H - bottom
        vol_h = ph * 0.18
        price_h = ph - vol_h - 6
        bw = pw / self.n_vis
        lo = min(min(b["l"] for b in vis), min(e20[start:end]), min(e10[start:end]))
        hi = max(max(b["h"] for b in vis), max(e20[start:end]), max(e10[start:end]))
        poss = self._positions()
        for p in poss:
            for lv in (p.get("stop"), p.get("entry"), p.get("take"), p.get("take2")):
                if lv:
                    lo, hi = min(lo, lv), max(hi, lv)
        pad = (hi - lo) * 0.06 or hi * 0.01
        lo, hi = lo - pad, hi + pad
        y = lambda v: 4 + (hi - v) / (hi - lo) * (price_h - 8)
        x0 = pw - len(vis) * bw                       # вправо прижато
        # сетка и шкала цен
        step = _nice((hi - lo) / 6)
        v = (lo // step + 1) * step
        while v < hi:
            yy = y(v)
            cv.create_line(0, yy, pw, yy, fill=GRID)
            cv.create_text(pw + 6, yy, text=_fmt(v), fill=TXT, anchor="w", font=("Consolas", 9))
            v += step
        # объём
        vmax = max((b.get("v", 0) for b in vis), default=0) or 1
        # время
        last_lbl = -1e9
        for i, b in enumerate(vis):
            xc = x0 + (i + 0.5) * bw
            if xc - last_lbl > 90:
                lbl = b["t"].strftime("%d.%m" if self.tf >= 1440 else ("%d.%m %H:%M" if b["t"].hour * 60 + b["t"].minute < self.tf * 2 or i == 0 else "%H:%M"))
                cv.create_line(xc, 0, xc, ph, fill=GRID)
                cv.create_text(xc, ph + 11, text=lbl, fill=TXT, font=("Consolas", 8))
                last_lbl = xc
        cw = max(1, bw * 0.7)
        for i, b in enumerate(vis):
            xc = x0 + (i + 0.5) * bw
            col = UP if b["c"] >= b["o"] else DOWN
            cv.create_line(xc, y(b["h"]), xc, y(b["l"]), fill=col)
            yo, yc = y(b["o"]), y(b["c"])
            cv.create_rectangle(xc - cw / 2, min(yo, yc), xc + cw / 2, max(yo, yc) + (1 if abs(yo - yc) < 1 else 0),
                                fill=col, outline=col)
            vh = b.get("v", 0) / vmax * (vol_h - 4)
            cv.create_rectangle(xc - cw / 2, ph - vh, xc + cw / 2, ph, fill=col, outline="", stipple="gray50")
        for series, col in ((e20, EMA20_C), (e10, EMA10_C)):
            pts = []
            for i in range(len(vis)):
                pts += [x0 + (i + 0.5) * bw, y(series[start + i])]
            if len(pts) >= 4:
                cv.create_line(*pts, fill=col, width=1.6)
        # уровни позиции
        for p in poss:
            for lv, col, name in ((p.get("entry"), "#9598a1", "вход"), (p.get("stop"), DOWN, "стоп"),
                                  (None if p.get("stage", 0) >= 1 else p.get("take"), UP, "тейк1"),
                                  (None if p.get("stage", 0) >= 2 else p.get("take2"), UP, "тейк2")):
                if lv:
                    cv.create_line(0, y(lv), pw, y(lv), fill=col, dash=(6, 4))
                    cv.create_text(6, y(lv) - 8, text=f"{name} {_fmt(lv)}", fill=col, anchor="w", font=("Consolas", 9))
        # последняя цена
        lc = vis[-1]["c"]
        col = UP if vis[-1]["c"] >= vis[-1]["o"] else DOWN
        cv.create_line(0, y(lc), pw, y(lc), fill=col, dash=(2, 3))
        cv.create_rectangle(pw, y(lc) - 9, W, y(lc) + 9, fill=col, outline=col)
        cv.create_text(pw + 6, y(lc), text=_fmt(lc), fill="white", anchor="w", font=("Consolas", 9, "bold"))
        cv.create_text(pw + 6, y(e10[end - 1]) + (12 if abs(y(e10[end - 1]) - y(lc)) < 14 else 0), text=_fmt(e10[end - 1]),
                       fill=EMA10_C, anchor="w", font=("Consolas", 8))
        cv.create_text(pw + 6, y(e20[end - 1]) + (12 if abs(y(e20[end - 1]) - y(lc)) < 14 else 0), text=_fmt(e20[end - 1]),
                       fill=EMA20_C, anchor="w", font=("Consolas", 8))
        # наведение мышью
        b = vis[-1]
        k = len(vis) - 1
        if self.hover and self.hover[0] < pw:
            k = int((self.hover[0] - x0) // bw)
            if 0 <= k < len(vis):
                b = vis[k]
                xc = x0 + (k + 0.5) * bw
                cv.create_line(xc, 0, xc, ph, fill="#4a4f5c", dash=(2, 2))
                cv.create_line(0, self.hover[1], pw, self.hover[1], fill="#4a4f5c", dash=(2, 2))
            else:
                b, k = vis[-1], len(vis) - 1
        chg = (b["c"] / b["o"] - 1) * 100 if b["o"] else 0
        self.info.configure(text=f"{self.ticker} {dict(TFS)[self.tf]}  {b['t']:%d.%m %H:%M}  О {_fmt(b['o'])}  М {_fmt(b['h'])}  "
                                 f"Н {_fmt(b['l'])}  З {_fmt(b['c'])} ({chg:+.2f}%)  "
                                 f"EMA10 {_fmt(e10[start + k])}  EMA20 {_fmt(e20[start + k])}")


def _nice(x):
    import math
    if x <= 0:
        return 1
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * e:
            return m * e
    return 10 * e


def _fmt(v):
    if v >= 1000:
        return f"{v:.1f}"
    if v >= 10:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.3f}"
    return f"{v:.5f}"
