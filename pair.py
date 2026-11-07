# -*- coding: utf-8 -*-
"""
Пара серверов: основной и резервный.

* Торгует только один (активный). Второй (пассивный) каждые 20 секунд проверяет первый и раз в минуту забирает у него
  позиции, настройки и журналы — чтобы в любой момент продолжить с того же места.
* Если активный не отвечает 5 минут подряд — пассивный становится активным и продолжает вести позиции и торговать.
  Вернувшийся сервер видит, что второй уже активен, и остаётся резервным (без двойной торговли).
* Если у сервера не проходит запрос к Т-Инвестициям, Мосбирже или Telegram (нет связи, таймаут) —
  запрос автоматически выполняется через второй сервер.
* Пассивный сервер пересылает запросы телефона/программы активному, поэтому подключаться можно к любому из двух.
"""
import base64
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

import scanner as sc

PAIR_FILE = os.path.join(sc.HERE, "pair.json")
TAKEOVER_SEC = int(os.environ.get("MOEX_TAKEOVER", 300))                      # сколько ждать молчания активного сервера, прежде чем взять торговлю на себя
SYNC_FILES = ["auto_state.json", "settings.json", "sandbox_trades.csv", "sandbox_spikes.csv", "manual_trades.csv", "balance.csv",
              "forward_journal.csv", "forward_days.csv"]
RELAY_HOSTS = ("tinkoff.ru", "tbank.ru", "moex.com", "telegram.org", "finam.ru")


def relay_allowed(url):
    h = (urlparse(url).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in RELAY_HOSTS)


def do_relay(req):
    """Выполнить чужой запрос напрямую (на стороне второго сервера)."""
    url = req["url"]
    if not relay_allowed(url):
        raise ValueError("адрес не из списка разрешённых")
    body = base64.b64decode(req["body"]) if req.get("body") else None
    r = sc.http().direct(req.get("method", "GET"), url, headers=req.get("headers") or {}, data=body,
                         timeout=min(float(req.get("timeout") or 30), 120))
    return {"status": r.status_code, "headers": {k: v for k, v in r.headers.items()
                                                  if k.lower() in ("content-type", "x-ratelimit-reset", "retry-after")},
            "body": base64.b64encode(r.content).decode()}


class Pair:
    def __init__(self, log, on_activate, on_deactivate):
        self.log = log
        self.on_activate = on_activate          # fn(trade: bool)
        self.on_deactivate = on_deactivate
        self.cfg = {}
        try:
            self.cfg = json.load(open(PAIR_FILE, encoding="utf-8"))
        except Exception:
            pass
        self.client = None
        if self.cfg.get("peer"):
            import remote
            try:
                self.client = remote.Client(self.cfg["peer"])
            except Exception as e:
                self.log(f"Пара серверов: код второго сервера не подошёл ({e}) — работаю один.")
        self.role = self.cfg.get("role", "primary")
        self.active = self.client is None             # без пары — всегда активен
        self.peer_ok = False
        self.peer_info = {}
        self.last_ok = time.time()                    # отсчёт молчания второго — с момента запуска
        self.last_sync = 0
        self.peer_trade = None
        self.decided = self.client is None

    @property
    def enabled(self):
        return self.client is not None

    def info(self):
        return {"enabled": self.enabled, "role": self.role, "active": self.active, "peer_ok": self.peer_ok,
                "peer_host": self.client.host if self.client else "", "peer_code": self.cfg.get("peer", ""),
                "peer_last_ok": datetime.fromtimestamp(self.last_ok).strftime("%H:%M:%S") if self.enabled else ""}

    # --- запрос через второй сервер
    def relay(self, method, url, kw):
        if not (self.enabled and self.peer_ok and relay_allowed(url)):
            return None
        prep = requests.Request(method, url, params=kw.get("params"), json=kw.get("json"), data=kw.get("data"),
                                headers=kw.get("headers")).prepare()
        body = prep.body.encode() if isinstance(prep.body, str) else (prep.body or b"")
        js = self.client.post("/api/relay", {"method": method, "url": prep.url, "headers": dict(prep.headers),
                                             "body": base64.b64encode(body).decode(), "timeout": kw.get("timeout") or 30})
        r = requests.Response()
        r.status_code = js["status"]
        r._content = base64.b64decode(js["body"])
        r.headers.update(js.get("headers") or {})
        r.url = prep.url
        r.encoding = "utf-8"
        self.log(f"Запрос к {urlparse(url).hostname} не прошёл напрямую — выполнен через второй сервер ({self.client.host}).")
        return r

    # --- главный цикл пары
    def loop(self):
        while True:
            try:
                self._tick()
            except Exception as e:
                self.log(f"Пара серверов: {e}")
            time.sleep(int(os.environ.get("MOEX_PAIR_TICK", 20)))

    def _tick(self):
        if not self.enabled:
            return
        try:
            self.peer_info = self.client.get("/api/ping")
            self.peer_ok, self.last_ok = True, time.time()
        except Exception:
            self.peer_ok = False
        peer_active = self.peer_ok and self.peer_info.get("active")
        if self.peer_ok:
            self.peer_trade = self.peer_info.get("trade", self.peer_trade)
        if not self.decided:                           # первое решение после запуска
            self.decided = True
            if self.role == "primary" and not peer_active:
                return self._activate("основной сервер запущен, второй не торгует")
            if peer_active:
                self.log(f"Пара серверов: торгует второй сервер ({self.client.host}) — я в резерве, слежу за ним.")
        if self.active:
            if peer_active and self.role == "standby":     # оба активны (связь между ними пропадала) — резервный уступает
                self.active = False
                self.log("Пара серверов: основной сервер снова торгует — перехожу в резерв.")
                self.on_deactivate()
            return
        if peer_active:
            if time.time() - self.last_sync > 15:          # каждые ~20 с: резерв знает свежие позиции
                self._sync()
            return
        silent = time.time() - self.last_ok
        if not self.peer_ok and silent >= TAKEOVER_SEC:
            self._activate(f"второй сервер ({self.client.host}) не отвечает {silent / 60:.0f} мин")
        elif self.peer_ok and not peer_active and self.role == "primary":
            self._activate("второй сервер на связи, но не торгует")

    def _activate(self, why):
        self.active = True
        self.log(f"Пара серверов: беру работу на себя — {why}.")
        self.on_activate(self.peer_trade)

    def _sync(self):
        """Забрать у активного сервера позиции, настройки и журналы (пассивный держит их наготове)."""
        js = self.client.get("/api/pair_sync")
        for name, b64 in (js.get("files") or {}).items():
            if name not in SYNC_FILES:
                continue
            path = os.path.join(sc.HERE, name)
            if b64 is None:                      # у активного файла нет (например, после сброса) — убрать и здесь
                if name not in ("settings.json",) and os.path.exists(path):
                    os.remove(path)
                continue
            tmp = path + ".sync"
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(b64))
            os.replace(tmp, path)
        self.last_sync = time.time()


def sync_payload():
    files = {}
    for name in SYNC_FILES:
        p = os.path.join(sc.HERE, name)
        if not os.path.exists(p):
            files[name] = None
        elif os.path.getsize(p) < 5_000_000:
            files[name] = base64.b64encode(open(p, "rb").read()).decode()
    return {"files": files}


def save_pair(peer, role):
    with open(PAIR_FILE, "w", encoding="utf-8") as f:
        json.dump({"peer": peer, "role": role}, f)
    os.chmod(PAIR_FILE, 0o600)
