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
        # мониторинг формаций — сразу при открытии программы; торговля — по «Старт» (или сразу, если так в настройках)
        self.after(1500, lambda: self.robot_start(trade=bool(self.s.get("autostart")), quiet=True))
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
        lnb.add(lf, text="Индекс и акции (от открытия 10:00)")
        sf = ttk.Frame(lnb, padding=4)
        lnb.add(sf, text="Всплески объёма")
        self.spk_tree = self._tree(sf, [("time", "Импульс", 55), ("sig", "Сигнал", 55), ("ticker", "Бумага", 60), ("tf", "ТФ", 35), ("val", "Оборот, млн", 75),
                                        ("mult", "×", 40), ("move", "Импульс", 65), ("dir", "Ожидание", 75),
                                        ("reason", "Статус", 125)])
        self.scan_lbl = ttk.Label(lf, text="—")
        self.scan_lbl.pack(anchor="w")
        self.scan_tree = self._tree(lf, [("ticker", "Бумага", 70), ("move", "Движение", 80), ("reason", "Статус", 110)])
        rf = ttk.LabelFrame(mid, text="Открытые позиции", padding=4)
        mid.add(rf, weight=1)
        pb = ttk.Frame(rf)
        pb.pack(fill="x", pady=(0, 4))
        ttk.Button(pb, text="Закрыть выбранную", command=self.close_selected).pack(side="left")
        ttk.Button(pb, text="Закрыть все", command=self.close_all).pack(side="left", padx=6)
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

        # --- Результаты: баланс и итоги за периоды
        t7 = ttk.Frame(nb, padding=12)
        nb.add(t7, text="Результаты")
        top7 = ttk.Frame(t7); top7.pack(fill="x")
        self.res_bal = ttk.Label(top7, text="Баланс: —", style="Big.TLabel")
        self.res_bal.pack(side="left")
        ttk.Button(top7, text="Обновить", command=self.refresh_results).pack(side="right")
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
        nb.bind("<<NotebookTabChanged>>", lambda _: self.refresh_results() if nb.select() == str(t7) else None)
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
        ttk.Button(f6, text="Установить / обновить сервер", style="Start.TButton", command=self.server_install).pack(side="left")
        ttk.Button(f6, text="Показать QR-код для телефона", command=self.server_qr).pack(side="left", padx=8)
        ttk.Button(f6, text="Обновить сервер без пароля", command=self.server_update_quick).pack(side="left")

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
        self.DAYC = {0: "нет — выход в 17:00", 1: "на 1 день", 2: "на 2 дня", 3: "на 3 дня"}
        self.v_dayc = tk.StringVar(value=self.DAYC.get(int(self.s.get("day_carry", 0) or 0), self.DAYC[0]))
        self.v_sound = tk.BooleanVar(value=self.s.get("sound_on", True))
        self.v_trend_on = tk.BooleanVar(value=self.s.get("trend_on", True))
        self.v_trend_trade = tk.BooleanVar(value=self.s.get("trend_trade", False))
        self.v_tier = tk.BooleanVar(value=self.s.get("tier_profile", True))
        self.WIN = {"9-19": "09:00–19:00", "10-18": "10:00–18:00"}
        self.EXIT_EARLY = {"9-19": "19:25", "10-18": "18:45"}
        self.v_win = tk.StringVar(value=self.WIN.get(self.s.get("entry_window", "9-19"), self.WIN["9-19"]))
        self.v_carry_all = tk.BooleanVar(value=bool(self.s.get("carry_all", True)))
        self.v_exit = tk.StringVar(value="23:45" if self.s.get("exit_late", True) else
                                   self.EXIT_EARLY.get(self.s.get("entry_window", "9-19"), "19:25"))
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
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(f, text="Включать торговлю сразу при открытии программы (поиск формаций включается всегда)", variable=self.v_autostart).pack(side="left")
        ttk.Checkbutton(f, text="Звук при входе и выходе из позиции", variable=self.v_sound).pack(side="left", padx=(16, 0))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=(4, 0))
        ttk.Label(f, text="Торговать:").pack(side="left")
        self.DIRS = {"both": "лонг и шорт", "long": "только лонг", "short": "только шорт"}
        self.v_dir = tk.StringVar(value=self.DIRS.get(self.s.get("direction", "both"), "лонг и шорт"))
        for k, name in self.DIRS.items():
            ttk.Radiobutton(f, text=name, value=name, variable=self.v_dir).pack(side="left", padx=(8, 0))
        ttk.Label(f, text="(для обеих стратегий)", foreground="#777").pack(side="left", padx=(8, 0))
        bg = ttk.Style().lookup("TFrame", "background") or None
        # --- формация
        r += 1
        ttk.Separator(g).grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1
        ttk.Label(g, text="1. Всплеск объёма + откат к EMA20", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w")
        ttk.Checkbutton(g, text="Робот торгует по этой формации", variable=self.v_spk).grid(row=r, column=1, sticky="w")
        r += 1
        tk.Label(g, text="Время входа не задаётся: робот входит сразу, как только формация подтверждена "
                         "(в выбранное ниже время входов) и выходит по стопу, тейку, трейлингу "
                         "или во время выхода; перенос позиций на следующий день — ниже.",
                 justify="left", anchor="w", font=("Segoe UI", 9, "bold"), fg="#0a5a2f", bg=bg, wraplength=740).grid(row=r, column=1, sticky="w")
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(f, text="Таймфреймы:").pack(side="left")
        for tf in (5, 15, 60):
            ttk.Checkbutton(f, text="1 час" if tf == 60 else (f"{tf} мин (только уведомления)" if tf == 5 else f"{tf} мин"),
                            variable=self.v_spk_tf[tf]).pack(side="left", padx=(6, 0))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Checkbutton(f, text="Искать трендовые откаты (импульс на объёме был в последние 3 дня, EMA10 над EMA20, касание EMA20 + подтверждение)",
                        variable=self.v_trend_on).pack(anchor="w")
        ttk.Checkbutton(f, text="…и торговать все трендовые откаты (на истории в целом в минусе; по умолчанию только по профилю ниже)",
                        variable=self.v_trend_trade).pack(anchor="w")
        ttk.Checkbutton(f, text="Профиль по эшелонам (по проверке на 152 акциях): индекс — формация 15 мин и 1 час; "
                                "2-й эшелон — формация 1 час и трендовый откат 15 мин после роста ≥5% на объёме ×10; "
                                "3-й эшелон и 5 мин — никогда не торгуются, только уведомления", variable=self.v_tier).pack(anchor="w")
        fw = ttk.Frame(f); fw.pack(anchor="w", pady=(4, 0))
        ttk.Label(fw, text="Время входов:").pack(side="left")
        cw = ttk.Combobox(fw, textvariable=self.v_win, width=13, state="readonly", values=list(self.WIN.values()))
        cw.pack(side="left", padx=6)
        ttk.Label(fw, text="выход:").pack(side="left", padx=(8, 0))
        ce = ttk.Combobox(fw, textvariable=self.v_exit, width=7, state="readonly")
        ce.pack(side="left", padx=6)

        def _exits(*_):
            k = {v: k for k, v in self.WIN.items()}.get(self.v_win.get(), "9-19")
            vals = [self.EXIT_EARLY[k], "23:45"]
            ce.configure(values=vals)
            if self.v_exit.get() not in vals:
                self.v_exit.set(vals[0] if self.v_exit.get() != "23:45" else "23:45")
        cw.bind("<<ComboboxSelected>>", _exits)
        _exits()
        self._exits_cb = _exits
        ttk.Checkbutton(f, text="Переносить на следующий день ВСЕ позиции — держать, пока не закроют стоп, тейк или трейлинг. "
                                "Если снять — в указанное время закрываются позиции в минусе, переносятся только в плюсе или с взятым тейком1 (до 2 дней)",
                        variable=self.v_carry_all).pack(anchor="w")
        r += 1
        ttk.Label(g, text="Чем торговать:").grid(row=r, column=0, sticky="e", padx=(0, 6))
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        for i, (k, name) in enumerate(at.MARKETS):
            ttk.Checkbutton(f, text=name, variable=self.v_mk[k]).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 14))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(f, text="Тейки:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_takes, width=15, state="readonly", values=["два тейка", "один тейк у хая"]).pack(side="left", padx=(4, 12))
        ttk.Label(f, text="Закрыть на тейке1, %:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_half, width=5, state="readonly", values=["30", "50", "70", "100"]).pack(side="left", padx=(4, 12))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(f, text="Трейлинг, R (расстояние до стопа):").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_trail, width=5, state="readonly", values=["0.5", "1", "1.5", "2"]).pack(side="left", padx=(4, 12))
        ttk.Label(f, text="Макс. позиций:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_max, width=15, state="readonly",
                     values=["без ограничения", "1", "2", "3", "5", "8", "10", "15", "20"]).pack(side="left", padx=(4, 0))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(f, text="Вход в позицию, % от портфеля:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_spk_share, width=5,
                     values=["1", "2", "3", "5", "10", "15", "20", "25", "33", "50", "100"]).pack(side="left", padx=(4, 8))
        LEV = {"1": "без маржи", "auto": "с маржой — плечо брокера по бумаге", "2": "с маржой ×2", "3": "с маржой ×3",
               "5": "с маржой ×5"}
        self.LEV = LEV
        self.v_lev = tk.StringVar(value=LEV.get(str(self.s.get("leverage", "1")), "без маржи"))
        ttk.Combobox(f, textvariable=self.v_lev, width=32, state="readonly", values=list(LEV.values())).pack(side="left", padx=(0, 8))
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        self.share_lbl = ttk.Label(f, text="", foreground="#555")
        self.share_lbl.pack(side="left")
        def _upd_share(*_):
            try:
                pct = float(self.v_spk_share.get().replace(",", "."))
                lk = {v: k for k, v in self.LEV.items()}.get(self.v_lev.get(), "1")
                lv = 1.0 if lk in ("1", "auto") else float(lk)
                if not self.v_spk_max.get().isdigit():
                    self.share_lbl.configure(foreground="#555", text=(
                        f"одна позиция = {pct * lv:g}% портфеля; число позиций не ограничено — новые сделки открываются, пока "
                        f"хватает денег на счёте" + (f" (без маржи — примерно до {int(100 // pct)} позиций одновременно)" if lk == "1" else "")))
                    return
                mx = int(self.v_spk_max.get())
                tot = pct * mx * lv
                txt = (f"одна позиция = {pct * lv:g}% портфеля" + (" × плечо брокера по бумаге" if lk == "auto" else "")
                       + f"; при {mx} позициях до {tot:g}%" + (" × плечо" if lk == "auto" else ""))
                if lk == "1" and pct * mx > 100:
                    txt += " — больше 100%: без маржи брокер не даст открыть все позиции"
                self.share_lbl.configure(text=txt, foreground="#b3261e" if (lk == "1" and pct * mx > 100) else "#555")
            except ValueError:
                self.share_lbl.configure(text="введите число")
        self.v_spk_share.trace_add("write", _upd_share)
        self.v_spk_max.trace_add("write", _upd_share)
        self.v_lev.trace_add("write", _upd_share)
        _upd_share()
        r += 1
        tk.Label(g, text="На истории (44 акции индекса, 10.2025–10.2026):\n" + "\n".join(f"  • {at.SPIKE_PRESETS[tf]['bt']}" for tf in (5, 15, 60))
                 + "\nСтоп — под самой низкой точкой всего отката (с закруглением) с запасом. Размер позиции — % от текущей стоимости портфеля песочницы."
                 + "\nДва тейка (полпути до хая 50% → б/у и трейлинг, хай — 50% остатка): на 15 мин в плюсе 64% сделок, в среднем +0,08% (трейлинг 1R)"
                 + " или +0,12% (1,5R); один тейк у хая: в плюсе 53%, в среднем +0,24%.",
                 justify="left", anchor="w", font=("Segoe UI", 9), fg="#555", bg=bg, wraplength=760).grid(row=r, column=1, sticky="w")
        # --- движение дня
        r += 1
        ttk.Separator(g).grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1
        ttk.Label(g, text="2. Движение дня (отдельная стратегия)", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w")
        ttk.Checkbutton(g, text="Робот торгует и по этой стратегии — вход в одно заданное время дня", variable=self.v_day).grid(row=r, column=1, sticky="w")
        r += 1
        fdc = ttk.Frame(g); fdc.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(fdc, text="Переносить позиции:").pack(side="left")
        ttk.Combobox(fdc, textvariable=self.v_dayc, width=20, state="readonly", values=list(self.DAYC.values())).pack(side="left", padx=6)
        ttk.Label(fdc, text="выход в 17:00 последнего дня, стоп 3% действует всё время", foreground="#555").pack(side="left")
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="Порог движения акции от открытия, %:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_thr, width=6, state="readonly",
                     values=[f"{x:g}" for x in sc.THRESHOLD_CHOICES]).pack(side="left", padx=(4, 0))
        ttk.Label(f, text="   Время входа этой стратегии:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_entry, width=7, state="readonly", values=sc.ENTRY_CHOICES).pack(side="left", padx=(4, 0))
        r += 1
        self.bt_lbl = tk.Label(g, text="", justify="left", anchor="w", font=("Segoe UI", 9), bg=bg)
        self.bt_lbl.grid(row=r, column=1, sticky="w")
        self.v_thr.trace_add("write", lambda *_: self._show_bt())
        self.v_entry.trace_add("write", lambda *_: self._show_bt())
        self._show_bt()
        r += 1
        ttk.Checkbutton(g, text="Добирать новые сигналы до 16:30 (на истории хуже: −8…−20% вместо +23…+35%)",
                        variable=self.v_rescan).grid(row=r, column=1, sticky="w")
        r += 1
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(f, text="Показывать её сигналы с сигналом звука", variable=self.v_as).pack(side="left")
        ttk.Checkbutton(f, text="Журнал форвард-теста в 17:20", variable=self.v_aj).pack(side="left", padx=(16, 0))
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
            tv.column(key, width=w, anchor="w" if key in ("ticker", "side", "reason", "date", "period", "from") else "e")
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

    def save(self):
        try:
            cap = float(self.v_cap.get().replace(" ", ""))
        except ValueError:
            messagebox.showerror(APP_TITLE, "Капитал должен быть числом, например 1000000.")
            return
        self.s.update(threshold=float(self.v_thr.get()), entry=self.v_entry.get(), capital=cap,
                      token=self.v_tok.get().strip(), sandbox_token=self.v_sbtok.get(), remember_token=self.v_rem.get(),
                      auto_signals=self.v_as.get(), auto_journal=self.v_aj.get(),
                      rescan=self.v_rescan.get(), autostart=self.v_autostart.get(), day_on=self.v_day.get(),
                      day_carry={v: k for k, v in self.DAYC.items()}.get(self.v_dayc.get(), 0), sound_on=self.v_sound.get(),
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
                      exit_late=self.v_exit.get() == "23:45", carry_all=self.v_carry_all.get())
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

    def download_history(self):
        import history_dl
        tok = os.environ.get("TINVEST_TOKEN")
        self.submit("Выгрузка истории всех акций",
                    lambda: history_dl.download_all(tok, bool(self.s.get("sandbox_token", True))))

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
                                        on_event=lambda kind, info: self.q.put(("call", self.on_trade, (kind, info))))
            except Exception as e:
                log(f"Код подключения к серверу не подошёл: {e}")
                return
            rr.on_settings = lambda srv: self.q.put(("call", self._settings_from_server, srv))
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
        if not tok or not self.s["sandbox_token"]:
            if not quiet:
                messagebox.showinfo(APP_TITLE, "Для автоторговли нужен токен песочницы: введите его во вкладке «Настройки», "
                                               "отметьте «Это токен песочницы» и нажмите «Сохранить настройки».")
            return
        self._apply_settings()
        log = lambda m: self.q.put(("log", f"[робот {now_msk():%H:%M:%S}] {m}\n"))
        broker = at.SandboxBroker(tok, float(self.s["capital"]), log=log)
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
        self.scan_lbl.configure(text=(f"Обновлено {ls:%H:%M}: рынок {mk * 100:+.2f}%, порог {thr * 100:g}%"
                                      if ls and mk is not None else "Данные появятся после 10:01."))
        self.scan_tree.delete(*self.scan_tree.get_children())
        for tk, mv, why in snap["scan"][:60]:
            tag = ("pos" if mv > 0 else "neg") if why == "СИГНАЛ" else ()
            self.scan_tree.insert("", "end", tags=tag, values=(tk, f"{mv * 100:+.2f}%",
                                                               f"кандидат — вход в {self.s.get('entry', '15:00')}" if why == "СИГНАЛ" else why))
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
        self.pos_tree.delete(*self.pos_tree.get_children())
        money = lambda v: "—" if v is None else (("+" if v > 0 else "") + f"{v:,.0f}").replace(",", " ")
        PS = rc.positions_summary(snap["positions"])
        groups = [("всплеск", "Всплеск + EMA20", [p for p in snap["positions"] if p.get("strat") == "spike"]),
                  ("15:00", "Движение дня", [p for p in snap["positions"] if p.get("strat") != "spike"])]
        for key, name, G in groups:
            if not G:
                continue
            d = PS["by_strat"].get(key, {})
            self.pos_tree.insert("", "end", tags=("hdr",), values=(
                f"▸ {name}", f"{len(G)} поз.", "", "", "", "", "", "",
                f"{d['avg_pct']:+.2f}%" if d.get("avg_pct") is not None else "", money(d.get("pnl_rub")) if d.get("known") else "—"))
            for p in G:
                last = p.get("last")
                res = p["side"] * (last / p["entry"] - 1) if last else None
                strat = "всплеск" + (" (б/у)" if p.get("took") else "") if p.get("strat") == "spike" else "15:00"
                self.pos_tree.insert("", "end", tags=(("pos" if res > 0 else "neg"),) if res is not None else (), values=(
                    p["ticker"], strat, "лонг" if p["side"] > 0 else "шорт", p["lots"], f"{p['entry']:.4g}", f"{p['stop']:.4g}",
                    (f"{p['take']:.4g}" if p.get("stage", 0) == 0 else (f"т2 {p['take2']:.4g}" if p.get("stage") == 1 and p.get("take2") else "трейл"))
                    if p.get("take") else "—",
                    f"{last:.4g}" if last else "—", f"{res * 100:+.2f}%" if res is not None else "—", money(p.get("pnl_rub"))))
        if snap["positions"]:
            T = PS["total"]
            self.pos_tree.insert("", "end", tags=("hdr",), values=(
                "ИТОГО", f"{T['n']} поз.", "", "", "", "", "", "", "", money(T["pnl_rub"]) if T["known"] else "—"))
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

    SERVER_KEYS = ["threshold", "entry", "capital", "sandbox_token", "rescan", "day_on", "spike_on", "spike_tfs", "spike_markets",
                   "direction", "leverage", "spike_half", "spike_trail", "spike_max", "spike_share", "two_takes", "tg_chat", "tg_form",
                   "tg_trades", "tg_imp", "tg_trend", "trend_on", "trend_trade", "tier_profile", "entry_window", "exit_late",
                   "carry_all", "day_carry", "auto_journal"]

    def _settings_from_server(self, srv):
        """В режиме пульта вкладка «Настройки» показывает настройки робота на сервере (они и действуют)."""
        for k in self.SERVER_KEYS:
            if k in srv:
                self.s[k] = srv[k]
        save_settings(self.s)
        s = self.s
        self.v_thr.set(f"{float(s['threshold']):g}"); self.v_entry.set(s["entry"]); self.v_cap.set(str(int(float(s["capital"]))))
        self.v_sbtok.set(bool(s["sandbox_token"])); self.v_aj.set(bool(s.get("auto_journal", True)))
        self.v_rescan.set(bool(s.get("rescan"))); self.v_day.set(bool(s.get("day_on", True)))
        self.v_dayc.set(self.DAYC.get(int(s.get("day_carry", 0) or 0), self.DAYC[0]))
        self.v_trend_on.set(bool(s.get("trend_on", True))); self.v_trend_trade.set(bool(s.get("trend_trade")))
        self.v_tier.set(bool(s.get("tier_profile", True)))
        self.v_win.set(self.WIN.get(s.get("entry_window", "9-19"), self.WIN["9-19"]))
        self.v_exit.set("23:45" if s.get("exit_late", True) else self.EXIT_EARLY.get(s.get("entry_window", "9-19"), "19:25"))
        if hasattr(self, "_exits_cb"):
            self._exits_cb()
        self.v_carry_all.set(bool(s.get("carry_all", True)))
        self.v_takes.set("два тейка" if s.get("two_takes", True) else "один тейк у хая")
        self.v_spk.set(bool(s.get("spike_on", True)))
        for tf, v in self.v_spk_tf.items():
            v.set(tf in s.get("spike_tfs", []))
        for k, v in self.v_mk.items():
            v.set(k in s.get("spike_markets", []))
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
                q = sc.index_quote()
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

    # --- результаты
    def _results_tick(self):
        self.refresh_results()
        self.after(60000, self._results_tick)

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
            for k, n in (("всплеск", "Всплеск + EMA20"), ("15:00", "Движение дня")):
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

    def server_update_quick(self):
        """Обновить программу на сервере по коду подключения (без пароля от VPS)."""
        if not (self.remote_mode() and self.robot_running() and hasattr(self.robot, "update_server")):
            messagebox.showinfo(APP_TITLE, "Нет связи с сервером. Используйте «Установить / обновить сервер» с паролем.")
            return
        if not messagebox.askyesno(APP_TITLE, f"Обновить программу на сервере до версии {VERSION}?\n\n"
                                              "Робот на сервере перезапустится на ~20 секунд; позиции, настройки и журналы сохранятся."):
            return
        import serverinstall as si
        rr = self.robot
        self.submit("Обновление сервера", lambda: print("Сервер обновлён до версии " + rr.update_server(si.bundle_bytes())
                                                         + " и перезапускается."))

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

    def close_all(self):
        if not self.robot_running() or not self.robot.positions:
            messagebox.showinfo(APP_TITLE, "Открытых позиций нет или робот не запущен.")
            return
        if messagebox.askyesno(APP_TITLE, "Закрыть ВСЕ открытые позиции по рынку?"):
            self.robot.request_close(None)
            self.log("Запрошено закрытие всех позиций.\n")

    def chart_selected(self):
        self.open_chart(self._sel_ticker(self.pos_tree) or "SBER")

    def _chart_from(self, tv, col):
        tkr = self._sel_ticker(tv, col)
        if tkr:
            tf = 5
            if tv is self.spk_tree:
                tfn = tv.item(tv.selection()[0], "values")[3]
                tf = {"5м": 5, "15м": 15, "1ч": 60}.get(tfn, 5)
            self.open_chart(tkr, tf)

    def _row_menu(self, e, tv, col):
        row = tv.identify_row(e.y)
        if not row or "hdr" in tv.item(row, "tags"):
            return
        tv.selection_set(row)
        tkr = tv.item(row, "values")[col]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label=f"График {tkr}", command=lambda: self._chart_from(tv, col))
        m.add_command(label=f"Открыть {tkr} в Т-Инвестициях", command=lambda: self.open_tbank(tkr))
        m.add_command(label="Терминал Т-Инвестиций", command=lambda: self.open_tbank(None))
        m.tk_popup(e.x_root, e.y_root)

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
        if not os.environ.get("TINVEST_TOKEN"):
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
               [dict(r, strat="всплеск") for r in read_csv(sc.SPIKE_LOG)[-500:]]
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
