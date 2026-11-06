# -*- coding: utf-8 -*-
"""
Сканер Мосбиржи — настольное приложение.
Стратегия: сильное движение дня по рынку, вход в 15:00, стоп 3%, выход в 17:00.
Поиск формаций идёт с открытия программы по будням 07:00–18:50; «Старт торговли» включает сделки на демо-счёте.
"""
import contextlib
import csv
import json
import os
import queue
import threading
import traceback
import time
import types
from datetime import date, datetime, timedelta

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

import scanner as sc
import autotrader as at
import chart
import tgnotify as tg
import robotcore as rc

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
SETTINGS = rc.SETTINGS
DEFAULTS = rc.DEFAULTS
load_settings = rc.load_settings
save_settings = rc.save_settings


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
        self._purged = sc.purge_ru_certs()          # на компьютере сертификатов Минцифры быть не должно
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
        # живые цены: на каждую сделку — IMOEX, позиции, графики (и стопы/тейки своего робота)
        self.live_px, self.live_idx, self.live_t, self._live_dirty, self.charts = {}, None, 0.0, False, []
        self._feed_ti_cache = {}
        self.feed = sc.LiveFeed(get_ti=self._feed_ti, get_robot=self._feed_robot, always=True,
                                has_token=lambda: not self.remote_mode() and bool(os.environ.get("TINVEST_TOKEN")))
        self.feed.listeners.append(lambda tk, px: self._live_in({tk: px}, self.feed.index_now() if tk == "IMOEX" else None))
        self.after(25, self._poll)
        self.after(500, self.refresh_journal)
        self.after(600, self.refresh_sandbox)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # мониторинг формаций — сразу при открытии программы; торговля — по «Старт» (или сразу, если так в настройках)
        self.after(1500, lambda: self.robot_start(trade=bool(self.s.get("autostart")), quiet=True))
        if self._purged:
            self.log("Удалены оставшиеся от прошлых версий сертификаты Минцифры: " + ", ".join(self._purged) + "\n")
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
        self.idx_lbl = tk.Label(top, text="IMOEX …", font=("Segoe UI", 11, "bold"))
        self.idx_lbl.pack(side="right", padx=14)
        threading.Thread(target=self._index_loop, daemon=True).start()

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
        ttk.Label(t1, text="Капитал делится поровну между сигналами. Все позиции закрыть в 18:45, раньше — только по стопу или тейку.",
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
        bar2 = ttk.Frame(t2)
        bar2.pack(fill="x", pady=(6, 0))
        ttk.Button(bar2, text="Скачать историю всех акций (для проверки стратегий)", command=self.download_history).pack(side="left")
        ttk.Button(bar2, text="Стоп", command=self.stop).pack(side="left", padx=6)
        ttk.Label(bar2, text="5-минутки с 01.10.2025 по всем акциям Мосбиржи → архив history5m.zip рядом с программой",
                  foreground="#777").pack(side="left", padx=6)
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
        self.btn_start = ttk.Button(bar, text="▶  Старт торговли", command=self.robot_start, style="Start.TButton")
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  Стоп торговли", command=self.robot_stop, style="Start.TButton", state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="Проверить доступ", command=self.check_access).pack(side="left", padx=(18, 0))
        ttk.Button(bar, text="Открыть график…", command=lambda: self.open_chart("SBER")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Терминал Т-Инвестиций", command=lambda: self.open_tbank(None)).pack(side="left", padx=(6, 0))
        self.robot_lbl = ttk.Label(t3, text="Запускаю поиск формаций…", style="Big.TLabel")
        self.robot_lbl.pack(anchor="w", pady=(10, 2))
        ttk.Label(t3, text="Поиск формаций идёт постоянно с открытия программы — по будням с 07:00 до 23:50. "
                           "«Старт торговли» — робот сам входит и выходит на демо-счёте (входы 09:00–19:00 или 10:00–18:00 — в настройках; "
                           "перенос позиций на следующий день — см. настройки); "
                           "«Стоп торговли» — только наблюдение и уведомления. Стратегия «15:00»: каждую минуту "
                           "пересчитывает движение акций индекса, во время входа сам открывает сделки, стоп 3%, выход 16:59:50. "
                           "Всплески объёма: после каждой 5-минутки ищет импульс на объёме (одна или несколько свечей подряд) → касание EMA20 → свечу-подтверждение "
                           "(5м/15м/1ч), входит сам, стоп под низом всего отката, тейк1 на половине пути до хая (50%) → стоп в б/у и трейлинг, тейк2 на хае (50% остатка), остаток — трейлинг. "
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
        lnb.add(lf, text="«Движение дня»: кандидаты")
        sf = ttk.Frame(lnb, padding=4)
        lnb.add(sf, text="Всплески объёма")
        self.spk_tree = self._tree(sf, [("time", "Импульс", 55), ("sig", "Сигнал", 55), ("ticker", "Бумага", 60), ("tf", "ТФ", 35), ("val", "Оборот, млн", 75),
                                        ("mult", "×", 40), ("move", "Импульс", 65), ("dir", "Ожидание", 75),
                                        ("reason", "Статус", 125)])
        cf = ttk.Frame(lnb, padding=4)
        lnb.add(cf, text="Календарь")
        self.cal_lbl = ttk.Label(cf, text="—", foreground="#555")
        self.cal_lbl.pack(anchor="w")
        self.cal_tree = self._tree(cf, [("date", "Дата", 70), ("dow", "День", 40), ("kind", "Биржа", 170), ("time", "Время", 90), ("robot", "Робот", 170)])
        self.cal_tree.tag_configure("off", foreground="#b33")
        self.after(4000, self._cal_tick)
        self.scan_lbl = ttk.Label(lf, text="—")
        self.scan_lbl.pack(anchor="w")
        self.scan_tree = self._tree(lf, [("ticker", "Бумага", 70), ("move", "Движение", 80), ("reason", "Статус", 110)])
        rf = ttk.LabelFrame(mid, text="Открытые позиции", padding=4)
        mid.add(rf, weight=1)
        pb = ttk.Frame(rf)
        pb.pack(fill="x", pady=(0, 4))
        ttk.Button(pb, text="Закрыть выбранную", command=self.close_selected).pack(side="left")
        ttk.Button(pb, text="Закрыть все", command=self.close_all).pack(side="left", padx=6)
        ttk.Button(pb, text="Закрыть все в плюсе", command=self.close_profit).pack(side="left")
        ttk.Button(pb, text="+ Открыть позицию…", command=self.open_manual).pack(side="left", padx=6)
        ttk.Label(pb, text="   Выбранную во время выхода:").pack(side="left")
        for txt, mode in (("перенести", "carry"), ("закрыть", "close"), ("авто", "")):
            ttk.Button(pb, text=txt, width=10, command=lambda m=mode: self.carry_selected(m)).pack(side="left", padx=2)
        ttk.Button(pb, text="График", command=self.chart_selected).pack(side="left", padx=(12, 0))
        ttk.Button(pb, text="В Т-Инвестиции", command=lambda: self.open_tbank(self._sel_ticker(self.pos_tree))).pack(side="left", padx=(6, 0))
        ttk.Label(pb, text="двойной щелчок по бумаге — график", foreground="#777").pack(side="left", padx=8)
        self.pos_tree = self._tree(rf, [("ticker", "Бумага", 60), ("strat", "Стратегия", 70), ("side", "Сторона", 55), ("lots", "Лотов", 55),
                                        ("entry", "Вход", 65), ("stop", "Стоп", 65), ("take", "Тейк", 75), ("last", "Сейчас", 65),
                                        ("res", "Итог", 60), ("rub", "Итог, ₽", 80)])
        self.pos_tree.tag_configure("hdr", background="#e8ecf3", font=("Segoe UI", 9, "bold"))
        for tv, col in ((self.scan_tree, 0), (self.spk_tree, 2), (self.pos_tree, 0)):
            tv.bind("<Double-1>", lambda e, tv=tv, col=col: self._chart_from(tv, col))
            tv.bind("<Button-3>", lambda e, tv=tv, col=col: self._row_menu(e, tv, col))
        hf = ttk.LabelFrame(vp, text="Закрытые сделки", padding=4)
        vp.add(hf, weight=2)
        self.sb_sum = ttk.Label(hf, text="", justify="left")
        self.sb_sum.pack(anchor="w")
        self.sb_tree = self._tree(hf, [("date", "Дата", 90), ("strat", "Стратегия", 90), ("ticker", "Бумага", 70), ("side", "Сторона", 70), ("lots", "Лотов", 60),
                                       ("entry", "Вход", 80), ("exit", "Выход", 80), ("reason", "Причина", 120), ("res", "Результат", 90)])
        nb.select(t3)

        # --- Рынки: состояние рынка, фильтры и сортировка
        t8 = ttk.Frame(nb, padding=10)
        nb.add(t8, text="Рынки")
        top8 = ttk.Frame(t8); top8.pack(fill="x")
        self.mk_type = tk.StringVar(value="Акции")
        self.mk_tier = tk.StringVar(value="Все")
        self.mk_q = tk.StringVar()
        ttk.Label(top8, text="Тип:").pack(side="left")
        ttk.Combobox(top8, textvariable=self.mk_type, width=10, state="readonly",
                     values=[n for _, n in rc.MARKET_TYPES]).pack(side="left", padx=(4, 12))
        ttk.Label(top8, text="Акции:").pack(side="left")
        ttk.Combobox(top8, textvariable=self.mk_tier, width=16, state="readonly",
                     values=["Все", "Индекс МосБиржи", "Голубые фишки", "2-й эшелон", "3-й эшелон"]).pack(side="left", padx=(4, 12))
        ttk.Label(top8, text="Поиск:").pack(side="left")
        ttk.Entry(top8, textvariable=self.mk_q, width=16).pack(side="left", padx=(4, 12))
        self.mk_form = tk.BooleanVar(value=False)
        ttk.Checkbutton(top8, text="только с формацией", variable=self.mk_form, command=self._render_market).pack(side="left", padx=(0, 12))
        ttk.Button(top8, text="Обновить", command=self.refresh_market).pack(side="left")
        self.mk_sum = ttk.Label(t8, text="", foreground="#555")
        self.mk_sum.pack(anchor="w", pady=4)
        self.mk_tree = self._tree(t8, [("ticker", "Тикер", 90), ("name", "Название", 200), ("price", "Цена", 90), ("pct", "Изм. за день", 100),
                                       ("value", "Оборот, млн ₽", 110), ("time", "Время", 70), ("tier", "Эшелон", 110),
                                       ("form", "Формация EMA20 (сегодня)", 260)])
        self.mk_sort = ("pct", True)
        for col in ("ticker", "name", "price", "pct", "value", "time", "tier", "form"):
            self.mk_tree.heading(col, command=lambda c=col: self._mk_sort_by(c))
        for v in (self.mk_type, self.mk_tier, self.mk_q):
            v.trace_add("write", lambda *_: self._render_market())
        self.mk_tree.bind("<Double-1>", lambda e: (lambda tkr: self.open_chart(tkr, 15) if tkr else None)(self._sel_ticker(self.mk_tree)))
        self._mk = None
        self._mk_tab = t8
        nb.bind("<<NotebookTabChanged>>", lambda _: self.refresh_market() if nb.select() == str(t8) else None, add="+")
        self.after(30000, self._market_tick)

        # --- Результаты: баланс и итоги за периоды
        t7 = ttk.Frame(nb, padding=12)
        nb.add(t7, text="Результаты")
        top7 = ttk.Frame(t7); top7.pack(fill="x")
        self.res_bal = ttk.Label(top7, text="Баланс: —", style="Big.TLabel")
        self.res_bal.pack(side="left")
        ttk.Button(top7, text="Обновить", command=self.refresh_results).pack(side="right")
        ttk.Button(top7, text="Сбросить баланс и начать сначала", command=self.reset_trading).pack(side="right", padx=8)
        self.res_tree = self._tree(t7, [("period", "Период", 180), ("from", "С", 90), ("start", "Баланс на начало", 130),
                                        ("chg", "Изменение, ₽", 120), ("pct", "Изменение, %", 100), ("n", "Сделок", 70),
                                        ("win", "В плюсе", 70), ("pnl", "Итог сделок, ₽", 120), ("avg", "Средняя сделка", 110)])
        self.res_tree.tag_configure("sub", foreground="#5f6673")
        self.res_tree.master.pack_configure(fill="x", expand=False)
        self.res_tree.configure(height=12)
        ttk.Label(t7, text="Изменение баланса", style="Big.TLabel").pack(anchor="w", pady=(10, 2))
        self.res_cv = tk.Canvas(t7, height=260, bg="#131722", highlightthickness=0)
        self.res_cv.pack(fill="both", expand=True)
        self.res_cv.bind("<Configure>", lambda _: self._draw_balance())
        ttk.Label(t7, foreground="#555", wraplength=1000, justify="left", text=(
            "Изменение баланса — по стоимости портфеля демо-счёта (пополнения вычитаются), с учётом открытых позиций; "
            "баланс записывается каждые несколько минут, пока работает робот. Сделки и «итог сделок, ₽» — по закрытым сделкам "
            "обеих стратегий (₽ считаются со сделок, закрытых этой версией, без комиссий).")).pack(anchor="w", pady=4)
        self._res = None
        nb.bind("<<NotebookTabChanged>>", lambda _: self.refresh_results() if nb.select() == str(t7) else None, add="+")
        self.after(5000, self._results_tick)

        # --- Telegram
        t5 = ttk.Frame(nb, padding=14)
        nb.add(t5, text="Telegram")
        self.v_tg_tok = tk.StringVar(value=self.s.get("tg_token", ""))
        self.v_tg_chat = tk.StringVar(value=self.s.get("tg_chat", ""))
        self.v_tg_form = tk.BooleanVar(value=self.s.get("tg_form", True))
        self.v_tg_trades = tk.BooleanVar(value=self.s.get("tg_trades", True))
        self.v_tg_imp = tk.BooleanVar(value=self.s.get("tg_imp", False))
        self.v_tg_trend = tk.BooleanVar(value=self.s.get("tg_trend", True))
        ttk.Label(t5, text="Уведомления в Telegram о найденных формациях и сделках робота", style="Big.TLabel").pack(anchor="w")
        ttk.Label(t5, justify="left", foreground="#555", text=(
            "1. В Telegram откройте @BotFather → /newbot → придумайте имя → скопируйте токен бота.\n"
            "2. Вставьте токен ниже, откройте своего нового бота в Telegram и отправьте ему любое сообщение (например «привет»).\n"
            "3. Нажмите «Узнать chat id» — программа найдёт ваш чат сама. Затем «Тест» и «Сохранить».")).pack(anchor="w", pady=(6, 10))
        g5 = ttk.Frame(t5); g5.pack(anchor="w")
        ttk.Label(g5, text="Токен бота:").grid(row=0, column=0, sticky="w", pady=4)
        self.tg_entry = ttk.Entry(g5, textvariable=self.v_tg_tok, width=52, show="•")
        self.tg_entry.grid(row=0, column=1, sticky="w")
        ttk.Button(g5, text="Вставить из буфера", command=lambda: self._paste_to(self.v_tg_tok)).grid(row=0, column=2, padx=6)
        ttk.Label(g5, text="Chat id:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(g5, textvariable=self.v_tg_chat, width=20).grid(row=1, column=1, sticky="w")
        f5 = ttk.Frame(g5); f5.grid(row=2, column=1, sticky="w", pady=6)
        ttk.Button(f5, text="Узнать chat id", command=self.tg_find_chat).pack(side="left")
        ttk.Button(f5, text="Тест", command=self.tg_test).pack(side="left", padx=6)
        ttk.Checkbutton(g5, text="Все найденные формации (откат к EMA20 с подтверждением) — и те, где робот вошёл, и те, где нет",
                        variable=self.v_tg_form).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(g5, text="Трендовые откаты к EMA20 (объём был в последние 3 дня)",
                        variable=self.v_tg_trend).grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(g5, text="Всплески объёма сразу при появлении (до отката; сообщений будет много)",
                        variable=self.v_tg_imp).grid(row=5, column=1, sticky="w")
        ttk.Checkbutton(g5, text="Входы и выходы робота", variable=self.v_tg_trades).grid(row=6, column=1, sticky="w")
        ttk.Button(g5, text="Сохранить настройки", command=self.save).grid(row=7, column=1, sticky="w", pady=12)

        # --- Сервер и телефон
        t6 = ttk.Frame(nb, padding=14)
        nb.add(t6, text="Сервер и телефон")
        ttk.Label(t6, text="Круглосуточная работа на сервере и управление с телефона", style="Big.TLabel").pack(anchor="w")
        ttk.Label(t6, justify="left", foreground="#555", wraplength=900, text=(
            "1. Арендуйте VPS на Ubuntu 22.04 или 24.04 (1 ядро, 1–2 ГБ памяти; например Timeweb Cloud, Selectel, REG.RU — "
            "около 300–600 ₽ в месяц). Провайдер пришлёт IP-адрес и пароль root.\n"
            "2. Введите их ниже и нажмите «Установить / обновить сервер» — программа сама поставит робота на сервер "
            "(служба запускается сама после перезагрузки и сбоев) и перенесёт туда токен, настройки Telegram и журналы.\n"
            "3. Установите на телефон приложение «Робот Мосбиржи» (файл MoexRobot-android.apk из той же сборки) и "
            "отсканируйте им QR-код, который покажет программа.\n\n"
            "Пароль от VPS не сохраняется. Соединение телефона с сервером шифруется собственным сертификатом сервера, "
            "который знает только приложение по отпечатку из QR-кода — ни в Windows, ни в телефон сертификаты не устанавливаются.\n"
            "Важно: после переноса робот на этом компьютере переходит в режим наблюдения, чтобы два робота не торговали "
            "одним демо-счётом.")).pack(anchor="w", pady=(6, 10))
        g6 = ttk.Frame(t6); g6.pack(anchor="w")
        self.v_srv_ip = tk.StringVar(value=self.s.get("srv_ip", ""))
        self.v_srv_user = tk.StringVar(value=self.s.get("srv_user", "root"))
        self.v_srv_pass = tk.StringVar()
        self.v_srv_tr = tk.BooleanVar(value=True)
        ttk.Label(g6, text="IP-адрес сервера:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(g6, textvariable=self.v_srv_ip, width=24).grid(row=0, column=1, sticky="w")
        ttk.Label(g6, text="Логин:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(g6, textvariable=self.v_srv_user, width=24).grid(row=1, column=1, sticky="w")
        ttk.Label(g6, text="Пароль:").grid(row=2, column=0, sticky="w", pady=4)
        pe = ttk.Entry(g6, textvariable=self.v_srv_pass, width=24, show="•")
        pe.grid(row=2, column=1, sticky="w")
        ttk.Button(g6, text="Вставить из буфера", command=lambda: self._paste_to(self.v_srv_pass)).grid(row=2, column=2, padx=6)
        ttk.Checkbutton(g6, text="Перенести на сервер токен, настройки, Telegram, открытые позиции и журналы",
                        variable=self.v_srv_tr).grid(row=3, column=1, columnspan=2, sticky="w", pady=4)
        self.v_srv_remote = tk.BooleanVar(value=bool(self.s.get("srv_remote", True)))
        ttk.Checkbutton(g6, text="Эта программа — пульт робота на сервере (свой робот на компьютере не запускается)",
                        variable=self.v_srv_remote, command=self._srv_remote_toggle).grid(row=5, column=1, columnspan=2, sticky="w")
        f6 = ttk.Frame(g6); f6.grid(row=4, column=1, columnspan=2, sticky="w", pady=10)
        ttk.Separator(g6).grid(row=6, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(g6, text="Резервный сервер (пара):", font=("Segoe UI", 10, "bold")).grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(g6, foreground="#555", wraplength=760, justify="left", text=(
            "Второй VPS, лучше у другого провайдера. Работают в паре: торгует основной, резервный держит копию позиций и "
            "настроек и продолжает торговлю, если основной не отвечает 5 минут. Если запрос к Т-Инвестициям, Мосбирже или "
            "Telegram не проходит напрямую — он идёт через второй сервер. Телефон и программа переключаются сами.")).grid(
            row=8, column=0, columnspan=3, sticky="w")
        self.v_srv2_ip = tk.StringVar(value=self.s.get("srv2_ip", ""))
        self.v_srv2_user = tk.StringVar(value=self.s.get("srv2_user", "root"))
        self.v_srv2_pass = tk.StringVar()
        ttk.Label(g6, text="IP-адрес:").grid(row=9, column=0, sticky="w", pady=4)
        ttk.Entry(g6, textvariable=self.v_srv2_ip, width=24).grid(row=9, column=1, sticky="w")
        ttk.Label(g6, text="Логин:").grid(row=10, column=0, sticky="w", pady=4)
        ttk.Entry(g6, textvariable=self.v_srv2_user, width=24).grid(row=10, column=1, sticky="w")
        ttk.Label(g6, text="Пароль:").grid(row=11, column=0, sticky="w", pady=4)
        ttk.Entry(g6, textvariable=self.v_srv2_pass, width=24, show="•").grid(row=11, column=1, sticky="w")
        ttk.Button(g6, text="Вставить из буфера", command=lambda: self._paste_to(self.v_srv2_pass)).grid(row=11, column=2, padx=6)
        fb2 = ttk.Frame(g6); fb2.grid(row=12, column=1, columnspan=3, sticky="w", pady=10)
        ttk.Button(fb2, text="Установить резервный сервер и связать в пару", command=self.server_install_backup).pack(side="left")
        ttk.Button(fb2, text="Обновить резервный сервер без пароля", command=lambda: self.server_update_quick("backup")).pack(side="left", padx=8)
        ttk.Button(f6, text="Установить / обновить сервер", style="Start.TButton", command=self.server_install).pack(side="left")
        ttk.Button(f6, text="Показать QR-код для телефона", command=self.server_qr).pack(side="left", padx=8)
        ttk.Button(f6, text="Обновить сервер без пароля (оба)", command=self.server_update_quick).pack(side="left")

        # --- Настройки
        t4o = ttk.Frame(nb)
        nb.add(t4o, text="Настройки")
        cv4 = tk.Canvas(t4o, highlightthickness=0, bg=ttk.Style().lookup("TFrame", "background") or None)
        sb4 = ttk.Scrollbar(t4o, orient="vertical", command=cv4.yview)
        cv4.configure(yscrollcommand=sb4.set)
        sb4.pack(side="right", fill="y")
        cv4.pack(side="left", fill="both", expand=True)
        t4 = ttk.Frame(cv4, padding=14)
        cv4.create_window(0, 0, window=t4, anchor="nw")
        t4.bind("<Configure>", lambda e: cv4.configure(scrollregion=cv4.bbox("all")))
        cv4.bind("<Enter>", lambda e: cv4.bind_all("<MouseWheel>", lambda ev: cv4.yview_scroll(int(-ev.delta / 120), "units")))
        cv4.bind("<Leave>", lambda e: cv4.unbind_all("<MouseWheel>"))
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
        self.v_day = tk.BooleanVar(value=self.s.get("day_on", True))
        self.DMODE = {"fixed": "в заданное время (проверено на истории)", "signal": "сразу при сигнале 09:00–19:00 (на истории в минусе)"}
        self.v_dmode = tk.StringVar(value=self.DMODE.get(self.s.get("day_mode", "fixed"), self.DMODE["fixed"]))
        self.DAYC = {0: "нет", 1: "на 1 день", 2: "на 2 дня", 3: "на 3 дня"}
        self.v_dexit = tk.StringVar(value=self.s.get("day_exit", "18:45"))
        self.v_dmk = {k: tk.BooleanVar(value=k in self.s.get("day_markets", ["imoex"])) for k, _ in at.MARKETS}
        self.v_dayc = tk.StringVar(value=self.DAYC.get(int(self.s.get("day_carry", 0) or 0), self.DAYC[0]))
        self.v_idx_form = tk.BooleanVar(value=bool(self.s.get("idx_form", True)))
        self.v_e200 = tk.BooleanVar(value=bool(self.s.get("ema200", False)))
        self.v_e10 = tk.BooleanVar(value=bool(self.s.get("ema10_touch", False)))
        self.v_first = tk.BooleanVar(value=bool(self.s.get("first_form", True)))
        self.v_t5 = tk.BooleanVar(value=bool(self.s.get("trade_5m", False)))
        self.E2TF = [("form", "ТФ формации"), ("5", "5 мин"), ("15", "15 мин"), ("60", "1 час"), ("1440", "1 день")]
        self.v_e2tf = {k: tk.BooleanVar(value=k in (self.s.get("ema200_tfs") or ["form"])) for k, _ in self.E2TF}
        self.v_idx_day = tk.BooleanVar(value=bool(self.s.get("idx_day", True)))
        self.EFROM = {"": "как в окне формации / времени входа «Движения дня»", "7": "с открытия рынка — 07:00",
                      "9": "с 09:00",
                      "10": "с начала основной сессии — 10:00"}
        self.v_efrom = tk.StringVar(value=self.EFROM.get(str(self.s.get("entry_from") or ""), self.EFROM[""]))
        self.DREF = {"main": "от начала основной сессии (10:00)", "market": "от открытия рынка (07:00)"}
        self.v_dref = tk.StringVar(value=self.DREF.get(self.s.get("day_ref", "main"), self.DREF["main"]))
        self.IDXREF = {"imoex": "IMOEX — реальное изменение к вчерашнему дню", "imoex_open": "IMOEX — от открытия основной сессии (10:00)",
                       "open": "акции индекса с открытия дня (как в проверке на истории)"}
        self.v_idxref = tk.StringVar(value=self.IDXREF.get(self.s.get("idx_ref", "imoex"), self.IDXREF["imoex"]))
        self.v_wkend = tk.BooleanVar(value=bool(self.s.get("trade_weekends", False)))
        self.v_dtp = tk.StringVar(value=str(self.s.get("day_tp", 8)))
        self.v_dhalf = tk.StringVar(value=str(self.s.get("day_tp_half", 50)))
        self.v_dtrail = tk.StringVar(value=str(self.s.get("day_trail", 2)))
        self.v_dact = tk.StringVar(value=str(self.s.get("day_act", 0)))
        self.v_sound = tk.BooleanVar(value=self.s.get("sound_on", True))
        self.v_trend_on = tk.BooleanVar(value=self.s.get("trend_on", True))
        self.v_trend_trade = tk.BooleanVar(value=self.s.get("trend_trade", False))
        self.v_tier = tk.BooleanVar(value=self.s.get("tier_profile", True))
        self.WIN = {"9-19": "09:00–19:00", "10-18": "10:00–18:00"}
        self.EXIT_EARLY = {"9-19": "19:25", "10-18": "18:45"}
        self.v_win = tk.StringVar(value=self.WIN.get(self.s.get("entry_window", "9-19"), self.WIN["9-19"]))
        self.CARRY = {True: "переносить все позиции (держать до стопа, тейка или трейлинга)",
                      False: "закрыть только в минусе (в плюсе или с тейком1 — перенести, до 2 дней)",
                      "none": "закрыть все позиции"}
        self.v_carry_all = tk.StringVar(value=self.CARRY[rc.carry_choice(self.s)])
        self.v_exit = tk.StringVar(value=self._exit_txt(self.s))
        self.v_takes = tk.StringVar(value="два тейка" if self.s.get("two_takes", True) else "один тейк у хая")
        self.v_spk = tk.BooleanVar(value=self.s.get("spike_on", True))
        self.v_spk_tf = {tf: tk.BooleanVar(value=tf in self.s.get("spike_tfs", [15, 60])) for tf in (5, 15, 60)}
        self.v_mk = {k: tk.BooleanVar(value=k in self.s.get("spike_markets", ["all_shares"])) for k, _ in at.MARKETS}
        self.v_spk_half = tk.StringVar(value=str(self.s.get("spike_half", 50)))
        self.v_spk_trail = tk.StringVar(value=f"{self.s.get('spike_trail', 1.0):g}")
        self.v_spk_max = tk.StringVar(value=str(self.s.get("spike_max", 0) or "без ограничения"))
        self.v_spk_share = tk.StringVar(value=f"{self.s.get('spike_share', 10):g}")
        g = ttk.Frame(t4)
        g.pack(anchor="nw", fill="x")
        r = 0
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
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(f, text="Это токен песочницы", variable=self.v_sbtok).pack(side="left")
        ttk.Checkbutton(f, text="Запомнить токен (открытым текстом в settings.json)", variable=self.v_rem).pack(side="left", padx=(16, 0))
        r += 1
        self.BROKERS = dict(at.BROKER_NAMES)
        self.v_rtok = tk.StringVar(value="")
        self.v_racc = tk.StringVar(value=self.s.get("real_account", ""))
        self.v_broker = tk.StringVar(value=self.BROKERS.get(self.s.get("broker", "tinvest"), self.BROKERS["tinvest"]))
        self.v_fsec = tk.StringVar(value="")
        self.v_facc = tk.StringVar(value=self.s.get("finam_account", ""))
        ttk.Label(g, text="Брокер для торговли:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Combobox(f, textvariable=self.v_broker, width=42, state="readonly", values=list(self.BROKERS.values())).pack(side="left")
        ttk.Label(f, foreground="#a33", text="  реальный счёт Т-Инвестиций и Финам — сделки на реальные деньги").pack(side="left")
        r += 1
        ttk.Label(g, text="Токен реального счёта Т-Инв.:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Entry(f, textvariable=self.v_rtok, width=40, show="•").pack(side="left")
        self.rtok_lbl = ttk.Label(f, foreground="#555", text="  сохранён — оставьте пустым, чтобы не менять" if self.s.get("real_token") else "  не введён (нужен «полный доступ»)")
        self.rtok_lbl.pack(side="left")
        ttk.Label(f, text="   Счёт:").pack(side="left")
        ttk.Entry(f, textvariable=self.v_racc, width=14).pack(side="left")
        ttk.Label(f, foreground="#555", text=" (пусто — основной)").pack(side="left")
        r += 1
        ttk.Label(g, text="Секретный токен Финама:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Entry(f, textvariable=self.v_fsec, width=40, show="•").pack(side="left")
        self.fsec_lbl = ttk.Label(f, foreground="#555", text="  сохранён — оставьте пустым, чтобы не менять" if self.s.get("finam_secret") else "  не введён")
        self.fsec_lbl.pack(side="left")
        ttk.Label(f, text="   Счёт:").pack(side="left")
        ttk.Entry(f, textvariable=self.v_facc, width=14).pack(side="left")
        ttk.Label(f, foreground="#555", text=" (пусто — первый доступный)").pack(side="left")
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(f, text="Включать торговлю сразу при открытии программы (поиск формаций включается всегда)", variable=self.v_autostart).pack(side="left")
        ttk.Checkbutton(f, text="Звук при входе и выходе из позиции", variable=self.v_sound).pack(side="left", padx=(16, 0))
        r += 1
        self.DIRS = {"both": "лонг и шорт", "long": "только лонг", "short": "только шорт"}
        self.v_dir = tk.StringVar(value=self.DIRS.get(self.s.get("direction", "both"), "лонг и шорт"))
        self.FLIP = {"off": "ничего не делать", "all": "закрыть все сделки", "profit": "закрыть сделки в плюсе"}
        self.v_flip = tk.StringVar(value=self.FLIP.get(self.s.get("idx_flip", "off"), self.FLIP["off"]))
        LEV = {"1": "без маржи", "auto": "с маржой — плечо брокера по бумаге", "2": "с маржой ×2", "3": "с маржой ×3",
               "5": "с маржой ×5"}
        self.LEV = LEV
        self.v_lev = tk.StringVar(value=LEV.get(str(self.s.get("leverage", "1")), "без маржи"))
        bg = ttk.Style().lookup("TFrame", "background") or None
        GREY = "#666"

        def row(parent, *items, pady=3):
            """Строка настроек: подписи (str), поля (виджет-фабрики) и подсказки (("hint", текст))."""
            fr = ttk.Frame(parent); fr.pack(anchor="w", fill="x", pady=pady)
            for it in items:
                if isinstance(it, str):
                    ttk.Label(fr, text=it).pack(side="left", padx=(0, 4))
                elif isinstance(it, tuple) and it[0] == "hint":
                    ttk.Label(fr, text=it[1], foreground=GREY).pack(side="left", padx=(4, 0))
                else:
                    it(fr).pack(side="left", padx=(0, 12))
            return fr
        cb = lambda var, vals, w: (lambda fr: ttk.Combobox(fr, textvariable=var, values=vals, width=w, state="readonly"))
        ck = lambda txt, var: (lambda fr: ttk.Checkbutton(fr, text=txt, variable=var))

        def box(title):
            nonlocal r
            r += 1
            lf = ttk.LabelFrame(g, text=" " + title + " ", padding=(10, 6))
            lf.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            return lf

        def markets(parent, vars_):
            fr = ttk.Frame(parent); fr.pack(anchor="w", pady=3)
            ttk.Label(fr, text="Чем торговать:").grid(row=0, column=0, sticky="nw", padx=(0, 6))
            for i, (k, name) in enumerate(at.MARKETS):
                ttk.Checkbutton(fr, text=name, variable=vars_[k]).grid(row=i // 3, column=1 + i % 3, sticky="w", padx=(0, 14))

        # --- общее для обеих стратегий
        b0 = box("Общее для обеих стратегий")
        row(b0, "Сделки:", cb(self.v_dir, list(self.DIRS.values()), 14),
            "Направление рынка — по:", cb(self.v_idxref, list(self.IDXREF.values()), 50))
        row(b0, "Входы не раньше:", cb(self.v_efrom, list(self.EFROM.values()), 46),
            "При развороте рынка:", cb(self.v_flip, list(self.FLIP.values()), 24),
            ("hint", "разворот — рынок ушёл больше чем на 0,1% в другую сторону"))
        f = row(b0, "Размер позиции, % портфеля:", lambda fr: ttk.Combobox(fr, textvariable=self.v_spk_share, width=5,
                values=["1", "2", "3", "5", "10", "15", "20", "25", "33", "50", "100"]), cb(self.v_lev, list(LEV.values()), 32),
                ck("торговать в выходные и праздники (сессии выходного дня)", self.v_wkend))
        self.share_lbl = ttk.Label(b0, text="", foreground=GREY)
        self.share_lbl.pack(anchor="w")

        # --- формация
        b1 = box("1. Всплеск объёма + откат к EMA20 — вход сразу после подтверждения формации")
        f = row(b1, ck("Торговать по этой формации", self.v_spk), "   Таймфреймы:",
                *[ck("1 час" if tf == 60 else f"{tf} мин", self.v_spk_tf[tf]) for tf in (5, 15, 60)])
        markets(b1, self.v_mk)
        fe = row(b1, ck("Только по направлению рынка", self.v_idx_form), ck("Только по тренду EMA200 на:", self.v_e200),
                 *[ck(n, self.v_e2tf[k]) for k, n in self.E2TF])
        fw = row(b1, "Окно входов:")
        cw = ttk.Combobox(fw, textvariable=self.v_win, width=13, state="readonly", values=list(self.WIN.values()))
        cw.pack(side="left", padx=(0, 12))
        ttk.Label(fw, text="выход:").pack(side="left", padx=(0, 4))
        ce = ttk.Combobox(fw, textvariable=self.v_exit, width=7, state="readonly")
        ce.pack(side="left", padx=(0, 12))
        ttk.Label(fw, text="во время выхода:").pack(side="left", padx=(0, 4))
        ttk.Combobox(fw, textvariable=self.v_carry_all, width=62, state="readonly", values=list(self.CARRY.values())).pack(side="left")

        def _exits(*_):
            k = {v: k for k, v in self.WIN.items()}.get(self.v_win.get(), "9-19")
            vals = sorted(set(at.EXIT_CHOICES) | {self.EXIT_EARLY[k]})
            ce.configure(values=vals)
            if self.v_exit.get() not in vals:
                self.v_exit.set(self.EXIT_EARLY[k])
        cw.bind("<<ComboboxSelected>>", _exits)
        _exits()
        self._exits_cb = _exits
        row(b1, "Тейки:", cb(self.v_takes, ["два тейка", "один тейк у хая"], 15), "закрыть на тейке1, %:",
            cb(self.v_spk_half, ["30", "50", "70", "100"], 5), "трейлинг, R:", cb(self.v_spk_trail, ["0.5", "1", "1.5", "2"], 5),
            "макс. позиций:", cb(self.v_spk_max, ["без ограничения", "1", "2", "3", "5", "8", "10", "15", "20"], 15))
        row(b1, ck("Только первая формация после всплеска", self.v_first),
            ("hint", "если после всплеска уже была формация (15 мин или 1 час; 5 мин — если на них торгуете) — повторную не торговать, только уведомление"))
        row(b1, ck("Профиль по эшелонам", self.v_tier), ("hint", "индекс — формация 15 мин и 1 час; 2-й эшелон — 1 час и трендовый откат 15 мин; "
                                                         "3-й эшелон — только уведомления"))
        row(b1, "Дополнительно (на истории хуже):", ck("входить и на 5 мин", self.v_t5), ck("откат к EMA10", self.v_e10),
            ck("все трендовые откаты", self.v_trend_trade), ck("искать трендовые откаты (уведомления)", self.v_trend_on))
        tk.Label(b1, text="На истории (152 акции, 10.2025–10.2026, после затрат): формация 15 мин и 1 час — около +0,24% на сделку; "
                          "5 мин — −0,12%. Стоп — под низом отката с запасом; тейк1 — полпути до хая импульса, тейк2 — хай, дальше трейлинг.",
                 justify="left", anchor="w", font=("Segoe UI", 9), fg=GREY, bg=bg, wraplength=980).pack(anchor="w", pady=(4, 0))

        # --- движение дня
        b2 = box("2. Движение дня — бумаги, которые сильно ушли от открытия в сторону рынка")
        row(b2, ck("Торговать по этой стратегии", self.v_day), "Вход:", cb(self.v_dmode, list(self.DMODE.values()), 34),
            "в", cb(self.v_entry, sc.ENTRY_CHOICES, 6), "порог, %:", cb(self.v_thr, [f"{x:g}" for x in sc.THRESHOLD_CHOICES], 5))
        row(b2, ck("Только по направлению рынка", self.v_idx_day), "Движение бумаги считать:", cb(self.v_dref, list(self.DREF.values()), 34),
            ("hint", "до 10:00 — всегда от 07:00"))
        markets(b2, self.v_dmk)
        row(b2, "Выход в", cb(self.v_dexit, sorted(set(at.EXIT_CHOICES)), 6), "перенос позиций:", cb(self.v_dayc, list(self.DAYC.values()), 10),
            ("hint", "стоп 3% действует всегда"))
        row(b2, "Тейк, %:", cb(self.v_dtp, ["0", "3", "5", "8", "10"], 4), "закрыть на тейке, %:", cb(self.v_dhalf, ["50", "100"], 4),
            "трейлинг, %:", cb(self.v_dtrail, ["0", "1", "2", "3"], 4), "трейлинг с +%:", cb(self.v_dact, ["0", "1", "2", "3"], 4),
            ("hint", "лучше на истории: тейк 8%, закрыть 50%, трейлинг 2%"))
        self.bt_lbl = tk.Label(b2, text="", justify="left", anchor="w", font=("Segoe UI", 9), bg=bg)
        self.bt_lbl.pack(anchor="w")
        self.v_thr.trace_add("write", lambda *_: self._show_bt())
        self.v_entry.trace_add("write", lambda *_: self._show_bt())
        self._show_bt()
        row(b2, ck("Добирать новые сигналы до 16:30 (на истории хуже)", self.v_rescan), ck("сигналы со звуком", self.v_as),
            ck("журнал форвард-теста в 17:20", self.v_aj))

        def _upd_share(*_):
            try:
                pct = float(self.v_spk_share.get().replace(",", "."))
                lk = {v: k for k, v in self.LEV.items()}.get(self.v_lev.get(), "1")
                lv = 1.0 if lk in ("1", "auto") else float(lk)
                if not self.v_spk_max.get().isdigit():
                    self.share_lbl.configure(foreground=GREY, text=(
                        f"одна позиция = {pct * lv:g}% портфеля (формация, ручной вход и «Движение дня» при входе сразу при сигнале); "
                        f"позиции открываются, пока хватает денег" + (f" — без маржи примерно до {int(100 // pct)} одновременно" if lk == "1" else "")))
                    return
                mx = int(self.v_spk_max.get())
                tot = pct * mx * lv
                txt = (f"одна позиция = {pct * lv:g}% портфеля" + (" × плечо брокера по бумаге" if lk == "auto" else "")
                       + f"; при {mx} позициях до {tot:g}%" + (" × плечо" if lk == "auto" else ""))
                if lk == "1" and pct * mx > 100:
                    txt += " — больше 100%: без маржи брокер не даст открыть все позиции"
                self.share_lbl.configure(text=txt, foreground="#b3261e" if (lk == "1" and pct * mx > 100) else GREY)
            except ValueError:
                self.share_lbl.configure(text="введите число")
        self.v_spk_share.trace_add("write", _upd_share)
        self.v_spk_max.trace_add("write", _upd_share)
        self.v_lev.trace_add("write", _upd_share)
        _upd_share()
        r += 1
        ttk.Button(g, text="Сохранить настройки", command=self.save).grid(row=r, column=0, columnspan=2, sticky="w", pady=12)
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
            tv.column(key, width=w, anchor="w" if key in ("ticker", "side", "reason", "date", "period", "from", "name", "tier") else "e")
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
        self.status.configure(text=(f"Движение дня: порог {self.s['threshold']:g}%, вход {self.s['entry']}" if self.s.get("day_on", True)
                                    else "Движение дня выключено") + f" · формация EMA20: {'вкл' if self.s.get('spike_on', True) else 'выкл'} · {busy}"
                                   + (f" · авто: {', '.join(auto)}" if auto else ""))
        self.after(1000, self._tick)

    def _apply_settings(self):
        sc.configure(float(self.s["threshold"]), self.s["entry"])

    def _exit_txt(self, s):
        v = s.get("exit_late", True)
        if isinstance(v, str) and ":" in v:
            return v
        return "23:45" if v else self.EXIT_EARLY.get(s.get("entry_window", "9-19"), "19:25")

    def _exit_val(self):
        """Значение настройки exit_late: True — 23:45, False — ранний выход окна, "18:45" — выход в 18:45 для 09:00–19:00."""
        win = {v: k for k, v in self.WIN.items()}.get(self.v_win.get(), "9-19")
        e = self.v_exit.get()
        if e == "23:45":
            return True
        return False if e == self.EXIT_EARLY.get(win) else e

    def save(self):
        try:
            cap = float(self.v_cap.get().replace(" ", ""))
        except ValueError:
            messagebox.showerror(APP_TITLE, "Капитал должен быть числом, например 1000000.")
            return
        broker = {v: k for k, v in self.BROKERS.items()}.get(self.v_broker.get(), "tinvest")
        if broker != self.s.get("broker", "tinvest"):
            if self.robot_running() and self.robot.positions:
                messagebox.showerror(APP_TITLE, "Сначала закройте открытые позиции — потом меняйте брокера.")
                return
            if broker in at.REAL_BROKERS and not messagebox.askyesno(APP_TITLE, (
                    "Робот будет торговать через Финам. Если указан реальный счёт — сделки пойдут на реальные деньги."
                    if broker == "finam" else "Робот будет торговать на РЕАЛЬНОМ счёте Т-Инвестиций — на реальные деньги.")
                    + " Продолжить?", icon="warning"):
                return
        if self.v_fsec.get().strip():
            self.s["finam_secret"] = self.v_fsec.get().strip()
            self.v_fsec.set("")
            self.fsec_lbl.configure(text="  сохранён — оставьте пустым, чтобы не менять")
        if self.v_rtok.get().strip():
            self.s["real_token"] = self.v_rtok.get().strip()
            self.v_rtok.set("")
            self.rtok_lbl.configure(text="  сохранён — оставьте пустым, чтобы не менять")
        self.s.update(broker=broker, finam_account=self.v_facc.get().strip(), real_account=self.v_racc.get().strip())
        self.s.update(threshold=float(self.v_thr.get()), entry=self.v_entry.get(), capital=cap,
                      token=self.v_tok.get().strip(), sandbox_token=self.v_sbtok.get(), remember_token=self.v_rem.get(),
                      auto_signals=self.v_as.get(), auto_journal=self.v_aj.get(),
                      rescan=self.v_rescan.get(), autostart=self.v_autostart.get(), day_on=self.v_day.get(),
                      day_carry={v: k for k, v in self.DAYC.items()}.get(self.v_dayc.get(), 0), day_exit=self.v_dexit.get(),
                      day_markets=[k for k, _ in at.MARKETS if self.v_dmk[k].get()] or ["imoex"],
                      day_mode={v: k for k, v in self.DMODE.items()}.get(self.v_dmode.get(), "fixed"),
                      idx_form=self.v_idx_form.get(), ema10_touch=self.v_e10.get(), first_form=self.v_first.get(), trade_5m=self.v_t5.get(), ema200=self.v_e200.get(), ema200_tfs=[k for k, _ in self.E2TF if self.v_e2tf[k].get()] or ["form"], idx_day=self.v_idx_day.get(),
                      trade_weekends=self.v_wkend.get(),
                      idx_ref={v: k for k, v in self.IDXREF.items()}.get(self.v_idxref.get(), "imoex"),
                      entry_from={v: k for k, v in self.EFROM.items()}.get(self.v_efrom.get(), ""),
                      day_ref={v: k for k, v in self.DREF.items()}.get(self.v_dref.get(), "main"),
                      idx_flip={v: k for k, v in self.FLIP.items()}.get(self.v_flip.get(), "off"),
                      day_tp=float(self.v_dtp.get()), day_tp_half=float(self.v_dhalf.get()),
                      day_trail=float(self.v_dtrail.get()), day_act=float(self.v_dact.get()), sound_on=self.v_sound.get(),
                      spike_on=self.v_spk.get(), spike_tfs=[tf for tf, v in self.v_spk_tf.items() if v.get()],
                      direction={v: k for k, v in self.DIRS.items()}.get(self.v_dir.get(), "both"),
                      spike_markets=[k for k, _ in at.MARKETS if self.v_mk[k].get()] or ["imoex"], spike_half=int(self.v_spk_half.get()),
                      spike_trail=float(self.v_spk_trail.get()), spike_max=int(self.v_spk_max.get()) if self.v_spk_max.get().isdigit() else 0,
                      spike_share=self._pct(self.v_spk_share.get()),
                      leverage={v: k for k, v in self.LEV.items()}.get(self.v_lev.get(), "1"),
                      two_takes=self.v_takes.get() == "два тейка",
                      tg_token=self.v_tg_tok.get().strip(), tg_chat=self.v_tg_chat.get().strip(),
                      tg_form=self.v_tg_form.get(), tg_trades=self.v_tg_trades.get(), tg_imp=self.v_tg_imp.get(),
                      tg_trend=self.v_tg_trend.get(), trend_on=self.v_trend_on.get(), trend_trade=self.v_trend_trade.get(),
                      tier_profile=self.v_tier.get(), entry_window={v: k for k, v in self.WIN.items()}.get(self.v_win.get(), "9-19"),
                      exit_late=self._exit_val(), carry_all={v: k for k, v in self.CARRY.items()}.get(self.v_carry_all.get(), True))
        if self.s["token"]:
            os.environ["TINVEST_TOKEN"] = self.s["token"]
        else:
            os.environ.pop("TINVEST_TOKEN", None)
        save_settings(self.s)
        self._apply_settings()
        self.refresh_journal()
        self.refresh_sandbox()
        self.log(f"Настройки сохранены: порог {self.s['threshold']:g}%, вход {self.s['entry']}.\n")
        if self.remote_mode() and self.robot_running() and hasattr(self.robot, "push_settings"):
            rr, snap_s = self.robot, dict(self.s)

            def push():
                try:
                    rr.push_settings(snap_s)
                    self.q.put(("log", "Настройки отправлены на сервер — робот там перезапускается с ними.\n"))
                except Exception as e:
                    self.q.put(("log", f"Настройки на сервер не отправились: {e}\n"))
            threading.Thread(target=push, daemon=True).start()
            return
        self.restart_robot()

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
        self.after(25, self._poll)                 # очередь из фоновых потоков — живые цены без задержки

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
    def _server(self):
        """Клиент сервера робота (режим пульта) или None."""
        if not self.remote_mode():
            return None
        if self.robot_running() and hasattr(self.robot, "c"):
            return self.robot.c
        try:
            import remote
            return remote.Client(self.s["srv_code"])
        except Exception:
            return None

    def journal_today(self):
        n = now_msk()
        if self._server():
            self.refresh_journal()
            return
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

        srv = self._server()
        if srv:
            self.submit(f"Дозаполнение журнала на сервере с {d0}",
                        lambda: (srv.post("/api/journal_fill", {"from": d0.isoformat()}),
                                 print("Сервер считает журнал — обновите вкладку через пару минут.")), lambda _: self.refresh_journal())
            return

        def job():
            tick = sc.imoex_tickers()
            d = d0
            while d <= d1 and not sc.STOP_EVENT.is_set():
                sc.journal_day(d, tick)
                d += timedelta(days=1)
            sc.stats()
        self.submit(f"Дозаполнение журнала с {d0}", job, lambda _: self.refresh_journal())

    def download_history(self):
        import history_dl
        tok = os.environ.get("TINVEST_TOKEN")
        self.submit("Выгрузка истории всех акций",
                    lambda: history_dl.download_all(tok, bool(self.s.get("sandbox_token", True))))

    def refresh_journal(self):
        srv = self._server()
        if srv:
            def job():
                try:
                    d = srv.get("/api/journal")
                    self.q.put(("call", self._show_journal_remote, d))
                except Exception as e:
                    self.q.put(("log", f"Журнал с сервера не получен: {e}\n"))
            threading.Thread(target=job, daemon=True).start()
            return
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

    def _show_journal_remote(self, d):
        self.j_tree.delete(*self.j_tree.get_children())
        for r in d.get("rows", []):
            net = float(r["net_pct"])
            self.j_tree.insert("", "end", tags=("pos" if net > 0 else "neg",), values=(
                r["date"], r["ticker"], r["side"], f"{float(r['move_pct']):+.2f}%", r["entry"], f"{float(r['exit']):.4g}",
                "стоп" if r["reason"] == "stop" else "17:00", f"{net:+.2f}%"))
        self.stats_lbl.configure(text=(
            f"Журнал сервера. Дней: {d.get('days', 0)} ({d.get('period', '')}). Сделок: {d.get('trades', 0)}, в плюсе {d.get('wins', 0)}. "
            f"Средняя после затрат: {d.get('avg', 0):+.3f}%. Итог: {d.get('total', 0):+.2f}%, просадка {d.get('dd', 0):.2f}%."
            if d.get("days") else "Журнал сервера пуст — нажмите «Дозаполнить»."))

    # --- песочница
    def check_access(self):
        srv = self._server()
        if srv:
            self.submit("Проверка доступа через сервер", lambda: print("Сервер отвечает:", srv.get("/api/ping")))
            return
        if not os.environ.get("TINVEST_TOKEN"):
            messagebox.showinfo(APP_TITLE, "Сначала введите токен во вкладке «Настройки» и нажмите «Сохранить».")
            return
        self.submit("Проверка доступа", lambda: sc.check_mode(types.SimpleNamespace(sandbox_token=self.s["sandbox_token"])))

    # --- робот
    # --- живые цены
    def _feed_robot(self):
        r = self.robot if self.robot_running() and not self.remote_mode() else None
        return r if r is not None and hasattr(r, "on_price") else None

    def _feed_ti(self):
        r = self._feed_robot()
        ti = getattr(getattr(r, "b", None), "ti", None)
        if ti is not None:
            return ti
        tok = os.environ.get("TINVEST_TOKEN")
        if not tok:
            return None
        key = (tok, bool(self.s.get("sandbox_token")))
        if key not in self._feed_ti_cache:
            self._feed_ti_cache[key] = sc.TInvest(tok, "sandbox" if key[1] else "prod")
        return self._feed_ti_cache[key]

    def live_watch(self, tickers):
        """Окна графика сообщают, за какими бумагами следить."""
        self.feed.watch(tickers, ttl=5)
        r = self.robot if self.robot_running() else None
        if r is not None and hasattr(r, "live_watch"):
            r.live_watch = set(tickers)

    def _live_in(self, prices, idx):
        """Из любого потока: свежие цены → окно (одним обновлением, сколько бы цен ни пришло за это время)."""
        self.live_px.update(prices)
        if idx:
            self.live_idx = idx
        self.live_t = time.time()
        if not self._live_dirty:
            self._live_dirty = True
            self.q.put(("call", self.show_live, None))

    def show_live(self, _=None):
        self._live_dirty = False
        if self.live_idx:
            self._show_index(self.live_idx)
        rows = getattr(self, "_pos_rows", [])
        if rows and not getattr(self, "_menu_open", False):
            money = lambda v: "—" if v is None else (("+" if v > 0 else "") + f"{v:,.0f}").replace(",", " ")
            for iid, p in rows:
                px = self.live_px.get(p["ticker"])
                if not px:
                    continue
                p["last"] = px
                if p.get("mult"):
                    p["pnl_rub"] = round(p["side"] * (px - p["entry"]) * p["mult"], 2)
                res = p["side"] * (px / p["entry"] - 1)
                try:
                    self.pos_tree.set(iid, "last", f"{px:.4g}")
                    self.pos_tree.set(iid, "res", f"{res * 100:+.2f}%")
                    self.pos_tree.set(iid, "rub", money(p.get("pnl_rub")))
                    self.pos_tree.item(iid, tags=("pos" if res > 0 else "neg",))
                except tk.TclError:
                    pass
            PS = rc.positions_summary([p for _, p in rows])
            for key, iid in getattr(self, "_pos_hdr", {}).items():
                d = PS["total"] if key == "total" else PS["by_strat"].get(key, {})
                try:
                    if key != "total":
                        self.pos_tree.set(iid, "res", f"{d['avg_pct']:+.2f}%" if d.get("avg_pct") is not None else "")
                    self.pos_tree.set(iid, "rub", money(d.get("pnl_rub")) if d.get("known") else "—")
                except tk.TclError:
                    pass
        for w in list(self.charts):
            px = self.live_px.get(w.ticker)
            if px:
                w.live_price(px)

    def remote_mode(self):
        """Сервер установлен — программа служит его пультом, своего робота не запускает."""
        return bool(self.s.get("srv_code")) and bool(self.s.get("srv_remote", True))

    def robot_running(self):
        return bool(self.robot_thread and self.robot_thread.is_alive())

    def robot_start(self, trade=True, quiet=False):
        if self.robot_running():
            if trade and not self.robot.trade:
                self.robot.set_trade(True)
            self._robot_buttons()
            return
        if self.remote_mode():
            import remote
            log = lambda m: self.q.put(("log", f"[{now_msk():%H:%M:%S}] {m}\n"))
            try:
                rr = remote.RemoteRobot(self.s["srv_code"], log,
                                        on_update=lambda snap: self.q.put(("call", self.show_robot, snap)),
                                        on_event=lambda kind, info: self.q.put(("call", self.on_trade, (kind, info))),
                                        backup=self.s.get("srv_code2"),
                                        on_backup=lambda c: self.q.put(("call", self._save_backup_code, c)))
            except Exception as e:
                log(f"Код подключения к серверу не подошёл: {e}")
                return
            rr.on_settings = lambda srv: self.q.put(("call", self._settings_from_server, srv))
            rr.on_live = self._live_in
            rr.live_watch = {w.ticker for w in self.charts}
            self.robot = rr
            self.robot_lbl.configure(text=f"Подключаюсь к роботу на сервере {rr.where}…")

            def run_remote():
                try:
                    rr.run()
                finally:
                    self.q.put(("call", lambda _: self._robot_buttons(), None))
            self.robot_thread = threading.Thread(target=run_remote, daemon=True)
            self.robot_thread.start()
            self._robot_buttons()
            return
        tok = os.environ.get("TINVEST_TOKEN")
        if self.s.get("broker") == "tinvest_real":
            tok = self.s.get("real_token")
        if self.s.get("broker") == "tinvest_real" and not tok:
            if not quiet:
                messagebox.showinfo(APP_TITLE, "Выбран реальный счёт Т-Инвестиций — введите токен с полным доступом в «Настройках».")
            return
        elif self.s.get("broker") != "tinvest_real" and (not tok or not self.s["sandbox_token"]):
            if not quiet:
                messagebox.showinfo(APP_TITLE, "Для автоторговли нужен токен песочницы: введите его во вкладке «Настройки», "
                                               "отметьте «Это токен песочницы» и нажмите «Сохранить настройки».")
            return
        self._apply_settings()
        log = lambda m: self.q.put(("log", f"[робот {now_msk():%H:%M:%S}] {m}\n"))
        broker = at.make_broker(self.s, log=log)
        spike = rc.spike_cfg(self.s)
        self.robot = at.AutoTrader(broker, float(self.s["capital"]), log=log, rescan=bool(self.s.get("rescan")), spike=spike,
                                   day_on=bool(self.s.get("day_on", True)), direction=self.s.get("direction", "both"), trade=trade,
                                   on_event=lambda kind, info: self.q.put(("call", self.on_trade, (kind, info))),
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
        """«Стоп» выключает торговлю; мониторинг формаций продолжается."""
        if not self.robot_running():
            return
        if self.robot.positions:
            ans = messagebox.askyesnocancel(APP_TITLE, "Есть открытые позиции. Закрыть их по рынку?\n\n"
                                                       "«Да» — закрыть все и выключить торговлю.\n"
                                                       "«Нет» — выключить торговлю, а открытые позиции робот доведёт до стопа/тейка/выхода.")
            if ans is None:
                return
            if ans:
                self.robot.request_close(None)
        self.robot.set_trade(False)
        self._robot_buttons()

    def restart_robot(self):
        """Применить новые настройки: перезапустить робота с тем же режимом (наблюдение/торговля)."""
        if not self.robot_running():
            self.robot_start(trade=False, quiet=True)
            return
        old, trade = self.robot, self.robot.trade
        old.request_stop(False)
        self.robot_lbl.configure(text="Применяю настройки…")

        def wait():
            if self.robot_thread and self.robot_thread.is_alive():
                self.after(500, wait)
            else:
                self.robot_start(trade=trade, quiet=True)
        self.after(500, wait)

    def _robot_buttons(self):
        run = self.robot_running()
        trading = run and self.robot.trade
        self.btn_start.configure(state="disabled" if trading else "normal")
        self.btn_stop.configure(state="normal" if trading else "disabled")
        if not run:
            self.robot_lbl.configure(text="Нет связи с сервером." if self.remote_mode() else
                                     "Робот выключен (нужен токен песочницы в «Настройках»). Нажмите «Старт».")
            self.refresh_sandbox()

    def show_robot(self, snap):
        if self.robot_running():
            bal = snap.get("balance")
            self.robot_lbl.configure(text=f"Робот: {snap['status']}"
                                     + (f" · баланс {bal:,.0f} ₽".replace(",", " ") if bal else ""))
            self._robot_buttons()
        mk = snap.get("market")
        ls = snap.get("last_scan")
        thr = snap.get("threshold") or sc.THRESHOLD
        early = self.s.get("day_ref", "main") == "market" or (ls and ls.hour < 10)
        ref_t = "07:00" if early else "10:00"
        ir = snap.get("idx_ref") or self.s.get("idx_ref", "imoex")
        mo = snap.get("market_open")
        extra = f", акции индекса с открытия {mo * 100:+.2f}%" if mo is not None else ""
        mk_txt = ((f"рынок по IMOEX (к вчерашнему дню) {mk * 100:+.2f}%" + extra) if ir == "imoex" else
                  (f"рынок по IMOEX (от открытия основной сессии) {mk * 100:+.2f}%" + extra) if ir == "imoex_open" else
                  f"рынок — акции индекса с открытия {ref_t}: {mk * 100:+.2f}%") if mk is not None else ""
        self.scan_lbl.configure(text=(f"Обновлено {ls:%H:%M}: {mk_txt}; движение бумаг — от открытия {ref_t}; порог {thr * 100:g}%"
                                      if ls and mk is not None else "Данные появятся после 07:01."))
        self.scan_tree.delete(*self.scan_tree.get_children())
        for tk, mv, why in snap["scan"][:60]:
            tag = ("pos" if mv > 0 else "neg") if why == "СИГНАЛ" else ()
            self.scan_tree.insert("", "end", tags=tag, values=(tk, f"{mv * 100:+.2f}%",
                                                               (("СИГНАЛ — вход сразу" if self.s.get("day_mode", "fixed") == "signal"
                                                                 else f"кандидат — вход в {self.s.get('entry', '15:00')}") if why == "СИГНАЛ" else why)))
        self.spk_tree.delete(*self.spk_tree.get_children())
        mk = snap.get("market")
        for row in reversed(snap.get("spikes", [])):
            tm, tkr, tfn, val, mult, body, why = row[:7]
            sig_t = row[7] if len(row) > 7 and row[7] else "—"
            d = "ЛОНГ ⬆" if body > 0 else "ШОРТ ⬇"
            if why.startswith("жду") and mk is not None and mk != 0 and (mk > 0) != (body > 0):
                why = why + " (индекс против)"
            self.spk_tree.insert("", "end", tags=(("pos" if body > 0 else "neg"),),
                                 values=(tm, sig_t, tkr, tfn, f"{val / 1e6:.0f}", f"{mult:.1f}", f"{body * 100:+.2f}%", d, why))
        if getattr(self, "_menu_open", False):
            return                                     # открыто меню правой кнопки — таблицу не перестраиваем
        self.pos_tree.delete(*self.pos_tree.get_children())
        self._pos_rows, self._pos_hdr = [], {}
        money = lambda v: "—" if v is None else (("+" if v > 0 else "") + f"{v:,.0f}").replace(",", " ")
        PS = rc.positions_summary(snap["positions"])
        groups = [("всплеск", "Всплеск + EMA20", [p for p in snap["positions"] if p.get("strat") == "spike"]),
                  ("15:00", "Движение дня", [p for p in snap["positions"] if p.get("strat") not in ("spike", "manual")]),
                  ("вручную", "Открытые вручную", [p for p in snap["positions"] if p.get("strat") == "manual"])]
        for key, name, G in groups:
            if not G:
                continue
            d = PS["by_strat"].get(key, {})
            self._pos_hdr[key] = self.pos_tree.insert("", "end", tags=("hdr",), values=(
                f"▸ {name}", f"{len(G)} поз.", "", "", "", "", "", "",
                f"{d['avg_pct']:+.2f}%" if d.get("avg_pct") is not None else "", money(d.get("pnl_rub")) if d.get("known") else "—"))
            for p in G:
                last = p.get("last")
                res = p["side"] * (last / p["entry"] - 1) if last else None
                tfl = {5: "5м", 15: "15м", 60: "1ч"}.get(int(p.get("tf") or 0), "")
                strat = ("всплеск " + (tfl or "15м") + (" (б/у)" if p.get("took") else "")) if p.get("strat") == "spike" else \
                    ("вручную" + (" " + tfl if tfl else "")) if p.get("strat") == "manual" else "15:00"
                strat += {"carry": " · перенести", "close": " · закрыть"}.get(p.get("carry_pick"), "")
                self._pos_rows.append((self.pos_tree.insert("", "end", tags=(("pos" if res > 0 else "neg"),) if res is not None else (), values=(
                    p["ticker"], strat, "лонг" if p["side"] > 0 else "шорт", p["lots"], f"{p['entry']:.4g}", f"{p['stop']:.4g}",
                    (f"{p['take']:.4g}" if p.get("stage", 0) == 0 else (f"т2 {p['take2']:.4g}" if p.get("stage") == 1 and p.get("take2") else "трейл"))
                    if p.get("take") else "—",
                    f"{last:.4g}" if last else "—", f"{res * 100:+.2f}%" if res is not None else "—", money(p.get("pnl_rub")))), p))
        if snap["positions"]:
            T = PS["total"]
            self._pos_hdr["total"] = self.pos_tree.insert("", "end", tags=("hdr",), values=(
                "ИТОГО", f"{T['n']} поз.", "", "", "", "", "", "", "", money(T["pnl_rub"]) if T["known"] else "—"))
        if self.live_px:
            self.show_live()                           # поверх — самые свежие цены из потока
        if len(snap["positions"]) != getattr(self, "_npos", None):
            self._npos = len(snap["positions"])
            self.refresh_sandbox()

    # --- сделки: звук, ручное закрытие, график
    def on_trade(self, ev):
        kind, info = ev
        if info.get("remote"):                              # событие робота на сервере: Telegram шлёт сам сервер
            if kind in ("open", "close", "carry"):
                self.log(f"[сервер] {info.get('text', '')}\n")
            if kind in ("open", "close") and self.s.get("sound_on", True):
                try:
                    import winsound
                    winsound.PlaySound("SystemAsterisk" if kind == "open" else "SystemExclamation",
                                       winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception:
                    self.bell()
            if kind in ("open", "close"):
                self.refresh_sandbox()
            return
        tok, chat = self.s.get("tg_token"), self.s.get("tg_chat")
        tglog = lambda m: self.q.put(("log", m + "\n"))
        if kind == "formation":
            if self.s.get("tg_trend" if info.get("trend") else "tg_form", True):
                tg.send(tok, chat, tg.formation_text(info), tglog)
            return
        if kind == "impulse":
            if self.s.get("tg_imp", False):
                tg.send(tok, chat, tg.impulse_text(info), tglog)
            return
        if self.s.get("tg_trades", True):
            tg.send(tok, chat, tg.trade_text(kind, info), tglog)
        if kind == "carry" or not self.s.get("sound_on", True):
            return
        try:
            import winsound
            winsound.PlaySound("SystemAsterisk" if kind == "open" else "SystemExclamation",
                               winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            self.bell()

    @staticmethod
    def _pct(v):
        try:
            x = float(str(v).replace(",", ".").replace("%", ""))
        except ValueError:
            return 10.0
        return min(max(x, 0.5), 100.0)

    def _paste_to(self, var):
        try:
            var.set(self.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo(APP_TITLE, "Буфер обмена пуст.")

    def tg_find_chat(self):
        tok = self.v_tg_tok.get().strip()
        if not tok:
            messagebox.showinfo(APP_TITLE, "Сначала вставьте токен бота.")
            return
        try:
            cid, name = tg.find_chat_id(tok)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Telegram не ответил: {e}")
            return
        if not cid:
            messagebox.showinfo(APP_TITLE, "Сообщений боту не найдено. Откройте бота в Telegram, отправьте ему любое сообщение и нажмите ещё раз.")
            return
        self.v_tg_chat.set(cid)
        messagebox.showinfo(APP_TITLE, f"Найден чат {name} (id {cid}). Нажмите «Тест», затем «Сохранить настройки».")

    def tg_test(self):
        try:
            tg.send_now(self.v_tg_tok.get().strip(), self.v_tg_chat.get().strip(),
                        "✅ Сканер Мосбиржи: уведомления работают. Сюда будут приходить формации и сделки робота.")
            messagebox.showinfo(APP_TITLE, "Тестовое сообщение отправлено — проверьте Telegram.")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Не отправилось: {e}")

    # --- сервер на VPS
    def _srv_args(self):
        ip, user, pw = self.v_srv_ip.get().strip(), self.v_srv_user.get().strip() or "root", self.v_srv_pass.get()
        if not ip or not pw:
            messagebox.showinfo(APP_TITLE, "Введите IP-адрес сервера и пароль (их присылает провайдер VPS после аренды).")
            return None
        self.s.update(srv_ip=ip, srv_user=user)
        save_settings(self.s)
        return ip, user, pw

    def server_install(self):
        a = self._srv_args()
        if not a:
            return
        if not messagebox.askyesno(APP_TITLE, f"Установить (или обновить) робота на сервер {a[0]}?\n\n"
                                              "Займёт 2–5 минут, ход виден в журнале действий внизу."):
            return
        import serverinstall as si
        transfer = self.v_srv_tr.get()
        settings = dict(self.s)

        def job():
            return si.install(a[0], a[1], a[2], settings, transfer=transfer, log=lambda m: print(m))

        def done(code):
            if code:
                self.s["srv_code"] = code
                if transfer and self.robot_running() and self.robot.trade:
                    self.robot.set_trade(False)            # два робота на одном демо-счёте торговать не должны
                    self._robot_buttons()
                    self.log("Робот на этом компьютере переведён в режим наблюдения — торговать будет сервер.\n")
                self.s["autostart"] = False
                self.s["srv_remote"] = True
                self.v_srv_remote.set(True)
                save_settings(self.s)
                self.show_qr(code)
                self.restart_robot()                       # окно программы — пульт робота на сервере
        self.submit(f"Установка сервера на {a[0]}", job, done)

    SERVER_KEYS = ["threshold", "entry", "capital", "sandbox_token", "rescan", "day_on", "spike_on", "spike_tfs", "spike_markets", "day_markets",
                   "direction", "leverage", "spike_half", "spike_trail", "spike_max", "spike_share", "two_takes", "tg_chat", "tg_form",
                   "tg_trades", "tg_imp", "tg_trend", "trend_on", "trend_trade", "tier_profile", "entry_window", "exit_late",
                   "carry_all", "day_carry", "day_exit", "day_mode", "day_tp", "day_tp_half", "day_trail", "day_act", "idx_form", "ema10_touch", "first_form", "trade_5m", "ema200", "ema200_tfs", "idx_day", "idx_flip", "idx_ref", "entry_from", "day_ref", "trade_weekends", "broker", "finam_account", "real_account", "auto_journal"]

    def _settings_from_server(self, srv):
        """В режиме пульта вкладка «Настройки» показывает настройки робота на сервере (они и действуют)."""
        for k in self.SERVER_KEYS:
            if k in srv:
                self.s[k] = srv[k]
        save_settings(self.s)
        s = self.s
        self.v_thr.set(f"{float(s['threshold']):g}"); self.v_entry.set(s["entry"]); self.v_cap.set(str(int(float(s["capital"]))))
        self.v_sbtok.set(bool(s["sandbox_token"])); self.v_aj.set(bool(s.get("auto_journal", True)))
        self.v_broker.set(self.BROKERS.get(s.get("broker", "tinvest"), self.BROKERS["tinvest"])); self.v_facc.set(s.get("finam_account", "")); self.v_racc.set(s.get("real_account", ""))
        if srv.get("real_set"):
            self.rtok_lbl.configure(text="  сохранён на сервере — оставьте пустым, чтобы не менять")
        if srv.get("finam_set"):
            self.fsec_lbl.configure(text="  сохранён на сервере — оставьте пустым, чтобы не менять")
        self.v_rescan.set(bool(s.get("rescan"))); self.v_day.set(bool(s.get("day_on", True)))
        self.v_dayc.set(self.DAYC.get(int(s.get("day_carry", 0) or 0), self.DAYC[0])); self.v_dexit.set(s.get("day_exit", "18:45"))
        self.v_dmode.set(self.DMODE.get(s.get("day_mode", "fixed"), self.DMODE["fixed"]))
        self.v_e200.set(bool(s.get("ema200", False))); self.v_e10.set(bool(s.get("ema10_touch", False))); self.v_first.set(bool(s.get("first_form", True))); self.v_t5.set(bool(s.get("trade_5m", False)))
        for k, v in self.v_e2tf.items():
            v.set(k in (s.get("ema200_tfs") or ["form"]))
        self.v_idx_form.set(bool(s.get("idx_form", True))); self.v_idx_day.set(bool(s.get("idx_day", True)))
        self.v_wkend.set(bool(s.get("trade_weekends", False)))
        self.v_idxref.set(self.IDXREF.get(s.get("idx_ref", "imoex"), self.IDXREF["imoex"]))
        self.v_efrom.set(self.EFROM.get(str(s.get("entry_from") or ""), self.EFROM[""]))
        self.v_dref.set(self.DREF.get(s.get("day_ref", "main"), self.DREF["main"]))
        self.v_flip.set(self.FLIP.get(s.get("idx_flip", "off"), self.FLIP["off"]))
        self.v_dtp.set(f"{float(s.get('day_tp', 8)):g}"); self.v_dhalf.set(f"{float(s.get('day_tp_half', 50)):g}")
        self.v_dtrail.set(f"{float(s.get('day_trail', 2)):g}"); self.v_dact.set(f"{float(s.get('day_act', 0)):g}")
        self.v_trend_on.set(bool(s.get("trend_on", True))); self.v_trend_trade.set(bool(s.get("trend_trade")))
        self.v_tier.set(bool(s.get("tier_profile", True)))
        self.v_win.set(self.WIN.get(s.get("entry_window", "9-19"), self.WIN["9-19"]))
        self.v_exit.set(self._exit_txt(s))
        if hasattr(self, "_exits_cb"):
            self._exits_cb()
        self.v_carry_all.set(self.CARRY[rc.carry_choice(s)])
        self.v_takes.set("два тейка" if s.get("two_takes", True) else "один тейк у хая")
        self.v_spk.set(bool(s.get("spike_on", True)))
        for tf, v in self.v_spk_tf.items():
            v.set(tf in s.get("spike_tfs", []))
        for k, v in self.v_mk.items():
            v.set(k in s.get("spike_markets", []))
        for k, v in self.v_dmk.items():
            v.set(k in s.get("day_markets", ["imoex"]))
        self.v_spk_half.set(str(s.get("spike_half", 50))); self.v_spk_trail.set(f"{float(s.get('spike_trail', 1.0)):g}")
        self.v_spk_max.set(str(s.get("spike_max", 0) or "без ограничения")); self.v_spk_share.set(f"{float(s.get('spike_share', 10)):g}")
        self.v_dir.set(self.DIRS.get(s.get("direction", "both"), "лонг и шорт"))
        self.v_lev.set(self.LEV.get(str(s.get("leverage", "1")), "без маржи"))
        self.v_tg_chat.set(s.get("tg_chat", "")); self.v_tg_form.set(bool(s.get("tg_form", True)))
        self.v_tg_trades.set(bool(s.get("tg_trades", True))); self.v_tg_imp.set(bool(s.get("tg_imp")))
        self.v_tg_trend.set(bool(s.get("tg_trend", True)))
        sc.configure(float(s["threshold"]), s["entry"])
        self.log("Настройки взяты с сервера — во вкладке «Настройки» показаны те, с которыми работает робот на сервере.\n")

    # --- индекс в реальном времени
    def _index_loop(self):
        import time as _t
        while True:
            try:
                if time.time() - self.live_t > 5 or not self.live_idx:        # живой поток молчит — запасной путь
                    srv = self._server()
                    q = (srv.get("/api/state").get("index") if srv else sc.index_quote())
                    if q:
                        self.q.put(("call", self._show_index, q))
            except Exception:
                pass
            n = now_msk()
            _t.sleep(10 if n.weekday() < 5 and 6 <= n.hour < 24 else 300)

    def _show_index(self, q):
        v = f"{q['value']:,.2f}".replace(",", " ").replace(".", ",")
        ch = f"{q['chg']:+,.2f}".replace(",", " ").replace(".", ",")
        self.idx_lbl.configure(text=f"IMOEX {v} ({ch}; {q['pct']:+.2f}%)".replace(".", ","),
                               fg="#0a7a2f" if q["pct"] > 0 else "#b3261e" if q["pct"] < 0 else "#333")

    # --- рынки
    def _market_tick(self):
        if self.nb.select() == str(self._mk_tab):
            self.refresh_market()
        self.after(30000, self._market_tick)

    def refresh_market(self):
        rr = self.robot if self.robot_running() else None

        def job():
            try:
                if rr is not None and hasattr(rr, "c"):
                    data = rr.c.get("/api/market")            # с сервера
                else:
                    ti = getattr(getattr(rr, "b", None), "ti", None)
                    live = (lambda tks: ti.last_prices([t for t in tks if t in ti._shares])) if ti is not None else None
                    data = rc.market_overview(tier_map=dict(getattr(rr, "tier", {}) or {}) if rr else None, live_prices=live)
                    data = rc.attach_formations(data, list(getattr(rr, "spikes", []) or []))
                self.q.put(("call", self._show_market, data))
            except Exception as e:
                self.q.put(("log", f"Рынки: данные не получены: {e}\n"))
        threading.Thread(target=job, daemon=True).start()

    def _show_market(self, data):
        self._mk = data
        self._render_market()

    def _mk_sort_by(self, col):
        key, rev = self.mk_sort
        self.mk_sort = (col, not rev if key == col else col in ("pct", "value", "price", "form"))
        self._render_market()

    def _render_market(self):
        d = self._mk
        if not d:
            self.mk_sum.configure(text="Загружаю…")
            return
        typ = {n: k for k, n in rc.MARKET_TYPES}.get(self.mk_type.get(), "share")
        R = [r for r in d["rows"] if r["type"] == typ and r.get("price") is not None]
        tier = self.mk_tier.get()
        if typ == "share" and tier != "Все":
            R = [r for r in R if (r.get("blue") if tier == "Голубые фишки" else
                                  r.get("tier") == {"Индекс МосБиржи": "index", "2-й эшелон": "2", "3-й эшелон": "3"}[tier])]
        if self.mk_form.get():
            R = [r for r in R if r.get("forms")]
        q = self.mk_q.get().strip().upper()
        if q:
            R = [r for r in R if q in r["ticker"].upper() or q in str(r["name"]).upper()]
        col, rev = self.mk_sort
        keyf = {"pct": lambda r: r.get("pct") if r.get("pct") is not None else -1e9, "value": lambda r: r.get("value") or 0,
                "price": lambda r: r.get("price") or 0, "time": lambda r: r.get("time") or "",
                "tier": lambda r: str(r.get("tier") or ""), "ticker": lambda r: r["ticker"], "name": lambda r: str(r["name"]),
                "form": lambda r: len(r.get("forms") or [])}[col]
        R.sort(key=keyf, reverse=rev)
        self.mk_tree.delete(*self.mk_tree.get_children())
        tn = {"index": "индекс", "2": "2-й", "3": "3-й"}
        for r in R[:600]:
            p = r.get("pct")
            self.mk_tree.insert("", "end", tags=(("pos" if p > 0 else "neg"),) if p else (), values=(
                r["ticker"], r["name"], f"{r['price']:.6g}" + (" ●" if r.get("live") else ""),
                f"{p:+.2f}%" if p is not None else "—", f"{(r.get('value') or 0) / 1e6:,.0f}".replace(",", " "),
                r.get("time") or "—", ("голубая · " if r.get("blue") else "") + tn.get(r.get("tier"), "") if typ == "share" else "",
                "; ".join(f"{f['tf']} {'▲' if f['side'] > 0 else '▼'} {f['status']}" for f in r.get("forms") or [])))
        s = d.get("summary", {})
        self.mk_sum.configure(text=(f"Акции: растут {s.get('up', 0)}, падают {s.get('down', 0)}, оборот {s.get('value', 0) / 1e9:.1f} млрд ₽ · "
                                    + ("цены акций в реальном времени (● — Т-Инвестиции)" if s.get("live") else "данные Мосбиржи (могут быть с задержкой)")
                                    + f" · обновлено {d.get('time', '')[11:]} · показано {len(R)} · с формацией EMA20: {d.get('forms_n', 0)} · щелчок по заголовку — сортировка, двойной по строке — график"
                                    + (f" · ошибки: {'; '.join(d['errors'])}" if d.get("errors") else "")))

    # --- результаты
    def _results_tick(self):
        self.refresh_results()
        self.after(60000, self._results_tick)

    def reset_trading(self):
        cap = f"{float(self.s.get('capital', 0)):,.0f}".replace(",", " ")
        if not messagebox.askyesno(APP_TITLE, f"Сбросить торговлю и начать сначала?\n\nВсе открытые позиции закроются, демо-счёт "
                                              f"откроется заново на {cap} ₽ (капитал из настроек), история сделок и баланса очистится.\n"
                                              "Журнал 15:00 и найденные сегодня формации останутся.", icon="warning"):
            return
        if not messagebox.askyesno(APP_TITLE, "Точно? Это нельзя отменить.", icon="warning"):
            return
        srv = self._server()
        if srv:
            def job():
                r = srv.post("/api/reset", {"confirm": "СБРОС"})
                print(r.get("msg", "Сброс начат на сервере."))
            self.submit("Сброс торговли на сервере", job, lambda _: self.after(20000, self.refresh_results))
            return
        tok = self.s.get("token")
        if not tok or not self.s.get("sandbox_token"):
            messagebox.showerror(APP_TITLE, "Нужен токен песочницы Т-Инвестиций (вкладка «Настройки»).")
            return
        was = self.robot_running()
        trade = bool(getattr(self.robot, "trade", False)) if was else False

        def job():
            if was:
                print("Останавливаю робота…")
                self.robot.request_stop(False)
                if self.robot_thread:
                    self.robot_thread.join(120)
            rc.reset_trading(tok, float(self.s["capital"]), log=print)

        def done(_):
            if was:
                self.robot_start(trade=trade, quiet=True)
            self.refresh_results()
        self.submit("Сброс торговли", job, done)

    def _cal_tick(self):
        srv = self._server()
        tok = self.s.get("token")

        def job():
            try:
                if srv:
                    c = srv.get("/api/calendar")
                else:
                    if getattr(self, "_cal", None) is None:
                        self._cal = sc.TradingCalendar(sc.TInvest(tok, "sandbox" if self.s.get("sandbox_token", True) else "prod") if tok else None)
                    c = self._cal.listing(14)
                    c["trade_weekends"] = bool(self.s.get("trade_weekends"))
                self.q.put(("call", self._show_cal, c))
            except Exception as e:
                self.q.put(("log", f"Календарь не получен: {e}\n"))
        threading.Thread(target=job, daemon=True).start()
        self.after(30 * 60000, self._cal_tick)

    def _show_cal(self, c):
        self.cal_tree.delete(*self.cal_tree.get_children())
        for d in c.get("days", []):
            on = d["kind"] == "trading" or (d["kind"] == "weekend_session" and c.get("trade_weekends"))
            rb = "работает" if on else ("не торгует (выходные выключены)" if d["kind"] == "weekend_session" else "отдыхает")
            self.cal_tree.insert("", "end", tags=(() if on else ("off",)), values=(
                ".".join(reversed(d["date"].split("-")))[:5], d["dow"], d["name"], f"{d['start']}–{d['end']}" if d["start"] else "", rb))
        self.cal_lbl.configure(text=f"Источник: {c.get('source', '')}. Торговля в выходные: {'включена' if c.get('trade_weekends') else 'выключена'} (Настройки).")

    def refresh_results(self):
        rr = self.robot if self.robot_running() else None

        def job():
            try:
                if rr is not None and hasattr(rr, "results"):
                    data = rr.results()                       # с сервера
                else:
                    data = rc.results(getattr(rr, "balance", None))
                self.q.put(("call", self._show_results, data))
            except Exception as e:
                self.q.put(("log", f"Результаты не получены: {e}\n"))
        threading.Thread(target=job, daemon=True).start()

    def _show_results(self, data):
        self._res = data
        fmt = lambda v, sign=False: "—" if v is None else ((("+" if sign and v > 0 else "") + f"{v:,.0f}").replace(",", " "))
        bal = data.get("balance")
        self.res_bal.configure(text=f"Баланс: {fmt(bal)} ₽ (на {data.get('time', '')} МСК)" if bal else "Баланс: появится после запуска робота")
        self.res_tree.delete(*self.res_tree.get_children())
        for p in data.get("periods", []):
            ch = p.get("change")
            bs = p.get("by_strat", {})
            self.res_tree.insert("", "end", tags=(("pos" if ch > 0 else "neg"),) if ch else (), values=(
                p["name"], ".".join(reversed((p.get("from") or "").split("-"))), fmt(p.get("start_balance")),
                fmt(ch, True), f"{p['change_pct']:+.2f}%" if p.get("change_pct") is not None else "—",
                p.get("trades", 0), f"{p['wins'] / p['trades'] * 100:.0f}%" if p.get("trades") else "—",
                fmt(p.get("pnl_rub"), True), f"{p['avg_pct']:+.2f}%" if p.get("avg_pct") is not None else "—"))
            for k, n in (("всплеск", "Всплеск + EMA20"), ("15:00", "Движение дня"), ("вручную", "Вручную")):
                x = bs.get(k) or {"n": 0}
                self.res_tree.insert("", "end", tags=("sub",), values=(
                    f"   ↳ {n}", "", "", "", "", x.get("n", 0),
                    f"{x['wins'] / x['n'] * 100:.0f}%" if x.get("n") else "—", fmt(x.get("pnl_rub"), True),
                    f"{x['avg_pct']:+.2f}%" if x.get("avg_pct") is not None else "—"))
        self._draw_balance()

    def _draw_balance(self):
        cv = self.res_cv
        cv.delete("all")
        ser = (self._res or {}).get("series", [])
        W, H = cv.winfo_width(), cv.winfo_height()
        if len(ser) < 2 or W < 50:
            cv.create_text(W / 2, H / 2, text="График появится, когда накопится баланс хотя бы за 2 дня", fill="#8b93a7")
            return
        vals = [x["value"] for x in ser]
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.1 or hi * 0.01
        lo, hi = lo - pad, hi + pad
        L, R, T, B = 10, 90, 10, 24
        x = lambda i: L + (W - L - R) * i / (len(vals) - 1)
        y = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            cv.create_line(L, y(v), W - R, y(v), fill="#1f2433")
            cv.create_text(W - R + 6, y(v), text=f"{v:,.0f}".replace(",", " "), fill="#b2b5be", anchor="w", font=("Consolas", 9))
        step = max(1, len(vals) // 8)
        for i in range(0, len(vals), step):
            cv.create_text(x(i), H - 10, text=".".join(reversed(ser[i]["date"].split("-")[1:])), fill="#b2b5be", font=("Consolas", 8))
        col = "#26a69a" if vals[-1] >= vals[0] else "#ef5350"
        pts = [c for i, v in enumerate(vals) for c in (x(i), y(v))]
        cv.create_polygon(*pts, x(len(vals) - 1), H - B, x(0), H - B, fill="#16303a" if col == "#26a69a" else "#3a1c22", outline="")
        cv.create_line(*pts, fill=col, width=2)

    def server_update_quick(self, which="both"):
        """Обновить программу на серверах по кодам подключения (без пароля от VPS): основной и резервный — одновременно."""
        codes = []
        if which in ("both", "main") and self.s.get("srv_code"):
            codes.append(("основной", self.s.get("srv_ip", ""), self.s["srv_code"]))
        if which in ("both", "backup") and self.s.get("srv_code2"):
            codes.append(("резервный", self.s.get("srv2_ip", ""), self.s["srv_code2"]))
        if not codes:
            messagebox.showinfo(APP_TITLE, "Нет кода подключения к " + ("резервному серверу — установите его кнопкой ниже."
                                                                       if which == "backup" else "серверу — установите его с паролем."))
            return
        names = " и ".join(f"{n} ({ip})" for n, ip, _ in codes)
        if not messagebox.askyesno(APP_TITLE, f"Обновить программу на сервере: {names} — до версии {VERSION}?\n\n"
                                              "Робот перезапустится на ~20 секунд; позиции, настройки и журналы сохранятся."):
            return
        import serverinstall as si
        import remote

        def job():
            data = si.bundle_bytes()
            res = {}

            def one(n, ip, code):
                try:
                    res[n] = "обновлён до версии " + remote.update_code(code, data)
                except Exception as e:
                    res[n] = f"НЕ обновлён: {e}"
            th = [threading.Thread(target=one, args=x, daemon=True) for x in codes]
            for t in th:
                t.start()
            for t in th:
                t.join(150)
            for n, ip, _ in codes:
                print(f"Сервер {n} ({ip}): {res.get(n, 'нет ответа')}.")
        self.submit("Обновление серверов" if len(codes) > 1 else "Обновление сервера", job)

    def _save_backup_code(self, code):
        if self.s.get("srv_code2") != code:
            self.s["srv_code2"] = code
            save_settings(self.s)

    def server_install_backup(self):
        """Второй (резервный) сервер: установить и связать с основным в пару."""
        if not self.s.get("srv_code"):
            messagebox.showinfo(APP_TITLE, "Сначала установите основной сервер.")
            return
        ip, user, pw = self.v_srv2_ip.get().strip(), self.v_srv2_user.get().strip() or "root", self.v_srv2_pass.get()
        if not ip or not pw:
            messagebox.showinfo(APP_TITLE, "Введите IP-адрес и пароль второго сервера.")
            return
        if ip == self.s.get("srv_ip"):
            messagebox.showinfo(APP_TITLE, "Это адрес основного сервера — нужен другой VPS.")
            return
        self.s.update(srv2_ip=ip, srv2_user=user)
        save_settings(self.s)
        if not messagebox.askyesno(APP_TITLE, f"Установить резервный сервер на {ip} и связать его в пару с основным "
                                              f"({self.s.get('srv_ip', '')})?\n\nЗаймёт 3–6 минут. Основной сервер при этом "
                                              "обновится и перезапустится (на ~1 минуту), позиции сохранятся."):
            return
        import serverinstall as si
        import remote
        code_a = self.s["srv_code"]
        settings = dict(self.s)

        def wait_up(c, label):
            import time as _t
            for _ in range(40):
                _t.sleep(5)
                try:
                    return c.get("/api/ping")
                except Exception:
                    pass
            raise RuntimeError(f"{label} не поднялся после обновления")

        def job():
            code_b = si.install(ip, user, pw, settings, transfer=True, log=lambda m: print(m))
            a, b = remote.Client(code_a), remote.Client(code_b)
            bundle = si.bundle_bytes()
            for i in (1, 2):                    # дважды: старый сервер при первом обновлении не копирует модуль пары
                print(f"Обновляю основной сервер ({i}/2)…")
                a.post("/api/update", {"zip": __import__("base64").b64encode(bundle).decode(), "from_peer": True})
                wait_up(a, "Основной сервер")
            print("Связываю серверы в пару…")
            a.post("/api/pair_set", {"peer": code_b, "role": "primary"})
            b.post("/api/pair_set", {"peer": code_a, "role": "standby"})
            wait_up(a, "Основной сервер")
            wait_up(b, "Резервный сервер")
            print(f"Готово: основной {a.host} торгует, резервный {b.host} следит за ним, держит копию позиций и настроек "
                  f"и возьмёт работу на себя, если основной не отвечает 5 минут. Запросы, которые не проходят напрямую, "
                  f"идут через второй сервер.")
            return code_b

        def done(code_b):
            if code_b:
                self._save_backup_code(code_b)
                self.restart_robot()
        self.submit(f"Резервный сервер на {ip}", job, done)

    def _srv_remote_toggle(self):
        on = self.v_srv_remote.get()
        if on and not self.s.get("srv_code"):
            messagebox.showinfo(APP_TITLE, "Сначала установите сервер (или нажмите «Показать QR-код», чтобы программа узнала код подключения).")
            self.v_srv_remote.set(False)
            return
        if not on and not messagebox.askyesno(APP_TITLE, "Запустить робота на этом компьютере?\n\nЕсли робот на сервере тоже торгует, "
                                                         "два робота будут торговать одним демо-счётом. Выключите торговлю на сервере («Стоп»)."):
            self.v_srv_remote.set(True)
            return
        self.s["srv_remote"] = on
        save_settings(self.s)
        self.restart_robot()

    def server_qr(self):
        if self.s.get("srv_code") and self.s.get("srv_ip") == self.v_srv_ip.get().strip():
            self.show_qr(self.s["srv_code"])
            return
        a = self._srv_args()
        if not a:
            return
        import serverinstall as si
        def got(c):
            if not c:
                messagebox.showinfo(APP_TITLE, "На сервере робот не найден — сначала установите его.")
                return
            self.s["srv_code"] = c
            save_settings(self.s)
            self.show_qr(c)
            if self.remote_mode():
                self.restart_robot()
        self.submit("Код подключения с сервера", lambda: si.connect_code(*a), got)

    def show_qr(self, code):
        import serverinstall as si
        w = tk.Toplevel(self)
        w.title("Код подключения для телефона")
        w.configure(bg="white")
        tk.Label(w, text="Откройте на телефоне «Робот Мосбиржи» → «Сканировать QR-код»", bg="white",
                 font=("Segoe UI", 11, "bold")).pack(padx=16, pady=(14, 6))
        m = si.qr_matrix(code)
        cell = max(4, 330 // len(m))
        cv = tk.Canvas(w, width=len(m) * cell, height=len(m) * cell, bg="white", highlightthickness=0)
        cv.pack(padx=16, pady=6)
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    cv.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell, fill="black", outline="black")
        tk.Label(w, text="или вставьте код вручную:", bg="white").pack()
        e = ttk.Entry(w, width=70)
        e.insert(0, code)
        e.configure(state="readonly")
        e.pack(padx=16, pady=4)
        ttk.Button(w, text="Скопировать код", command=lambda: (self.clipboard_clear(), self.clipboard_append(code))).pack(pady=(4, 14))
        tk.Label(w, text="Код — это ключ к управлению роботом. Никому его не пересылайте.", bg="white", fg="#b3261e").pack(pady=(0, 12))

    def _sel_ticker(self, tv, col=0):
        sel = tv.selection()
        if not sel or "hdr" in tv.item(sel[0], "tags"):
            return None
        return tv.item(sel[0], "values")[col]

    def close_selected(self):
        tkr = self._sel_ticker(self.pos_tree)
        if not tkr:
            messagebox.showinfo(APP_TITLE, "Выберите позицию в таблице «Открытые позиции».")
            return
        if not self.robot_running():
            messagebox.showinfo(APP_TITLE, "Робот не запущен. Нажмите «Старт» — он подхватит позиции, и их можно будет закрыть.")
            return
        if messagebox.askyesno(APP_TITLE, f"Закрыть позицию {tkr} по рынку?"):
            self.robot.request_close(tkr)
            self.log(f"Запрошено закрытие {tkr}.\n")

    def open_manual(self):
        """Окно «Открыть позицию вручную»: тикер, лонг/шорт, доля портфеля, стоп/тейк/трейлинг, время выхода и перенос."""
        if not self.robot_running():
            messagebox.showinfo(APP_TITLE, "Робот не запущен — нажмите «Старт».")
            return
        w = tk.Toplevel(self)
        w.title("Открыть позицию вручную")
        w.transient(self)
        f = ttk.Frame(w, padding=12); f.pack(fill="both")
        v = {k: tk.StringVar(value=d) for k, d in (("ticker", ""), ("side", "лонг"), ("share", f"{float(self.s.get('spike_share', 10)):g}"),
                                                    ("stop", "3"), ("take", "0"), ("trail", "0"), ("exit", self.s.get("day_exit", "18:45")),
                                                    ("carry", "перенести"), ("form", "по формации (стоп за лоем отката, тейк на хай)"))}
        rows = [("Тикер", ttk.Entry(f, textvariable=v["ticker"], width=12)),
                ("Стоп и тейк", ttk.Combobox(f, textvariable=v["form"], values=["по формации (стоп за лоем отката, тейк на хай)",
                                                                                 "в % (поля ниже)"], state="readonly", width=44)),
                ("Направление", ttk.Combobox(f, textvariable=v["side"], values=["лонг", "шорт"], state="readonly", width=10)),
                ("% портфеля", ttk.Entry(f, textvariable=v["share"], width=8)),
                ("Стоп, % от входа", ttk.Entry(f, textvariable=v["stop"], width=8)),
                ("Тейк, % (0 — нет)", ttk.Entry(f, textvariable=v["take"], width=8)),
                ("Трейлинг, % (0 — нет)", ttk.Entry(f, textvariable=v["trail"], width=8)),
                ("Время выхода", ttk.Combobox(f, textvariable=v["exit"], values=sorted(set(at.EXIT_CHOICES)), state="readonly", width=8)),
                ("Во время выхода", ttk.Combobox(f, textvariable=v["carry"], values=["перенести", "закрыть"], state="readonly", width=10))]
        for i, (lbl, wd) in enumerate(rows):
            ttk.Label(f, text=lbl + ":").grid(row=i, column=0, sticky="w", pady=3)
            wd.grid(row=i, column=1, sticky="w", pady=3)
        rows[0][1].focus_set()

        def num(k):
            try:
                return float(v[k].get().replace(",", ".") or 0) / 100
            except ValueError:
                return 0.0

        def go():
            t = v["ticker"].get().strip().upper()
            if not t:
                messagebox.showinfo(APP_TITLE, "Укажите тикер.", parent=w)
                return
            side = 1 if v["side"].get() == "лонг" else -1
            if not messagebox.askyesno(APP_TITLE, f"Открыть {'ЛОНГ' if side > 0 else 'ШОРТ'} {t} по рынку на {v['share'].get()}% портфеля, "
                                                  f"стоп {v['stop'].get()}%?", parent=w):
                return
            self.robot.request_open(t, side, num("share") or None, num("stop") or None, num("take"), num("trail"),
                                    v["exit"].get(), v["carry"].get() == "перенести", v["form"].get().startswith("по формации"))
            self.log(f"Запрошено открытие вручную: {t} {'лонг' if side > 0 else 'шорт'} — результат в журнале.\n")
            w.destroy()
        b = ttk.Frame(f); b.grid(row=len(rows), column=0, columnspan=2, pady=(10, 0), sticky="w")
        ttk.Button(b, text="Открыть по рынку", command=go).pack(side="left")
        ttk.Button(b, text="Отмена", command=w.destroy).pack(side="left", padx=6)
        ttk.Label(f, foreground="#555", wraplength=330, text="Позицию ведёт робот: стоп, тейк, трейлинг и время выхода. "
                  "«Перенести/закрыть» можно поменять потом кнопками под таблицей.").grid(row=len(rows) + 1, column=0, columnspan=2, pady=(8, 0), sticky="w")

    def carry_selected(self, mode):
        tkr = self._sel_ticker(self.pos_tree)
        if not tkr:
            messagebox.showinfo(APP_TITLE, "Выберите позицию в таблице «Открытые позиции».")
            return
        if not self.robot_running():
            messagebox.showinfo(APP_TITLE, "Робот не запущен.")
            return
        self.robot.request_carry(tkr, mode)
        self.log(f"{tkr}: во время выхода — " + {"carry": "перенести на следующий день", "close": "закрыть"}.get(mode, "по настройкам стратегии") + ".\n")

    def close_profit(self):
        if not self.robot_running() or not self.robot.positions:
            return
        if messagebox.askyesno(APP_TITLE, "Закрыть по рынку все позиции, которые сейчас в плюсе?"):
            self.robot.request_close("+")

    def close_all(self):
        if not self.robot_running() or not self.robot.positions:
            messagebox.showinfo(APP_TITLE, "Открытых позиций нет или робот не запущен.")
            return
        if messagebox.askyesno(APP_TITLE, "Закрыть ВСЕ открытые позиции по рынку?"):
            self.robot.request_close(None)
            self.log("Запрошено закрытие всех позиций.\n")

    def chart_selected(self):
        if self._sel_ticker(self.pos_tree):
            self._chart_from(self.pos_tree, 0)            # ТФ, на котором открыта позиция
        else:
            self.open_chart("SBER")

    def _chart_from(self, tv, col):
        tkr = self._sel_ticker(tv, col)
        if tkr:
            tf = 5
            if tv is self.spk_tree:
                tfn = tv.item(tv.selection()[0], "values")[3]
                tf = {"5м": 5, "15м": 15, "1ч": 60}.get(tfn, 5)
            elif tv is self.pos_tree and self.robot_running():
                p = next((x for x in self.robot.positions if x["ticker"] == tkr), None)
                tf = int(p.get("tf") or (15 if p.get("strat") == "spike" else 5)) if p else 5
            self.open_chart(tkr, tf)

    def _row_menu(self, e, tv, col):
        row = tv.identify_row(e.y)
        if not row or "hdr" in tv.item(row, "tags"):
            return
        tv.selection_set(row)
        tkr = tv.item(row, "values")[col]
        if getattr(self, "_menu", None) is not None:
            try:
                self._menu.destroy()
            except Exception:
                pass
        m = self._menu = tk.Menu(self, tearoff=0)        # держим ссылку — иначе меню пропадает сразу после щелчка
        if tv is self.pos_tree and self.robot_running():
            m.add_command(label=f"Закрыть позицию {tkr} по рынку", command=lambda: self._close_ticker(tkr))
            sub = tk.Menu(m, tearoff=0)
            for txt, mode in (("перенести на следующий день", "carry"), ("закрыть", "close"), ("по настройкам (авто)", "")):
                sub.add_command(label=txt, command=lambda md=mode, t=txt: (self.robot.request_carry(tkr, md),
                                                                             self.log(f"{tkr}: во время выхода — {t}.\n")))
            m.add_cascade(label="Во время выхода…", menu=sub)
            m.add_command(label="Изменить стоп / тейк…", command=lambda: self._edit_levels(tkr))
            m.add_separator()
        m.add_command(label=f"График {tkr}", command=lambda: self._chart_from(tv, col))
        m.add_command(label=f"Открыть {tkr} в Т-Инвестициях", command=lambda: self.open_tbank(tkr))
        m.add_command(label="Терминал Т-Инвестиций", command=lambda: self.open_tbank(None))
        self._menu_open = True
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
            self.after(300, lambda: setattr(self, "_menu_open", False))

    def _edit_levels(self, tkr):
        p = next((x for x in self.robot.positions if x["ticker"] == tkr), None)
        if not p:
            return
        w = tk.Toplevel(self); w.title(f"{tkr}: стоп и тейк"); w.transient(self)
        f = ttk.Frame(w, padding=12); f.pack()
        vs = tk.StringVar(value=f"{p.get('stop', 0):.6g}" if p.get("stop") else "")
        vt = tk.StringVar(value=f"{p.get('take', 0):.6g}" if p.get("take") else "")
        ttk.Label(f, text=f"{tkr} {'лонг' if p['side'] > 0 else 'шорт'}, вход {p['entry']:.6g}").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Стоп:").grid(row=1, column=0, sticky="w", pady=4)
        es = ttk.Entry(f, textvariable=vs, width=12); es.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="Тейк:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=vt, width=12).grid(row=2, column=1, sticky="w")
        es.focus_set()

        def num(v):
            try:
                return float(v.get().replace(",", ".")) if v.get().strip() else None
            except ValueError:
                return None

        def go(_=None):
            self.robot.request_levels(tkr, num(vs), num(vt))
            self.log(f"{tkr}: стоп {vs.get() or '—'}, тейк {vt.get() or '—'} — отправлено.\n")
            w.destroy()
        b = ttk.Frame(f); b.grid(row=3, column=0, columnspan=2, pady=(8, 0), sticky="w")
        ttk.Button(b, text="Сохранить", command=go).pack(side="left")
        ttk.Button(b, text="Отмена", command=w.destroy).pack(side="left", padx=6)
        ttk.Label(f, foreground="#555", text="Уровни можно и перетащить мышью на графике.").grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        w.bind("<Return>", go)

    def _close_ticker(self, tkr):
        self.robot.request_close(tkr)                  # из меню правой кнопки — сразу, без подтверждения
        self.log(f"Запрошено закрытие {tkr}.\n")

    def open_tbank(self, ticker):
        """Страница бумаги в Т-Инвестициях или веб-терминал — в браузере по умолчанию."""
        import webbrowser
        if not ticker:
            webbrowser.open(sc.TBANK_TERMINAL)
            return
        kind = "share"
        try:
            if self.robot:
                kind = self.robot._terms_cache.get(ticker, ("share",))[0]
        except Exception:
            pass
        webbrowser.open(sc.tbank_url(ticker, kind))

    def open_chart(self, ticker, tf=5):
        if not os.environ.get("TINVEST_TOKEN") and not self.remote_mode():
            messagebox.showinfo(APP_TITLE, "Для графика нужен токен Т-Инвестиций (вкладка «Настройки»).")
            return
        chart.ChartWindow(self, ticker, tf)

    def on_close(self):
        if self.robot_running() and not self.remote_mode() and (self.robot.trade or self.robot.positions):
            if not messagebox.askyesno(APP_TITLE, "Робот работает. Если закрыть программу, он перестанет торговать "
                                                  "(открытые позиции останутся и будут подхвачены при следующем «Старт»).\n\nЗакрыть?"):
                return
        self.destroy()

    def refresh_sandbox(self):
        if self.remote_mode() and self.robot_running() and hasattr(self.robot, "trades"):
            rr = self.robot

            def job():                                       # закрытые сделки — с сервера
                try:
                    rows = rr.trades().get("rows", [])
                    self.q.put(("call", self._fill_sandbox, rows))
                except Exception:
                    pass
            threading.Thread(target=job, daemon=True).start()
            return
        rows = [dict(r, strat="15:00") for r in read_csv(sc.SANDBOX_LOG)[-500:]] + \
               [dict(r, strat="всплеск") for r in read_csv(sc.SPIKE_LOG)[-500:]] + \
               [dict(r, strat="вручную") for r in read_csv(sc.MANUAL_LOG)[-500:]]
        rows.sort(key=lambda r: r["date"])
        self._fill_sandbox(list(reversed(rows)))

    def _fill_sandbox(self, rows):
        self.sb_tree.delete(*self.sb_tree.get_children())
        money = lambda v: "—" if v is None else (("+" if v > 0 else "") + f"{v:,.0f} ₽").replace(",", " ")

        def line(name, x):
            if not x["n"]:
                return f"{name}: сделок нет"
            return (f"{name}: сделок {x['n']}, в плюсе {x['wins'] / x['n'] * 100:.0f}%, средняя {x['avg_pct']:+.2f}%, "
                    f"итог {money(x['pnl_rub'])}")
        self.sb_sum.configure(text="\n".join([
            line("Всплеск + EMA20", rc.strat_summary([r for r in rows if r.get("strat") == "всплеск"])),
            line("Движение дня", rc.strat_summary([r for r in rows if r.get("strat") == "15:00"])),
            line("Вручную", rc.strat_summary([r for r in rows if r.get("strat") == "вручную"])),
            line("ВСЕГО", rc.strat_summary(rows))]))
        for r in rows:
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
                        if self.s["auto_signals"] and self.s.get("day_on", True) and not self.robot_running():
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
