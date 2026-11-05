# -*- coding: utf-8 -*-
"""Окно графика: свечи, объём, EMA10 и EMA20, смена таймфрейма, уровни открытой позиции."""
import os
import queue
import threading
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import ttk

import scanner as sc
import autotrader as at
import robotcore as rc

BG, GRID, TXT = "#131722", "#1f2433", "#b2b5be"
UP, DOWN = "#26a69a", "#ef5350"
EMA10_C, EMA20_C = "#e8e8e8", "#2962ff"
TFS = [(1, "1м"), (5, "5м"), (15, "15м"), (60, "1ч"), (1440, "Д")]
DEPTH = rc.CHART_DEPTH


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
        self.trades = []
        self.n_vis = 120
        self.offset = 0               # сколько последних свечей сдвинуто вправо за край (прокрутка в прошлое)
        self.ypan, self.yzoom = 0.0, 1.0   # сдвиг цены вверх-вниз (доля диапазона) и масштаб по вертикали
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
        ttk.Button(bar, text="Т-Инвестиции", command=lambda: self.app.open_tbank(self.ticker)).pack(side="left")
        ttk.Button(bar, text="Терминал", command=lambda: self.app.open_tbank(None)).pack(side="left", padx=(4, 0))
        legend = tk.Frame(self, bg=BG)
        legend.pack(fill="x", padx=8)
        self.info = tk.Label(self, text="", bg=BG, fg=TXT, font=("Consolas", 10), anchor="w")
        self.info.pack(fill="x", padx=8)
        tk.Label(legend, text="━ EMA 10", fg=EMA10_C, bg=BG).pack(side="left")
        tk.Label(legend, text="━ EMA 20", fg=EMA20_C, bg=BG).pack(side="left", padx=10)
        self.v_hist = tk.BooleanVar(value=True)
        tk.Checkbutton(legend, text="сделки робота на истории", variable=self.v_hist, command=self.draw,
                       bg=BG, fg=TXT, selectcolor=GRID, activebackground=BG, activeforeground=TXT).pack(side="left", padx=10)
        self.sum_lbl = tk.Label(legend, text="", fg=TXT, bg=BG)
        self.sum_lbl.pack(side="left", padx=6)
        self.go_btn = tk.Button(legend, text="◀ к формации", relief="flat", bg=GRID, fg=TXT, command=self.goto_trade)
        self.go_btn.pack(side="left", padx=4)
        self._go_i = 0
        tk.Label(legend, text="колесо — масштаб · Ctrl+колесо — масштаб по вертикали · тащить — во все стороны · двойной щелчок — авто", fg="#6b7080", bg=BG).pack(side="right")
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda _: self.draw())
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", lambda e: self._zoom(-1))
        self.cv.bind("<Button-5>", lambda e: self._zoom(1))
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Double-1>", lambda _: (setattr(self, "ypan", 0.0), setattr(self, "yzoom", 1.0), self.draw()))   # авто-масштаб
        self.cv.bind("<Control-MouseWheel>", self._ywheel)
        self.cv.bind("<Shift-MouseWheel>", self._ywheel)
        self._lv_hit, self._lv_drag = [], None             # линии стопа/тейка, которые можно тащить мышью
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", lambda _: (setattr(self, "hover", None), self.draw()))
        self._mark_tf()

    def _mark_tf(self):
        for tf, b in self.tf_btns.items():
            b.configure(bg="#2962ff" if tf == self.tf else GRID, fg="white" if tf == self.tf else TXT)
        name = dict(TFS)[self.tf]
        self.title(f"{self.ticker} · {name} · график")

    def set_tf(self, tf):
        self.tf, self.offset, self.ypan, self.yzoom = tf, 0, 0.0, 1.0
        self._mark_tf()
        self.load()

    def set_ticker(self):
        t = self.v_tk.get().strip().upper()
        if t:
            self.ticker, self.offset, self.ypan, self.yzoom = t, 0, 0.0, 1.0
            self._mark_tf()
            self.load()

    # ------------------------------------------------------------------ данные
    def load(self):
        if self.loading:
            return
        tok = os.environ.get("TINVEST_TOKEN")
        srv = self.app._server() if hasattr(self.app, "_server") else None
        if not tok and not srv:
            self.info.configure(text="Для графика нужен токен Т-Инвестиций (вкладка «Настройки»).")
            return
        self.loading = True
        self.info.configure(text=f"Загружаю {self.ticker}…")
        env = "sandbox" if self.app.s.get("sandbox_token") else "prod"
        tk_, tf = self.ticker, self.tf

        def job():
            try:
                if srv:                          # через сервер робота: компьютер к Т-Инвестициям не обращается
                    rows, trades, tier = _from_server(srv.get("/api/chart", ticker=tk_, tf=tf))
                else:
                    rows, trades, tier = rc.chart_data(tok, bool(self.app.s.get("sandbox_token")), tk_, tf, self.app.s)
                self.tier = tier or ""
                self.res.put((tk_, tf, (rows, trades), None))
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
        rows, self.trades = rows if rows else ([], [])
        self.bars = rows or []
        self._summary()
        if not self.bars:
            self.info.configure(text=f"{self.ticker}: свечей нет")
        self.draw()

    def _sp(self):
        s = self.app.s
        sp = dict(at.SPIKE_DEFAULTS)
        sp.update(two_takes=bool(s.get("two_takes", True)), half=int(s.get("spike_half", 50)) / 100,
                  trail_r=float(s.get("spike_trail", 1.0)))
        return sp

    def _summary(self):
        if self.tf not in (5, 15, 60):
            self.sum_lbl.configure(text="сделки робота показываются на 15м и 1ч")
            return
        if self.tf == 5:
            self.sum_lbl.configure(text="на 5 мин робот не торгует — сделок нет (переключите на 15м или 1ч)")
            return
        if getattr(self, "tier", "") == "3":
            self.sum_lbl.configure(text="3-й эшелон — робот не торгует, сделок нет")
            return
        done = [t for t in self.trades if not t["open"]]
        if not self.trades:
            self.sum_lbl.configure(text=f"сделок робота за {DEPTH[self.tf]} дн. нет")
            return
        wins = sum(1 for t in done if t["result"] > 0)
        tot = sum(t["result"] for t in done) * 100
        dates = ""
        tier = {"index": "индекс", "2": "2-й эшелон"}.get(getattr(self, "tier", ""), "")
        self.sum_lbl.configure(text=f"{tier}: сделок за {DEPTH[self.tf]} дн. {len(self.trades)}, в плюсе {wins}, сумма {tot:+.2f}%")
        self._go_i = 0

    def goto_trade(self):
        """Прокрутить график к формации (по очереди, от последней к первой)."""
        if not self.trades:
            return
        tr = self.trades[-1 - (self._go_i % len(self.trades))]
        self._go_i += 1
        center = tr["entry_i"] + self.n_vis // 3
        self.offset = max(0, len(self.bars) - 1 - center)
        self.draw()

    def _auto(self):
        if not self.winfo_exists():
            return
        if self.offset == 0:
            self.load()
        self.after(20000 if self.tf <= 15 else 60000, self._auto)

    # ------------------------------------------------------------------ мышь
    def _ywheel(self, e):
        self.yzoom = max(0.2, min(10.0, self.yzoom * (1 / 1.15 if e.delta > 0 else 1.15)))
        self.draw()

    def _wheel(self, e):
        self._zoom(-1 if e.delta > 0 else 1)

    def _zoom(self, d):
        self.n_vis = int(max(30, min(600, self.n_vis * (1.15 if d > 0 else 1 / 1.15))))
        self.draw()

    def _lv_at(self, yy):
        for name, ly in self._lv_hit:
            if abs(ly - yy) <= 6:
                return name
        return None

    def _price_at(self, yy):
        hi, lo, ph = self._ymap
        return hi - (yy - 4) / (ph - 8) * (hi - lo)

    def _press(self, e):
        self._px, self._off0 = e.x, self.offset
        self._py, self._pan0 = e.y, self.ypan
        name = self._lv_at(e.y) if getattr(self, "_ymap", None) else None
        self._lv_drag = [name, self._price_at(e.y)] if name else None

    def _release(self, e):
        if not self._lv_drag:
            return
        name, val = self._lv_drag
        self._lv_drag = None
        robot = getattr(self.app, "robot", None)
        if robot and hasattr(robot, "request_levels"):
            key = {"стоп": "stop", "тейк1": "take", "тейк2": "take2"}[name]
            robot.request_levels(self.ticker, **{key: round(val, 6)})
            try:
                self.app.log(f"{self.ticker}: {name} перенесён на {_fmt(val)} (с графика).\n")
            except Exception:
                pass
        self.draw()

    def _drag(self, e):
        if self._lv_drag:
            self._lv_drag[1] = self._price_at(e.y)
            self.draw()
            return
        bw = max(1.0, (self.cv.winfo_width() - 70) / self.n_vis)
        self.offset = int(max(0, min(len(self.bars) - 10, self._off0 + (e.x - self._px) / bw)))
        ph = max(50, getattr(self, "_ymap", (0, 0, 400))[2] - 8)
        self.ypan = self._pan0 + (e.y - self._py) / ph      # вниз мышью — цена «едет» вниз, видно то, что выше
        self.draw()

    def _motion(self, e):
        self.cv.configure(cursor="sb_v_double_arrow" if self._lv_at(e.y) else "")
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
        pad_r = max(8, self.n_vis // 8)               # пустое место справа от последней свечи
        bw = pw / (self.n_vis + pad_r)
        lo = min(min(b["l"] for b in vis), min(e20[start:end]), min(e10[start:end]))
        hi = max(max(b["h"] for b in vis), max(e20[start:end]), max(e10[start:end]))
        poss = self._positions()
        for p in poss:
            for lv in (p.get("stop"), p.get("entry"), p.get("take"), p.get("take2")):
                if lv:
                    lo, hi = min(lo, lv), max(hi, lv)
        pad = (hi - lo) * 0.06 or hi * 0.01
        lo, hi = lo - pad, hi + pad
        if self.yzoom != 1.0 or self.ypan:                 # ручной масштаб и сдвиг по вертикали
            mid, half = (hi + lo) / 2, (hi - lo) / 2 * self.yzoom
            sh = self.ypan * 2 * half
            lo, hi = mid - half + sh, mid + half + sh
        y = lambda v: 4 + (hi - v) / (hi - lo) * (price_h - 8)
        self._ymap, self._lv_hit = (hi, lo, price_h), []
        x0 = pw - (len(vis) + pad_r) * bw              # отступ справа
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
        # формации из истории: вход и выходы
        hover_trade = None
        if self.v_hist.get():
            for tr in self.trades:
                ie = tr["entry_i"] - start
                ix = tr["exits"][-1][0] - start
                if ix < 0 or ie >= len(vis):
                    continue
                s_ = tr["side"]
                good = tr["result"] > 0
                lc_ = UP if good else DOWN
                xe = x0 + (ie + 0.5) * bw
                xl = x0 + (ix + 0.5) * bw
                cv.create_line(xe, y(tr["entry"]), xl, y(tr["exits"][-1][1]), fill=lc_, dash=(4, 3))
                if 0 <= ie < len(vis):
                    ye = y(tr["entry"])
                    if s_ > 0:
                        cv.create_polygon(xe, ye + 2, xe - 7, ye + 14, xe + 7, ye + 14, fill="#00e676", outline="black")
                    else:
                        cv.create_polygon(xe, ye - 2, xe - 7, ye - 14, xe + 7, ye - 14, fill="#ff5252", outline="black")
                    cv.create_line(xe - 9, ye, xe + 9, ye, fill="white", width=2)
                    cv.create_line(xe, y(tr["stop"]) , xe + 30, y(tr["stop"]), fill=DOWN, width=2)
                    if self.hover and abs(self.hover[0] - xe) < max(6, bw):
                        hover_trade = tr
                for qi, px_, frac, why in tr["exits"]:
                    i_ = qi - start
                    if 0 <= i_ < len(vis):
                        xx, yy = x0 + (i_ + 0.5) * bw, y(px_)
                        col_ = "#ffd54f" if why.startswith("тейк") else "#b388ff" if why == "перенос" else ("#9e9e9e" if why in ("б/у", "открыта", "конец дня", "18:25")
                                                                         else ("#4fc3f7" if why == "трейлинг" else "#ff5252"))
                        cv.create_oval(xx - 5, yy - 5, xx + 5, yy + 5, fill=col_, outline="black")
                        cv.create_text(xx + 7, yy - 9, text=why, fill=col_, anchor="w", font=("Segoe UI", 8))
                if 0 <= ix < len(vis):
                    cv.create_text(xl + 7, y(tr["exits"][-1][1]) + 10, text=f"{tr['result'] * 100:+.2f}%", fill=lc_,
                                   anchor="w", font=("Consolas", 9, "bold"))
        # уровни позиции
        for p in poss:
            for lv, col, name in ((p.get("entry"), "#9598a1", "вход"), (p.get("stop"), DOWN, "стоп"),
                                  (None if p.get("stage", 0) >= 1 else p.get("take"), UP, "тейк1"),
                                  (None if p.get("stage", 0) >= 2 else p.get("take2"), UP, "тейк2")):
                if self._lv_drag and self._lv_drag[0] == name and lv:
                    lv = self._lv_drag[1]                       # тащим мышью — рисуем на новом месте
                if lv:
                    wid = 2 if name != "вход" else 1
                    cv.create_line(0, y(lv), pw, y(lv), fill=col, dash=(6, 4), width=wid)
                    cv.create_text(6, y(lv) - 8, text=f"{name} {_fmt(lv)}" + ("  ⇕" if name != "вход" else ""), fill=col,
                                   anchor="w", font=("Consolas", 9))
                    if name != "вход":
                        self._lv_hit.append((name, y(lv)))
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
                                 f"EMA10 {_fmt(e10[start + k])}  EMA20 {_fmt(e20[start + k])}"
                                 + (self._trade_txt(hover_trade) if hover_trade else ""))

    def _trade_txt(self, tr):
        b = self.bars
        ex = ", ".join(f"{w} {b[i]['t']:%H:%M} {_fmt(p)} ({f * 100:.0f}%)" for i, p, f, w in tr["exits"])
        return (f"\n{'ЛОНГ' if tr['side'] > 0 else 'ШОРТ'}: импульс {tr['impulse_t']:%d.%m %H:%M}, касание {tr['touch_t']:%H:%M}, "
                f"вход {b[tr['entry_i']]['t']:%H:%M} по {_fmt(tr['entry'])}, стоп {_fmt(tr['stop'])}, тейк1 {_fmt(tr['t1'])}"
                + (f", тейк2 {_fmt(tr['t2'])}" if tr.get("t2") else "") + f" → {ex}; итог {tr['result'] * 100:+.2f}%")


def _from_server(d):
    """Ответ /api/chart сервера → свечи и сделки в том виде, что рисует окно графика."""
    from datetime import timezone as _tz
    conv = lambda ts: datetime.fromtimestamp(ts, _tz.utc).replace(tzinfo=sc.MSK)     # сервер отдаёт время МСК «как есть»
    rows = [{"t": conv(b["t"]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)} for b in d.get("bars", [])]
    idx = {b["t"]: i for i, b in enumerate(d.get("bars", []))}
    year = datetime.now(sc.MSK).year

    def dt(s_):
        try:
            return datetime.strptime(f"{s_}.{year}", "%d.%m %H:%M.%Y").replace(tzinfo=sc.MSK)
        except Exception:
            return rows[0]["t"] if rows else datetime.now(sc.MSK)
    trades = []
    for t in d.get("trades", []):
        if t["entry_t"] not in idx:
            continue
        trades.append({"side": t["side"], "entry": t["entry"], "stop": t["stop"], "t1": t.get("t1"), "t2": t.get("t2"),
                       "entry_i": idx[t["entry_t"]], "result": t["result"], "open": t.get("open", False),
                       "impulse_t": dt(t.get("impulse")), "touch_t": dt(t.get("touch")),
                       "exits": [(idx.get(x["t"], idx[t["entry_t"]]), x["px"], x["frac"], x["why"]) for x in t.get("exits", [])]})
    return rows, trades, d.get("tier")


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
