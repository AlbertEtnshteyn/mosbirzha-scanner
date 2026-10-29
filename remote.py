# -*- coding: utf-8 -*-
"""Пульт сервера робота в программе на компьютере: тот же интерфейс, что у AutoTrader, но всё делает сервер."""
import re
import threading
from datetime import datetime

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def parse_code(code):
    m = re.match(r"^moexrobot://([^/:\s]+):(\d+)/([A-Za-z0-9_\-]+)/([0-9a-fA-F]{64})$", (code or "").strip())
    return (m.group(1), int(m.group(2)), m.group(3), m.group(4).lower()) if m else None


class _PinAdapter(HTTPAdapter):
    """Доверяем только сертификату сервера с этим отпечатком (из кода подключения) — в Windows ничего не ставится."""
    def __init__(self, fp, **kw):
        self.fp = fp
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["assert_fingerprint"] = self.fp
        kw["cert_reqs"] = "CERT_NONE"
        return super().init_poolmanager(*a, **kw)


class Client:
    def __init__(self, code):
        p = parse_code(code)
        if not p:
            raise ValueError("код подключения не распознан")
        self.host, self.port, self.key, fp = p
        self.base = f"https://{self.host}:{self.port}"
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.verify = False                     # проверка — по отпечатку (assert_fingerprint), не по центрам сертификации
        self.s.mount("https://", _PinAdapter(fp))
        self.s.headers["X-Key"] = self.key

    def get(self, path, **params):
        r = self.s.get(self.base + path, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None):
        r = self.s.post(self.base + path, json=body or {}, timeout=20)
        r.raise_for_status()
        return r.json()


class RemoteRobot:
    """Подменяет AutoTrader в окне программы: позиции, статус и кнопки — с сервера."""
    def __init__(self, code, log, on_update, on_event):
        self.c = Client(code)
        self.log = log
        self.on_update = on_update
        self.on_event = on_event
        self.trade = False
        self.positions = []
        self._terms_cache = {}
        self.stop_event = threading.Event()
        self.ev = None
        self.log_seq = 0
        self.ok = None
        self.server_version = ""

    @property
    def where(self):
        return f"{self.c.host}"

    def run(self):
        while not self.stop_event.is_set():
            try:
                st = self.c.get("/api/state", ev=self.ev or 0, log=self.log_seq)
                if self.ok is not True:
                    self.log(f"Связь с сервером {self.where} есть (версия сервера {st.get('version')}). "
                             f"Робот работает на сервере — здесь его экран и кнопки.")
                self.ok = True
                self.server_version = st.get("version", "")
                self.trade = bool(st.get("trade"))
                self.positions = st.get("positions") or []
                if self.ev is not None:
                    for e in st.get("events", []):
                        self.on_event(e.get("kind"), {"remote": True, "text": e.get("text", ""), "ticker": e.get("ticker")})
                if self.log_seq:
                    for ln in st.get("log", []):
                        self.log("[сервер] " + ln)
                self.ev = st.get("ev_seq", 0)
                self.log_seq = st.get("log_seq", 0)
                ls = st.get("last_scan")
                try:
                    ls = datetime.fromisoformat(ls) if ls else None
                except Exception:
                    ls = None
                run = st.get("running")
                status = (("ТОРГОВЛЯ" if self.trade else "НАБЛЮДЕНИЕ") + f" на сервере {self.where}: " + (st.get("status") or "")) if run \
                    else f"на сервере {self.where}: {st.get('status') or 'робот не запущен'}"
                self.on_update({"status": status, "market": st.get("market"), "scan": st.get("scan") or [],
                                "spikes": list(reversed(st.get("spikes") or [])), "positions": self.positions,
                                "last_scan": ls, "threshold": st.get("threshold"), "remote": True})
            except Exception as e:
                if self.ok is not False:
                    self.log(f"Нет связи с сервером {self.where}: {e}. Повторяю каждые 10 секунд "
                             f"(робот на сервере при этом продолжает работать).")
                    self.on_update({"status": f"нет связи с сервером {self.where}", "market": None, "scan": [], "spikes": [],
                                    "positions": self.positions, "last_scan": None, "remote": True})
                self.ok = False
                self.stop_event.wait(7)
            self.stop_event.wait(3)

    # --- кнопки (в фоне, чтобы окно не подвисало при плохой связи)
    def _bg(self, fn, what):
        def job():
            try:
                fn()
            except Exception as e:
                self.log(f"Сервер не выполнил «{what}»: {e}")
        threading.Thread(target=job, daemon=True).start()

    def set_trade(self, on):
        if on:
            def f():
                r = self.c.post("/api/start")
                if not r.get("ok"):
                    self.log("Сервер: " + (r.get("msg") or "не удалось включить торговлю"))
                else:
                    self.trade = True
                    self.log(f"Торговля на сервере {self.where} включена.")
            self._bg(f, "включить торговлю")
        else:
            self.trade = False
            self._bg(lambda: self.c.post("/api/stop", {"close": False}), "выключить торговлю")

    def request_close(self, ticker=None):
        self._bg(lambda: self.c.post("/api/close", {"ticker": ticker}), "закрыть позицию")

    def request_stop(self, close_positions=False):
        self.stop_event.set()

    def update_server(self, data):
        import base64
        r = self.c.s.post(self.c.base + "/api/update", json={"zip": base64.b64encode(data).decode()}, timeout=120)
        if r.status_code == 404:
            raise RuntimeError("программа на сервере старая и не умеет обновляться так — обновите её с паролем")
        r.raise_for_status()
        return r.json().get("version", "")

    def trades(self):
        return self.c.get("/api/trades")

    def push_settings(self, s):
        keys = ["threshold", "entry", "capital", "sandbox_token", "rescan", "day_on", "spike_on", "spike_tfs", "spike_markets",
                "direction", "leverage", "spike_half", "spike_trail", "spike_max", "spike_share", "two_takes", "tg_chat", "tg_form",
                "tg_trades", "tg_imp", "tg_trend", "trend_on", "trend_trade", "tier_profile", "entry_window", "exit_late", "carry_all", "auto_journal"]
        body = {k: s[k] for k in keys if k in s}
        if s.get("token"):
            body["token"] = s["token"]
        if s.get("tg_token"):
            body["tg_token"] = s["tg_token"]
        return self.c.post("/api/settings", body)
