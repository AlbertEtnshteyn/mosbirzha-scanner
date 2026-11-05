#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сканер стратегии «продолжение сильного движения дня по рынку» для акций индекса Мосбиржи.

Правила (как в бэктесте):
  • будни, основная сессия; цена открытия дня — открытие минутной свечи 10:00;
  • в 15:00 изменение акции от открытия = закрытие свечи 14:59 / открытие 10:00 − 1;
  • сигнал, если |изменение| ≥ 2%;
  • фильтр рынка: среднее изменение всех акций индекса к 15:00 в ту же сторону;
  • вход в 15:00 по направлению движения, стоп 3% от цены входа, выход в 17:00
    (закрытие свечи 16:59); капитал дня делится поровну между сигналами.

Режимы:
  python scanner.py journal                 # после 17:15: сигналы и итог дня в журнал (без токена)
  python scanner.py journal --date 2026-10-27
  python scanner.py journal --from 2026-10-01 --to 2026-10-27   # дозаполнить пропущенные дни
  python scanner.py stats                   # статистика по журналу
  python scanner.py signals                 # в 15:00: список сигналов для ручной торговли
  python scanner.py check                   # проверка доступа к API Т-Инвестиций (нужен токен)
  python scanner.py sandbox --capital 1000000   # торговля на демо-счёте песочницы
  Ключ --entry 14:30 у journal, stats, signals и sandbox — вариант со входом в 14:30 (свой журнал).
  Ключ --threshold 2.5 — порог движения 2,5% вместо 2% (меньше сделок, выше средняя сделка; свой журнал).

Токен Т-Инвестиций берётся только из переменной окружения TINVEST_TOKEN (никуда его не пересылайте).
Для песочницы нужен токен песочницы.
"""
import argparse
import csv
import json
import os
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Установите библиотеку: pip install requests")

MSK = timezone(timedelta(hours=3))

# ---- параметры стратегии (из бэктеста) -------------------------------------
ENTRY_MIN = 15 * 60          # 15:00
EXIT_MIN = 17 * 60           # 17:00 — выход по закрытию свечи 16:59
OPEN_MIN = 10 * 60           # 10:00 — открытие основной сессии
THRESHOLD = 0.02             # 2%
STOP = 0.03                  # 3%
COST = 0.0016                # 0.16% на сделку туда-обратно (для журнала)

FALLBACK_TICKERS = [
    "AFKS", "AFLT", "ALRS", "BSPB", "CBOM", "CHMF", "CNRU", "DOMRF", "ENPG", "FLOT", "GAZP", "GMKN",
    "HEAD", "IRAO", "LKOH", "MAGN", "MDMG", "MOEX", "MTSS", "NLMK", "NVTK", "OZON", "PHOR", "PLZL",
    "POSI", "RAGR", "RENI", "ROSN", "RTKM", "RUAL", "SBER", "SBERP", "SNGS", "SNGSP", "SVCB", "T",
    "TATN", "TATNP", "TRNFP", "UGLD", "VKCO", "VTBR", "X5", "YDEX",
]

HERE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
JOURNAL = os.path.join(HERE, "forward_journal.csv")
DAYS_LOG = os.path.join(HERE, "forward_days.csv")
SANDBOX_LOG = os.path.join(HERE, "sandbox_trades.csv")
SPIKE_LOG = os.path.join(HERE, "sandbox_spikes.csv")      # сделки стратегии «всплеск объёма»
MANUAL_LOG = os.path.join(HERE, "manual_trades.csv")     # сделки, открытые вручную
STOP_EVENT = threading.Event()   # кнопка «Стоп» в приложении

# ============================================================================ защищённые соединения
# Сертификаты проверяются по трём источникам: хранилище Windows, список certifi и сертификаты,
# которым пользователь явно доверил (trusted_certs.pem рядом с программой). Последнее нужно, если
# сервер предъявляет сертификат, неизвестный Windows (российский корневой сертификат Минцифры,
# антивирус с проверкой HTTPS). Проверка при этом не отключается: доверяется один конкретный
# сертификат, любой другой будет отвергнут.
import hashlib
import socket
import ssl
import tempfile
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter

TRUST_FILE = os.path.join(HERE, "trusted_certs.pem")
# Сертификаты НУЦ Минцифры допускаются ТОЛЬКО на сервере робота (Linux, папка программы /opt/moexrobot/certs) —
# для связи с Т-Инвестициями. Программа на компьютере (Windows) их не скачивает, не хранит и не использует:
# всё, что ей нужно от Т-Инвестиций, идёт через сервер.
RU_CERTS_ALLOWED = sys.platform.startswith("linux") and not getattr(sys, "frozen", False)


def purge_ru_certs():
    """На компьютере: удалить из папки программы всё, что осталось от сертификатов Минцифры прошлых версий."""
    if RU_CERTS_ALLOWED:
        return []
    import shutil
    gone = []
    for p in (os.path.join(HERE, "certs"), TRUST_FILE):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
                gone.append(p)
            elif os.path.exists(p):
                os.remove(p)
                gone.append(p)
        except OSError:
            pass
    return gone


class CertError(RuntimeError):
    """Сервер предъявил сертификат, которому нет доверия."""
    def __init__(self, host, port=443):
        self.host = host
        self.port = port or 443
        if not RU_CERTS_ALLOWED:
            super().__init__(
                f"Сертификат сервера {host} не прошёл стандартную проверку Windows. Программа на компьютере не использует "
                "сертификаты Минцифры — запросы к Т-Инвестициям идут через сервер робота. Подключите программу к серверу "
                "(вкладка «Сервер и телефон»). Если это не Т-Инвестиции, соединение может подменять антивирус.")
            return
        super().__init__(
            f"Сертификат сервера {host} не прошёл проверку даже с сертификатами Минцифры. "
            "Скорее всего, защищённое соединение подменяет антивирус или программа фильтрации трафика. "
            "Добавьте MoexScanner.exe в исключения проверки защищённых соединений антивируса "
            "(в Kaspersky: Настройки → Сетевые параметры → Исключения; в ESET: Фильтрация SSL/TLS → Исключения) "
            "и повторите.")


def _ssl_context():
    ctx = ssl.create_default_context()          # в Windows сюда загружается хранилище системы
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    if not RU_CERTS_ALLOWED:                       # компьютер: только стандартная проверка (Windows + certifi)
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        return ctx
    # сервер робота: сертификаты НУЦ Минцифры из папки программы (в систему не ставятся)
    for base in {HERE}:
        cdir = os.path.join(base, "certs")
        if os.path.isdir(cdir):
            for fn in sorted(os.listdir(cdir)):
                if fn.endswith(".pem"):
                    try:
                        ctx.load_verify_locations(os.path.join(cdir, fn))
                    except Exception as e:
                        print(f"Сертификат {fn} не загрузился: {e}")
    if os.path.exists(TRUST_FILE):
        try:
            ctx.load_verify_locations(TRUST_FILE)
        except Exception as e:
            print("Файл доверенных сертификатов повреждён:", e)
    # доверенный сертификат может быть промежуточным, а не корневым
    ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    # строгий режим Python 3.13 отвергает часть реальных сертификатов (без расширения keyUsage и т.п.);
    # обычная проверка цепочки, срока и имени сервера при этом остаётся
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


class _CtxAdapter(HTTPAdapter):
    def __init__(self, ctx, **kw):
        self._ctx = ctx
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._ctx
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        kw["ssl_context"] = self._ctx
        return super().proxy_manager_for(*a, **kw)


_SESSION = None
RELAY = None          # запасной путь запросов через второй сервер (пара серверов): fn(method, url, kwargs) -> Response | None


class _Session(requests.Session):
    """Если запрос не прошёл (нет связи, таймаут) — повторить его через второй сервер, когда он настроен."""
    def request(self, method, url, **kw):
        try:
            return super().request(method, url, **kw)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if RELAY is None or isinstance(e, requests.exceptions.SSLError):
                raise
            try:
                r = RELAY(method, url, kw)
            except Exception:
                r = None
            if r is None:
                raise
            return r

    def direct(self, method, url, **kw):
        """Запрос только напрямую, без второго сервера (для выполнения чужих запросов)."""
        return super().request(method, url, **kw)


def http():
    global _SESSION
    if _SESSION is None:
        s = _Session()
        s.mount("https://", _CtxAdapter(_ssl_context()))
        _SESSION = s
    return _SESSION


def reset_http():
    global _SESSION
    _SESSION = None


def _is_cert_error(e):
    t = str(e)
    return "CERTIFICATE_VERIFY_FAILED" in t or "certificate verify failed" in t


def _decode_der(der):
    """Издатель, владелец и срок сертификата (без внешних библиотек)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        d = ssl._ssl._test_decode_cert(path)
    except Exception:
        d = {}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    name = lambda key: ", ".join(f"{k}={v}" for rdn in d.get(key, ()) for k, v in rdn if k in ("commonName", "organizationName", "countryName"))
    return {"pem": pem, "subject": name("subject") or "?", "issuer": name("issuer") or "?",
            "not_after": d.get("notAfter", "?"), "sha256": hashlib.sha256(der).hexdigest().upper()}


def server_chain(host, port=443):
    """Цепочка сертификатов, которую предъявляет сервер (без проверки — только чтобы показать пользователю)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            chain = []
            if hasattr(ss, "get_unverified_chain"):
                for c in ss.get_unverified_chain() or []:
                    der = c if isinstance(c, (bytes, bytearray)) else (c.public_bytes(ssl._ssl.ENCODING_DER) if hasattr(c, "public_bytes") else None)
                    if der:
                        chain.append(bytes(der))
            if not chain:
                chain = [ss.getpeercert(binary_form=True)]
    return [_decode_der(d) for d in chain]


def trust_anchor(host, port=443):
    if not RU_CERTS_ALLOWED:
        raise RuntimeError("на компьютере сертификаты не добавляются")
    """Сертификат, которому предлагаем доверять: самый верхний из предъявленных сервером."""
    chain = server_chain(host, port)
    return chain[-1], chain


def add_trusted(cert):
    note = f"# {cert['subject']} | issuer: {cert['issuer']} | SHA-256 {cert['sha256']}\n"
    with open(TRUST_FILE, "a", encoding="ascii", errors="replace") as f:
        f.write(note.encode("ascii", "replace").decode("ascii"))
        f.write(cert["pem"])
    reset_http()


RU_CERTS = {
    "russian_trusted_root_ca.pem": ("Russian Trusted Root CA",
                                    ["https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
                                     "https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer"]),
    "russian_trusted_sub_ca.pem": ("Russian Trusted Sub CA",
                                   ["https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
                                    "https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer"]),
}
_RU_TRIED = False


def fetch_ru_certs(dest=None, verbose=True):
    if not RU_CERTS_ALLOWED:
        return False
    """Скачивает сертификаты НУЦ Минцифры с сервера госуслуг в папку программы (не в Windows).
    Только по проверенному HTTPS; сохраняет, только если это действительно нужные сертификаты."""
    import urllib.request
    dest = dest or os.path.join(HERE, "certs")
    os.makedirs(dest, exist_ok=True)
    ok = 0
    for fname, (cn_need, urls) in RU_CERTS.items():
        if os.path.exists(os.path.join(dest, fname)):
            ok += 1
            continue
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=20, context=_ssl_context()).read()
                pem = data.decode("ascii", "ignore") if b"BEGIN CERTIFICATE" in data else ssl.DER_cert_to_PEM_cert(data)
                pem = pem.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n"
                info = _decode_der(ssl.PEM_cert_to_DER_cert(pem))
                if cn_need not in info["subject"]:
                    if verbose:
                        print(f"{url}: неожиданный сертификат ({info['subject']}), пропуск")
                    continue
                with open(os.path.join(dest, fname), "w", encoding="ascii") as f:
                    f.write(pem)
                if verbose:
                    print(f"Сертификат «{cn_need}» сохранён в папку программы (SHA-256 {info['sha256'][:16]}…)")
                ok += 1
                break
            except Exception as e:
                if verbose:
                    print(f"{url}: {e}")
    reset_http()
    return ok == len(RU_CERTS)


def with_ru_certs(fn):
    """Выполняет запрос; на сервере при ошибке сертификата один раз докачивает сертификаты Минцифры в папку программы.
    На компьютере ничего не докачивается."""
    global _RU_TRIED
    try:
        return fn()
    except CertError:
        if _RU_TRIED or not RU_CERTS_ALLOWED:
            raise
        _RU_TRIED = True
        print("Сертификат сервера не прошёл проверку. Загружаю сертификаты НУЦ Минцифры с госуслуг в папку программы…")
        if not fetch_ru_certs():
            raise
        print("Повторяю запрос.")
        return fn()


def explain_issuer(cert):
    txt = (cert["issuer"] + " " + cert["subject"]).lower()
    if "russian trusted" in txt or "минцифр" in txt:
        return "Это российский удостоверяющий центр Минцифры: им подписаны сертификаты многих российских сервисов."
    if any(w in txt for w in ("kaspersky", "eset", "dr.web", "drweb", "avast", "avg", "bitdefender", "norton", "fortinet")):
        return "Это сертификат антивируса: он проверяет защищённые соединения на вашем компьютере."
    return "Издатель неизвестен. Доверяйте, только если это ваш домашний компьютер и вы понимаете, откуда этот сертификат."


def configure(threshold_pct=2.0, entry="15:00"):
    """Задаёт порог и время входа; у каждой комбинации свой журнал."""
    global THRESHOLD, ENTRY_MIN, JOURNAL, DAYS_LOG, SANDBOX_LOG
    THRESHOLD = threshold_pct / 100
    hh, mm = (int(x) for x in str(entry).split(":"))
    ENTRY_MIN = hh * 60 + mm
    global LATE_CUTOFF_MIN
    LATE_CUTOFF_MIN = min(max(16 * 60 + 30, ENTRY_MIN + 60), 18 * 60 + 30)   # опоздавший вход — не позже чем через час
    sfx = (f"_{hh:02d}{mm:02d}" if ENTRY_MIN != 15 * 60 else "") + (f"_t{threshold_pct:g}".replace(".", "_") if threshold_pct != 2.0 else "")
    JOURNAL = os.path.join(HERE, f"forward_journal{sfx}.csv")
    DAYS_LOG = os.path.join(HERE, f"forward_days{sfx}.csv")
    SANDBOX_LOG = os.path.join(HERE, f"sandbox_trades{sfx}.csv")
    global SPIKE_LOG
    SPIKE_LOG = os.path.join(HERE, "sandbox_spikes.csv")


# ============================================================================ ядро
def minute_of(ts):
    return ts.hour * 60 + ts.minute


LAST_MOVES = {}
ENTRY_CHOICES = [f"{h:02d}:00" for h in range(9, 19)] + ["14:30"]   # 09:00–18:00 по часам (+14:30 — проверенный вариант)
ENTRY_CHOICES.sort()
THRESHOLD_CHOICES = [1.0, 1.5, 2.0, 2.5]
BACKTEST = {  # (вход, порог %) -> (средняя сделка %, итог %, макс. просадка %, сделок) за 10.2025–10.2026, затраты 0,16%
    ("12:00", 1): (-0.2, -33.7, -37.4, 2061),
    ("12:00", 1.5): (-0.17, -11.9, -21.0, 919),
    ("12:00", 2): (-0.09, -0.1, -11.0, 426),
    ("12:00", 2.5): (-0.16, 6.4, -9.5, 204),
    ("13:00", 1): (-0.06, -8.1, -18.2, 2529),
    ("13:00", 1.5): (-0.05, 7.8, -10.2, 1307),
    ("13:00", 2): (-0.06, 3.4, -16.5, 679),
    ("13:00", 2.5): (-0.01, 14.5, -13.6, 360),
    ("14:00", 1): (-0.02, 1.8, -12.9, 3003),
    ("14:00", 1.5): (-0.02, 12.3, -10.1, 1584),
    ("14:00", 2): (0.04, 37.0, -5.7, 855),
    ("14:00", 2.5): (0.08, 40.4, -6.1, 483),
    ("14:30", 1): (0.02, 4.5, -8.1, 3033),
    ("14:30", 1.5): (0.02, 16.7, -9.1, 1649),
    ("14:30", 2): (0.04, 21.8, -13.0, 905),
    ("14:30", 2.5): (0.08, 38.1, -9.8, 522),
    ("15:00", 1): (0.04, -1.2, -9.5, 3288),
    ("15:00", 1.5): (0.1, 8.1, -6.4, 1789),
    ("15:00", 2): (0.15, 23.3, -6.9, 992),
    ("15:00", 2.5): (0.22, 35.3, -6.6, 587),
}
LATE_OK_MIN = 5              # опоздание до 5 мин — обычный вход
LATE_CUTOFF_MIN = 16 * 60 + 30   # позже 16:30 не входим: до выхода в 17:00 слишком мало времени


def report_movers(market, top=5):
    """Пять бумаг с наибольшим движением и почему каждая стала или не стала сигналом."""
    if not LAST_MOVES:
        return
    print(f"Самые сильные движения к {ENTRY_MIN // 60}:{ENTRY_MIN % 60:02d} (порог {THRESHOLD * 100:g}%, рынок {market * 100:+.2f}%):")
    for tk, mv in sorted(LAST_MOVES.items(), key=lambda x: -abs(x[1]))[:top]:
        if abs(mv) < THRESHOLD:
            why = f"меньше порога {THRESHOLD * 100:g}%"
        elif (mv > 0) != (market > 0):
            why = "против направления рынка — фильтр"
        else:
            why = "сигнал"
        print(f"  {tk:6s} {mv * 100:+.2f}%  — {why}")


def entry_wait():
    """Ждёт времени входа, если оно ещё не наступило. False — нажали «Стоп»."""
    now = datetime.now(MSK)
    if (now.hour * 60 + now.minute) < ENTRY_MIN:
        print(f"Жду {ENTRY_MIN // 60}:{ENTRY_MIN % 60:02d}:05 МСК…", flush=True)
        if not wait_until(ENTRY_MIN // 60, ENTRY_MIN % 60, 5):
            print("Остановлено.")
            return False
    return True


def compute_day(candles, threshold=None, stop=STOP, cost=COST, with_result=True, entry_min=None):
    """
    candles: {ticker: [ {t: datetime(МСК), o, h, l, c}, ... ]} — минутки одного дня.
    Возвращает (market_move, signals). Сигнал: ticker, side (+1/-1), move, entry, stop_price,
    и при with_result — exit, reason, gross, net.
    Если в какую-то минуту сделок не было, берётся ближайшая свеча: открытие — первая свеча
    с 10:00 до 10:05, цена к моменту входа — последняя свеча за 10 минут до входа.
    """
    ENTRY = entry_min or ENTRY_MIN
    threshold = threshold or THRESHOLD
    moves = {}
    for tk, rows in candles.items():
        main = [r for r in rows if OPEN_MIN <= minute_of(r["t"]) < ENTRY]
        if not main or minute_of(main[0]["t"]) > OPEN_MIN + 5:
            continue
        if minute_of(main[-1]["t"]) < ENTRY - 10:
            continue
        moves[tk] = main[-1]["c"] / main[0]["o"] - 1
    global LAST_MOVES
    LAST_MOVES = dict(moves)
    if not moves:
        return None, []
    market = sum(moves.values()) / len(moves)
    signals = []
    for tk, mv in sorted(moves.items(), key=lambda x: -abs(x[1])):
        if abs(mv) < threshold:
            continue
        side = 1 if mv > 0 else -1
        if (market > 0) != (side > 0) or market == 0:
            continue
        sig = {"ticker": tk, "side": side, "move": mv}
        if with_result:
            rows = [r for r in candles[tk] if ENTRY <= minute_of(r["t"]) < EXIT_MIN]
            if not rows or minute_of(rows[0]["t"]) > ENTRY + 2:
                continue
            entry = rows[0]["o"]
            sp = entry * (1 - side * stop)
            res, reason, exit_px = None, "time", rows[-1]["c"]
            for r in rows:
                adverse = r["l"] <= sp if side > 0 else r["h"] >= sp
                if adverse:
                    res, reason, exit_px = -stop, "stop", sp
                    break
            if res is None:
                res = side * (exit_px / entry - 1)
            sig.update(entry=entry, stop_price=sp, exit=exit_px, reason=reason, gross=res, net=res - cost)
        signals.append(sig)
    return market, signals


# ============================================================================ данные Мосбиржи
ISS = "https://iss.moex.com/iss"


def iss_get(path, params, retries=4):
    for i in range(retries):
        try:
            def _get():
                try:
                    return http().get(ISS + path, params={**params, "iss.meta": "off"}, timeout=30)
                except requests.exceptions.SSLError as e:
                    if _is_cert_error(e):
                        raise CertError(urlparse(ISS).hostname, urlparse(ISS).port) from None
                    raise
            r = with_ru_certs(_get)
            r.raise_for_status()
            return r.json()
        except CertError:
            raise
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


TBANK_TERMINAL = "https://www.tbank.ru/invest/terminal/"


def tbank_url(ticker, kind="share"):
    """Страница инструмента в Т-Инвестициях (оттуда — кнопка «Купить/Продать» и переход в терминал)."""
    sect = {"future": "futures", "currency": "currencies"}.get(kind, "stocks")
    return f"https://www.tbank.ru/invest/{sect}/{ticker}/"


BLUE_CHIPS = ["SBER", "GAZP", "LKOH", "GMKN", "NVTK", "ROSN", "TATN", "YDEX", "PLZL", "SNGSP", "MTSS", "CHMF",
              "NLMK", "T", "PHOR"]


def index_members(code, fallback=None, min_n=10):
    """Состав индекса Мосбиржи (IMOEX, MOEXBC — голубые фишки, MOEXBMI — широкий рынок)."""
    try:
        out, start = set(), 0
        while True:
            js = iss_get(f"/statistics/engines/stock/markets/index/analytics/{code}.json", {"limit": 100, "start": start})
            b = js["analytics"]
            key = "secids" if "secids" in b["columns"] else "ticker"
            k = b["columns"].index(key)
            rows = [row[k] for row in b["data"] if row[k]]
            out.update(rows)
            if len(b["data"]) < 100:
                break
            start += 100
        if len(out) >= min_n:
            return sorted(out)
    except CertError:
        raise
    except Exception as e:
        print(f"Состав индекса {code} не получен ({e}), беру запасной список.")
    return fallback


def index_quote(code="IMOEX"):
    """Индекс Мосбиржи сейчас: значение, изменение к закрытию прошлого дня (пункты и %), время обновления."""
    js = iss_get(f"/engines/stock/markets/index/boards/SNDX/securities/{code}.json", {"iss.only": "marketdata"}, retries=1)
    md = js["marketdata"]
    if not md["data"]:
        return None
    x = dict(zip(md["columns"], md["data"][0]))
    val = x.get("CURRENTVALUE") or x.get("LASTVALUE")
    if not val:
        return None
    return {"code": code, "value": float(val), "chg": float(x.get("LASTCHANGE") or 0),
            "pct": float(x.get("LASTCHANGEPRC") or 0), "time": str(x.get("UPDATETIME") or x.get("TIME") or "")[:5]}


_IDX_OPEN = {}


def index_open(code="IMOEX", main=True):
    """Значение индекса на открытии основной сессии (10:00) сегодня; до 10:00 или main=False — на открытии утренней сессии."""
    now = datetime.now(MSK)
    key = (code, now.date(), main and now.hour >= 10)
    if key in _IDX_OPEN:
        return _IDX_OPEN[key]
    js = iss_get(f"/engines/stock/markets/index/securities/{code}/candles.json",
                 {"from": now.date().isoformat(), "till": now.date().isoformat(), "interval": 1}, retries=1)
    c = js["candles"]
    rows = [dict(zip(c["columns"], r)) for r in c["data"]]
    if key[2]:
        rows = [r for r in rows if str(r.get("begin", ""))[11:16] >= "10:00"]
    val = float(rows[0]["open"]) if rows else None
    if val:
        _IDX_OPEN[key] = val
    return val


def imoex_tickers():
    try:
        js = iss_get("/statistics/engines/stock/markets/index/analytics/IMOEX.json", {"limit": 100})
        b = js["analytics"]
        key = "secids" if "secids" in b["columns"] else "ticker"
        k = b["columns"].index(key)
        t = sorted({row[k] for row in b["data"] if row[k]})
        if len(t) >= 20:
            return t
    except CertError:
        raise
    except Exception as e:
        print("Состав индекса не получен, беру список из бэктеста:", e)
    return FALLBACK_TICKERS


def iss_candles(ticker, day):
    url = f"/engines/stock/markets/shares/boards/TQBR/securities/{ticker}/candles.json"
    out, start = [], 0
    while True:
        js = iss_get(url, {"interval": 1, "from": day.isoformat(), "till": day.isoformat(), "start": start})
        c = js["candles"]
        if not c["data"]:
            break
        cols = c["columns"]
        for row in c["data"]:
            d = dict(zip(cols, row))
            t = datetime.strptime(d["begin"], "%Y-%m-%d %H:%M:%S")
            out.append({"t": t, "o": float(d["open"]), "h": float(d["high"]), "l": float(d["low"]), "c": float(d["close"])})
        start += len(c["data"])
        if len(c["data"]) < 500:
            break
    return out


def iss_day(day, tickers):
    data = {}
    for tk in tickers:
        try:
            rows = iss_candles(tk, day)
            if rows:
                data[tk] = rows
        except CertError:
            raise
        except Exception as e:
            print(f"  {tk}: не загрузилось ({e})")
    return data


# ============================================================================ API Т-Инвестиций (REST)
TINV_HOSTS = {"prod": "https://invest-public-api.tinkoff.ru/rest/",
              "sandbox": "https://sandbox-invest-public-api.tinkoff.ru/rest/"}
SVC = "tinkoff.public.invest.api.contract.v1."


class TInvest:
    def __init__(self, token, env="sandbox"):
        self.base = TINV_HOSTS[env]
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        self._shares = {}

    def call(self, service, method, body):
        def _post():
            try:
                return http().post(f"{self.base}{SVC}{service}/{method}", headers=self.h, data=json.dumps(body), timeout=30)
            except requests.exceptions.SSLError as e:
                if _is_cert_error(e):
                    raise CertError(urlparse(self.base).hostname, urlparse(self.base).port) from None
                raise RuntimeError(f"Ошибка защищённого соединения с {self.base}: {e}") from None
            except requests.exceptions.ConnectionError as e:
                raise RuntimeError(f"Нет соединения с сервером Т-Инвестиций ({self.base}). Проверьте интернет. Детали: {e}") from None
        for attempt in range(4):
            r = with_ru_certs(_post)
            if r.status_code == 429:          # превышен лимит запросов — ждём сброса
                try:
                    wait = float(r.headers.get("x-ratelimit-reset", 5))
                except ValueError:
                    wait = 5
                time.sleep(min(max(wait, 1), 60))
                continue
            break
        if r.status_code != 200:
            raise RuntimeError(f"{service}/{method}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def all_shares(self):
        """Все акции Мосбиржи (режим TQBR), доступные для торговли через API. Заодно кэширует лоты и шорт."""
        js = self.call("InstrumentsService", "Shares", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        out = []
        for ins in js.get("instruments", []):
            if ins.get("classCode") != "TQBR" or str(ins.get("currency", "")).lower() != "rub":
                continue
            if ins.get("apiTradeAvailableFlag") is False or ins.get("buyAvailableFlag") is False:
                continue
            if ins.get("forQualInvestorFlag"):
                continue
            tk = ins["ticker"]
            self._shares[tk] = {"uid": ins["uid"], "lot": int(ins.get("lot", 1)), "short": bool(ins.get("shortEnabledFlag", False)),
                                "kind": "share", "pv": 1.0,
                                "dlong": self.q(ins.get("dlong")), "dshort": self.q(ins.get("dshort"))}
            out.append(tk)
        return sorted(out)

    def all_currencies(self):
        """Валюты и драгметаллы валютного рынка Мосбиржи (CETS): доллар, юань, золото, серебро и др."""
        js = self.call("InstrumentsService", "Currencies", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        out = []
        for ins in js.get("instruments", []):
            if ins.get("classCode") != "CETS" or ins.get("apiTradeAvailableFlag") is False:
                continue
            tk = ins["ticker"]
            if not tk.endswith("TOM"):          # только расчёты «завтра» — основной ликвидный инструмент
                continue
            self._shares[tk] = {"uid": ins["uid"], "lot": int(ins.get("lot", 1)), "short": bool(ins.get("shortEnabledFlag", False)),
                                "kind": "currency", "pv": 1.0, "name": ins.get("name", tk)}
            out.append(tk)
        return sorted(out)

    def nearest_futures(self, asset_types=None):
        """Ближайшие фьючерсы по каждому базовому активу (не истекающие в ближайшие 3 дня)."""
        js = self.call("InstrumentsService", "Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        best = {}
        for ins in js.get("instruments", []):
            if ins.get("classCode") != "SPBFUT" or ins.get("apiTradeAvailableFlag") is False:
                continue
            if asset_types and ins.get("assetType") not in asset_types:
                continue
            try:
                exp = datetime.fromisoformat(ins["expirationDate"].replace("Z", "+00:00"))
            except Exception:
                continue
            if exp < soon:
                continue
            key = ins.get("basicAsset") or ins["ticker"][:2]
            if key not in best or exp < best[key][0]:
                best[key] = (exp, ins)
        out = []
        for exp, ins in best.values():
            tk = ins["ticker"]
            self._shares[tk] = {"uid": ins["uid"], "lot": int(ins.get("lot", 1)), "short": True, "kind": "future",
                                "pv": None, "name": ins.get("name", tk), "asset": ins.get("assetType", "")}
            out.append(tk)
        return sorted(out)

    def future_terms(self, ticker):
        """Стоимость пункта цены (₽ за 1 единицу цены) и гарантийное обеспечение на 1 контракт."""
        sh = self.share(ticker)
        if sh.get("pv") and sh.get("margin"):
            return sh["pv"], sh["margin"]
        js = self.call("InstrumentsService", "GetFuturesMargin", {"instrumentId": sh["uid"]})
        step = self.q(js.get("minPriceIncrement"))
        step_rub = self.q(js.get("minPriceIncrementAmount"))
        sh["pv"] = step_rub / step if step else 1.0
        sh["margin"] = max(self.q(js.get("initialMarginOnBuy")), self.q(js.get("initialMarginOnSell")))
        return sh["pv"], sh["margin"]

    @staticmethod
    def q(x):
        """Quotation/MoneyValue {units, nano} -> float."""
        if not x:
            return 0.0
        return int(x.get("units", 0)) + int(x.get("nano", 0)) / 1e9

    def share(self, ticker):
        if ticker not in self._shares:
            js = self.call("InstrumentsService", "ShareBy",
                           {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": "TQBR", "id": ticker})
            ins = js["instrument"]
            self._shares[ticker] = {"uid": ins["uid"], "lot": int(ins.get("lot", 1)),
                                    "short": bool(ins.get("shortEnabledFlag", False)), "kind": "share", "pv": 1.0,
                                "dlong": self.q(ins.get("dlong")), "dshort": self.q(ins.get("dshort"))}
        return self._shares[ticker]

    INTERVALS = {1: "CANDLE_INTERVAL_1_MIN", 5: "CANDLE_INTERVAL_5_MIN", 15: "CANDLE_INTERVAL_15_MIN", 60: "CANDLE_INTERVAL_HOUR",
                 1440: "CANDLE_INTERVAL_DAY"}

    def candles_range(self, ticker, frm, to, tf):
        """Свечи tf за период frm..to (с поясом); запросы режутся по дням (дневные — по году)."""
        out, step = [], timedelta(days=365 if tf >= 1440 else (7 if tf >= 60 else 1))
        a = frm
        while a < to:
            b = min(a + step, to)
            out += self.candles(ticker, a.date(), a, b, tf)
            a = b
        seen, res = set(), []
        for r in out:
            if r["t"] not in seen:
                seen.add(r["t"]); res.append(r)
        return res

    def candles(self, ticker, day, frm=None, to=None, tf=1):
        """Свечи tf минут (время МСК без пояса). v — объём в лотах. frm/to — время с поясом (по умолчанию весь день)."""
        uid = self.share(ticker)["uid"]
        frm = (frm or datetime(day.year, day.month, day.day, 9, 55, tzinfo=MSK)).astimezone(timezone.utc)
        to = (to or datetime(day.year, day.month, day.day, 18, 45, tzinfo=MSK)).astimezone(timezone.utc)
        js = self.call("MarketDataService", "GetCandles",
                       {"instrumentId": uid, "from": frm.isoformat().replace("+00:00", "Z"),
                        "to": to.isoformat().replace("+00:00", "Z"), "interval": self.INTERVALS[tf]})
        out = []
        for c in js.get("candles", []):
            t = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).astimezone(MSK).replace(tzinfo=None)
            out.append({"t": t, "o": self.q(c["open"]), "h": self.q(c["high"]), "l": self.q(c["low"]), "c": self.q(c["close"]),
                        "v": int(c.get("volume", 0) or 0)})
        return out

    def last_prices(self, tickers):
        uids = {self.share(t)["uid"]: t for t in tickers}
        js = self.call("MarketDataService", "GetLastPrices", {"instrumentId": list(uids)})
        out = {}
        for p in js.get("lastPrices", []):
            t = uids.get(p.get("instrumentUid"))
            if t:
                out[t] = self.q(p.get("price"))
        return out

    # --- песочница
    def sandbox_account(self):
        js = self.call("SandboxService", "GetSandboxAccounts", {})
        accs = js.get("accounts", [])
        if accs:
            return accs[0]["id"]
        return self.call("SandboxService", "OpenSandboxAccount", {})["accountId"]

    def sandbox_pay_in(self, acc, rub):
        self.call("SandboxService", "SandboxPayIn",
                  {"accountId": acc, "amount": {"currency": "rub", "units": str(int(rub)), "nano": 0}})

    def sandbox_live_value(self, acc):
        """Стоимость счёта по текущим ценам: деньги + позиции × последняя цена (в песочнице цены в портфеле
        обновляются редко, поэтому акции переоцениваем по GetLastPrices). Шорт — отрицательное количество."""
        js = self.call("SandboxService", "GetSandboxPortfolio", {"accountId": acc, "currency": "RUB"})
        total = self.q(js.get("totalAmountCurrencies"))
        pos = [p for p in js.get("positions", []) if p.get("instrumentType") != "currency"]
        uids = [p.get("instrumentUid") for p in pos if p.get("instrumentUid")]
        last = {}
        if uids:
            try:
                lp = self.call("MarketDataService", "GetLastPrices", {"instrumentId": uids})
                last = {x.get("instrumentUid"): self.q(x.get("price")) for x in lp.get("lastPrices", [])}
            except Exception:
                pass
        for p in pos:
            qty = self.q(p.get("quantity"))
            px = last.get(p.get("instrumentUid")) if p.get("instrumentType") == "share" else None
            px = px or self.q(p.get("currentPrice"))
            total += qty * px
        return total or self.q(js.get("totalAmountPortfolio"))

    # --- реальный брокерский счёт (токен с полным доступом из личного кабинета Т-Инвестиций)
    def real_accounts(self):
        js = self.call("UsersService", "GetAccounts", {})
        return [a for a in js.get("accounts", []) if a.get("status") == "ACCOUNT_STATUS_OPEN"]

    def real_live_value(self, acc):
        js = self.call("OperationsService", "GetPortfolio", {"accountId": acc, "currency": "RUB"})
        total = self.q(js.get("totalAmountCurrencies"))
        pos = [p for p in js.get("positions", []) if p.get("instrumentType") != "currency"]
        uids = [p.get("instrumentUid") for p in pos if p.get("instrumentUid")]
        last = {}
        if uids:
            try:
                lp = self.call("MarketDataService", "GetLastPrices", {"instrumentId": uids})
                last = {x.get("instrumentUid"): self.q(x.get("price")) for x in lp.get("lastPrices", [])}
            except Exception:
                pass
        for p in pos:
            qty = self.q(p.get("quantity"))
            px = last.get(p.get("instrumentUid")) if p.get("instrumentType") == "share" else None
            total += qty * (px or self.q(p.get("currentPrice")))
        return total or self.q(js.get("totalAmountPortfolio"))

    def real_portfolio_value(self, acc):
        js = self.call("OperationsService", "GetPortfolio", {"accountId": acc, "currency": "RUB"})
        return self.q(js.get("totalAmountPortfolio"))

    def real_order(self, acc, ticker, lots, side):
        return self.call("OrdersService", "PostOrder",
                         {"accountId": acc, "instrumentId": self.share(ticker)["uid"], "quantity": str(lots),
                          "direction": "ORDER_DIRECTION_BUY" if side > 0 else "ORDER_DIRECTION_SELL",
                          "orderType": "ORDER_TYPE_MARKET", "orderId": str(uuid.uuid4())})

    def sandbox_portfolio_value(self, acc):
        js = self.call("SandboxService", "GetSandboxPortfolio", {"accountId": acc, "currency": "RUB"})
        return self.q(js.get("totalAmountPortfolio"))

    def sandbox_order(self, acc, ticker, lots, side):
        js = self.call("SandboxService", "PostSandboxOrder",
                       {"accountId": acc, "instrumentId": self.share(ticker)["uid"], "quantity": str(lots),
                        "direction": "ORDER_DIRECTION_BUY" if side > 0 else "ORDER_DIRECTION_SELL",
                        "orderType": "ORDER_TYPE_MARKET", "orderId": str(uuid.uuid4())})
        return js


# ============================================================================ календарь торгов
DAY_KINDS = {"trading": "торговый день", "weekend_session": "сессия выходного дня", "holiday": "праздник — биржа закрыта",
             "weekend": "выходной — биржа закрыта"}


class TradingCalendar:
    """Календарь Мосбиржи на ближайшие 2 недели по расписанию торгов из Т-Инвестиций (TradingSchedules):
    торговый день, праздник, выходной или торговая сессия выходного дня. Если расписание не получено —
    будни считаются торговыми, суббота и воскресенье — выходными."""
    def __init__(self, ti=None):
        self.ti = ti
        self.days = {}            # date -> {"kind", "start", "end"}
        self.loaded = None        # дата последней загрузки
        self.source = "по дням недели"

    @staticmethod
    def _fallback(d):
        return {"kind": "trading" if d.weekday() < 5 else "weekend", "start": "", "end": ""}

    def _load(self, today):
        if self.loaded == today:
            return
        self.loaded = today
        if self.ti is None:
            return
        try:
            frm = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
            js = self.ti.call("InstrumentsService", "TradingSchedules",
                              {"from": frm.isoformat().replace("+00:00", "Z"),
                               "to": (frm + timedelta(days=13)).isoformat().replace("+00:00", "Z")})
        except Exception:
            return
        main, wk = {}, {}
        for ex in js.get("exchanges", []):
            name = str(ex.get("exchange", "")).upper()
            for d in ex.get("days", []):
                try:
                    day = date.fromisoformat(str(d.get("date", ""))[:10])
                except ValueError:
                    continue
                if not d.get("isTradingDay"):
                    continue
                times = ("", "")
                try:
                    st = datetime.fromisoformat(d["startTime"].replace("Z", "+00:00")).astimezone(MSK)
                    en = datetime.fromisoformat(d["endTime"].replace("Z", "+00:00")).astimezone(MSK)
                    times = (st.strftime("%H:%M"), en.strftime("%H:%M"))
                except Exception:
                    pass
                if name == "MOEX":
                    main[day] = times
                elif "MOEX" in name and "WEEKEND" in name:
                    wk.setdefault(day, times)
        if not main and not wk:
            return
        out = {}
        for i in range(14):
            d = today + timedelta(days=i)
            if d in main and d.weekday() < 5:
                out[d] = {"kind": "trading", "start": main[d][0], "end": main[d][1]}
            elif d in main or d in wk:
                t = main.get(d) or wk.get(d)
                out[d] = {"kind": "weekend_session", "start": t[0], "end": t[1]}
            else:
                out[d] = {"kind": "holiday" if d.weekday() < 5 else "weekend", "start": "", "end": ""}
        self.days = out
        self.source = "расписание Мосбиржи"

    def day(self, d):
        self._load(datetime.now(MSK).date())
        return self.days.get(d) or self._fallback(d)

    def kind(self, d):
        return self.day(d)["kind"]

    def next_trading(self, d, weekends=False):
        for i in range(1, 15):
            x = d + timedelta(days=i)
            k = self.kind(x)
            if k == "trading" or (weekends and k == "weekend_session"):
                return x
        return None

    def listing(self, n=14):
        today = datetime.now(MSK).date()
        self._load(today)
        names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        out = []
        for i in range(n):
            d = today + timedelta(days=i)
            x = self.day(d)
            out.append({"date": d.isoformat(), "dow": names[d.weekday()], "kind": x["kind"], "name": DAY_KINDS[x["kind"]],
                        "start": x["start"], "end": x["end"]})
        return {"days": out, "source": self.source}


# ============================================================================ Финам (Trade API, REST)
class Finam:
    """Торговый счёт Финама через Trade API: https://api.finam.ru/v1. Секретный токен из личного кабинета
    обменивается на сессионный JWT (живёт ~15 минут, обновляем заранее). Количество в заявках — в штуках."""
    BASE = "https://api.finam.ru/v1"
    MIC = "MISX"                                         # акции Мосбиржи: SBER@MISX

    def __init__(self, secret, account_id=""):
        self.secret = (secret or "").strip()
        self.account = str(account_id or "").strip()
        self.jwt, self.jwt_t = None, 0.0

    @staticmethod
    def dec(x):
        if isinstance(x, dict):
            x = x.get("value", x.get("units"))
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    def _req(self, method, path, body=None, params=None, auth=True):
        url = self.BASE + path
        for attempt in range(3):
            h = {"Content-Type": "application/json", "Accept": "application/json"}
            if auth:
                h["Authorization"] = self.token()
            try:
                r = with_ru_certs(lambda: http().request(method, url, headers=h, params=params,
                                                         data=json.dumps(body) if body is not None else None, timeout=30))
            except requests.exceptions.SSLError as e:
                if _is_cert_error(e):
                    raise CertError("api.finam.ru", 443) from None
                raise RuntimeError(f"Ошибка защищённого соединения с Финамом: {e}") from None
            except requests.exceptions.ConnectionError as e:
                raise RuntimeError(f"Нет соединения с Финамом (api.finam.ru): {e}") from None
            if r.status_code == 429:
                time.sleep(5)
                continue
            if r.status_code == 401 and auth and attempt == 0:
                self.jwt = None                         # сессия истекла — получить новую
                continue
            break
        if r.status_code != 200:
            try:
                msg = r.json().get("message") or r.text[:300]
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"Финам {method} {path}: HTTP {r.status_code} {msg}")
        return r.json() if r.content else {}

    def token(self):
        if not self.secret:
            raise RuntimeError("не введён секретный токен Финама (Trade API)")
        if not self.jwt or time.time() - self.jwt_t > 600:
            js = self._req("POST", "/sessions", {"secret": self.secret}, auth=False)
            self.jwt, self.jwt_t = js.get("token"), time.time()
            if not self.jwt:
                raise RuntimeError("Финам не выдал сессию — проверьте секретный токен")
        return self.jwt

    def details(self):
        return self._req("POST", "/sessions/details", {"token": self.token()}, auth=False)

    def accounts(self):
        return [str(a) for a in self.details().get("accountIds", [])]

    def ensure_account(self):
        accs = self.accounts()
        if not accs:
            raise RuntimeError("у токена Финама нет доступа ни к одному счёту")
        if self.account and self.account not in accs:
            raise RuntimeError(f"счёт {self.account} не найден у этого токена (доступны: {', '.join(accs)})")
        if not self.account:
            self.account = accs[0]
        return self.account

    def account_info(self):
        return self._req("GET", f"/accounts/{self.account}")

    def equity(self):
        js = self.account_info()
        eq = self.dec(js.get("equity"))
        if eq:
            return eq
        cash = sum(self.dec(c) + (c.get("nanos", 0) or 0) / 1e9 for c in js.get("cash", []) if isinstance(c, dict))
        return cash + sum(self.dec(p.get("quantity")) * self.dec(p.get("currentPrice")) for p in js.get("positions", []))

    def symbol(self, tk):
        return tk if "@" in tk else f"{tk}@{self.MIC}"

    def market_order(self, tk, qty, side):
        """Рыночная заявка на qty штук. Возвращает цену исполнения (средняя по сделкам) или None."""
        cid = uuid.uuid4().hex[:20]
        js = self._req("POST", f"/accounts/{self.account}/orders", {
            "symbol": self.symbol(tk), "quantity": {"value": str(int(qty))},
            "side": "SIDE_BUY" if side > 0 else "SIDE_SELL", "type": "ORDER_TYPE_MARKET",
            "timeInForce": "TIME_IN_FORCE_DAY", "clientOrderId": cid})
        oid = js.get("orderId")
        st = js.get("status", "")
        for _ in range(10):                              # дождаться исполнения (рыночная — обычно сразу)
            if st in ("ORDER_STATUS_FILLED",) or not oid:
                break
            if st in ("ORDER_STATUS_REJECTED", "ORDER_STATUS_CANCELED", "ORDER_STATUS_FAILED", "ORDER_STATUS_EXPIRED"):
                raise RuntimeError(f"Финам отклонил заявку: {js.get('statusDescription') or st}")
            time.sleep(0.5)
            try:
                js = self._req("GET", f"/accounts/{self.account}/orders/{oid}")
                st = js.get("status", "")
            except Exception:
                break
        return self._fill_price(oid)

    def _fill_price(self, oid):
        if not oid:
            return None
        try:
            now = datetime.now(timezone.utc)
            js = self._req("GET", f"/accounts/{self.account}/trades", params={
                "interval.start_time": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                "interval.end_time": (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"), "limit": 100})
            fills = [t for t in js.get("trades", []) if str(t.get("orderId")) == str(oid)]
            q = sum(self.dec(t.get("size")) for t in fills)
            if q:
                return sum(self.dec(t.get("price")) * self.dec(t.get("size")) for t in fills) / q
        except Exception:
            pass
        return None


# ============================================================================ журнал
JOURNAL_COLS = ["date", "ticker", "side", "move_pct", "market_pct", "entry", "stop_price", "exit", "reason",
                "gross_pct", "net_pct"]


def read_journal_dates():
    done = set()
    if os.path.exists(DAYS_LOG):
        with open(DAYS_LOG, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                done.add(r["date"])
    return done


def append_rows(path, cols, rows):
    new = not os.path.exists(path)
    if not new:                                   # в журнал добавились колонки — переписать старый файл с новой шапкой
        with open(path, encoding="utf-8") as f:
            head = f.readline().strip().split(",")
        if head != cols and set(head) <= set(cols):
            with open(path, encoding="utf-8") as f:
                old_rows = list(csv.DictReader(f))
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in old_rows:
                    w.writerow({k: r.get(k, "") for k in cols})
            os.replace(tmp, path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def journal_day(day, tickers, force=False):
    if day.weekday() >= 5:
        return
    if day.isoformat() in read_journal_dates() and not force:
        print(f"{day}: уже в журнале")
        return
    print(f"{day}: загружаю минутки {len(tickers)} бумаг…", flush=True)
    data = iss_day(day, tickers)
    if not data:
        print(f"{day}: данных нет (выходной или праздник)")
        return
    market, sigs = compute_day(data)
    if market is None:
        print(f"{day}: нет данных к 15:00")
        return
    rows = [{"date": day.isoformat(), "ticker": s["ticker"], "side": "лонг" if s["side"] > 0 else "шорт",
             "move_pct": round(s["move"] * 100, 3), "market_pct": round(market * 100, 3),
             "entry": s["entry"], "stop_price": round(s["stop_price"], 6), "exit": s["exit"], "reason": s["reason"],
             "gross_pct": round(s["gross"] * 100, 4), "net_pct": round(s["net"] * 100, 4)} for s in sigs]
    if rows:
        append_rows(JOURNAL, JOURNAL_COLS, rows)
    day_ret = sum(s["net"] for s in sigs) / len(sigs) if sigs else 0.0
    append_rows(DAYS_LOG, ["date", "market_pct", "signals", "day_net_pct"],
                [{"date": day.isoformat(), "market_pct": round(market * 100, 3), "signals": len(sigs),
                  "day_net_pct": round(day_ret * 100, 4)}])
    print(f"{day}: рынок к моменту входа {market * 100:+.2f}%, сигналов {len(sigs)}, итог дня {day_ret * 100:+.3f}%")
    for s in sigs:
        print(f"   {s['ticker']:6s} {'лонг' if s['side'] > 0 else 'шорт'}  движение {s['move'] * 100:+.2f}%  "
              f"вход {s['entry']}  выход {s['exit']:.4g} ({'стоп' if s['reason'] == 'stop' else '17:00'})  "
              f"после затрат {s['net'] * 100:+.2f}%")


def stats():
    if not os.path.exists(DAYS_LOG):
        print("Журнал пуст. Запустите: python scanner.py journal")
        return
    with open(DAYS_LOG, encoding="utf-8") as f:
        days = sorted(csv.DictReader(f), key=lambda r: r["date"])
    eq, peak, dd, n_tr, active = 1.0, 1.0, 0.0, 0, 0
    for d in days:
        r = float(d["day_net_pct"]) / 100
        n = int(d["signals"])
        if n:
            active += 1
            n_tr += n
        eq *= 1 + r
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    wins = 0
    if os.path.exists(JOURNAL):
        with open(JOURNAL, encoding="utf-8") as f:
            nets = [float(r["net_pct"]) for r in csv.DictReader(f)]
        wins = sum(1 for x in nets if x > 0)
    print(f"Дней в журнале: {len(days)} ({days[0]['date']} — {days[-1]['date']}), дней с сигналами: {active}")
    print(f"Сделок: {n_tr}, в плюсе: {wins} ({(wins / n_tr * 100) if n_tr else 0:.0f}%)")
    print(f"Итог (капитал дня делится поровну между сигналами, затраты {COST * 100:.2f}%): {(eq - 1) * 100:+.2f}%")
    print(f"Макс. просадка: {dd * 100:.2f}%")
    print("Для сравнения, бэктест: в среднем +0,16% на сделку после затрат, около 5 сделок в день с сигналами.")


# ============================================================================ сигналы в 15:00
def wait_until(hh, mm, ss=0):
    """Ждёт времени МСК. Возвращает False, если нажали «Стоп»."""
    while True:
        if STOP_EVENT.is_set():
            return False
        now = datetime.now(MSK)
        target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if now >= target:
            return True
        time.sleep(min(2, (target - now).total_seconds()))


def live_candles(tickers, day, ti=None):
    if ti:
        data = {}
        for tk in tickers:
            try:
                data[tk] = ti.candles(tk, day)
            except CertError:
                raise
            except Exception as e:
                print(f"  {tk}: {e}")
        return data
    return iss_day(day, tickers)


def signals_mode(args):
    token = os.environ.get("TINVEST_TOKEN")
    ti = TInvest(token, "sandbox" if args.sandbox_token else "prod") if token else None
    if not ti:
        print("Внимание: без токена Т-Инвестиций данные берутся с сайта Мосбиржи, а бесплатные данные там "
              "обычно приходят с задержкой около 15 минут. Сигналы будут рассчитаны по неполным данным.")
    tickers = imoex_tickers()
    day = datetime.now(MSK).date()
    if not args.now and not entry_wait():
        return []
    data = live_candles(tickers, day, ti)
    market, sigs = compute_day(data, with_result=False)
    if market is None:
        print("Нет данных за сегодня до 15:00.")
        return []
    print(f"\nРынок к {ENTRY_MIN // 60}:{ENTRY_MIN % 60:02d}: {market * 100:+.2f}% (по {len(LAST_MOVES)} бумагам). Сигналов: {len(sigs)}")
    report_movers(market)
    prices = ti.last_prices([s["ticker"] for s in sigs]) if (ti and sigs) else {}
    for s in sigs:
        px = prices.get(s["ticker"])
        s["market"] = market
        s["price"] = px
        s["stop_px"] = px * (1 - s["side"] * STOP) if px else None
        stop_txt = f"стоп {px * (1 - s['side'] * STOP):.4g}" if px else f"стоп {STOP * 100:.0f}% от цены входа"
        print(f"  {s['ticker']:6s} {'КУПИТЬ (лонг)' if s['side'] > 0 else 'ПРОДАТЬ (шорт)'}  "
              f"движение дня {s['move'] * 100:+.2f}%  {('цена ' + format(px, '.4g')) if px else ''}  {stop_txt}")
    if sigs:
        print(f"\nКапитал делится поровну между {len(sigs)} сигналами. Закрыть все позиции в 17:00, "
              f"раньше — только по стопу.")
    return sigs


# ============================================================================ песочница
def check_mode(args):
    token = os.environ.get("TINVEST_TOKEN")
    if not token:
        sys.exit("Задайте токен в переменной окружения TINVEST_TOKEN (см. инструкцию).")
    env = "sandbox" if args.sandbox_token else "prod"
    ti = TInvest(token, env)
    print("Сервер:", ti.base)
    sh = ti.share("SBER")
    print("Инструмент SBER найден, лот:", sh["lot"], "шорт разрешён:", sh["short"])
    c = ti.candles("SBER", datetime.now(MSK).date())
    print("Минутных свечей SBER за сегодня:", len(c), ("последняя " + c[-1]["t"].strftime("%H:%M")) if c else "")
    print("Последняя цена SBER:", ti.last_prices(["SBER"]).get("SBER"))
    if env == "sandbox":
        acc = ti.sandbox_account()
        print("Счёт песочницы:", acc, "стоимость портфеля:", ti.sandbox_portfolio_value(acc))
    print("Доступ работает.")


def sandbox_mode(args):
    token = os.environ.get("TINVEST_TOKEN")
    if not token:
        sys.exit("Задайте токен песочницы в переменной окружения TINVEST_TOKEN.")
    ti = TInvest(token, "sandbox")
    acc = ti.sandbox_account()
    value = ti.sandbox_portfolio_value(acc)
    if value < args.capital * 0.5:
        print(f"На счёте песочницы {value:.0f} ₽, пополняю на {args.capital:.0f} ₽")
        ti.sandbox_pay_in(acc, args.capital)
    tickers = imoex_tickers()
    day = datetime.now(MSK).date()
    if day.weekday() >= 5:
        sys.exit("Сегодня выходной: стратегия торгует только по будням.")
    if not args.now and not entry_wait():
        return
    late = (datetime.now(MSK).hour * 60 + datetime.now(MSK).minute) - ENTRY_MIN
    data = live_candles(tickers, day, ti)
    market, sigs = compute_day(data, with_result=False)
    if market is None:
        print(f"Нет данных к {ENTRY_MIN // 60}:{ENTRY_MIN % 60:02d}.")
        return
    print(f"Рынок к {ENTRY_MIN // 60}:{ENTRY_MIN % 60:02d}: {market * 100:+.2f}% (по {len(LAST_MOVES)} бумагам). Сигналов: {len(sigs)}")
    report_movers(market)
    if not sigs:
        print("Сегодня сигналов нет — сделок не будет.")
        return
    if not args.now and late > LATE_OK_MIN:
        now_min = datetime.now(MSK).hour * 60 + datetime.now(MSK).minute
        if now_min >= LATE_CUTOFF_MIN:
            print(f"Сейчас {now_min // 60}:{now_min % 60:02d} — до выхода в 17:00 слишком мало времени, сделки не открываются.")
            return
        # сигнал должен сохраниться и сейчас: движение от открытия ≥ порога и рынок в ту же сторону
        _, now_sigs = compute_day(data, with_result=False, entry_min=now_min)
        still = {(x["ticker"], x["side"]) for x in now_sigs}
        keep = [x for x in sigs if (x["ticker"], x["side"]) in still]
        print(f"Вход с опозданием на {late} мин. Проверка, сохранились ли сигналы сейчас: "
              f"{len(keep)} из {len(sigs)} — {', '.join(x['ticker'] for x in keep) or 'нет'}.")
        for x in sigs:
            if (x["ticker"], x["side"]) not in still:
                print(f"  {x['ticker']}: к {now_min // 60}:{now_min % 60:02d} сигнал пропал (движение ослабло или рынок развернулся) — не входим")
        sigs = keep
        if not sigs:
            print("Подходящих сделок сейчас нет.")
            return
    alloc = args.capital / len(sigs)
    prices = ti.last_prices([s["ticker"] for s in sigs])
    pos = []
    for s in sigs:
        tk, side = s["ticker"], s["side"]
        sh, px = ti.share(tk), prices.get(tk)
        if not px:
            print(f"  {tk}: нет цены, пропуск")
            continue
        if side < 0 and not sh["short"]:
            print(f"  {tk}: шорт по бумаге недоступен, пропуск")
            continue
        lots = int(alloc // (px * sh["lot"]))
        if lots < 1:
            print(f"  {tk}: на долю капитала не хватает даже на один лот, пропуск")
            continue
        try:
            ti.sandbox_order(acc, tk, lots, side)
        except Exception as e:
            print(f"  {tk}: заявка отклонена: {e}")
            continue
        entry = prices[tk]
        pos.append({"ticker": tk, "side": side, "lots": lots, "entry": entry, "stop": entry * (1 - side * STOP), "open": True})
        print(f"  {tk}: {'куплено' if side > 0 else 'продано'} {lots} лот., цена ~{entry:.4g}, стоп {entry * (1 - side * STOP):.4g}")
    if not pos:
        return
    # сопровождение: стоп и выход в 17:00
    while True:
        now = datetime.now(MSK)
        mins = now.hour * 60 + now.minute
        if mins >= EXIT_MIN or (mins == EXIT_MIN - 1 and now.second >= 50):
            break
        try:
            lp = ti.last_prices([p["ticker"] for p in pos if p["open"]])
        except Exception as e:
            print("  цены не получены:", e)
            lp = {}
        for p in pos:
            if not p["open"] or p["ticker"] not in lp:
                continue
            px = lp[p["ticker"]]
            if (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"]):
                close_pos(ti, acc, p, px, "стоп")
        if not any(p["open"] for p in pos):
            break
        if STOP_EVENT.is_set():
            print("Нажат «Стоп»: закрываю открытые позиции.")
            break
        for _ in range(15):
            if STOP_EVENT.is_set():
                break
            time.sleep(1)
    lp = ti.last_prices([p["ticker"] for p in pos if p["open"]]) if any(p["open"] for p in pos) else {}
    for p in pos:
        if p["open"]:
            close_pos(ti, acc, p, lp.get(p["ticker"], p["entry"]), "кнопка «Стоп»" if STOP_EVENT.is_set() else "17:00")
    res = [p["result"] for p in pos]
    print(f"Итог дня (до комиссий песочницы): {sum(res) / len(res) * 100:+.2f}% в среднем на сделку")


def close_pos(ti, acc, p, px, why):
    try:
        ti.sandbox_order(acc, p["ticker"], p["lots"], -p["side"])
    except Exception as e:
        print(f"  {p['ticker']}: не удалось закрыть ({e}); закройте вручную в приложении!")
        return
    p["open"] = False
    p["result"] = p["side"] * (px / p["entry"] - 1)
    print(f"  {p['ticker']}: закрыто ({why}) по ~{px:.4g}, результат {p['result'] * 100:+.2f}%")
    append_rows(SANDBOX_LOG, ["date", "ticker", "side", "lots", "entry", "exit", "reason", "result_pct"],
                [{"date": datetime.now(MSK).date().isoformat(), "ticker": p["ticker"],
                  "side": "лонг" if p["side"] > 0 else "шорт", "lots": p["lots"], "entry": p["entry"], "exit": px,
                  "reason": why, "result_pct": round(p["result"] * 100, 4)}])


# ============================================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    j = sub.add_parser("journal", help="сигналы и итог дня в журнал (после 17:15)")
    j.add_argument("--date")
    j.add_argument("--from", dest="dfrom")
    j.add_argument("--to", dest="dto")
    j.add_argument("--force", action="store_true", help="пересчитать день, даже если он уже в журнале")
    sub.add_parser("stats", help="статистика журнала")
    s = sub.add_parser("signals", help="сигналы в 15:00 для ручной торговли")
    s.add_argument("--now", action="store_true", help="не ждать 15:00, посчитать сразу")
    s.add_argument("--sandbox-token", action="store_true", help="токен от песочницы")
    c = sub.add_parser("check", help="проверить доступ к API Т-Инвестиций")
    c.add_argument("--sandbox-token", action="store_true", help="токен от песочницы")
    b = sub.add_parser("sandbox", help="торговля на демо-счёте песочницы")
    b.add_argument("--capital", type=float, default=1_000_000)
    b.add_argument("--now", action="store_true", help="не ждать 15:00 (для проверки)")
    for p_ in (j, sub.choices["stats"], s, b):
        p_.add_argument("--entry", choices=ENTRY_CHOICES, default="15:00",
                        help="время входа (основной вариант 15:00)")
        p_.add_argument("--threshold", type=float, default=2.0,
                        help="порог движения акции от открытия, %% (2 — основной, 2.5 — меньше сделок, выше средняя)")
    args = ap.parse_args()
    configure(getattr(args, "threshold", 2.0), getattr(args, "entry", "15:00"))

    if args.mode == "journal":
        tickers = imoex_tickers()
        if args.dfrom:
            d0, d1 = date.fromisoformat(args.dfrom), date.fromisoformat(args.dto or datetime.now(MSK).date().isoformat())
            d = d0
            while d <= d1:
                journal_day(d, tickers, args.force)
                d += timedelta(days=1)
        else:
            d = date.fromisoformat(args.date) if args.date else datetime.now(MSK).date()
            now = datetime.now(MSK)
            if d == now.date() and (now.hour, now.minute) < (17, 15):
                sys.exit("Итог дня считается после 17:15 (данные Мосбиржи идут с задержкой). Запустите позже "
                         "или укажите прошедший день: --date ГГГГ-ММ-ДД")
            journal_day(d, tickers, args.force)
        stats()
    elif args.mode == "stats":
        stats()
    elif args.mode == "signals":
        signals_mode(args)
    elif args.mode == "check":
        check_mode(args)
    elif args.mode == "sandbox":
        sandbox_mode(args)


if __name__ == "__main__":
    main()
