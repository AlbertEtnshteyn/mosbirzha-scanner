# -*- coding: utf-8 -*-
"""Общее для программы на Windows и сервера: настройки, параметры робота, данные для графика."""
import json
import os
from datetime import datetime, timedelta

import scanner as sc
import autotrader as at

SETTINGS = os.path.join(sc.HERE, "settings.json")
DEFAULTS = {"threshold": 2.0, "entry": "15:00", "capital": 1_000_000, "sandbox_token": True,
            "auto_signals": True, "auto_journal": True, "remember_token": False, "token": "",
            "rescan": False, "autostart": False, "day_on": True, "sound_on": True,
            "spike_on": True, "spike_tfs": [15, 60], "spike_markets": ["all_shares"], "day_markets": ["imoex"], "direction": "both", "leverage": "1",
            "spike_half": 50, "spike_trail": 1.0, "spike_max": 0, "spike_share": 10, "two_takes": True,
            "tg_token": "", "tg_chat": "", "tg_form": True, "tg_trades": True, "tg_imp": False,
            "tg_trend": True, "trend_on": True, "trend_trade": False, "tier_profile": True, "entry_window": "9-19", "exit_late": True, "carry_all": True, "day_carry": 0, "day_mode": "fixed",
            "day_tp": 8, "day_tp_half": 50, "day_trail": 2, "day_act": 0,
            "idx_form": True, "ema10_touch": False, "first_form": True, "trade_5m": False, "ema200": False, "ema200_tfs": ["form"], "idx_day": True, "idx_flip": "off",
            "day_exit": "18:45", "entry_from": "", "day_ref": "main", "trade_weekends": False, "idx_ref": "imoex", "broker": "tinvest", "finam_secret": "", "finam_account": "", "real_token": "", "real_account": "",
            # «Движение дня» (новый формат, 06.11): покупка на середине EMA10/EMA20, стоп за лоем дня, тейк на хае дня
            "mid_thr": 2.5, "mid_from": "10:00", "mid_to": "18:00", "mid_exit": "23:45", "mid_tf": 60, "mid_idx": False, "mid_share": 10}

CHART_DEPTH = {1: 2, 5: 10, 15: 30, 60: 90, 1440: 400}       # сколько дней истории грузить на график


def load_settings(path=SETTINGS):
    s = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
        s.pop("all_hours", None)
        if not s.get("max_v2"):                  # 29.10: число позиций по формации — без ограничения (по просьбе пользователя)
            s["spike_max"], s["max_v2"] = 0, True
    except Exception:
        pass
    return s


def save_settings(s, path=SETTINGS, keep_token=None):
    data = dict(s)
    if not (data.get("remember_token") if keep_token is None else keep_token):
        data["token"] = ""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def window_key(s):
    """Окно входов формации с учётом настройки «время входов от…»: "9-19", "9-19@7" (с 07:00), "9-19@9" (с 09:00), "9-19@10" (с 10:00)."""
    w = s.get("entry_window", "9-19")
    ef = str(s.get("entry_from") or "")
    return f"{w}@{ef}" if ef in ("7", "9", "10") else w


def exit_choice(s):
    """Время выхода формации: True — 23:45, False — ранний выход окна (19:25 / 18:45), или строка "18:45"."""
    v = s.get("exit_late", True)
    return v if isinstance(v, str) and ":" in v else bool(v)


def carry_choice(s):
    """Перенос формации во время выхода: True — все позиции, False — закрыть только в минусе, "none" — закрыть все."""
    v = s.get("carry_all", True)
    return "none" if v == "none" else bool(v)


def spike_cfg(s):
    """Параметры стратегии «всплеск объёма + откат к EMA20» из настроек."""
    return {"on": bool(s.get("spike_on")), "tfs": list(s.get("spike_tfs", [15, 60])),
            "markets": list(s.get("spike_markets", ["all_shares"])), "day_markets": list(s.get("day_markets", ["imoex"])) or ["imoex"], "half": int(s.get("spike_half", 50)) / 100,
            "trail_r": float(s.get("spike_trail", 1.0)), "max_pos": int(s.get("spike_max", 0) or 0),
            "share": float(s.get("spike_share", 10)) / 100,
            "leverage": s.get("leverage", "1"),
            "two_takes": bool(s.get("two_takes", True)),
            "trend_on": bool(s.get("trend_on", True)), "trend_trade": bool(s.get("trend_trade", False)),
            "tier_profile": bool(s.get("tier_profile", True)), "window": window_key(s),
            "entry_from": str(s.get("entry_from") or ""), "day_ref": "market" if s.get("day_ref") == "market" else "main",
            "exit_late": exit_choice(s), "carry_all": carry_choice(s),
            "day_carry": int(s.get("day_carry", 0) or 0), "day_exit": s.get("day_exit", "18:45"),
            "trade_weekends": bool(s.get("trade_weekends", False)),
            "idx_ref": s.get("idx_ref") if s.get("idx_ref") in ("open", "imoex_open") else "imoex", "day_mode": s.get("day_mode", "fixed"),
            "day_tp": float(s.get("day_tp", 8) or 0) / 100, "day_tp_half": float(s.get("day_tp_half", 50) or 100) / 100,
            "day_trail": float(s.get("day_trail", 2) or 0) / 100, "day_act": float(s.get("day_act", 0) or 0) / 100,
            "index": bool(s.get("idx_form", True)), "ema10_touch": bool(s.get("ema10_touch", False)), "first_form": bool(s.get("first_form", True)), "trade_5m": bool(s.get("trade_5m", False)), "ema200": bool(s.get("ema200", False)), "ema200_tfs": list(s.get("ema200_tfs") or ["form"]), "idx_day": bool(s.get("idx_day", True)),
            "idx_flip": s.get("idx_flip", "off") if s.get("idx_flip") in ("all", "profit") else "off",
            "mid": mid_cfg(s)}


def hhmm(v, default):
    """«10:00» / «10» / «9:30» → минуты от полуночи."""
    try:
        v = str(v).strip().replace(".", ":")
        hh, mm = (v.split(":") + ["0"])[:2]
        m = int(hh) * 60 + int(mm or 0)
        return m if 0 <= m < 24 * 60 else default
    except (ValueError, TypeError):
        return default


def mid_cfg(s):
    """Настройки «Движения дня»: покупка на середине EMA10/EMA20."""
    tf = int(s.get("mid_tf", 60) or 60)
    return {"thr": float(s.get("mid_thr", 2.5) or 2.5) / 100,
            "from": hhmm(s.get("mid_from", "10:00"), 600), "to": hhmm(s.get("mid_to", "18:00"), 1080),
            "exit": hhmm(s.get("mid_exit", "23:45"), 1425), "tf": tf if tf in (5, 15, 60, 1440) else 60,
            "idx": bool(s.get("mid_idx", False)), "share": float(s.get("mid_share", 10) or 10) / 100}


def chart_sp(s):
    sp = dict(at.SPIKE_DEFAULTS)
    sp.update(two_takes=bool(s.get("two_takes", True)), half=int(s.get("spike_half", 50)) / 100,
              trail_r=float(s.get("spike_trail", 1.0)))
    return sp


def chart_data(token, sandbox, ticker, tf, s):
    """Свечи для графика и сделки робота на истории по его текущим правилам.
    Возвращает (свечи, сделки, эшелон: "index" | "2" | "3" | None)."""
    ti = sc.TInvest(token, "sandbox" if sandbox else "prod")
    now = datetime.now(sc.MSK)
    frm = (now - timedelta(days=CHART_DEPTH[tf])).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = ti.candles_range(ticker, frm, now + timedelta(minutes=1), tf)
    trades, tier = [], None
    if tf in (5, 15, 60) and rows:
        per_lot = 1.0
        try:
            sh = ti.share(ticker)
            per_lot = float(sh["lot"])
            if sh.get("kind") == "future":
                per_lot *= ti.future_terms(ticker)[0]
        except Exception:
            pass
        bars = [dict(r, val=r.get("v", 0) * per_lot * (r["h"] + r["l"] + r["c"]) / 3) for r in rows]
        if ticker in sc.FALLBACK_TICKERS:
            tier = "index"
        else:
            days = {}
            for r in bars:
                m = r["t"].hour * 60 + r["t"].minute
                if 600 <= m < 1120:
                    days[r["t"].date()] = days.get(r["t"].date(), 0) + r["val"]
            med = at._median(list(days.values())) if days else 0
            tier = "2" if med >= at.TIER2_TURNOVER else "3"
        trades = at.history_trades(bars, at.params(chart_sp(s), tf), tf, tier, window_key(s),
                                   exit_choice(s), carry_choice(s))
    return rows, trades, tier


def ema(vals, span):
    k, e, out = 2 / (span + 1), None, []
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


# ============================================================================ результаты торговли за период
PERIODS = [("day", "День"), ("week", "Неделя"), ("month", "Месяц"), ("quarter", "Квартал"), ("year", "Год"), ("all", "Всё время")]


def _read(path):
    import csv
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


STRAT_NAMES = {"всплеск": "Всплеск + EMA20", "15:00": "Движение дня", "spike": "Всплеск + EMA20", "15": "Движение дня"}


def real_trades(ticker):
    """Настоящие сделки робота по бумаге (закрытые) — для отметок входа и выхода на графике."""
    out = []
    for path, strat in ((sc.SPIKE_LOG, "всплеск"), (sc.SANDBOX_LOG, "15:00"), (sc.MANUAL_LOG, "вручную")):
        for r in _read(path):
            if r.get("ticker") != ticker:
                continue
            out.append({"strat": strat, "side": 1 if r.get("side") == "лонг" else -1, "entry": _num(r.get("entry")),
                        "exit": _num(r.get("exit")), "reason": r.get("reason", ""), "result_pct": _num(r.get("result_pct")),
                        "exit_time": r.get("time", ""), "entry_time": r.get("entry_time", "")})
    return out[-200:]


def strat_summary(rows):
    """Сводка по закрытым сделкам: число, в плюсе, итог ₽ (где известен), средний %."""
    res = [x for x in (_num(t.get("result_pct")) for t in rows) if x is not None]
    pnl = [x for x in (_num(t.get("pnl_rub")) for t in rows) if x is not None]
    return {"n": len(res), "wins": sum(1 for x in res if x > 0), "pnl_rub": round(sum(pnl), 2) if pnl else None,
            "avg_pct": round(sum(res) / len(res), 3) if res else None, "sum_pct": round(sum(res), 3) if res else None}


def positions_summary(positions):
    """Открытые позиции по стратегиям: число и текущий результат (₽ и %), плюс общий итог."""
    out, tot = {}, {"n": 0, "pnl_rub": 0.0, "known": 0}
    for p in positions:
        k = "всплеск" if p.get("strat") == "spike" else "вручную" if p.get("strat") == "manual" else "15:00"
        d = out.setdefault(k, {"n": 0, "pnl_rub": 0.0, "known": 0, "pct": []})
        d["n"] += 1
        tot["n"] += 1
        last = p.get("last")
        if last and p.get("entry"):
            d["pct"].append(p["side"] * (last / p["entry"] - 1) * 100)
        if p.get("pnl_rub") is not None:
            d["pnl_rub"] += p["pnl_rub"]; d["known"] += 1
            tot["pnl_rub"] += p["pnl_rub"]; tot["known"] += 1
    for d in out.values():
        d["avg_pct"] = round(sum(d["pct"]) / len(d["pct"]), 3) if d["pct"] else None
        d.pop("pct")
    return {"by_strat": out, "total": tot}


def period_start(kind, now):
    d = now.date()
    if kind == "day":
        s = d
    elif kind == "week":
        s = d - timedelta(days=d.weekday())
    elif kind == "month":
        s = d.replace(day=1)
    elif kind == "quarter":
        s = d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)
    elif kind == "year":
        s = d.replace(month=1, day=1)
    else:
        s = None
    return s


def results(balance_now=None):
    """Баланс и результаты за день / неделю / месяц / квартал / год / всё время.
    Изменение баланса — по стоимости портфеля (пополнения песочницы вычитаются);
    сделки — по журналам закрытых сделок обеих стратегий."""
    now = datetime.now(sc.MSK)
    bal = []
    for r in _read(at.bal_log()):
        v = _num(r.get("value"))
        if v is None:
            continue
        try:
            t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        bal.append((t, v, _num(r.get("deposit")) or 0.0))
    bal.sort(key=lambda x: x[0])
    cur = balance_now if balance_now else (bal[-1][1] if bal else None)
    trades = [dict(r, strat="всплеск") for r in _read(sc.SPIKE_LOG)] + [dict(r, strat="15:00") for r in _read(sc.SANDBOX_LOG)] + \
        [dict(r, strat="вручную") for r in _read(sc.MANUAL_LOG)]
    out = []
    for kind, name in PERIODS:
        start = period_start(kind, now)
        row = {"kind": kind, "name": name, "from": start.isoformat() if start else (bal[0][0].date().isoformat() if bal else "")}
        if bal and cur:
            st_dt = datetime.combine(start, datetime.min.time()) if start else bal[0][0]
            before = [b for b in bal if b[0] < st_dt]
            if before:
                base = before[-1][1]
            else:
                first = next((b for b in bal if b[0] >= st_dt), None)
                base = (first[1] - first[2]) if first and first[2] else (first[1] if first else None)
            dep = sum(b[2] for b in bal if b[0] >= st_dt and b[2])
            if base is not None and base + dep > 0:
                ch = cur - base - dep
                row.update(start_balance=round(base if base > 0 else base + dep, 2), change=round(ch, 2), change_pct=round(ch / (base + dep) * 100, 3), deposits=dep)
        tr = [t for t in trades if (not start) or t.get("date", "") >= start.isoformat()]
        res = [_num(t.get("result_pct")) for t in tr]
        res = [x for x in res if x is not None]
        pnl = [_num(t.get("pnl_rub")) for t in tr]
        pnl = [x for x in pnl if x is not None]
        row.update(trades=len(res), wins=sum(1 for x in res if x > 0), avg_pct=round(sum(res) / len(res), 3) if res else None,
                   pnl_rub=round(sum(pnl), 2) if pnl else None, pnl_known=len(pnl),
                   by_strat={s: strat_summary([t for t in tr if t["strat"] == s]) for s in ("всплеск", "15:00", "вручную")})
        out.append(row)
    # баланс по дням (последнее значение дня) — для графика
    daily = {}
    for t, v, _ in bal:
        daily[t.date().isoformat()] = v
    if cur:
        daily[now.date().isoformat()] = cur
    return {"balance": cur, "time": now.strftime("%d.%m.%Y %H:%M"), "periods": out,
            "series": [{"date": d, "value": round(v, 2)} for d, v in sorted(daily.items())]}


# ============================================================================ вкладка «Рынки»
MAJOR_INDEXES = ["IMOEX", "IMOEX2", "RTSI", "MOEXBC", "MOEXBMI", "MCXSM", "MOEXOG", "MOEXFN", "MOEXMM", "MOEXEU",
                 "MOEXTL", "MOEXCN", "MOEXCH", "MOEXIT", "MOEXRE", "MOEXTN", "RGBI", "RUCBTRNS"]
MARKET_TYPES = [("share", "Акции"), ("index", "Индексы"), ("currency", "Валюта"), ("future", "Фьючерсы")]


def _iss_rows(path, params):
    js = sc.iss_get(path, params, retries=2)
    out = {}
    for block in ("securities", "marketdata"):
        b = js.get(block) or {}
        cols = b.get("columns") or []
        for row in b.get("data") or []:
            x = dict(zip(cols, row))
            key = (x.get("SECID"), x.get("BOARDID"))
            out.setdefault(key, {}).update({f"{block}.{k}": v for k, v in x.items()})
    return list(out.values())


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _hhmm(*vals):
    for v in vals:
        if v and str(v) not in ("0", "None", "00:00:00"):
            s = str(v)
            return s[-8:-3] if len(s) >= 8 and s[-3] == ":" else s[:5]
    return ""


def market_overview(tier_map=None, live_prices=None):
    """Состояние рынка по данным Мосбиржи: цена, изменение за день, оборот, время. Акции, индексы, валюта, фьючерсы.
    live_prices(tickers) -> {тикер: цена} — цены акций в реальном времени из Т-Инвестиций (если есть токен)."""
    rows, errors = [], []
    try:
        for x in _iss_rows("/engines/stock/markets/shares/boards/TQBR/securities.json", {"iss.only": "securities,marketdata"}):
            tk = x.get("securities.SECID") or x.get("marketdata.SECID")
            prev = _f(x.get("securities.PREVPRICE"))
            last = _f(x.get("marketdata.LAST")) or _f(x.get("marketdata.LCURRENTPRICE")) or _f(x.get("marketdata.MARKETPRICE"))
            pct = _f(x.get("marketdata.LASTTOPREVPRICE"))
            if pct is None and last and prev:
                pct = (last / prev - 1) * 100
            val0 = _f(x.get("marketdata.VALTODAY")) or 0
            tier = (tier_map or {}).get(tk) or ("index" if tk in sc.FALLBACK_TICKERS else ("2" if val0 >= at.TIER2_TURNOVER else "3"))
            rows.append({"type": "share", "ticker": tk, "name": x.get("securities.SHORTNAME") or tk, "price": last, "prev": prev,
                         "pct": pct, "value": _f(x.get("marketdata.VALTODAY")) or 0, "volume": _f(x.get("marketdata.VOLTODAY")) or 0,
                         "time": _hhmm(x.get("marketdata.UPDATETIME"), x.get("marketdata.TIME")),
                         "tier": tier, "blue": tk in sc.BLUE_CHIPS})
    except Exception as e:
        errors.append(f"акции: {e}")
    if live_prices:                                       # цены в реальном времени (данные Мосбиржи без подписки — с задержкой)
        try:
            lp = live_prices([r["ticker"] for r in rows if r["price"]])
            for r in rows:
                p = lp.get(r["ticker"])
                if p:
                    r["price"], r["live"] = p, True
                    r["time"] = datetime.now(sc.MSK).strftime("%H:%M")
                    if r["prev"]:
                        r["pct"] = (p / r["prev"] - 1) * 100
        except Exception as e:
            errors.append(f"цены Т-Инвестиций: {e}")
    try:
        for x in _iss_rows("/engines/stock/markets/index/boards/SNDX/securities.json", {"iss.only": "securities,marketdata"}):
            tk = x.get("securities.SECID") or x.get("marketdata.SECID")
            if tk not in MAJOR_INDEXES:
                continue
            last = _f(x.get("marketdata.CURRENTVALUE")) or _f(x.get("marketdata.LASTVALUE"))
            rows.append({"type": "index", "ticker": tk, "name": x.get("securities.SHORTNAME") or x.get("securities.NAME") or tk,
                         "price": last, "pct": _f(x.get("marketdata.LASTCHANGEPRC")), "value": _f(x.get("marketdata.VALTODAY")) or 0,
                         "volume": 0, "time": _hhmm(x.get("marketdata.UPDATETIME"), x.get("marketdata.TIME"))})
    except Exception as e:
        errors.append(f"индексы: {e}")
    try:
        for x in _iss_rows("/engines/currency/markets/selt/boards/CETS/securities.json", {"iss.only": "securities,marketdata"}):
            tk = x.get("securities.SECID") or x.get("marketdata.SECID")
            if not tk or not tk.endswith("TOM"):
                continue
            prev = _f(x.get("securities.PREVPRICE")) or _f(x.get("securities.PREVWAPRICE"))
            last = _f(x.get("marketdata.LAST")) or _f(x.get("marketdata.MARKETPRICE"))
            pct = _f(x.get("marketdata.LASTTOPREVPRICE"))
            if pct is None and last and prev:
                pct = (last / prev - 1) * 100
            val = _f(x.get("marketdata.VALTODAY_RUR")) or _f(x.get("marketdata.VALTODAY")) or 0
            if not last:
                continue
            rows.append({"type": "currency", "ticker": tk, "name": x.get("securities.SHORTNAME") or tk, "price": last, "pct": pct,
                         "value": val, "volume": _f(x.get("marketdata.VOLTODAY")) or 0,
                         "time": _hhmm(x.get("marketdata.UPDATETIME"), x.get("marketdata.TIME"))})
    except Exception as e:
        errors.append(f"валюта: {e}")
    try:
        fut = []
        for x in _iss_rows("/engines/futures/markets/forts/boards/RFUD/securities.json", {"iss.only": "securities,marketdata"}):
            tk = x.get("securities.SECID") or x.get("marketdata.SECID")
            prev = _f(x.get("securities.PREVSETTLEPRICE")) or _f(x.get("securities.PREVPRICE"))
            last = _f(x.get("marketdata.LAST"))
            val = _f(x.get("marketdata.VALTODAY")) or 0
            if not last or not val:
                continue
            pct = _f(x.get("marketdata.LASTTOPREVPRICE"))
            if pct is None and prev:
                pct = (last / prev - 1) * 100
            fut.append({"type": "future", "ticker": tk, "name": x.get("securities.SHORTNAME") or tk, "price": last, "pct": pct,
                        "value": val, "volume": _f(x.get("marketdata.VOLTODAY")) or 0,
                        "time": _hhmm(x.get("marketdata.UPDATETIME"), x.get("marketdata.TIME")),
                        "asset": x.get("securities.ASSETCODE") or ""})
        rows += fut
    except Exception as e:
        errors.append(f"фьючерсы: {e}")
    sh = [r for r in rows if r["type"] == "share" and r.get("pct") is not None and r.get("value")]
    summary = {"up": sum(1 for r in sh if r["pct"] > 0), "down": sum(1 for r in sh if r["pct"] < 0),
               "flat": sum(1 for r in sh if r["pct"] == 0), "value": sum(r["value"] for r in sh),
               "live": any(r.get("live") for r in rows)}
    return {"time": datetime.now(sc.MSK).strftime("%d.%m.%Y %H:%M:%S"), "rows": rows, "summary": summary,
            "types": MARKET_TYPES, "errors": errors}


def _form_short(why):
    w = str(why or "")
    if w.startswith("ВХОД"):
        return "вход"
    if w.startswith("ТРЕНД"):
        return "тренд"
    if w.startswith("жду"):
        return "жду отката"
    if w.startswith("коснулся"):
        return "касание EMA20"
    if "ПОДТВЕРЖД" in w:
        return "подтверждена"
    if "против" in w:
        return "против индекса"
    if w.startswith("отмен") or w.startswith("время вышло"):
        return "отменена"
    return w.split(" — ")[0][:24] or "формация"


def attach_formations(data, spikes):
    """Отметить в «Рынках» бумаги, где сегодня есть всплеск/формация EMA20: ТФ, направление, статус (последний по ТФ)."""
    by = {}
    for row in spikes or []:
        tm, tk, tfn, _v, _m, body, why = row[:7]
        by.setdefault(tk, {})[tfn] = {"tf": tfn, "side": 1 if body > 0 else -1, "status": _form_short(why), "time": tm,
                                      "full": str(why)}
    out = dict(data)
    out["rows"] = [dict(r, forms=list(by[r["ticker"]].values())) if r.get("ticker") in by else r for r in data.get("rows", [])]
    out["forms_n"] = len(by)
    return out


# ============================================================================ сброс: новый демо-счёт и чистая история торговли
def reset_trading(token, capital, log=print, broker="tinvest"):
    """Закрыть все счета песочницы, открыть новый и пополнить его на сумму капитала; историю сделок и баланса
    убрать в архивную папку reset_ГГГГММДД_ЧЧММ (на всякий случай). Найденные сегодня формации сохраняются.
    Робот перед этим должен быть остановлен."""
    import glob
    import shutil
    if broker in ("finam", "tinvest_real"):
        # реальный счёт не пересоздать: позиции робот закрыл заранее, здесь — только чистая история
        try:
            with open(at.STATE_FILE, encoding="utf-8") as f:
                if json.load(f).get("positions"):
                    raise RuntimeError("у робота есть открытые позиции на реальном счёте — сначала закройте их")
        except (OSError, ValueError):
            pass
        log("Реальный счёт не меняется, очищаю историю сделок и баланса робота.")
    else:
        if not token:
            raise ValueError("нет токена песочницы Т-Инвестиций")
        ti = sc.TInvest(token, "sandbox")
        accs = ti.call("SandboxService", "GetSandboxAccounts", {}).get("accounts", [])
        for a in accs:
            ti.call("SandboxService", "CloseSandboxAccount", {"accountId": a["id"]})
        log(f"Счета песочницы закрыты: {len(accs)} (вместе с позициями).")
        acc = ti.call("SandboxService", "OpenSandboxAccount", {})["accountId"]
        ti.sandbox_pay_in(acc, capital)
        log("Открыт новый демо-счёт, пополнен на " + f"{capital:,.0f}".replace(",", " ") + " ₽.")

    now = datetime.now(sc.MSK)
    arch = os.path.join(sc.HERE, "reset_" + now.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(arch, exist_ok=True)
    files = glob.glob(os.path.join(sc.HERE, "sandbox_trades*.csv")) + [sc.SPIKE_LOG, sc.MANUAL_LOG, at.bal_log(), at.STATE_FILE]
    for p in files:
        if os.path.exists(p):
            shutil.copy2(p, arch)
    # состояние робота: позиции и сделки — с нуля, найденные сегодня всплески/формации — оставить
    try:
        with open(at.STATE_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = {}
    st = at.AutoTrader._fresh(now.date().isoformat())
    if old.get("date") == st["date"]:
        for k in ("found_spikes", "found_seen", "found_waits", "entered", "done"):   # вход «Движения дня» сегодня не повторять
            if k in old:
                st[k] = old[k]
    with open(at.STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(at.STATE_FILE + ".tmp", at.STATE_FILE)
    for p in files[:-1]:
        if os.path.exists(p):
            os.remove(p)
    if broker not in ("finam", "tinvest_real"):
        sc.append_rows(at.bal_log(), at.BAL_COLS, [{"time": now.strftime("%Y-%m-%d %H:%M"), "value": round(capital, 2),
                                                    "deposit": capital}])
    log(f"История сделок и баланса очищена (копия: {os.path.basename(arch)}). Начинаем с {capital:,.0f} ₽.".replace(",", " "))
    return {"capital": capital, "archive": os.path.basename(arch)}
