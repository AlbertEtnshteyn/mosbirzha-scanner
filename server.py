# -*- coding: utf-8 -*-
"""
Сервер робота: тот же робот, что в программе на Windows, но без окна — для круглосуточной работы на VPS.
Управление — с телефона (приложение для Андроида) или из браузера: https://адрес:8443

Защита: соединение шифруется собственным сертификатом сервера (создаётся при первом запуске и лежит
только в папке программы — в систему ничего не ставится); телефон узнаёт сервер по отпечатку
этого сертификата из кода подключения. Каждый запрос подписан ключом доступа из того же кода.

Запуск:  python3 server.py [--port 8443] [--host адрес-для-кода-подключения]
"""
import argparse
import base64
import calendar
import collections
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import ssl
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import scanner as sc
import autotrader as at
import robotcore as rc
import tgnotify as tg
try:
    import pair as pairmod                       # пара серверов (появляется со второго обновления — старый сервер его не копировал)
except ImportError:
    pairmod = None

HERE = sc.HERE
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SERVER_CFG = os.path.join(HERE, "server.json")
CERT = os.path.join(HERE, "server_cert.pem")
KEY = os.path.join(HERE, "server_key.pem")


def app_version():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")
    try:
        return open(p, encoding="utf-8").read().strip()
    except Exception:
        return "dev"


VERSION = app_version()


def now_msk():
    return datetime.now(sc.MSK)


# ============================================================================ журнал действий
class LogBuf:
    """Последние строки журнала действий (для телефона) + вывод в консоль/systemd."""
    def __init__(self, n=3000):
        self.lines = collections.deque(maxlen=n)
        self.seq = 0
        self.lock = threading.Lock()
        self._part = ""

    def add(self, text):
        with self.lock:
            for ln in str(text).rstrip("\n").split("\n"):
                self.seq += 1
                self.lines.append((self.seq, ln))

    def since(self, n):
        with self.lock:
            return [x for x in self.lines if x[0] > n][-800:], self.seq


LOG = LogBuf()


class Tee:
    def __init__(self, orig):
        self.orig = orig
        self.buf = ""

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        self.buf += s
        while "\n" in self.buf:
            ln, self.buf = self.buf.split("\n", 1)
            LOG.add(ln)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass


def log(msg):
    print(f"[{now_msk():%d.%m %H:%M:%S}] {msg}", flush=True)


# ============================================================================ робот
class Robot:
    def __init__(self):
        self.s = rc.load_settings()
        self.robot = None
        self.thread = None
        self.snap = {}
        self.events = collections.deque(maxlen=200)       # для звука/уведомлений на телефоне
        self.ev_seq = 0
        self.lock = threading.Lock()
        self.done = {}
        self.busy = None
        self.index = None
        self._apply()
        threading.Thread(target=self._index_loop, daemon=True, name="index").start()

    def _index_loop(self):
        """IMOEX в реальном времени для телефона: раз в 10 секунд днём, реже ночью и в выходные."""
        while True:
            try:
                q = sc.index_quote()
                if q:
                    self.index = q
            except Exception:
                pass
            n = now_msk()
            time.sleep(10 if n.weekday() < 5 and 6 <= n.hour < 24 else 300)

    # --- настройки
    def _apply(self):
        if self.s.get("token"):
            os.environ["TINVEST_TOKEN"] = self.s["token"]
        else:
            os.environ.pop("TINVEST_TOKEN", None)
        sc.configure(float(self.s["threshold"]), self.s["entry"])

    def save(self, new):
        allowed = set(rc.DEFAULTS) | {"spike_share"}
        for k, v in new.items():
            if k in allowed:
                self.s[k] = v
        self.s["remember_token"] = True                  # на сервере токен хранится (иначе робот не переживёт перезапуск)
        rc.save_settings(self.s, keep_token=True)
        self._apply()
        log("Настройки сохранены с телефона.")
        self.restart()

    def public_settings(self):
        d = dict(self.s)
        tok = d.pop("token", "")
        d["token_set"] = bool(tok)
        d["token_hint"] = (tok[:4] + "…" + tok[-4:]) if len(tok) > 12 else ""
        tgt = d.get("tg_token", "")
        d["tg_token"] = ""
        d["tg_token_set"] = bool(tgt)
        d["markets_all"] = at.MARKETS
        return d

    # --- работа робота
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, trade):
        if PAIR and PAIR.enabled and not PAIR.active:
            return False                                 # резервный сервер: торгует второй
        with self.lock:
            if self.running():
                if trade and not self.robot.trade:
                    self.robot.set_trade(True)
                    log("Торговля включена.")
                return True
            tok = self.s.get("token")
            if not tok or not self.s.get("sandbox_token", True):
                log("Робот ждёт токен песочницы Т-Инвестиций — введите его в настройках на телефоне.")
                return False
            self._apply()
            rlog = lambda m: print(f"[робот {now_msk():%H:%M:%S}] {m}", flush=True)
            broker = at.SandboxBroker(tok, float(self.s["capital"]), log=rlog)
            self.robot = at.AutoTrader(broker, float(self.s["capital"]), log=rlog, rescan=bool(self.s.get("rescan")),
                                       spike=rc.spike_cfg(self.s), day_on=bool(self.s.get("day_on", True)),
                                       direction=self.s.get("direction", "both"), trade=trade,
                                       on_event=self.on_event, on_update=self.on_update)
            robot = self.robot

            def run():
                try:
                    robot.run()
                except sc.CertError as e:
                    rlog(f"Ошибка защищённого соединения: {e}. Робот остановлен, повтор через минуту.")
                except BaseException:
                    rlog("Сбой робота:\n" + traceback.format_exc())
            self.thread = threading.Thread(target=run, daemon=True, name="robot")
            self.thread.start()
            self.s["autostart"] = bool(trade)            # после перезагрузки сервера — в том же режиме
            rc.save_settings(self.s, keep_token=True)
            return True

    def pair_activate(self, peer_trade):
        """Второй сервер замолчал — продолжаем с его позициями и настройками (они уже синхронизированы)."""
        self.s = rc.load_settings()
        self._apply()
        trade = bool(peer_trade) if peer_trade is not None else bool(self.s.get("autostart"))
        self.start(trade)
        try:
            tg.send(self.s.get("tg_token"), self.s.get("tg_chat"),
                    "⚠️ Робот Мосбиржи: основной сервер не отвечает — работу продолжил резервный сервер.", lambda m: print(m))
        except Exception:
            pass

    def pair_deactivate(self):
        if self.running():
            self.robot.request_stop(False)               # позиции не закрываем — их ведёт второй сервер

    def stop_trade(self, close):
        if not self.running():
            return
        if close and self.robot.positions:
            self.robot.request_close(None)
        self.robot.set_trade(False)
        self.s["autostart"] = False
        rc.save_settings(self.s, keep_token=True)
        log("Торговля выключена" + (", открытые позиции закрываются по рынку." if close else
                                     "; открытые позиции робот доведёт до стопа/тейка/выхода."))

    def restart(self):
        def job():
            trade = bool(self.robot.trade) if self.running() else bool(self.s.get("autostart"))
            if self.running():
                self.robot.request_stop(False)
                for _ in range(240):
                    if not self.running():
                        break
                    time.sleep(0.5)
            self.start(trade)
        threading.Thread(target=job, daemon=True).start()

    def on_update(self, snap):
        self.snap = snap

    def on_event(self, kind, info):
        with self.lock:
            self.ev_seq += 1
            self.events.append({"id": self.ev_seq, "kind": kind, "time": now_msk().strftime("%H:%M:%S"),
                                "ticker": info.get("ticker"), "text": self._ev_text(kind, info)})
        tok, chat = self.s.get("tg_token"), self.s.get("tg_chat")
        tglog = lambda m: print(m, flush=True)
        try:
            if kind == "formation":
                if self.s.get("tg_trend" if info.get("trend") else "tg_form", True):
                    tg.send(tok, chat, tg.formation_text(info), tglog)
            elif kind == "impulse":
                if self.s.get("tg_imp", False):
                    tg.send(tok, chat, tg.impulse_text(info), tglog)
            elif self.s.get("tg_trades", True):
                tg.send(tok, chat, tg.trade_text(kind, info), tglog)
        except Exception as e:
            print("Telegram:", e, flush=True)

    @staticmethod
    def _ev_text(kind, info):
        try:
            if kind in ("open", "close", "carry"):
                return tg.trade_text(kind, info)
            if kind == "formation":
                return (f"📐 Формация {info.get('ticker', '')} {info.get('tf', '')} {'лонг' if info.get('side', 1) > 0 else 'шорт'}"
                        + (f" — {info['decision']}" if info.get("decision") else ""))
            if kind == "impulse":
                return tg.impulse_text(info)
        except Exception:
            pass
        return f"{kind} {info.get('ticker', '')}"

    # --- присмотр: робот всегда должен работать; журнал «движения дня» в 17:20
    def watchdog(self):
        last_try = 0
        while True:
            try:
                if PAIR and PAIR.enabled and not PAIR.active:
                    time.sleep(20)
                    continue
                if not self.running() and time.time() - last_try > 60 and self.s.get("token"):
                    last_try = time.time()
                    self.start(bool(self.s.get("autostart")))
                n = now_msk()
                today = n.date().isoformat()
                if (n.weekday() < 5 and self.s.get("auto_journal", True) and (n.hour, n.minute) >= (17, 20) and n.hour < 23
                        and not self.done.get(("journal", today))):
                    self.done[("journal", today)] = True
                    threading.Thread(target=self._journal, args=(n.date(),), daemon=True).start()
            except Exception:
                traceback.print_exc()
            time.sleep(20)

    def _journal(self, d):
        try:
            sc.journal_day(d, sc.imoex_tickers())
            sc.stats()
        except Exception as e:
            print("Журнал за день:", e, flush=True)

    def state(self):
        snap = self.snap or {}
        r = self.robot if self.running() else None
        return {"version": VERSION, "time": now_msk().strftime("%d.%m.%Y %H:%M:%S"),
                "running": bool(r), "trade": bool(r and r.trade), "token_set": bool(self.s.get("token")),
                "status": snap.get("status") if r else ("ждёт токен песочницы в настройках" if not self.s.get("token") else "запускается…"),
                "market": snap.get("market"), "last_scan": snap.get("last_scan"),
                "positions": snap.get("positions", []) if r else [],
                "spikes": list(reversed(snap.get("spikes", []))), "scan": snap.get("scan", [])[:60],
                "threshold": sc.THRESHOLD, "ev_seq": self.ev_seq, "balance": snap.get("balance"),
                "entry": self.s.get("entry", "15:00"), "day_on": bool(self.s.get("day_on", True)), "day_mode": self.s.get("day_mode", "fixed"),
                "pos_summary": rc.positions_summary(snap.get("positions", []) if r else []), "index": self.index,
                "pair": PAIR.info() if PAIR else None}


ROBOT = None
PAIR = None


# ============================================================================ данные для вкладок
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def closed_trades():
    rows = [dict(r, strat="15:00") for r in read_csv(sc.SANDBOX_LOG)[-500:]] + \
           [dict(r, strat="всплеск") for r in read_csv(sc.SPIKE_LOG)[-500:]]
    rows.sort(key=lambda r: r.get("date", ""))
    out = list(reversed(rows))
    res = [float(r.get("result_pct") or 0) for r in out]
    return {"rows": out[:400], "n": len(res), "wins": sum(1 for x in res if x > 0), "sum": sum(res),
            "by_strat": {k: rc.strat_summary([r for r in out if r["strat"] == k]) for k in ("всплеск", "15:00")},
            "total": rc.strat_summary(out)}


def journal():
    rows = read_csv(sc.JOURNAL)
    days = read_csv(sc.DAYS_LOG)
    eq, peak, dd = 1.0, 1.0, 0.0
    for d in sorted(days, key=lambda x: x["date"]):
        eq *= 1 + float(d["day_net_pct"]) / 100
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    nets = [float(r["net_pct"]) for r in rows]
    return {"rows": list(reversed(rows[-300:])), "days": len(days), "trades": len(nets),
            "wins": sum(1 for x in nets if x > 0), "avg": (sum(nets) / len(nets)) if nets else 0,
            "total": (eq - 1) * 100, "dd": dd * 100,
            "period": f"{days[0]['date']} — {days[-1]['date']}" if days else ""}


def _ts(t):
    return calendar.timegm(t.replace(tzinfo=None).timetuple())   # время МСК «как есть» (график подписывает его без сдвига)


def chart(ticker, tf):
    s = ROBOT.s
    tok = s.get("token")
    if not tok:
        raise ValueError("Для графика нужен токен Т-Инвестиций (Настройки).")
    rows, trades, tier = rc.chart_data(tok, bool(s.get("sandbox_token", True)), ticker, tf, s)
    closes = [r["c"] for r in rows]
    e10, e20 = rc.ema(closes, 10), rc.ema(closes, 20)
    bars = [{"t": _ts(r["t"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r.get("v", 0)} for r in rows]
    tr_out = []
    for t in trades:
        tr_out.append({"side": t["side"], "entry": t["entry"], "stop": t["stop"], "t1": t.get("t1"), "t2": t.get("t2"),
                       "entry_t": bars[t["entry_i"]]["t"], "result": t["result"], "open": t.get("open", False),
                       "impulse": t["impulse_t"].strftime("%d.%m %H:%M") if t.get("impulse_t") else "",
                       "touch": t["touch_t"].strftime("%d.%m %H:%M") if t.get("touch_t") else "",
                       "exits": [{"t": bars[i]["t"], "px": px, "frac": fr, "why": why} for i, px, fr, why in t["exits"]]})
    poss = [p for p in (ROBOT.robot.positions if ROBOT.running() else []) if p["ticker"] == ticker]
    return {"ticker": ticker, "tf": tf, "bars": bars, "ema10": e10, "ema20": e20, "trades": tr_out, "tier": tier,
            "days": rc.CHART_DEPTH[tf], "positions": poss,
            "tbank": sc.tbank_url(ticker, (ROBOT.robot._terms_cache.get(ticker, ("share",))[0] if ROBOT.robot else "share"))}


# ============================================================================ обновление программы с телефона/компьютера
CODE_FILES = {"server.py", "scanner.py", "autotrader.py", "robotcore.py", "tgnotify.py", "history_dl.py", "pair.py", "remote.py", "version.txt"}
CODE_DIRS = ("web/", "tools/")


def apply_update(data):
    """Новая версия программы сервера (тот же архив, что ставит установщик): заменить код и перезапуститься.
    Настройки, журналы, позиции, ключ и сертификат сервера не трогаются."""
    import shutil
    import tempfile
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    if "server.py" not in names or "autotrader.py" not in names:
        raise ValueError("это не архив программы сервера")
    code_dir = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp(prefix="upd_", dir=code_dir)
    try:
        for n in names:
            if n.endswith("/") or ".." in n or n.startswith("/"):
                continue
            if n in CODE_FILES or n.startswith(CODE_DIRS):
                dst = os.path.join(tmp, n)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(z.read(n))
        for py in ("server.py", "scanner.py", "autotrader.py", "robotcore.py"):    # архив целый и код читается
            compile(open(os.path.join(tmp, py), encoding="utf-8").read(), py, "exec")
        for d in CODE_DIRS:
            if os.path.isdir(os.path.join(tmp, d)):
                shutil.rmtree(os.path.join(code_dir, d), ignore_errors=True)
                shutil.copytree(os.path.join(tmp, d), os.path.join(code_dir, d))
        for f in CODE_FILES:
            if os.path.exists(os.path.join(tmp, f)):
                os.replace(os.path.join(tmp, f), os.path.join(code_dir, f))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ver = ""
    try:
        ver = z.read("version.txt").decode("utf-8").strip()
    except Exception:
        pass
    log(f"Программа сервера обновлена до версии {ver}. Перезапускаюсь (позиции и настройки сохраняются)…")

    def restart():
        time.sleep(2)                                        # сначала ответ телефону/компьютеру
        try:
            if ROBOT and ROBOT.running():
                ROBOT.robot.request_stop(False)
                time.sleep(3)
        finally:
            os._exit(3)                                      # служба systemd поднимет сервер уже с новым кодом
    threading.Thread(target=restart, daemon=True).start()
    return ver


# ============================================================================ сертификат и ключ доступа
def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "moex-robot")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(k, hashes.SHA256()))
    with open(KEY, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(KEY, 0o600)
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def cert_fp():
    der = ssl.PEM_cert_to_DER_cert(open(CERT).read())
    return hashlib.sha256(der).hexdigest()


def load_cfg():
    try:
        cfg = json.load(open(SERVER_CFG, encoding="utf-8"))
    except Exception:
        cfg = {}
    if not cfg.get("key"):
        cfg["key"] = secrets.token_urlsafe(24)
        with open(SERVER_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.chmod(SERVER_CFG, 0o600)
    return cfg


def connect_code(host, port, key):
    return f"moexrobot://{host}:{port}/{key}/{cert_fp()}"


# ============================================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "MoexRobot"
    key = ""

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        got = self.headers.get("X-Key", "")
        if got and hmac.compare_digest(got, self.key):
            return True
        time.sleep(1.0)                                      # замедляем подбор ключа
        self._send(401, {"error": "Нужен код подключения"})
        return False

    def _body(self):
        if getattr(self, "_raw_body", None) is not None:
            return json.loads(self._raw_body or b"{}")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 12_000_000:
            raise ValueError("слишком большой запрос")
        return json.loads(self.rfile.read(n) or b"{}")

    LOCAL_API = {"/api/ping", "/api/pair_sync", "/api/relay", "/api/pair_set", "/api/pair_info", "/api/update"}

    def _proxy(self, method, raw=None):
        """Резервный сервер пересылает запросы телефона/программы активному серверу."""
        c = PAIR.client
        try:
            r = c.s.request(method, c.base + self.path, data=raw, timeout=60,
                            headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
            self._send(r.status_code, r.content, r.headers.get("Content-Type", "application/json"))
            return True
        except Exception:
            return False                                  # второй не отвечает — отвечаем сами

    def _passive(self, path):
        return PAIR and PAIR.enabled and not PAIR.active and PAIR.peer_ok and path not in self.LOCAL_API

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._static(u.path)
        if not self._auth():
            return
        if self._passive(u.path) and self._proxy("GET"):
            return
        if u.path == "/api/ping":
            r = ROBOT.robot if ROBOT.running() else None
            return self._send(200, {"active": bool(PAIR.active) if PAIR else True, "trade": bool(r and r.trade),
                                    "version": VERSION, "time": now_msk().strftime("%H:%M:%S")})
        if u.path == "/api/pair_sync" and pairmod:
            return self._send(200, pairmod.sync_payload())
        if u.path == "/api/pair_info":
            return self._send(200, PAIR.info() if PAIR else {"enabled": False})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/state":
                st = ROBOT.state()
                since = int(q.get("ev", 0))
                st["events"] = [e for e in ROBOT.events if e["id"] > since]
                lines, seq = LOG.since(int(q.get("log", 0)))
                st["log"] = [ln for _, ln in lines]
                st["log_seq"] = seq
                return self._send(200, st)
            if u.path == "/api/trades":
                return self._send(200, closed_trades())
            if u.path == "/api/results":
                return self._send(200, rc.results((ROBOT.snap or {}).get("balance")))
            if u.path == "/api/journal":
                return self._send(200, journal())
            if u.path == "/api/settings":
                return self._send(200, ROBOT.public_settings())
            if u.path == "/api/chart":
                tf = int(q.get("tf", 15))
                if tf not in rc.CHART_DEPTH:
                    raise ValueError("таймфрейм")
                return self._send(200, chart(q.get("ticker", "SBER").upper().strip(), tf))
            return self._send(404, {"error": "нет такого"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth():
            return
        if self._passive(u.path):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if self._proxy("POST", raw):
                return
            self._raw_body = raw
        try:
            b = self._body()
            if u.path in ("/api/relay", "/api/pair_set") and not pairmod:
                return self._send(404, {"error": "на сервере нет модуля пары — обновите сервер ещё раз"})
            if u.path == "/api/relay":
                return self._send(200, pairmod.do_relay(b))
            if u.path == "/api/pair_set":
                pairmod.save_pair(b.get("peer", ""), b.get("role", "primary"))
                log(f"Пара серверов настроена: этот сервер — {'основной' if b.get('role') == 'primary' else 'резервный'}. Перезапускаюсь.")
                threading.Thread(target=lambda: (time.sleep(2), os._exit(3)), daemon=True).start()
                return self._send(200, {"ok": True})
            if u.path == "/api/start":
                ok = ROBOT.start(True)
                return self._send(200, {"ok": ok, "msg": "" if ok else "Сначала введите токен песочницы в настройках."})
            if u.path == "/api/stop":
                ROBOT.stop_trade(bool(b.get("close")))
                return self._send(200, {"ok": True})
            if u.path == "/api/close":
                if not ROBOT.running():
                    return self._send(200, {"ok": False, "msg": "Робот не запущен."})
                ROBOT.robot.request_close(b.get("ticker") or None)
                log(f"С телефона запрошено закрытие: {b.get('ticker') or 'все позиции'}.")
                return self._send(200, {"ok": True})
            if u.path == "/api/settings":
                new = dict(b)
                if not new.get("token"):
                    new.pop("token", None)                   # пустое поле — токен не менять
                if not new.get("tg_token"):
                    new.pop("tg_token", None)
                ROBOT.save(new)
                return self._send(200, {"ok": True})
            if u.path == "/api/tg_test":
                tg.send_now(ROBOT.s.get("tg_token"), ROBOT.s.get("tg_chat"),
                            "✅ Сервер робота Мосбиржи: уведомления работают.")
                return self._send(200, {"ok": True})
            if u.path == "/api/tg_find":
                tok = b.get("tg_token") or ROBOT.s.get("tg_token")
                cid, name = tg.find_chat_id(tok)
                return self._send(200, {"ok": bool(cid), "chat": cid, "name": name})
            if u.path == "/api/update":
                if PAIR and PAIR.enabled and PAIR.peer_ok and not b.get("from_peer"):
                    try:                                   # и второй сервер — той же версией
                        PAIR.client.post("/api/update", dict(b, from_peer=True))
                    except Exception as e:
                        log(f"Второй сервер не обновился: {e}")
                ver = apply_update(base64.b64decode(b.get("zip", "")))
                return self._send(200, {"ok": True, "version": ver})
            if u.path == "/api/journal_fill":
                d0 = date.fromisoformat(b.get("from"))
                threading.Thread(target=self._fill, args=(d0,), daemon=True).start()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "нет такого"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    @staticmethod
    def _fill(d0):
        n = now_msk()
        d1 = n.date() if (n.hour, n.minute) >= (17, 15) else n.date() - timedelta(days=1)
        print(f"=== Дозаполнение журнала с {d0} ===", flush=True)
        tick = sc.imoex_tickers()
        d = d0
        while d <= d1:
            sc.journal_day(d, tick)
            d += timedelta(days=1)
        sc.stats()

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype, {"Content-Security-Policy":
                                              "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"})


def main():
    global ROBOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("MOEX_PORT", 8443)))
    ap.add_argument("--host", default=os.environ.get("MOEX_HOST", ""), help="адрес сервера для кода подключения")
    ap.add_argument("--print-code", action="store_true", help="только показать код подключения")
    a = ap.parse_args()
    sys.stdout = Tee(sys.stdout)
    ensure_cert()
    cfg = load_cfg()
    host = a.host or cfg.get("host") or "IP-СЕРВЕРА"
    if a.host and cfg.get("host") != a.host:
        cfg["host"] = a.host
        json.dump(cfg, open(SERVER_CFG, "w", encoding="utf-8"))
    code = connect_code(host, a.port, cfg["key"])
    with open(os.path.join(HERE, "connect_code.txt"), "w", encoding="utf-8") as f:
        f.write(code + "\n")
    if a.print_code:
        print(code)
        return
    Handler.key = cfg["key"]
    global PAIR
    ROBOT = Robot()
    PAIR = pairmod.Pair(log, ROBOT.pair_activate, ROBOT.pair_deactivate) if pairmod else None
    if PAIR and PAIR.enabled:
        sc.RELAY = PAIR.relay
        log(f"Пара серверов: этот сервер — {'основной' if PAIR.role == 'primary' else 'резервный'}, второй — {PAIR.client.host}.")
        threading.Thread(target=PAIR.loop, daemon=True, name="pair").start()
    log(f"Сервер робота, версия {VERSION}. Папка: {HERE}. Порт {a.port}.")
    log(f"Код подключения для телефона сохранён в {os.path.join(HERE, 'connect_code.txt')}")
    threading.Thread(target=ROBOT.watchdog, daemon=True, name="watchdog").start()
    if PAIR and PAIR.enabled:
        pass                                              # кто торгует — решит пара после проверки второго сервера
    elif ROBOT.s.get("token"):
        ROBOT.start(bool(ROBOT.s.get("autostart")))
    else:
        log("Токен песочницы ещё не введён — откройте приложение на телефоне → Настройки.")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    httpd = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    httpd.daemon_threads = True
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
