# -*- coding: utf-8 -*-
"""
Сканер Мосбиржи — настольное приложение.
Стратегия: сильное движение дня по рынку, вход в 15:00, стоп 3%, выход в 17:00.
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

APP_TITLE = "Сканер Мосбиржи"
SETTINGS = os.path.join(sc.HERE, "settings.json")
DEFAULTS = {"threshold": 2.0, "entry": "15:00", "capital": 1_000_000, "sandbox_token": True,
            "auto_signals": True, "auto_journal": True, "auto_sandbox": False, "remember_token": False, "token": ""}


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
        self.title(APP_TITLE)
        self.geometry("1000x700")
        self.minsize(820, 560)
        self.s = load_settings()
        if self.s.get("token"):
            os.environ["TINVEST_TOKEN"] = self.s["token"]
        self.q = queue.Queue()
        self.tasks = queue.Queue()
        self.busy = threading.Event()
        self.done_today = {}
        self._apply_settings()
        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()
        self.after(200, self._poll)
        self.after(500, self.refresh_journal)
        self.after(600, self.refresh_sandbox)
        self.log(f"Готово. Порог {self.s['threshold']:g}%, вход в {self.s['entry']}. "
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
        pw.add(nb, weight=3)
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

        # --- Песочница
        t3 = ttk.Frame(nb, padding=10)
        nb.add(t3, text="Песочница Т-Инвестиций")
        bar = ttk.Frame(t3)
        bar.pack(fill="x")
        ttk.Button(bar, text="Проверить доступ", command=self.check_access).pack(side="left")
        ttk.Button(bar, text="Торговать сегодня", command=self.sandbox_today).pack(side="left", padx=6)
        ttk.Button(bar, text="Стоп (закрыть позиции)", command=self.stop).pack(side="left")
        ttk.Button(bar, text="Обновить", command=self.refresh_sandbox).pack(side="left", padx=6)
        ttk.Label(t3, text="Торговля идёт только на демо-счёте песочницы. Компьютер и приложение должны работать до 17:00.",
                  foreground="#555").pack(anchor="w", pady=(8, 4))
        self.sb_tree = self._tree(t3, [("date", "Дата", 90), ("ticker", "Бумага", 70), ("side", "Сторона", 70), ("lots", "Лотов", 60),
                                       ("entry", "Вход", 80), ("exit", "Выход", 80), ("reason", "Причина", 120), ("res", "Результат", 90)])

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
        self.v_asb = tk.BooleanVar(value=self.s["auto_sandbox"])
        g = ttk.Frame(t4)
        g.pack(anchor="nw", fill="x")
        r = 0
        ttk.Label(g, text="Порог движения акции:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Radiobutton(f, text="2% (больше сделок)", value="2", variable=self.v_thr).pack(side="left")
        ttk.Radiobutton(f, text="2,5% (выше средняя сделка)", value="2.5", variable=self.v_thr).pack(side="left", padx=10)
        r += 1
        ttk.Label(g, text="Время входа:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(g); f.grid(row=r, column=1, sticky="w")
        ttk.Radiobutton(f, text="15:00 (основной)", value="15:00", variable=self.v_entry).pack(side="left")
        ttk.Radiobutton(f, text="14:30 (слабее, для сравнения)", value="14:30", variable=self.v_entry).pack(side="left", padx=10)
        r += 1
        ttk.Label(g, text="Капитал песочницы, ₽:").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(g, textvariable=self.v_cap, width=14).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(g, text="Токен Т-Инвестиций:").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(g, textvariable=self.v_tok, width=60, show="•").grid(row=r, column=1, sticky="w")
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
        ttk.Checkbutton(g, text="Торговать в песочнице во время входа (нужен токен песочницы)", variable=self.v_asb).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Button(g, text="Сохранить настройки", command=self.save).grid(row=r, column=1, sticky="w", pady=12)
        ttk.Label(t4, text="Без токена данные берутся с сайта Мосбиржи; бесплатные данные там обычно идут с задержкой около 15 минут.\n"
                           "Для журнала форвард-теста это не важно, для сигналов в реальном времени лучше задать токен.",
                  foreground="#555", justify="left").pack(anchor="w", pady=(10, 0))
        self._tick()

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
        if self.s["auto_sandbox"]:
            auto.append(f"песочница в {self.s['entry']}")
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
                      auto_signals=self.v_as.get(), auto_journal=self.v_aj.get(), auto_sandbox=self.v_asb.get())
        if self.s["token"]:
            os.environ["TINVEST_TOKEN"] = self.s["token"]
        else:
            os.environ.pop("TINVEST_TOKEN", None)
        save_settings(self.s)
        self._apply_settings()
        self.refresh_journal()
        self.refresh_sandbox()
        self.log(f"Настройки сохранены: порог {self.s['threshold']:g}%, вход {self.s['entry']}.\n")

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
            f"Ориентир по бэктесту: {'+0,22' if self.s['threshold'] >= 2.5 else '+0,16'}% на сделку после затрат."))

    # --- песочница
    def check_access(self):
        if not os.environ.get("TINVEST_TOKEN"):
            messagebox.showinfo(APP_TITLE, "Сначала введите токен во вкладке «Настройки» и нажмите «Сохранить».")
            return
        self.submit("Проверка доступа", lambda: sc.check_mode(types.SimpleNamespace(sandbox_token=self.s["sandbox_token"])))

    def sandbox_today(self, now=False):
        if not os.environ.get("TINVEST_TOKEN") or not self.s["sandbox_token"]:
            messagebox.showinfo(APP_TITLE, "Для песочницы нужен токен песочницы: введите его в «Настройках» и отметьте «Это токен песочницы».")
            return
        args = types.SimpleNamespace(capital=float(self.s["capital"]), now=now)
        self.submit("Песочница: торговля на сегодня", lambda: sc.sandbox_mode(args), lambda _: self.refresh_sandbox())

    def refresh_sandbox(self):
        self.sb_tree.delete(*self.sb_tree.get_children())
        for r in reversed(read_csv(sc.SANDBOX_LOG)[-500:]):
            res = float(r["result_pct"])
            self.sb_tree.insert("", "end", tags=("pos" if res > 0 else "neg",), values=(
                r["date"], r["ticker"], r["side"], r["lots"], r["entry"], r["exit"], r["reason"], f"{res:+.2f}%"))

    # ------------------------------------------------------------------ расписание
    def _scheduler(self):
        while True:
            try:
                n = now_msk()
                today = n.date().isoformat()
                if n.weekday() < 5:
                    eh, em = (14, 30) if self.s["entry"] == "14:30" else (15, 0)
                    start = n.replace(hour=eh, minute=em, second=0, microsecond=0)
                    in_window = start - timedelta(minutes=2) <= n < start + timedelta(minutes=10)
                    if in_window and self.done_today.get(("entry", today)) is None:
                        self.done_today[("entry", today)] = True
                        if self.s["auto_sandbox"] and os.environ.get("TINVEST_TOKEN") and self.s["sandbox_token"]:
                            self.q.put(("call", lambda _: self.sandbox_today(), None))
                        elif self.s["auto_signals"]:
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
