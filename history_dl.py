# -*- coding: utf-8 -*-
"""
Выгрузка истории всех российских акций (режим TQBR) для проверки стратегий на истории.

Источник 1 — архив исторических данных Т-Инвестиций (минутные свечи за год одним файлом, быстро; нужен токен).
Источник 2 (запасной) — сайт Мосбиржи (ISS), минутные свечи постранично; дольше, токен не нужен.

Минутки сразу сворачиваются в 5-минутные свечи (из них строятся 15 мин и 1 час) и пишутся в папку
history5m рядом с программой: по файлу на бумагу (begin,open,high,low,close,value,volume), затем всё
упаковывается в history5m.zip — этот архив и нужно прислать для проверки.
"""
import csv
import io
import os
import time
import zipfile
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

import scanner as sc

HIST_URL = "https://invest-public-api.tinkoff.ru/history-data"


def _bucket(t):
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


class Agg5:
    """Сворачивает минутки в 5-минутки (время МСК)."""
    def __init__(self):
        self.bars = OrderedDict()

    def add(self, t, o, h, l, c, value, volume):
        k = _bucket(t)
        b = self.bars.get(k)
        if b is None:
            self.bars[k] = [o, h, l, c, value, volume]
        else:
            b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = c; b[4] += value; b[5] += volume

    def write(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["begin", "open", "high", "low", "close", "value", "volume"])
            for k in sorted(self.bars):
                o, h, l, c, val, vol = self.bars[k]
                w.writerow([k.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, round(val, 2), vol])
        return len(self.bars)


def tinvest_year(token, uid, year, lot, agg, since):
    """Минутки за год из архива Т-Инвестиций. True — получилось, False — источник недоступен."""
    for attempt in range(6):
        if sc.STOP_EVENT.is_set():
            return True
        r = sc.http().get(HIST_URL, params={"instrumentId": uid, "year": year},
                          headers={"Authorization": f"Bearer {token}"}, timeout=(15, 90))
        if r.status_code == 429:
            wait = float(r.headers.get("x-ratelimit-reset", 20) or 20)
            print(f"    лимит запросов архива — жду {wait:.0f} с")
            time.sleep(min(max(wait, 5), 70))
            continue
        if r.status_code == 404:
            return True                       # за этот год данных нет
        if r.status_code != 200:
            raise RuntimeError(f"архив Т-Инвестиций: HTTP {r.status_code} {r.text[:200]}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        for name in sorted(z.namelist()):
            for line in z.read(name).decode("utf-8", "ignore").splitlines():
                p = line.split(";")
                if len(p) < 7:
                    continue
                t = datetime.fromisoformat(p[1].replace("Z", "+00:00")).astimezone(sc.MSK).replace(tzinfo=None)
                if t.date() < since:
                    continue
                o, c, h, l, vol = float(p[2]), float(p[3]), float(p[4]), float(p[5]), int(float(p[6]))
                agg.add(t, o, h, l, c, vol * lot * c, vol * lot)
        return True
    raise RuntimeError("архив Т-Инвестиций не отвечает (лимит запросов)")


def iss_range(ticker, since, until, agg):
    """Минутки с сайта Мосбиржи постранично (по месяцу за запрос)."""
    d = since
    while d <= until and not sc.STOP_EVENT.is_set():
        d2 = min(until, (d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
        start = 0
        while True:
            js = sc.iss_get(f"/engines/stock/markets/shares/boards/TQBR/securities/{ticker}/candles.json",
                            {"interval": 1, "from": d.isoformat(), "till": d2.isoformat(), "start": start})
            c = js["candles"]
            if not c["data"]:
                break
            cols = c["columns"]
            for row in c["data"]:
                x = dict(zip(cols, row))
                t = datetime.strptime(x["begin"], "%Y-%m-%d %H:%M:%S")
                agg.add(t, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                        float(x.get("value") or 0), float(x.get("volume") or 0))
            start += len(c["data"])
            if len(c["data"]) < 500:
                break
        d = d2 + timedelta(days=1)


def download_all(token=None, sandbox=True, since=date(2025, 10, 1), out_dir=None):
    out_dir = out_dir or os.path.join(sc.HERE, "history5m")
    os.makedirs(out_dir, exist_ok=True)
    until = datetime.now(sc.MSK).date()
    shares = []
    if token:
        try:
            ti = sc.TInvest(token, "sandbox" if sandbox else "prod")
            for tk in ti.all_shares():
                sh = ti.share(tk)
                shares.append((tk, sh["uid"], sh["lot"]))
        except Exception as e:
            print("Список акций из Т-Инвестиций не получен:", e)
    if not shares:
        print("Список акций беру с сайта Мосбиржи…")
        js = sc.iss_get("/engines/stock/markets/shares/boards/TQBR/securities.json", {"iss.only": "securities"})
        cols = js["securities"]["columns"]
        for row in js["securities"]["data"]:
            x = dict(zip(cols, row))
            shares.append((x["SECID"], None, int(x.get("LOTSIZE") or 1)))
    print(f"Акций: {len(shares)}. Период: {since} — {until}. Папка: {out_dir}")
    use_archive = bool(token)
    done = 0
    t0 = time.time()
    for n, (tk, uid, lot) in enumerate(shares, 1):
        if sc.STOP_EVENT.is_set():
            print("Остановлено. Уже скачанное сохранено — при следующем запуске продолжится с места остановки.")
            break
        path = os.path.join(out_dir, f"{tk}.csv")
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            done += 1
            continue
        agg = Agg5()
        try:
            if use_archive and uid:
                try:
                    for y in range(since.year, until.year + 1):
                        tinvest_year(token, uid, y, lot, agg, since)
                except RuntimeError as e:
                    if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                        print(f"  архив Т-Инвестиций недоступен для этого токена ({e}) — дальше качаю с сайта Мосбиржи (дольше)")
                        use_archive = False
                        agg = Agg5()
                    else:
                        raise
            if not agg.bars:
                iss_range(tk, since, until, agg)
            if agg.bars:
                rows = agg.write(path)
                done += 1
                el = time.time() - t0
                left = el / n * (len(shares) - n)
                print(f"  [{n}/{len(shares)}] {tk}: {rows} свечей 5 мин · осталось примерно {left / 60:.0f} мин")
            else:
                print(f"  [{n}/{len(shares)}] {tk}: данных нет")
        except sc.CertError:
            raise
        except Exception as e:
            print(f"  [{n}/{len(shares)}] {tk}: ошибка {e}")
    zpath = os.path.join(sc.HERE, "history5m.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(out_dir)):
            if f.endswith(".csv"):
                z.write(os.path.join(out_dir, f), f"history5m/{f}")
    mb = os.path.getsize(zpath) / 1e6
    print(f"Готово: {done} бумаг. Архив для проверки: {zpath} ({mb:.0f} МБ). Пришлите его в чат.")
    return zpath
