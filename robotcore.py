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
            "spike_half": 50, "spike_trail": 1.0, "spike_max": 5, "spike_share": 10, "two_takes": True,
            "tg_token": "", "tg_chat": "", "tg_form": True, "tg_trades": True, "tg_imp": False,
            "tg_trend": True, "trend_on": True, "trend_trade": False, "tier_profile": True, "entry_window": "9-19", "exit_late": True, "carry_all": True}

CHART_DEPTH = {1: 2, 5: 10, 15: 30, 60: 90, 1440: 400}       # сколько дней истории грузить на график


def load_settings(path=SETTINGS):
    s = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
        s.pop("all_hours", None)                 # старое окно 07:00–23:40 заменено выбором 9–19 / 10–18
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
            "trail_r": float(s.get("spike_trail", 1.0)), "max_pos": int(s.get("spike_max", 5)),
            "share": float(s.get("spike_share", 10)) / 100,
            "leverage": s.get("leverage", "1"),
            "two_takes": bool(s.get("two_takes", True)),
            "trend_on": bool(s.get("trend_on", True)), "trend_trade": bool(s.get("trend_trade", False)),
            "tier_profile": bool(s.get("tier_profile", True)), "window": s.get("entry_window", "9-19"),
            "exit_late": bool(s.get("exit_late", True)), "carry_all": bool(s.get("carry_all", True))}


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
