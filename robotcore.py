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
            "mid_thr": 2.5, "mid_from": "10:00", "mid_to": "18:00", "mid_exit": "23:45", "mid_tf": 60, "mid_tfs": [60], "mid_idx": False, "mid_share": 10, "mid_ref": "7", "mid_multi": True, "mid_carry": "none", "mid_carry_days": 0, "mid_dir": "long", "mid_modes": ["long"], "mid_liq": 1.0, "mid_min_turn": 20, "mid_liq_on": True, "mid_minv_on": False, "mid_vol_on": False, "mid_vol": 5,
            "mid_prev_stop": True, "mid_take_mode": "all", "mid_take_part": 50, "mid_trail": 1.0}

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
    tfs = [int(x) for x in (s.get("mid_tfs") or [s.get("mid_tf", 60) or 60]) if int(x) in (5, 15, 60, 1440)] or [60]
    return {"thr": float(s.get("mid_thr", 2.5) or 2.5) / 100,
            "from": hhmm(s.get("mid_from", "10:00"), 600), "to": hhmm(s.get("mid_to", "18:00"), 1080),
            "exit": hhmm(s.get("mid_exit", "23:45"), 1425), "tfs": sorted(set(tfs), reverse=True),
            "idx": False, "share": float(s.get("mid_share", 10) or 10) / 100,
            "ref": 600 if str(s.get("mid_ref", "7")) == "10" else 420, "multi": bool(s.get("mid_multi", True)),
            "carry": s.get("mid_carry") if s.get("mid_carry") in ("all", "profit") else "none",
            "carry_days": int(s.get("mid_carry_days", 0) or 0),
            "modes": mid_modes(s),
            "liq": float(s.get("mid_liq", 1.0) or 0) / 100 if s.get("mid_liq_on", True) else 0.0,       # доля дневного оборота
            "min_turn": float(s.get("mid_min_turn", 0) or 0) * 1e6 if s.get("mid_minv_on", False) else 0.0,  # мин. оборот, ₽ в день
            "min_vol": float(s.get("mid_vol", 0) or 0) * 1e6 if s.get("mid_vol_on", False) else 0.0,     # мин. объём сегодня, ₽
            "prev_stop": bool(s.get("mid_prev_stop", True)),               # 2-й и следующие входы — стоп за лоем предыдущего движения
            "take_part": (min(95.0, max(5.0, float(s.get("mid_take_part", 50) or 50))) / 100) if s.get("mid_take_mode") == "part" else 1.0,
            "trail": max(0.0, float(s.get("mid_trail", 1.0) or 0)) / 100}


MID_MODES = [("idx_long", "индекс идёт вверх — лонг"), ("idx_short", "индекс идёт вниз — шорт"),
             ("both", "в обе стороны при любом индексе"), ("long", "только лонг при любом индексе"),
             ("short", "только шорт при любом индексе")]


def mid_modes(s):
    """Режимы торговли (можно несколько): лонг разрешён при long/both или idx_long и растущем IMOEX; шорт — зеркально."""
    v = s.get("mid_modes")
    if isinstance(v, list):
        out = [x for x in v if x in dict(MID_MODES)]
    else:                                             # старая настройка mid_dir
        d = s.get("mid_dir", "long")
        out = ["idx_long", "idx_short"] if d == "index" else [d] if d in ("long", "short", "both") else ["long"]
    return out or ["long"]


mid_allowed = at.mid_allowed
mid_sides = at.mid_sides


def mid_modes_text(modes):
    return ", ".join(n for k, n in MID_MODES if k in modes)


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
            d["pct"].append(at.net_pct(p, last) * 100)
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
        fees = [x for x in (_num(t.get("fee_rub")) for t in tr) if x is not None]
        row.update(fees_rub=round(sum(fees), 2) if fees else None)
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
    return {"balance": cur, "time": now.strftime("%d.%m.%Y %H:%M"), "periods": out, "fee": sc.fee_text(),
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


_AVG_TURN = {"date": None, "data": {}, "busy": False}


def avg_turnover(days=10):
    """Средний дневной оборот акций (₽) за прошлые торговые дни по итогам торгов Мосбиржи — {тикер: ₽}.
    Считается раз в день в фоне (около 30 запросов к Мосбирже); пока не готово — пустой словарь."""
    import threading as _t
    today = datetime.now(sc.MSK).date()
    if _AVG_TURN["date"] == today or _AVG_TURN["busy"]:
        return _AVG_TURN["data"]
    path = os.path.join(sc.HERE, "avg_turnover.json")
    try:
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
        if js.get("date") == today.isoformat():
            _AVG_TURN.update(date=today, data=js["data"])
            return _AVG_TURN["data"]
    except Exception:
        pass

    def job():
        tot, cnt, d, got = {}, {}, today - timedelta(days=1), 0
        try:
            while got < days and d > today - timedelta(days=days * 3):
                start, n_rows = 0, 0
                while True:
                    js = sc.iss_get("/history/engines/stock/markets/shares/boards/TQBR/securities.json",
                                    {"date": d.isoformat(), "start": start, "iss.only": "history", "history.columns": "SECID,VALUE"}, retries=2)
                    h = js.get("history") or {}
                    rows = h.get("data") or []
                    for sec, val in rows:
                        if val:
                            tot[sec] = tot.get(sec, 0) + float(val); cnt[sec] = cnt.get(sec, 0) + 1
                    n_rows += len(rows)
                    start += len(rows)
                    if len(rows) < 100:
                        break
                if n_rows:
                    got += 1
                d -= timedelta(days=1)
            data = {k: round(tot[k] / cnt[k]) for k in tot}
            _AVG_TURN.update(date=today, data=data)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": today.isoformat(), "days": got, "data": data}, f)
        except Exception:
            pass
        finally:
            _AVG_TURN["busy"] = False
    _AVG_TURN["busy"] = True
    _t.Thread(target=job, daemon=True).start()
    return _AVG_TURN["data"]


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
    avg = avg_turnover()
    for r in rows:
        r["avg_value"] = avg.get(r["ticker"])
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


# ====================================================================== проверка «Движения дня» на истории
import csv as _csv
import threading as _th
import time
from datetime import date as _date

BT_DIR = os.path.join(sc.HERE, "history_bt")          # кэш 5-мин свечей для проверки на истории
BT_COST = 0.0016                                       # (старое) комиссия и проскальзывание туда-обратно — теперь по тарифу брокера
BT_SLIP = sc.SLIPPAGE                                   # проскальзывание рыночных заявок туда-обратно


def bt_fee(ti=None):
    """Комиссия для проверки на истории: фактическая ставка по сделкам робота / тариф счёта у брокера.
    Возвращает (ставка за сделку, текст, абонплата ₽/мес)."""
    d = sc.fee_info()
    if d.get("rate"):
        r, name, code = float(d["rate"]), d.get("name") or "тариф брокера", str(d.get("key", "")).split(":")[-1]
        if d.get("observed"):
            name += " (по фактическим сделкам)"
    elif ti is not None:
        try:
            r, name, code = ti.tariff()
        except Exception as e:
            r, name, code = sc.FEE_FALLBACK[0], f"тариф не получен ({e}) — {sc.FEE_FALLBACK[1]}", ""
    else:
        r, name, code = sc.SANDBOX_FEE[0], "песочница Т-Инвестиций", "sandbox"
    return r, name, (390.0 if code == "trader" else 0.0)


def _bt_read(path):
    out = []
    with open(path, encoding="utf-8") as f:
        r = _csv.reader(f)
        next(r, None)
        for row in r:
            try:
                out.append((datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S"), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                            float(row[5]) if len(row) > 5 and row[5] else 0.0))
            except (ValueError, IndexError):
                pass
    return out


def _bt_write(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["begin", "open", "high", "low", "close", "value"])
        for r in rows:
            w.writerow([r[0].strftime("%Y-%m-%d %H:%M:%S"), r[1], r[2], r[3], r[4], round(r[5], 2) if len(r) > 5 else 0])
    os.replace(tmp, path)


def bt_bars(ti, token, ticker, since, log=print, prog=None):
    """5-мин свечи бумаги с даты since: кэш на диске + архив Т-Инвестиций (один запрос на год), запасной путь — Мосбиржа."""
    import history_dl as hd
    os.makedirs(BT_DIR, exist_ok=True)
    path = os.path.join(BT_DIR, f"{ticker}.csv")
    today = datetime.now(sc.MSK).date()
    old = _bt_read(path) if os.path.exists(path) else []
    if old and not any(r[5] for r in old[-500:]):
        old = []                                       # кэш старого вида (без оборота) — скачать заново
    fresh = old and datetime.fromtimestamp(os.path.getmtime(path)).date() == today
    if fresh and old[0][0].date() <= since + timedelta(days=7):
        return [r for r in old if r[0].date() >= since]
    # что докачать: годы, которых нет (или с начала текущего года — архив дополняется каждый день)
    have_from = old[0][0].date() if old else None
    years = list(range(since.year, today.year + 1))
    if have_from and have_from <= since + timedelta(days=7):
        years = [today.year]
        keep = [r for r in old if r[0].date() < _date(today.year, 1, 1)]
    else:
        keep = []
    prog = prog or (lambda txt: None)
    agg = hd.Agg5()
    ok = False
    try:
        sh = ti.share(ticker)
        for y in years:
            prog(f"архив {y}")
            hd.tinvest_year(token, sh["uid"], y, float(sh.get("lot") or 1), agg, since if y == since.year else _date(y, 1, 1))
        ok = True
    except Exception as e:
        log(f"  {ticker}: архив Т-Инвестиций не ответил ({e}) — беру 5-минутки через API")
    if ok:
        new = [(k, b[0], b[1], b[2], b[3], b[4]) for k, b in sorted(agg.bars.items())]
    else:                                              # запасной путь: 5-мин свечи API по неделям, не дольше 3 минут на бумагу
        new, d0, t_end = [], (_date(years[0], 1, 1) if keep else since), time.time() + 180
        lot = float(ti.share(ticker).get("lot") or 1)
        d = d0
        while d <= today:
            if time.time() > t_end:
                raise RuntimeError("история не скачалась за 3 минуты")
            prog(f"свечи API с {d:%d.%m.%Y}")
            a_ = datetime(d.year, d.month, d.day, tzinfo=sc.MSK)
            for r in ti.candles_range(ticker, a_, min(a_ + timedelta(days=7), datetime.now(sc.MSK)), 5):
                new.append((r["t"], r["o"], r["h"], r["l"], r["c"], r.get("v", 0) * lot * r["c"]))
            d += timedelta(days=7)
        new.sort()
    lo = new[0][0] if new else None
    rows = [r for r in keep if not lo or r[0] < lo] + new
    # сегодняшний день из архива ещё не пришёл — добрать свечами API
    try:
        last = rows[-1][0].date() if rows else since
        d = last + timedelta(days=1)
        while d <= today:
            if d.weekday() < 5 or d == today:
                for r in ti.candles(ticker, d, datetime(d.year, d.month, d.day, 6, 50, tzinfo=sc.MSK),
                                    datetime(d.year, d.month, d.day, 23, 59, tzinfo=sc.MSK), 5):
                    rows.append((r["t"], r["o"], r["h"], r["l"], r["c"], r.get("v", 0) * float(ti.share(ticker).get("lot") or 1) * r["c"]))
            d += timedelta(days=1)
    except Exception:
        pass
    if rows:
        _bt_write(path, rows)
    return [r for r in rows if r[0].date() >= since]


def bt_imoex(since, log=print):
    """IMOEX по 10-мин свечам Мосбиржи: {время конца свечи → изменение к вчерашнему закрытию}. Для фильтра «IMOEX в плюсе»."""
    os.makedirs(BT_DIR, exist_ok=True)
    path = os.path.join(BT_DIR, "_IMOEX10.csv")
    today = datetime.now(sc.MSK).date()
    rows = _bt_read(path) if os.path.exists(path) else []
    if not rows or datetime.fromtimestamp(os.path.getmtime(path)).date() != today or rows[0][0].date() > since:
        rows, start = [], 0
        while True:
            js = sc.iss_get("/engines/stock/markets/index/securities/IMOEX/candles.json",
                            {"interval": 10, "from": (since - timedelta(days=7)).isoformat(), "till": today.isoformat(), "start": start})
            c = js["candles"]
            for x in (dict(zip(c["columns"], r)) for r in c["data"]):
                rows.append((datetime.strptime(x["begin"], "%Y-%m-%d %H:%M:%S"), float(x["open"]), float(x["high"]),
                             float(x["low"]), float(x["close"])))
            start += len(c["data"])
            if len(c["data"]) < 500:
                break
        if rows:
            _bt_write(path, rows)
    out, prev_close, day, last = {}, None, None, None
    for t, o, h, l, c, *_ in rows:
        if t.date() != day:
            prev_close, day = last, t.date()
        if prev_close:
            out[t + timedelta(minutes=10)] = c / prev_close - 1
        last = c
    return out


def _bt_mids(rows, tf):
    """Середина (EMA10+EMA20)/2 по последней закрытой свече ТФ — для каждой 5-мин свечи."""
    a10, a20 = 2 / 11, 2 / 21
    e10 = e20 = None
    out = [None] * len(rows)
    cur_key, cur_close, mid = None, None, None
    for i, (t, o, h, l, c, *_) in enumerate(rows):
        m = t.hour * 60 + t.minute
        key = t.date() if tf >= 1440 else (t.date(), m // tf)
        if key != cur_key:
            if cur_close is not None:                 # предыдущая свеча ТФ закрылась
                e10 = cur_close if e10 is None else e10 + a10 * (cur_close - e10)
                e20 = cur_close if e20 is None else e20 + a20 * (cur_close - e20)
                mid = (e10 + e20) / 2
            cur_key = key
        cur_close = c
        out[i] = mid
    return out


def bt_run(data, m, imoex=None):
    """Проверка «Движения дня» по 5-мин свечам. data: {тикер: [(t,o,h,l,c), …]}. Возвращает список сделок.
    Лонг: бумага выросла на порог → покупка на середине EMA10/EMA20, стоп за лоем дня, тейк на хае.
    Шорт (зеркально): бумага упала на порог → шорт на середине, стоп за хаем дня, тейк на лое."""
    trades = []
    ex = m["exit"]
    fee = float(m.get("fee", sc.SANDBOX_FEE[0]))           # комиссия брокера за сделку (вход и выход — каждая)
    can_l, can_s = mid_sides(m.get("modes", ["long"]))
    idx_need = any(x in m.get("modes", []) for x in ("idx_long", "idx_short"))
    for tk, rows in data.items():
        if len(rows) < 100:
            continue
        mids = {tf: _bt_mids(rows, tf) for tf in m["tfs"]}
        days, start = [], 0
        didx = [0] * len(rows)
        for i in range(1, len(rows) + 1):
            if i == len(rows) or rows[i][0].date() != rows[start][0].date():
                for j in range(start, i):
                    didx[j] = len(days)
                days.append((start, i)); start = i
        dval = [sum(rows[j][5] if len(rows[j]) > 5 else 0 for j in range(a_, z_)) for a_, z_ in days]
        busy = -1                                     # строка, до которой по бумаге открыта (перенесённая) позиция
        for di, (a, z) in enumerate(days):
            last_in = None                            # строка прошлого входа в этот день (для стопа за предыдущим движением)
            prev = [v for v in dval[max(0, di - 10):di] if v > 0]
            liq = sum(prev) / len(prev) if prev else 0.0          # средний дневной оборот за 10 прошлых дней, ₽
            if m.get("min_turn") and liq and liq < m["min_turn"]:
                continue
            tm = [r[0].hour * 60 + r[0].minute for r in rows[a:z]]
            ii = [k for k, x in enumerate(tm) if x >= m["ref"]]
            if not ii or tm[ii[0]] > m["ref"] + 5:
                continue
            op = rows[a + ii[0]][1]
            day_from = a + ([k for k, x in enumerate(tm) if x >= 420] or [0])[0]
            q, sd = None, 0
            for r in range(a + ii[0], z):
                x = tm[r - a]
                if x < m["from"] - 5:
                    continue
                if x >= m["to"]:
                    break
                mv = rows[r][4] / op - 1
                if mv >= m["thr"] and can_l:
                    q, sd = r, 1
                    break
                if mv <= -m["thr"] and can_s:
                    q, sd = r, -1
                    break
            if q is None:
                continue
            arm = {tf: (mids[tf][q] is not None and (rows[q][4] - mids[tf][q]) * sd > 0) for tf in m["tfs"]}
            r = q + 1
            while r < z and tm[r - a] < m["to"]:
                t, o, h, l, c = rows[r][:5]
                hit = None
                for tf in m["tfs"]:
                    md = mids[tf][r]
                    if md is None:
                        continue
                    if arm[tf] and hit is None and ((l <= md) if sd > 0 else (h >= md)):
                        hit = (tf, md)
                if r <= busy:
                    r += 1
                    continue
                if hit is None or not mid_allowed(m.get("modes", ["long"]), sd,
                                                  (imoex.get(t.replace(minute=t.minute - t.minute % 10)) if imoex is not None else None)):
                    for tf in m["tfs"]:
                        md = mids[tf][r]
                        if md is not None and (c - md) * sd > 0:
                            arm[tf] = True
                    r += 1
                    continue
                tf, md = hit
                px = (o if o <= md else md) if sd > 0 else (o if o >= md else md)
                for x in m["tfs"]:
                    arm[x] = False
                if r <= day_from:
                    r += 1; continue
                if m.get("min_vol") and sum(x[5] if len(x) > 5 else 0 for x in rows[day_from:r]) < m["min_vol"]:
                    r += 1; continue                        # объём торгов сегодня к моменту входа меньше заданного
                lo_d = min(x[3] for x in rows[day_from:r]); hi_d = max(x[2] for x in rows[day_from:r])
                if sd > 0:
                    stop, take = lo_d * (1 - at.MID_STOP_BUF), hi_d
                else:
                    stop, take = hi_d * (1 + at.MID_STOP_BUF), lo_d
                if m.get("multi") and m.get("prev_stop", True) and last_in is not None and last_in < r:
                    # 2-й и следующие входы за день: стоп за дном (вершиной) отката после прошлого входа
                    ext = min(x[3] for x in rows[last_in:r]) if sd > 0 else max(x[2] for x in rows[last_in:r])
                    st2 = ext * (1 - at.MID_STOP_BUF) if sd > 0 else ext * (1 + at.MID_STOP_BUF)
                    if (px - st2) * sd / px >= at.MID_PREV_MIN and (st2 - stop) * sd > 0:
                        stop = st2
                if not ((px - stop) * sd > 0 and (take - px) * sd > 0):
                    r += 1; continue
                last_in = r
                ex_day = di if tm[r - a] < ex else di + 1          # вошли после времени выхода — выход на следующий день
                ret = lambda p_: sd * (p_ / px - 1)
                part = m.get("take_part", 1.0)
                trail = m.get("trail", 0.0)
                took, part_res, best = False, 0.0, px               # после частичного тейка: стоп в безубыток и трейлинг
                hit_stop = lambda k_: (rows[k_][3] <= stop) if sd > 0 else (rows[k_][2] >= stop)
                hit_take = lambda k_: (rows[k_][2] >= take) if sd > 0 else (rows[k_][3] <= take)
                res, why, xr = None, "", r
                if hit_stop(r):
                    res, why = ret(stop), "стоп"
                else:
                    k = r + 1
                    while k < len(rows):
                        tk_t = rows[k][0]
                        kd = didx[k]
                        if kd > ex_day or (kd == ex_day and tk_t.hour * 60 + tk_t.minute >= ex):
                            last_c = rows[k - 1][4]
                            held = ex_day - (di if tm[r - a] < ex else di + 1)       # сколько раз уже переносили
                            if m.get("carry", "none") != "none" and (m["carry"] == "all" or took or ret(last_c) > 0) \
                                    and (not m.get("carry_days") or held < m["carry_days"]) and k < len(rows) - 1:
                                ex_day += 1                                        # перенос на следующий день
                            else:
                                res, why, xr = ret(last_c), "время" if held == 0 else f"время (+{held} дн.)", k - 1
                                break
                        if took and trail:                                     # трейлинг от лучшей цены (по прошлым свечам)
                            ts = best * (1 - sd * trail)
                            if (ts - stop) * sd > 0:
                                stop = ts
                        if hit_stop(k):
                            fill = min(rows[k][1], stop) if sd > 0 else max(rows[k][1], stop)
                            res, why, xr = ret(fill), ("трейлинг" if (stop - px) * sd > 0 else "безубыток") if took else "стоп", k
                            break
                        if not took and hit_take(k):
                            fill = max(rows[k][1], take) if sd > 0 else min(rows[k][1], take)
                            if part >= 1:
                                res, why, xr = ret(fill), "тейк", k
                                break
                            took, part_res, best = True, ret(fill), fill
                            if (px - stop) * sd > 0:
                                stop = px                                      # стоп в безубыток
                            k += 1
                            continue
                        if took:
                            best = max(best, rows[k][2]) if sd > 0 else min(best, rows[k][3])
                        k += 1
                    if res is None:
                        res, why, xr = ret(rows[-1][4]), "конец данных", len(rows) - 1
                if took:
                    res = part * part_res + (1 - part) * res
                    why = f"тейк {part * 100:g}% + {why}"
                xp = px * (1 + sd * res)
                busy = xr
                trades.append({"date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M"), "ticker": tk, "tf": tf, "side": sd,
                               "liq": round(liq), "cap": round(liq * m["liq"]) if m.get("liq") and liq else None,
                               "entry": round(px, 6), "stop": round(stop, 6), "take": round(take, 6), "exit": round(xp, 6),
                               "exit_time": rows[xr][0].strftime("%d.%m %H:%M"), "reason": why,
                               "gross": round(res * 100, 3), "fee_pct": round(fee * (2 + res) * 100, 4),
                               "pct": round((res - fee * (2 + res) - BT_SLIP) * 100, 3)})
                if not m["multi"] or not (why in ("стоп", "тейк") or why.startswith("тейк ")) or xr >= z:
                    break
                r = xr + 1
    trades.sort(key=lambda x: (x["date"], x["time"]))
    return trades


def bt_summary(trades, share, capital=1_000_000, fee_month=0.0):
    """Итоги проверки. Каждая сделка — доля share от счёта на начало дня входа (как у робота), счёт растёт со сделками."""
    if not trades:
        return {"n": 0, "capital": capital, "final": capital, "profit_rub": 0, "profit_pct": 0}
    pcts = [t["pct"] for t in trades]
    win = [x for x in pcts if x > 0]
    los = [x for x in pcts if x <= 0]
    by_day = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t)
    eq, peak, dd, dd_rub, curve = float(capital), float(capital), 0.0, 0.0, []
    fees_rub, paid = 0.0, set()
    months = {}
    for d in sorted(by_day):
        start = eq
        mo = months.setdefault(d[:7], {"m": d[:7], "n": 0, "sum": 0.0, "start": start, "rub": 0.0})
        for t in by_day[d]:
            stake = start * share
            if t.get("cap"):
                stake = min(stake, t["cap"])                       # не больше доли дневного оборота бумаги
            t["stake"] = round(stake)
            t["rub"] = round(stake * t["pct"] / 100, 2)
            fees_rub += stake * t.get("fee_pct", 0) / 100
            if fee_month and d[:7] not in paid and eq < 1_500_000:      # абонплата тарифа — в месяц со сделками
                paid.add(d[:7])
                eq -= fee_month
                mo["rub"] -= fee_month
                fees_rub += fee_month
            eq += t["rub"]
            mo["n"] += 1
            mo["sum"] += t["pct"]
            mo["rub"] += t["rub"]
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
        dd_rub = min(dd_rub, eq - peak)
        curve.append([d, round((eq / capital - 1) * 100, 3), round(eq, 2)])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"n": len(trades), "win": round(len(win) / len(trades) * 100, 1), "avg": round(sum(pcts) / len(pcts), 3),
            "sum": round(sum(pcts), 2), "total": round((eq / capital - 1) * 100, 2), "dd": round(dd * 100, 2), "dd_rub": round(dd_rub, 2),
            "capital": capital, "final": round(eq, 2), "profit_rub": round(eq - capital, 2), "profit_pct": round((eq / capital - 1) * 100, 2),
            "pf": round(sum(win) / -sum(los), 2) if los and sum(los) < 0 else None,
            "avg_win": round(sum(win) / len(win), 3) if win else None, "avg_loss": round(sum(los) / len(los), 3) if los else None,
            "reasons": reasons, "curve": curve, "fees_rub": round(fees_rub, 2), "fee_month": fee_month,
            "months": [{"m": v["m"], "n": v["n"], "sum": round(v["sum"], 2), "rub": round(v["rub"], 2),
                        "eq": round(v["rub"] / v["start"] * 100, 2)} for _, v in sorted(months.items())]}


class Backtest:
    """Фоновая проверка на истории: одна за раз; состояние читают экран программы и телефон.
    На сервере (Linux) считается в отдельном процессе с низким приоритетом — робот и связь с телефоном не тормозят."""
    def __init__(self):
        self._state = {"running": False, "progress": "", "pct": 0, "result": None, "error": None}
        self._stop = _th.Event()
        self._proc = None
        self._sp = os.path.join(BT_DIR, "_state.json")

    @property
    def state(self):
        p = self._proc
        if p is not None:
            try:
                with open(self._sp, encoding="utf-8") as f:
                    st = json.load(f)
            except Exception:
                st = None
            if p.poll() is None:
                if st:
                    st["running"] = True
                    self._state = st
            else:
                self._proc = None
                if st:
                    self._state = st
                self._state["running"] = False
                if not st or st.get("progress") not in ("готово", "ошибка"):
                    self._state.setdefault("error", None)
                    self._state["error"] = self._state.get("error") or "проверка прервалась"
        return self._state

    @state.setter
    def state(self, v):
        self._state = v

    def start(self, token, sandbox, s, months, tickers=None, log=print, capital=None):
        if self.state["running"]:
            return False
        self._stop.clear()
        self.state = {"running": True, "progress": "начинаю…", "pct": 0, "result": None, "error": None,
                      "started": datetime.now(sc.MSK).strftime("%d.%m %H:%M")}
        cap = float(capital or s.get("capital") or 1_000_000)
        import sys as _sys
        if os.name != "nt" and not getattr(_sys, "frozen", False):
            return self._start_proc(token, sandbox, dict(s), int(months), tickers, log, cap)
        _th.Thread(target=self._run, args=(token, sandbox, dict(s), int(months), tickers, log, cap), daemon=True).start()
        return True

    def _start_proc(self, token, sandbox, s, months, tickers, log, cap):
        import subprocess, sys as _sys
        os.makedirs(BT_DIR, exist_ok=True)
        job = os.path.join(BT_DIR, "_job.json")
        with open(job, "w", encoding="utf-8") as f:
            json.dump({"token": token, "sandbox": sandbox, "s": s, "months": months, "tickers": tickers, "capital": cap,
                       "state": self._sp}, f, ensure_ascii=False)
        try:
            os.chmod(job, 0o600)
        except Exception:
            pass
        with open(self._sp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False)
        self._proc = subprocess.Popen([_sys.executable, "-c", f"import robotcore as rc; rc._bt_child({job!r})"], cwd=sc.HERE,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                      preexec_fn=_bt_limits)
        pr = self._proc

        def pump():
            for line in pr.stdout:
                line = line.rstrip()
                if line:
                    log(line)
        _th.Thread(target=pump, daemon=True).start()
        return True

    def stop(self):
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def reset(self):
        """Сброс: остановить, если идёт, и убрать результат (скачанная история остаётся — следующий прогон быстрый)."""
        self.stop()
        self._proc = None
        self._state = {"running": False, "progress": "", "pct": 0, "result": None, "error": None}
        try:
            os.remove(self._sp)
        except Exception:
            pass

    def _run(self, token, sandbox, s, months, tickers, log, capital=1_000_000):
        try:
            if not token:
                raise ValueError("нужен токен Т-Инвестиций (Настройки)")
            ti = sc.TInvest(token, "sandbox" if sandbox else "prod")
            m = mid_cfg(s)
            m["fee"], fee_name, fee_month = bt_fee(ti)
            dm = list(s.get("day_markets") or ["imoex"])
            if not tickers:
                self.state["progress"] = "список бумаг…"
                idx = sc.imoex_tickers()
                if dm == ["imoex"]:
                    tickers = idx
                else:
                    b = at.SandboxBroker.__new__(at.SandboxBroker)
                    b.ti, b.log = ti, log
                    tickers = b.universe(dm, idx, log) or idx
            since = (datetime.now(sc.MSK) - timedelta(days=int(months * 30.5))).date()
            imx = None
            if any(x in m["modes"] for x in ("idx_long", "idx_short")):
                self.state["progress"] = "IMOEX…"
                try:
                    imx = bt_imoex(since, log)
                except Exception as e:
                    log(f"IMOEX для фильтра не получен ({e}) — фильтр в проверке не учитывается")
            # по одной бумаге: скачать → посчитать → забыть свечи (память сервера не растёт с числом бумаг)
            trades, fails, done_n = [], [], 0
            for n, tk in enumerate(tickers, 1):
                if self._stop.is_set():
                    raise RuntimeError("остановлено")
                self.state.update(progress=f"свечи {tk} ({n}/{len(tickers)})", pct=int(n / len(tickers) * 98))
                pr = lambda txt, tk=tk, n=n: self.state.update(progress=f"свечи {tk} ({n}/{len(tickers)}): {txt}")
                try:
                    rows = bt_bars(ti, token, tk, since, log, pr)
                except Exception as e:
                    fails.append(tk)
                    log(f"  {tk}: история не получена ({e})")
                    continue
                if rows:
                    done_n += 1
                    trades += bt_run({tk: rows}, m, imx)
                del rows
            trades.sort(key=lambda x: (x["date"], x["time"]))
            sm = bt_summary(trades, m["share"], capital, fee_month)
            tfn = {5: "5м", 15: "15м", 60: "1ч", 1440: "1д"}
            self.state["result"] = {
                "summary": sm, "trades": trades[-400:], "tickers": done_n, "fails": fails,
                "period": f"{since:%d.%m.%Y} — {datetime.now(sc.MSK):%d.%m.%Y}",
                "settings": (f"движение {m['thr'] * 100:g}% от {at.hm(m['ref'])}, EMA {', '.join(tfn[x] for x in m['tfs'])}, "
                             f"входы {at.hm(m['from'])}–{at.hm(m['to'])}, выход {at.hm(m['exit'])}, "
                             + "режим: " + mid_modes_text(m["modes"]) + ", "
                             f"{'несколько входов' if m['multi'] else 'один вход'} в день"
                             + {"none": ", во время выхода всё закрыть", "all": ", перенос всех позиций", "profit": ", перенос позиций в плюсе"}[m["carry"]]
                             + (f" (до {m['carry_days']} дн.)" if m["carry"] != "none" and m["carry_days"] else "")
                             + f", {m['share'] * 100:g}% счёта на сделку"
                             + (f", но не больше {m['liq'] * 100:g}% дневного оборота бумаги" if m["liq"] else "")
                             + (f", оборот не меньше {m['min_turn'] / 1e6:g} млн ₽/день" if m["min_turn"] else "")
                             + (f", объём сегодня не меньше {m['min_vol'] / 1e6:g} млн ₽" if m.get("min_vol") else "")
                             + (f", на хае дня закрыть {m['take_part'] * 100:g}% → безубыток → трейлинг {m['trail'] * 100:g}%" if m["take_part"] < 1 else "")
                             + (", 2-й и следующие входы — стоп за лоем предыдущего движения" if m["multi"] and m["prev_stop"] else "") + "; "
                             f"рынок: {', '.join(dict(at.MARKETS).get(x, x) for x in dm)}; "
                             f"комиссия брокера {m['fee'] * 100:.3g}% за каждую покупку и продажу — {fee_name}"
                             + (f", абонплата {fee_month:g} ₽ в месяц со сделками" if fee_month else "")
                             + f", проскальзывание {BT_SLIP * 100:.2g}% на сделку"),
                "done": datetime.now(sc.MSK).strftime("%d.%m %H:%M")}
            self.state.update(progress="готово", pct=100)
        except Exception as e:
            self.state.update(error=str(e), progress="ошибка")
        finally:
            self.state["running"] = False


BACKTEST = Backtest()


def _bt_limits():
    """Для процесса проверки: низкий приоритет и не больше 700 МБ памяти — сервер робота не должен задыхаться."""
    os.nice(15)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (700 * 2 ** 20, 700 * 2 ** 20))
    except Exception:
        pass


def _bt_child(job_path):
    """Отдельный процесс проверки: читает задание, считает, раз в секунду пишет состояние в файл."""
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    try:
        os.remove(job_path)                          # там токен — сразу удалить
    except Exception:
        pass
    bt = Backtest()
    bt._state = {"running": True, "progress": "начинаю…", "pct": 0, "result": None, "error": None,
                 "started": datetime.now(sc.MSK).strftime("%d.%m %H:%M")}
    done = _th.Event()

    def writer():
        while True:
            try:
                tmp = job["state"] + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(bt._state, f, ensure_ascii=False)
                os.replace(tmp, job["state"])
            except Exception:
                pass
            if done.is_set():
                return
            done.wait(1)
    w = _th.Thread(target=writer, daemon=True)
    w.start()
    bt._run(job["token"], job["sandbox"], job["s"], job["months"], job.get("tickers"), lambda m: print(m, flush=True), job["capital"])
    done.set()
    w.join(5)
