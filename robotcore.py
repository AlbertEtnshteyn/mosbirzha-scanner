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
            "spike_on": True, "spike_tfs": [15, 60], "spike_markets": ["all_shares"], "direction": "both", "leverage": "1",
            "spike_half": 50, "spike_trail": 1.0, "spike_max": 0, "spike_share": 10, "two_takes": True,
            "tg_token": "", "tg_chat": "", "tg_form": True, "tg_trades": True, "tg_imp": False,
            "tg_trend": True, "trend_on": True, "trend_trade": False, "tier_profile": True, "entry_window": "9-19", "exit_late": True, "carry_all": True, "day_carry": 0}

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


def spike_cfg(s):
    """Параметры стратегии «всплеск объёма + откат к EMA20» из настроек."""
    return {"on": bool(s.get("spike_on")), "tfs": list(s.get("spike_tfs", [15, 60])),
            "markets": list(s.get("spike_markets", ["all_shares"])), "half": int(s.get("spike_half", 50)) / 100,
            "trail_r": float(s.get("spike_trail", 1.0)), "max_pos": int(s.get("spike_max", 0) or 0),
            "share": float(s.get("spike_share", 10)) / 100,
            "leverage": s.get("leverage", "1"),
            "two_takes": bool(s.get("two_takes", True)),
            "trend_on": bool(s.get("trend_on", True)), "trend_trade": bool(s.get("trend_trade", False)),
            "tier_profile": bool(s.get("tier_profile", True)), "window": s.get("entry_window", "9-19"),
            "exit_late": bool(s.get("exit_late", True)), "carry_all": bool(s.get("carry_all", True)),
            "day_carry": int(s.get("day_carry", 0) or 0)}


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
        trades = at.history_trades(bars, at.params(chart_sp(s), tf), tf, tier, s.get("entry_window", "9-19"),
                                   bool(s.get("exit_late", True)), bool(s.get("carry_all", True)))
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
        k = "всплеск" if p.get("strat") == "spike" else "15:00"
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
    trades = [dict(r, strat="всплеск") for r in _read(sc.SPIKE_LOG)] + [dict(r, strat="15:00") for r in _read(sc.SANDBOX_LOG)]
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
                   by_strat={s: strat_summary([t for t in tr if t["strat"] == s]) for s in ("всплеск", "15:00")})
        out.append(row)
    # баланс по дням (последнее значение дня) — для графика
    daily = {}
    for t, v, _ in bal:
        daily[t.date().isoformat()] = v
    if cur:
        daily[now.date().isoformat()] = cur
    return {"balance": cur, "time": now.strftime("%d.%m.%Y %H:%M"), "periods": out,
            "series": [{"date": d, "value": round(v, 2)} for d, v in sorted(daily.items())]}
