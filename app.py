# -*- coding: utf-8 -*-
"""
Сканер Мосбиржи — настольное приложение.
Стратегия: сильное движение дня по рынку, вход в 15:00, стоп 3%, выход в 17:00.
Кнопка «Старт» на вкладке «Автоторговля» запускает робота: он сам работает по будням 07:00–18:30.
"""
import contextlib
import csv
import json
import os
import queue
import threading
import traceback
import types
from datetime import date, datetime, timedelta

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

import scanner as sc
import autotrader as at

APP_TITLE = "Сканер Мосбиржи"


def app_version():
    """Версия сборки: файл version.txt, который кладёт сборка на GitHub (коммит и дата)."""
    import sys as _s
    for base in (getattr(_s, "_MEIPASS", ""), os.path.dirname(os.path.abspath(__file__))):
        p = os.path.join(base, "version.txt")
        if base and os.path.exists(p):
            try:
                return open(p, encoding="utf-8").read().strip()
            except Exception:
                pass
    return "dev"


VERSION = app_version()
SETTINGS = os.path.join(sc.HERE, "settings.json")
DEFAULTS = {"threshold": 2.0, "entry": "15:00", "capital": 1_000_000, "sandbox_token": True,
            "auto_signals": True, "auto_journal": True, "remember_token": False, "token": "",
            "rescan": False, "autostart": False,
            "spike_on": True, "spike_tfs": [5, 15, 60], "spike_all": True, "spike_half": 50, "spike_trail": 1.0,
            "spike_max": 5}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    data = dict(s)
    if not data.get("remember_token"):
        data["token"] = ""
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


class QueueWriter:
    """Перенаправляет print() из рабочих задач в окно журнала действий."""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))

    def flush(self):
        pass


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def now_msk():
    return datetime.now(sc.MSK)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} · версия {VERSION}")
        self.geometry("1100x760")
        self.minsize(820, 560)
        self.s = load_settings()
        if self.s.get("token"):
            os.environ["TINVEST_TOKEN"] = self.s["token"]
        self.q = queue.Queue()
        self.tasks = queue.Queue()
        self.busy = threading.Event()
        self.done_today = {}
        self.robot = None
        self.robot_thread = None
        import sys as _s
        _s.stdout = QueueWriter(self.q)      # print() из любых потоков — в журнал действий
        self._apply_settings()
        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()
        self.after(200, self._poll)
        self.after(500, self.refresh_journal)
        self.after(600, self.refresh_sandbox)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.s.get("autostart"):
            self.after(1500, self.robot_start)
        self.log(f"Версия {VERSION}. Готово. Порог {self.s['threshold']:g}%, вход в {self.s['entry']}. "
                 f"Журналы сохраняются в папке: {sc.HERE}\n")

    # ------------------------------------------------------------------ интерфейс
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 6))
        self._clipboard_bindings()

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        self.status = ttk.Label(top, text="", style="Big.TLabel")
        self.status.pack(side="left")
        self.clock = ttk.Label(top, text="")
        self.clock.pack(side="right")

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        nb = ttk.Notebook(pw)
        self.nb = nb
        pw.add(nb, weight=5)
        logf = ttk.LabelFrame(pw, text="Журнал действий", padding=4)
        pw.add(logf, weight=1)
        self.logbox = ScrolledText(logf, height=8, font=("Consolas", 9), state="disabled")
        self.logbox.pack(fill="both", expand=True)

        # --- Сегодня
        t1 = ttk.Frame(nb, padding=10)
        nb.add(t1, text="Сегодня")
        bar = ttk.Frame(t1)
        bar.pack(fill="x")
        ttk.Button(bar, text="Посчитать сигналы сейчас", command=self.signals_now).pack(side="left")
        ttk.Button(bar, text="Ждать времени входа и показать", command=self.signals_wait).pack(side="left", padx=6)
        ttk.Button(bar, text="Стоп", command=self.stop).pack(side="left")
        self.market_lbl = ttk.Label(t1, text="Сигналов пока нет.")
        self.market_lbl.pack(anchor="w", pady=(8, 4))
        self.sig_tree = self._tree(t1, [("ticker", "Бумага", 90), ("side", "Направление", 140), ("move", "Движение дня", 120),
                                        ("price", "Цена", 100), ("stop", "Стоп", 100)])
        ttk.Label(t1, text="Капитал делится поровну между сигналами. Все позиции закрыть в 17:00, раньше — только по стопу.",
                  foreground="#555").pack(anchor="w", pady=(6, 0))

        # --- Журнал
        t2 = ttk.Frame(nb, padding=10)
        nb.add(t2, text="Журнал форвард-теста")
        bar = ttk.Frame(t2)
        bar.pack(fill="x")
        ttk.Button(bar, text="Записать сегодняшний день", command=self.journal_today).pack(side="left")
        ttk.Label(bar, text="  Дозаполнить с даты:").pack(side="left")
        self.from_var = tk.StringVar(value=(date.today() - timedelta(days=14)).isoformat())
        ttk.Entry(bar, textvariable=self.from_var, width=12).pack(side="left")
        ttk.Button(bar, text="Дозаполнить", command=self.journal_backfill).pack(side="left", padx=6)
        ttk.Button(bar, text="Обновить", command=self.refresh_journal).pack(side="left")
        self.stats_lbl = ttk.Label(t2, text="", justify="left")
        self.stats_lbl.pack(anchor="w", pady=(8, 4))
        self.j_tree = self._tree(t2, [("date", "Дата", 90), ("ticker", "Бумага", 70), ("side", "Сторона", 70),
                                      ("move", "Движение", 80), ("entry", "Вход", 80), ("exit", "Выход", 80),
                                      ("reason", "Причина", 70), ("net", "Итог после затрат", 120)])

        # --- Автоторговля
        t3 = ttk.Frame(nb, padding=10)
        nb.insert(0, t3, text="Автоторговля")
        bar = ttk.Frame(t3)
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="▶  Старт", command=self.robot_start, style="Start.TButton")
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  Стоп", command=self.robot_stop, style="Start.TButton", state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="Проверить доступ", command=self.check_access).pack(side="left", padx=(18, 0))
        self.robot_lbl = ttk.Label(t3, text="Робот остановлен. Нажмите «Старт» — дальше он всё делает сам.", style="Big.TLabel")
        self.robot_lbl.pack(anchor="w", pady=(10, 2))
        ttk.Label(t3, text="По будням 07:00–18:30 МСК, сделки на демо-счёте песочницы. Стратегия «15:00»: каждую минуту "
                           "пересчитывает движение акций индекса, во время входа сам открывает сделки, стоп 3%, выход 16:59:50. "
                           "Всплески объёма: после каждой 5-минутки ищет импульс на объёме → касание EMA20 → свечу-подтверждение "
                           "(5м/15м/1ч), входит сам, стоп под низом всего отката, тейк1 50% у хая импульса, затем стоп в б/у и трейлинг. "
                           "Компьютер не должен засыпать, приложение должно быть открыто.",
                  foreground="#555", justify="left", wraplength=1040).pack(anchor="w", pady=(0, 6))
        vp = ttk.PanedWindow(t3, orient="vertical")
        vp.pack(fill="both", expand=True)
        mid = ttk.PanedWindow(vp, orient="horizontal")
        vp.add(mid, weight=3)
        lnb = ttk.Notebook(mid)
        self.lnb = lnb
        mid.add(lnb, weight=1)
        lf = ttk.Frame(lnb, padding=4)
        lnb.add(lf, text="Индекс и акции (от открытия 10:00)")
        sf = ttk.Frame(lnb, padding=4)
        lnb.add(sf, text="Всплески объёма")
        self.spk_tree = self._tree(sf, [("time", "Время", 50), ("ticker", "Бумага", 60), ("tf", "ТФ", 35), ("val", "Оборот, млн", 75),
                                        ("mult", "×", 40), ("move", "Импульс", 65), ("reason", "Статус", 125)])
        self.scan_lbl = ttk.Label(lf, text="—")
        self.scan_lbl.pack(anchor="w")
        self.scan_tree = self._tree(lf, [("ticker", "Бумага", 70), ("move", "Движение", 80), ("reason", "Статус", 110)])
        rf = ttk.LabelFrame(mid, text="Открытые позиции", padding=4)
        mid.add(rf, weight=1)
        self.pos_tree = self._tree(rf, [("ticker", "Бумага", 60), ("strat", "Стратегия", 70), ("side", "Сторона", 55), ("lots", "Лотов", 55),
                                        ("entry", "Вход", 65), ("stop", "Стоп", 65), ("take", "Тейк1", 65), ("last", "Сейчас", 65),
                                        ("res", "Итог", 60)])
        hf = ttk.LabelFrame(vp, text="Закрытые сделки", padding=4)
        vp.add(hf, weight=2)
        self.sb_tree = self._tree(hf, [("date", "Дата", 90), ("strat", "Стратегия", 90), ("ticker", "Бумага", 70), ("side", "Сторона", 70), ("lots", "Лотов", 60),
                                       ("entry", "Вход", 80), ("exit", "Выход", 80), ("reason", "Причина", 120), ("res", "Результат", 90)])
        nb.select(t3)

        # --- Настройки
        t4 = ttk.Frame(nb, padding=14)
        nb.add(t4, text="Настройки")
        self.v_thr = tk.StringVar(value=f"{self.s['threshold']:g}")
        self.v_entry = tk.StringVar(value=self.s["entry"])
        self.v_cap = tk.StringVar(value=str(int(self.s["capital"])))
        self.v_tok = tk.StringVar(value=self.s.get("token", ""))
        self.v_sbtok = tk.BooleanVar(value=self.s["sandbox_token"])
        self.v_rem = tk.BooleanVar(value=self.s["remember_token"])
        self.v_as = tk.BooleanVar(value=self.s["auto_signals"])
        self.v_aj = tk.BooleanVar(value=self.s["auto_journal"])
        self.v_rescan = tk.BooleanVar(value=self.s.get("rescan", False))
        self.v_autostart = tk.BooleanVar(value=self.s.get("autostart", False))
        self.v_spk = tk.BooleanVar(value=self.s.get("spike_on", True))
        self.v_spk_tf = {tf: tk.BooleanVar(value=tf in self.s.get("spike_tfs", [5, 15, 60])) for tf in (5, 15, 60)}
        self.v_spk_all = tk.BooleanVar(value=self.s.get("spike_all", True))
        self.v_spk_half = tk.StringVar(value=str(self.s.get("spike_half", 50)))
        self.v_spk_trail = tk.StringVar(value=f"{self.s.get('spike_trail', 1.0):g}")
        self.v_spk_max = tk.StringVar(value=str(self.s.get("spike_max", 5)))
        g = ttk.Frame(t4)
        g.pack(anchor="nw", fill="x")
        r = 0
        ttk.Label(g, text="Порог движения акции, %:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Combobox(f, textvariable=self.v_thr, width=6, state="readonly",
                     values=[f"{x:g}" for x in sc.THRESHOLD_CHOICES]).pack(side="left")
        ttk.Label(f, text="   Время входа:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_entry, width=7, state="readonly", values=sc.ENTRY_CHOICES).pack(side="left")
        r += 1
        self.bt_lbl = tk.Label(g, text="", justify="left", anchor="w", font=("Segoe UI", 9))
        self.bt_lbl.grid(row=r, column=1, sticky="w")
        self.v_thr.trace_add("write", lambda *_: self._show_bt())
        self.v_entry.trace_add("write", lambda *_: self._show_bt())
        self._show_bt()
        r += 1
        ttk.Label(g, text="Капитал песочницы, ₽:").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(g, textvariable=self.v_cap, width=14).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(g, text="Токен Т-Инвестиций:").grid(row=r, column=0, sticky="w", pady=4)
        tf = ttk.Frame(g); tf.grid(row=r, column=1, sticky="w")
        self.tok_entry = ttk.Entry(tf, textvariable=self.v_tok, width=52, show="•")
        self.tok_entry.pack(side="left")
        ttk.Button(tf, text="Вставить из буфера", command=self.paste_token).pack(side="left", padx=(6, 0))
        ttk.Button(tf, text="Показать", command=self.toggle_token).pack(side="left", padx=(6, 0))
        r += 1
        ttk.Checkbutton(g, text="Это токен песочницы", variable=self.v_sbtok).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(g, text="Запомнить токен на этом компьютере (хранится открытым текстом в settings.json)",
                        variable=self.v_rem).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Separator(g).grid(row=r, column=0, columnspan=2, sticky="ew", pady=10)
        r += 1
        ttk.Label(g, text="Автоматический режим (по будням, пока приложение открыто):").grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Checkbutton(g, text="Считать сигналы во время входа и подать звуковой сигнал", variable=self.v_as).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(g, text="Записывать день в журнал форвард-теста в 17:20", variable=self.v_aj).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(g, text="Запускать робота автоторговли сразу при открытии программы", variable=self.v_autostart).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(g, text="«15:00»: добирать новые сигналы до 16:30 (на истории хуже: −8…−20% вместо +23…+35%)",
                        variable=self.v_rescan).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Separator(g).grid(row=r, column=0, columnspan=2, sticky="ew", pady=10)
        r += 1
        ttk.Label(g, text="Всплеск объёма + откат к EMA20:").grid(row=r, column=0, sticky="w")
        ttk.Checkbutton(g, text="Робот торгует по этой формации (следит за всплесками всегда)", variable=self.v_spk).grid(row=r, column=1, sticky="w")
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(f, text="Таймфреймы:").pack(side="left")
        for tf in (5, 15, 60):
            ttk.Checkbutton(f, text="1 час" if tf == 60 else f"{tf} мин", variable=self.v_spk_tf[tf]).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(f, text="искать по всем акциям биржи (иначе только индекс)", variable=self.v_spk_all).pack(side="left", padx=(16, 0))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(f, text="Закрыть на тейке1, %:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_half, width=5, state="readonly", values=["30", "50", "70", "100"]).pack(side="left", padx=(4, 12))
        ttk.Label(f, text="Трейлинг, R (расстояние до стопа):").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_trail, width=5, state="readonly", values=["0.5", "1", "1.5", "2"]).pack(side="left", padx=(4, 12))
        ttk.Label(f, text="Макс. позиций:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_max, width=4, state="readonly", values=["1", "2", "3", "5", "8"]).pack(side="left", padx=(4, 0))
        r += 1
        tk.Label(g, text="На истории (44 акции индекса, 10.2025–10.2026):\n" + "\n".join(f"  • {at.SPIKE_PRESETS[tf]['bt']}" for tf in (5, 15, 60))
                 + "\nСтоп — под самой низкой точкой всего отката (с закруглением) с запасом. На каждую сделку — 10% капитала. Второй эшелон на истории не проверен — смотрите журнал «всплеск».",
                 justify="left", anchor="w", font=("Segoe UI", 9), fg="#555", bg=ttk.Style().lookup("TFrame", "background") or None).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Button(g, text="Сохранить настройки", command=self.save).grid(row=r, column=1, sticky="w", pady=12)
        ttk.Label(t4, text="Без токена данные берутся с сайта Мосбиржи; бесплатные данные там обычно идут с задержкой около 15 минут.\n"
                           "Для журнала форвард-теста это не важно, для сигналов в реальном времени лучше задать токен.",
                  foreground="#555", justify="left").pack(anchor="w", pady=(10, 0))
        self._tick()

    # ------------------------------------------------------------------ буфер обмена
    def _clipboard_bindings(self):
        """Ctrl+V/C/X/A при любой раскладке (по коду клавиши) и меню по правой кнопке мыши."""
        codes = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}   # коды клавиш V, C, X в Windows

        def on_ctrl(e):
            return self._on_ctrl(e, codes)
        def menu(e):
            w = e.widget
            m = tk.Menu(self, tearoff=0)
            m.add_command(label="Вставить", command=lambda: w.event_generate("<<Paste>>"))
            m.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
            m.add_command(label="Вырезать", command=lambda: w.event_generate("<<Cut>>"))
            m.tk_popup(e.x_root, e.y_root)

        for cls in ("TEntry", "Entry", "Text"):
            self.bind_class(cls, "<Control-KeyPress>", on_ctrl, add="+")
            self.bind_class(cls, "<Button-3>", menu, add="+")
            self.bind_class(cls, "<Shift-Insert>", lambda e: e.widget.event_generate("<<Paste>>"), add="+")

    def _on_ctrl(self, e, codes):
        ev = codes.get(e.keycode)
        if ev and e.keysym.lower() not in ("v", "c", "x"):    # латиница уже обработана самим Tk
            e.widget.event_generate(ev)
            return "break"
        if e.keycode == 65 and e.keysym.lower() != "a":         # Ctrl+A при русской раскладке
            try:
                e.widget.select_range(0, "end")
                e.widget.icursor("end")
            except Exception:
                e.widget.tag_add("sel", "1.0", "end")
            return "break"

    def paste_token(self):
        try:
            txt = self.clipboard_get().strip()
        except tk.TclError:
            messagebox.showinfo(APP_TITLE, "Буфер обмена пуст. Скопируйте токен ещё раз.")
            return
        self.v_tok.set(txt)
        self.log(f"Токен вставлен из буфера ({len(txt)} символов). Не забудьте нажать «Сохранить настройки».\n")

    def toggle_token(self):
        self.tok_entry.configure(show="" if self.tok_entry.cget("show") else "•")

    def _bt(self, entry, thr):
        return sc.BACKTEST.get((entry, float(thr)))

    def _show_bt(self):
        try:
            v = self._bt(self.v_entry.get(), float(self.v_thr.get()))
        except ValueError:
            v = None
        if not v:
            self.bt_lbl.configure(text="", fg="#555")
            return
        per, tot, dd, n = v
        txt = (f"На истории (10.2025–10.2026, затраты 0,16%): сделок {n}, средняя сделка {per:+.2f}%, "
               f"итог {tot:+.1f}%, макс. просадка {dd:.1f}%.")
        if per <= 0:
            txt += "\nВнимание: это сочетание на истории теряло деньги. Лучшее — вход 15:00, порог 2,5%."
        self.bt_lbl.configure(text=txt, fg="#b3261e" if per <= 0 else "#0a7a2f")

    def _tree(self, parent, cols):
        fr = ttk.Frame(parent)
        fr.pack(fill="both", expand=True)
        tv = ttk.Treeview(fr, columns=[c[0] for c in cols], show="headings")
        for key, title, w in cols:
            tv.heading(key, text=title)
            tv.column(key, width=w, anchor="w" if key in ("ticker", "side", "reason", "date") else "e")
        sb = ttk.Scrollbar(fr, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("pos", foreground="#0a7a2f")
        tv.tag_configure("neg", foreground="#b3261e")
        return tv

    # ------------------------------------------------------------------ служебное
    def log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _tick(self):
        n = now_msk()
        self.clock.configure(text=n.strftime("МСК %d.%m.%Y %H:%M:%S"))
        busy = "выполняется задача" if self.busy.is_set() else "ожидание"
        auto = []
        if self.s["auto_signals"]:
            auto.append(f"сигналы в {self.s['entry']}")
        if self.robot_running():
            auto.append("робот работает")
        if self.s["auto_journal"]:
            auto.append("журнал в 17:20")
        self.status.configure(text=f"Порог {self.s['threshold']:g}% · вход {self.s['entry']} · {busy}"
                                   + (f" · авто: {', '.join(auto)}" if auto else ""))
        self.after(1000, self._tick)

    def _apply_settings(self):
        sc.configure(float(self.s["threshold"]), self.s["entry"])

    def save(self):
        try:
            cap = float(self.v_cap.get().replace(" ", ""))
        except ValueError:
            messagebox.showerror(APP_TITLE, "Капитал должен быть числом, например 1000000.")
            return
        self.s.update(threshold=float(self.v_thr.get()), entry=self.v_entry.get(), capital=cap,
                      token=self.v_tok.get().strip(), sandbox_token=self.v_sbtok.get(), remember_token=self.v_rem.get(),
                      auto_signals=self.v_as.get(), auto_journal=self.v_aj.get(),
                      rescan=self.v_rescan.get(), autostart=self.v_autostart.get(),
                      spike_on=self.v_spk.get(), spike_tfs=[tf for tf, v in self.v_spk_tf.items() if v.get()],
                      spike_all=self.v_spk_all.get(), spike_half=int(self.v_spk_half.get()),
                      spike_trail=float(self.v_spk_trail.get()), spike_max=int(self.v_spk_max.get()))
        if self.s["token"]:
            os.environ["TINVEST_TOKEN"] = self.s["token"]
        else:
            os.environ.pop("TINVEST_TOKEN", None)
        save_settings(self.s)
        self._apply_settings()
        self.refresh_journal()
        self.refresh_sandbox()
        self.log(f"Настройки сохранены: порог {self.s['threshold']:g}%, вход {self.s['entry']}.\n")
        if self.robot_running():
            self.log("Робот уже работает со старыми настройками. Чтобы применить новые, нажмите «Стоп» и снова «Старт».\n")

    # ------------------------------------------------------------------ задачи
    def submit(self, name, fn, on_done=None):
        if self.busy.is_set() and name != "stop":
            self.log(f"Сейчас выполняется другая задача. «{name}» поставлена в очередь.\n")
        self.tasks.put((name, fn, on_done))

    def _worker(self):
        while True:
            name, fn, on_done = self.tasks.get()
            self.busy.set()
            sc.STOP_EVENT.clear()
            self.q.put(("log", f"\n=== {name} · {now_msk():%H:%M:%S} ===\n"))
            result = None
            try:
                with contextlib.redirect_stdout(QueueWriter(self.q)):
                    result = fn()
            except SystemExit as e:
                self.q.put(("log", f"{e}\n"))
            except sc.CertError as e:
                self.q.put(("log", f"Ошибка: {e}\n"))
                try:
                    anchor, chain = sc.trust_anchor(e.host, e.port)
                    self.q.put(("log", "Сервер предъявил сертификаты: " + "; ".join(
                        f"{c['subject']} (выдан: {c['issuer']})" for c in chain) + "\n"))
                except Exception:
                    pass
            except RuntimeError as e:
                self.q.put(("log", f"Ошибка: {e}\n"))
            except BaseException:
                self.q.put(("log", traceback.format_exc()))
            finally:
                self.busy.clear()
            if on_done:
                self.q.put(("call", on_done, result))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.log(item[1])
                elif item[0] == "call":
                    item[1](item[2])
        except queue.Empty:
            pass
        self.after(200, self._poll)

    def stop(self):
        sc.STOP_EVENT.set()
        self.log("Нажат «Стоп».\n")

    # --- сигналы
    def _signals(self, wait):
        args = types.SimpleNamespace(now=not wait, sandbox_token=self.s["sandbox_token"])
        return lambda: sc.signals_mode(args)

    def signals_now(self):
        self.submit("Сигналы сейчас", self._signals(False), self.show_signals)

    def signals_wait(self):
        self.submit(f"Ожидание {self.s['entry']} и сигналы", self._signals(True), self.show_signals)

    def show_signals(self, sigs, alert=False):
        self.sig_tree.delete(*self.sig_tree.get_children())
        sigs = sigs or []
        for s in sigs:
            px = s.get("price")
            self.sig_tree.insert("", "end", tags=("pos" if s["side"] > 0 else "neg",), values=(
                s["ticker"], "КУПИТЬ (лонг)" if s["side"] > 0 else "ПРОДАТЬ (шорт)", f"{s['move'] * 100:+.2f}%",
                f"{px:.4g}" if px else "—", f"{s['stop_px']:.4g}" if s.get("stop_px") else f"{sc.STOP * 100:.0f}% от входа"))
        mk = sigs[0].get("market") if sigs else None
        stamp = now_msk().strftime("%H:%M")
        if sigs:
            self.market_lbl.configure(text=f"{stamp}: сигналов {len(sigs)}" + (f", рынок с открытия {mk * 100:+.2f}%" if mk is not None else ""))
        else:
            self.market_lbl.configure(text=f"{stamp}: сигналов нет.")
        if alert and sigs:
            self.bell()
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(1500, lambda: self.attributes("-topmost", False))
            messagebox.showinfo(APP_TITLE, f"Сигналов: {len(sigs)}. Подробности во вкладке «Сегодня».")

    # --- журнал
    def journal_today(self):
        n = now_msk()
        if (n.hour, n.minute) < (17, 15):
            messagebox.showinfo(APP_TITLE, "Итог дня считается после 17:15 (данные Мосбиржи идут с задержкой).")
            return
        self.submit("Журнал за сегодня", lambda: (sc.journal_day(n.date(), sc.imoex_tickers()), sc.stats()), lambda _: self.refresh_journal())

    def journal_backfill(self):
        try:
            d0 = date.fromisoformat(self.from_var.get().strip())
        except ValueError:
            messagebox.showerror(APP_TITLE, "Дата в формате ГГГГ-ММ-ДД, например 2026-10-01.")
            return
        n = now_msk()
        d1 = n.date() if (n.hour, n.minute) >= (17, 15) else n.date() - timedelta(days=1)

        def job():
            tick = sc.imoex_tickers()
            d = d0
            while d <= d1 and not sc.STOP_EVENT.is_set():
                sc.journal_day(d, tick)
                d += timedelta(days=1)
            sc.stats()
        self.submit(f"Дозаполнение журнала с {d0}", job, lambda _: self.refresh_journal())

    def refresh_journal(self):
        self.j_tree.delete(*self.j_tree.get_children())
        rows = read_csv(sc.JOURNAL)
        for r in reversed(rows[-500:]):
            net = float(r["net_pct"])
            self.j_tree.insert("", "end", tags=("pos" if net > 0 else "neg",), values=(
                r["date"], r["ticker"], r["side"], f"{float(r['move_pct']):+.2f}%", r["entry"], f"{float(r['exit']):.4g}",
                "стоп" if r["reason"] == "stop" else "17:00", f"{net:+.2f}%"))
        days = read_csv(sc.DAYS_LOG)
        if not days:
            self.stats_lbl.configure(text="Журнал пуст. Нажмите «Дозаполнить», чтобы посчитать прошедшие дни, или ждите 17:20.")
            return
        eq, peak, dd = 1.0, 1.0, 0.0
        for d in sorted(days, key=lambda x: x["date"]):
            eq *= 1 + float(d["day_net_pct"]) / 100
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1)
        nets = [float(r["net_pct"]) for r in rows]
        wins = sum(1 for x in nets if x > 0)
        self.stats_lbl.configure(text=(
            f"Дней: {len(days)} ({days[0]['date']} — {days[-1]['date']}), с сигналами: {sum(1 for d in days if int(d['signals']))}. "
            f"Сделок: {len(nets)}, в плюсе {wins} ({(wins / len(nets) * 100) if nets else 0:.0f}%). "
            f"Средняя сделка после затрат: {(sum(nets) / len(nets)) if nets else 0:+.3f}%.\n"
            f"Итог: {(eq - 1) * 100:+.2f}%, макс. просадка {dd * 100:.2f}%. "
            + (lambda v: f"Ориентир по бэктесту: {v[0]:+.2f}% на сделку после затрат." if v else "")(
                self._bt(self.s["entry"], self.s["threshold"]))))

    # --- песочница
    def check_access(self):
        if not os.environ.get("TINVEST_TOKEN"):
            messagebox.showinfo(APP_TITLE, "Сначала введите токен во вкладке «Настройки» и нажмите «Сохранить».")
            return
        self.submit("Проверка доступа", lambda: sc.check_mode(types.SimpleNamespace(sandbox_token=self.s["sandbox_token"])))

    # --- робот
    def robot_running(self):
        return bool(self.robot_thread and self.robot_thread.is_alive())

    def robot_start(self):
        if self.robot_running():
            return
        tok = os.environ.get("TINVEST_TOKEN")
        if not tok or not self.s["sandbox_token"]:
            messagebox.showinfo(APP_TITLE, "Для автоторговли нужен токен песочницы: введите его во вкладке «Настройки», "
                                           "отметьте «Это токен песочницы» и нажмите «Сохранить настройки».")
            return
        self._apply_settings()
        log = lambda m: self.q.put(("log", f"[робот {now_msk():%H:%M:%S}] {m}\n"))
        broker = at.SandboxBroker(tok, float(self.s["capital"]), log=log)
        spike = {"on": bool(self.s.get("spike_on")), "tfs": list(self.s.get("spike_tfs", [5, 15, 60])),
                 "all_market": bool(self.s.get("spike_all", True)), "half": int(self.s.get("spike_half", 50)) / 100,
                 "trail_r": float(self.s.get("spike_trail", 1.0)), "max_pos": int(self.s.get("spike_max", 5))}
        self.robot = at.AutoTrader(broker, float(self.s["capital"]), log=log, rescan=bool(self.s.get("rescan")), spike=spike,
                                   on_update=lambda snap: self.q.put(("call", self.show_robot, snap)))
        robot = self.robot

        def run():
            try:
                robot.run()
            except sc.CertError as e:
                log(f"Ошибка защищённого соединения: {e}. Робот остановлен.")
            except BaseException:
                log("Сбой робота:\n" + traceback.format_exc())
            finally:
                self.q.put(("call", lambda _: self._robot_buttons(), None))
        self.robot_thread = threading.Thread(target=run, daemon=True)
        self.robot_thread.start()
        self._robot_buttons()

    def robot_stop(self):
        if not self.robot_running():
            return
        close = False
        if self.robot.positions:
            ans = messagebox.askyesnocancel(APP_TITLE, "Есть открытые позиции. Закрыть их по рынку?\n\n"
                                                       "«Да» — закрыть и остановить робота.\n"
                                                       "«Нет» — остановить, позиции оставить (после «Старт» робот продолжит их вести).")
            if ans is None:
                return
            close = ans
        self.robot.request_stop(close)
        self.robot_lbl.configure(text="Останавливаю робота…")

    def _robot_buttons(self):
        run = self.robot_running()
        self.btn_start.configure(state="disabled" if run else "normal")
        self.btn_stop.configure(state="normal" if run else "disabled")
        if not run:
            self.robot_lbl.configure(text="Робот остановлен. Нажмите «Старт» — дальше он всё делает сам.")
            self.refresh_sandbox()

    def show_robot(self, snap):
        if self.robot_running():
            self.robot_lbl.configure(text=f"Робот работает: {snap['status']}")
        mk = snap.get("market")
        ls = snap.get("last_scan")
        self.scan_lbl.configure(text=(f"Обновлено {ls:%H:%M}: рынок {mk * 100:+.2f}%, порог {sc.THRESHOLD * 100:g}%"
                                      if ls and mk is not None else "Данные появятся после 10:01."))
        self.scan_tree.delete(*self.scan_tree.get_children())
        for tk, mv, why in snap["scan"][:60]:
            tag = ("pos" if mv > 0 else "neg") if why == "СИГНАЛ" else ()
            self.scan_tree.insert("", "end", tags=tag, values=(tk, f"{mv * 100:+.2f}%", why))
        self.spk_tree.delete(*self.spk_tree.get_children())
        for tm, tkr, tfn, val, mult, body, why in reversed(snap.get("spikes", [])):
            tag = (("pos" if body > 0 else "neg"),) if why == "ПОДТВЕРЖДЁН" else ()
            self.spk_tree.insert("", "end", tags=tag, values=(tm, tkr, tfn, f"{val / 1e6:.0f}", f"{mult:.1f}", f"{body * 100:+.2f}%", why))
        self.pos_tree.delete(*self.pos_tree.get_children())
        for p in snap["positions"]:
            last = p.get("last")
            res = p["side"] * (last / p["entry"] - 1) if last else None
            strat = "всплеск" + (" (б/у)" if p.get("took") else "") if p.get("strat") == "spike" else "15:00"
            self.pos_tree.insert("", "end", tags=(("pos" if res > 0 else "neg"),) if res is not None else (), values=(
                p["ticker"], strat, "лонг" if p["side"] > 0 else "шорт", p["lots"], f"{p['entry']:.4g}", f"{p['stop']:.4g}",
                f"{p['take']:.4g}" if p.get("take") and not p.get("took") else "—",
                f"{last:.4g}" if last else "—", f"{res * 100:+.2f}%" if res is not None else "—"))
        if len(snap["positions"]) != getattr(self, "_npos", None):
            self._npos = len(snap["positions"])
            self.refresh_sandbox()

    def on_close(self):
        if self.robot_running():
            if not messagebox.askyesno(APP_TITLE, "Робот работает. Если закрыть программу, он перестанет торговать "
                                                  "(открытые позиции останутся и будут подхвачены при следующем «Старт»).\n\nЗакрыть?"):
                return
        self.destroy()

    def refresh_sandbox(self):
        self.sb_tree.delete(*self.sb_tree.get_children())
        rows = [dict(r, strat="15:00") for r in read_csv(sc.SANDBOX_LOG)[-500:]] + \
               [dict(r, strat="всплеск") for r in read_csv(sc.SPIKE_LOG)[-500:]]
        rows.sort(key=lambda r: r["date"])
        for r in reversed(rows):
            res = float(r["result_pct"])
            self.sb_tree.insert("", "end", tags=("pos" if res > 0 else "neg",), values=(
                r["date"], r["strat"], r["ticker"], r["side"], r["lots"], r["entry"], r["exit"], r["reason"], f"{res:+.2f}%"))

    # ------------------------------------------------------------------ расписание
    def _scheduler(self):
        while True:
            try:
                n = now_msk()
                today = n.date().isoformat()
                if n.weekday() < 5:
                    eh, em = (int(x) for x in self.s["entry"].split(":"))
                    start = n.replace(hour=eh, minute=em, second=0, microsecond=0)
                    in_window = start - timedelta(minutes=2) <= n < start + timedelta(minutes=10)
                    if in_window and self.done_today.get(("entry", today)) is None:
                        self.done_today[("entry", today)] = True
                        if self.s["auto_signals"] and not self.robot_running():
                            self.q.put(("call", lambda _: self.submit("Авто: сигналы", self._signals(True),
                                                                      lambda sg: self.show_signals(sg, alert=True)), None))
                    if self.s["auto_journal"] and (n.hour, n.minute) >= (17, 20) and n.hour < 23 and self.done_today.get(("journal", today)) is None:
                        self.done_today[("journal", today)] = True
                        self.q.put(("call", lambda _: self.submit("Авто: журнал за сегодня",
                                                                  lambda: (sc.journal_day(now_msk().date(), sc.imoex_tickers()), sc.stats()),
                                                                  lambda _r: self.refresh_journal()), None))
            except Exception:
                pass
            threading.Event().wait(20)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
