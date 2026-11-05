# -*- coding: utf-8 -*-
"""
Автоторговля: одна кнопка «Старт» — робот сам работает по будням с 07:00 до 18:30 МСК.

С запуска он следит за рынком: каждую минуту с открытия основной сессии (10:00) пересчитывает
движение всех акций индекса от цены открытия дня и среднее движение рынка (индекс), а также
ищет всплески объёма на минутках.

Стратегия «15:00» (проверена на истории): во время входа открывает сделки по правилу стратегии,
стоп 3%, в 16:59:50 закрывает всё. Если приложение запущено позже времени входа (до 16:30),
входит только в те бумаги, у которых сигнал был во время входа и сохранился сейчас.

Стратегия «всплеск объёма» (по желанию, на истории в минусе): оборот минуты ≥ 50 млн ₽ и в N раз
выше среднего за 20 минут, тело свечи ≥ порога, индекс в ту же сторону, следующая минута
продолжает движение → вход с тейком и стопом, выход не позже 18:25.

Открытые позиции и отметки «сегодня уже входили» хранятся в auto_state.json рядом с программой,
поэтому после перезапуска робот продолжает сопровождать позиции, а не открывает их повторно.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import scanner as sc

DAY_START = 7 * 60            # 07:00 — робот начинает рабочий день
DAY_END = 23 * 60 + 50        # 23:50 — мониторинг и торговля до конца вечерней сессии
SESSION_CLOSE = 18 * 60 + 40  # 18:40 — конец основной сессии
TRADE_END = 18 * 60 + 30      # 18:30 — к этому времени все позиции закрыты
STOP_POLL = 15                # проверка стопов и тейков, секунд
SCAN_EVERY = 60               # пересчёт рынка и поиск всплесков, секунд
MAX_POS_RESCAN = 8            # при добивке новых сигналов 15:00: доля капитала на сделку = 1/8
STATE_FILE = os.path.join(sc.HERE, "auto_state.json")

# Пресеты по таймфрейму: лучшие варианты на истории 10.2025–10.2026 (44 акции, затраты 0,16%)
# Параметры формации по таймфреймам и результат на истории 10.2025–10.2026 (44 акции индекса, затраты 0,16%)
SPIKE_PRESETS = {
    5: {"K": 6, "mult": 3.0, "body": 0.02, "win": 40, "min_value": 20e6, "buf": 0.003, "touch_close": False,
        "bt": "5 мин: 556 сделок за год (около 2 в день на 44 акциях), в плюсе 43%, в среднем −0,12% после затрат — в минусе"},
    15: {"K": 4, "mult": 3.0, "body": 0.03, "win": 16, "min_value": 60e6, "buf": 0.003, "touch_close": False,
         "bt": "15 мин: 78 сделок за год, в плюсе 53%, в среднем +0,24% после затрат (первая половина года +0,26%, вторая +0,22%)"},
    60: {"K": 1, "mult": 5.0, "body": 0.01, "win": 8, "min_value": 150e6, "buf": 0.001, "touch_close": True,
         "bt": "1 час: 20 сделок за год, в плюсе 60%, в среднем +0,24% после затрат"},
}
SPIKE_DEFAULTS = {"on": True, "tfs": [15, 60], "half": 0.5, "trail_r": 1.0, "buf": 0.001, "index": True,
                  "markets": ["all_shares"], "day_markets": ["imoex"], "entry_from": "", "day_ref": "main", "window": "9-19", "exit_late": True, "carry_all": True, "day_carry": 0, "day_mode": "fixed", "idx_day": True, "idx_flip": "off", "idx_ref": "imoex", "ema200": False, "trade_weekends": False, "max_pos": 0, "share": 0.10,
                  "from_min": 10 * 60, "to_min": 18 * 60, "exit_min": 18 * 60 + 25,   # окно входов (проверено на истории)
                  "scan_from": 7 * 60, "scan_to": 23 * 60 + 40}                     # окно поиска формаций — весь торговый день
# Какими инструментами торгует формация (выбор в настройках)
MARKETS = [
    ("all_shares", "Все российские акции (без 3-го эшелона)"),
    ("blue", "Голубые фишки Мосбиржи"),
    ("imoex", "Акции индекса Мосбиржи"),
    ("third", "Третий эшелон"),
    ("fx_metals", "Валюты и металлы"),
    ("idx_comm", "Индексы и товары (фьючерсы)"),
    ("futures", "Фьючерсы (все, ближайшие контракты)"),
]
HIST_DAYS = 5                 # сколько календарных дней истории грузить для EMA20 и медианы объёма


WIN_NOTIFY = {5: 96, 15: 48, 60: 14}   # сколько свечей после импульса ещё искать откат для уведомлений (весь день)


def params(sp, tf):
    p = dict(sp)
    p.update({k: v for k, v in SPIKE_PRESETS[tf].items() if k != "bt"})
    p["tf"] = tf
    p["win_trade"] = p["win"]                 # для входа — проверенное на истории окно
    p["win"] = max(p["win"], WIN_NOTIFY[tf])  # для поиска и уведомлений — весь день
    return p


def aggregate(bars5, tf):
    """Свечи 5 мин → свечи tf (границы по часам, как на графике)."""
    if tf == 5:
        return bars5
    out = []
    for b in bars5:
        m = b["t"].hour * 60 + b["t"].minute
        start = b["t"].replace(hour=(m // tf * tf) // 60, minute=(m // tf * tf) % 60)
        if out and out[-1]["t"] == start:
            o = out[-1]
            o["h"] = max(o["h"], b["h"]); o["l"] = min(o["l"], b["l"]); o["c"] = b["c"]; o["val"] += b["val"]
            o["n"] += 1
        else:
            out.append({"t": start, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "val": b["val"], "n": 1})
    return out


def ema(vals, span=20):
    k = 2 / (span + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def _median(xs):
    xs = sorted(xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _impulse(bars, i, sp, minv, em=None):
    """
    Импульс — серия из 1..K свечей подряд, заканчивающаяся свечой i (свеча i — по направлению импульса):
    движение от открытия первой свечи ≥ порога, средний оборот серии ≥ mult × медиана оборота 20 свечей до неё.
    Возвращает (начало, сторона, медиана, движение, оборот) или None.
    """
    bi = bars[i]
    tod = bi["t"].hour * 60 + bi["t"].minute
    if not (sp.get("scan_from", sp["from_min"]) <= tod < sp.get("scan_to", sp["to_min"])):
        return None
    total = 0.0
    for k in range(1, sp.get("K", 1) + 1):
        a = i - k + 1
        if a < 1 or bars[a]["t"].date() != bi["t"].date():
            return None
        total += bars[a]["val"]
        base = [b["val"] for b in bars[max(0, a - 20):a]]
        if len(base) < 10:
            return None
        med = _median(base)
        mv = bi["c"] / bars[a]["o"] - 1
        slot_ok = True
        if sp.get("slot_mult"):
            # и не меньше slot_mult × обычного оборота для этого времени дня (медиана тех же свечей за прошлые дни)
            sl = [b.get("slot") for b in bars[a:i + 1]]
            if all(x for x in sl):
                slot_ok = total >= sp["slot_mult"] * sum(sl)
        if med > 0 and slot_ok and abs(mv) >= sp["body"] and total / k >= sp["mult"] * med and total >= minv:
            s = 1 if mv > 0 else -1
            # свеча i — по направлению импульса и закрылась по свою сторону EMA20 (рост — над EMA20, падение — под ней).
            # Красная свеча над EMA20 после роста — это начало отката, а не шорт-импульс.
            if (bi["c"] - bi["o"]) * s > 0 and (em is None or (bi["c"] - em[i]) * s > 0):
                return a, s, med, mv, total
    return None


def add_slot(bars, days=10, min_days=3):
    """Каждой свече — медиана оборота свечей того же времени дня за прошлые дни (поле slot)."""
    hist = {}
    for b in bars:
        key = (b["t"].hour, b["t"].minute)
        prev = hist.setdefault(key, [])
        b["slot"] = _median(prev[-days:]) if len(prev) >= min_days else None
        prev.append(b["val"])
    return bars


def find_pullback(bars, sp, em=None, last=None):
    """
    bars: закрытые свечи ТФ подряд (t, o, h, l, c, val — оборот ₽). Ищет формацию, у которой свеча-подтверждение —
    последняя свеча: импульс на объёме → касание EMA20 → отскок. Возвращает dict или None.
    """
    last = len(bars) - 1 if last is None else last
    n = last + 1                      # свечи после last не видны (для поиска по истории)
    if n < 25:
        return None
    em = em or ema([b["c"] for b in bars])
    minv = sp["min_value"]
    for i in range(max(1, last - sp["win"]), last - 1):
        imp = _impulse(bars, i, sp, minv, em)
        if not imp:
            continue
        a, s, med, body, total = imp
        bi = dict(bars[i], o=bars[a]["o"], val=total, t=bars[a]["t"], n=i - a + 1, t_end=bars[i]["t"])   # импульс как одна «свеча»
        bi["h"] = max(b["h"] for b in bars[a:i + 1])
        bi["l"] = min(b["l"] for b in bars[a:i + 1])
        ext = bi["h"] if s > 0 else bi["l"]
        touch = -1
        pl = None                     # крайняя точка всего отката (с «закруглением» под EMA20) — под ней стоп
        for j in range(i + 1, min(n, i + sp["win"] + 1)):
            bj = bars[j]
            if bj["t"].date() != bi["t"].date() or (bj["c"] - bi["o"]) * s < 0:
                break
            if touch >= 0 and j == touch + 1:
                bt = bars[touch]
                conf = (bj["c"] > bj["o"] and bj["c"] > bt["c"]) if s > 0 else (bj["c"] < bj["o"] and bj["c"] < bt["c"])
                if conf and (bj["c"] - em[j]) * s > 0:
                    if j == last:
                        lo = min(pl, bj["l"]) if s > 0 else max(pl, bj["h"])
                        return {"side": s, "impulse": bi, "touch": bt, "confirm": bj, "level": lo, "t1": ext,
                                "ema": em[j], "value": bi["val"], "mult": bi["val"] / bi["n"] / med, "body": body,
                                "buf": sp["buf"], "age": j - i, "late": j - i > sp.get("win_trade", sp["win"])}
                    break
            tch = (bj["l"] <= em[j]) if s > 0 else (bj["h"] >= em[j])
            # отбой одной свечой: коснулась EMA20, закрылась по направлению, над EMA20 и в верхних 40% своего диапазона
            rng = bj["h"] - bj["l"]
            if tch and rng > 0 and (bj["c"] - em[j]) * s > 0 and (bj["c"] - bj["o"]) * s > 0 and \
                    ((bj["c"] - bj["l"]) if s > 0 else (bj["h"] - bj["c"])) >= 0.6 * rng:
                if j == last:
                    lo = (min(pl, bj["l"]) if pl is not None else bj["l"]) if s > 0 else \
                        (max(pl, bj["h"]) if pl is not None else bj["h"])
                    return {"side": s, "impulse": bi, "touch": bj, "confirm": bj, "level": lo, "t1": ext,
                            "ema": em[j], "value": bi["val"], "mult": bi["val"] / bi["n"] / med, "body": body,
                            "buf": sp["buf"], "age": j - i, "late": j - i > sp.get("win_trade", sp["win"]), "hammer": True}
                break
            if tch and (not sp.get("touch_close", True) or (bj["c"] - em[j] * (1 - s * 0.003)) * s >= 0):
                touch = j
            pl = (bj["l"] if pl is None else min(pl, bj["l"])) if s > 0 else (bj["h"] if pl is None else max(pl, bj["h"]))
            ext = max(ext, bj["h"]) if s > 0 else min(ext, bj["l"])
    return None


TREND_DAYS = 3     # трендовый откат: импульс на объёме был не раньше чем 3 торговых дня назад

# Что торговать по эшелонам — по прогону на 152 акциях за 10.2025–10.2026 (затраты 0,16%):
#   индекс: формация 15 мин (+0,16% на сделку) и 1 час;
#   2-й эшелон: формация 1 час с порогом оборота 15 млн (+0,24%, 50 сделок, обе половины года в плюсе)
#               и трендовый откат 15 мин после сильного роста: объём ×10, рост от 5%, не раньше вчера (+0,21%, 100 сделок);
#   3-й эшелон и всё остальное — только уведомления (на истории в минусе).
TIER2_TURNOVER = 30e6       # средний дневной оборот для 2-го эшелона, ₽
TIER2_TREND = {"mult": 10.0, "body": 0.05, "K": 4, "min_value": 5e6, "trend_days": 1, "slot_mult": 2.0}


MAX_CARRY_DAYS = 2
DAY_SIG_FROM, DAY_SIG_TO = 9 * 60, 19 * 60        # «движение дня» в режиме «сразу при сигнале»


WINDOWS = {
    "9-19": {"from": 9 * 60, "to": 19 * 60, "exit_early": 19 * 60 + 25, "carry": True, "name": "09:00–19:00"},
    "10-18": {"from": 10 * 60, "to": 18 * 60, "exit_early": 18 * 60 + 45, "carry": True, "name": "10:00–18:00"},
}
LATE_EXIT = 23 * 60 + 45
EXIT_CHOICES = [f"{h:02d}:00" for h in range(10, 24)] + ["18:45", "19:25", "23:45"]   # время выхода формации на выбор
DAY_EXIT = 18 * 60 + 45        # «Движение дня»: выход по умолчанию (выбор в настройках: 17:00 или 18:45)


def trade_profile(tier, tf, kind, window="9-19", late=True):
    """
    Окно входов и время выхода — выбор в настройках; позиция в плюсе (или с взятым тейком1) к выходу не закрывается,
    а переносится на следующий день (не больше 2 дней). Прогон на 152 акциях, 10.2025–10.2026, затраты 0,16%:
      09:00–19:00, выход 19:25: 303 сделки, в плюсе 44%, +0,52% на сделку, за год +16,8%, просадка −1,8%;
      09:00–19:00, выход 23:45: 303 сделки, в плюсе 45%, +0,45% на сделку, за год +14,2%, просадка −1,9%;
      10:00–18:00, выход 23:45: 251 сделка, в плюсе 47%, +0,53% на сделку, за год +14,0%, просадка −2,3%;
      10:00–18:00, выход 18:25 (для сравнения): 251 сделка, в плюсе 45%, +0,58%, за год +15,3%.
    """
    if not isinstance(window, str):
        window = "9-19"
    start = None
    if "@" in window:                                  # «9-19@7» — начало входов с 07:00 (настройка «время входов от…»)
        window, start = window.split("@", 1)
    w = dict(WINDOWS.get(window, WINDOWS["9-19"]))
    if start and start.isdigit():
        w["from"] = int(start) * 60
        w["name"] = f"{hm(w['from'])}–{hm(w['to'])}"
    if isinstance(late, str) and ":" in late:          # выбранное время выхода, например "18:45"
        hh, mm = (int(x) for x in late.split(":"))
        w["exit"] = hh * 60 + mm
    else:
        w["exit"] = LATE_EXIT if late else w["exit_early"]
    w["name"] = f"{w['name']}, выход {hm(w['exit'])}"
    return w


def tier_rules(tier, tf, kind):
    """(можно ли торговать, пояснение) для эшелона, таймфрейма и вида сигнала ("form" / "trend")."""
    if tier == "index":
        if kind == "form" and tf in (15, 60):
            return True, ""
        if kind == "form" and tf == 5:
            return True, ""                                          # 5 мин — только если включены в настройках
        return False, "акции индекса: трендовые откаты на истории в минусе — только уведомление"
    if tier == "2":
        if kind == "form" and tf == 60:
            return True, ""
        if kind == "trend" and tf == 15:
            return True, ""
        return False, "2-й эшелон: этот вариант на истории в минусе — только уведомление"
    return False, "3-й эшелон / прочее: на истории в минусе — только уведомление"


def find_trend_pullback(bars, sp, em20):
    """
    Трендовый откат к EMA20 (объём мог быть и раньше): за последние TREND_DAYS торговых дней в бумаге был импульс
    на объёме в ту же сторону, EMA10 над EMA20 и EMA20 растёт (для шорта — наоборот), предпоследняя свеча коснулась
    EMA20, последняя — подтверждение (по направлению, закрылась дальше предыдущей и по свою сторону EMA20).
    """
    n = len(bars)
    if n < 30:
        return None
    i, t = n - 1, n - 2
    em10 = ema([b["c"] for b in bars], 10)
    bi, bt = bars[i], bars[t]
    for s in (1, -1):
        if not ((em10[t] - em20[t]) * s > 0 and (em20[t] - em20[t - 5]) * s > 0):
            continue
        touch = (bt["l"] <= em20[t]) if s > 0 else (bt["h"] >= em20[t])
        conf = ((bi["c"] > bi["o"] and bi["c"] > bt["c"]) if s > 0 else (bi["c"] < bi["o"] and bi["c"] < bt["c"])) \
            and (bi["c"] - em20[i]) * s > 0
        if not (touch and conf):
            continue
        days = sorted({b["t"].date() for b in bars})
        nd = sp.get("trend_days", TREND_DAYS)
        first_day = days[-nd - 1] if len(days) > nd else days[0]
        imp = None
        k = t - 1
        while k > 0 and bars[k]["t"].date() >= first_day:
            r = _impulse(bars, k, sp, sp["min_value"], em20)
            if r and r[1] == s:
                imp = (k, r)
                break
            k -= 1
        if not imp:
            continue
        k, (a, s_, med, body, total) = imp
        ext = max(b["h"] for b in bars[a:i + 1]) if s > 0 else min(b["l"] for b in bars[a:i + 1])
        lo = min(bt["l"], bi["l"], bars[t - 1]["l"]) if s > 0 else max(bt["h"], bi["h"], bars[t - 1]["h"])
        impd = dict(bars[k], o=bars[a]["o"], t=bars[a]["t"], val=total, n=k - a + 1)
        return {"side": s, "impulse": impd, "touch": bt, "confirm": bi, "level": lo, "t1": ext, "ema": em20[i],
                "value": total, "mult": total / (k - a + 1) / med, "body": body, "buf": sp["buf"], "age": i - k,
                "late": False, "trend": True}
    return None


def history_trades(bars, sp, tf=None, tier="index", window="9-19", late=True, carry_all=True):
    """
    Сделки на истории графика по правилам робота: формация (и трендовый откат для 2-го эшелона на 15 мин),
    правила эшелонов (5 мин и 3-й эшелон не торгуются), окно входов, выход в конце дня, перенос позиции в плюсе
    или с взятым тейком1 на следующий день (до 2 дней). Фильтр индекса здесь не применяется (нет данных индекса).
    Внутри свечи — худший случай (сначала стоп). Возвращает список сделок.
    """
    tf = tf or sp.get("tf", 15)
    if tf == 5 or tier == "3":
        return []
    p = dict(sp)
    if tier == "2" and tf == 60:
        p["min_value"] = 15e6
    if tf == 60:
        p["slot_mult"] = 2.0
    add_slot(bars)
    pt = dict(p, **TIER2_TREND) if (tier == "2" and tf == 15) else None
    em = ema([b["c"] for b in bars])
    out, used_days, busy_until = [], set(), -1
    two = sp.get("two_takes", True)
    w1 = sp.get("half", 0.5)
    for k in range(25, len(bars) - 1):
        if k <= busy_until:
            continue
        sig, kind = find_pullback(bars, p, em, k), "form"
        if pt and not sig:
            sig, kind = find_trend_pullback(bars[:k + 1], pt, em[:k + 1]), "trend"
        if not sig or sig.get("late"):
            continue
        ok, _ = tier_rules(tier, tf, kind)
        if not ok:
            continue
        prof = trade_profile(tier, tf, kind, window, late)
        e = k + 1
        if bars[e]["t"].date() != bars[k]["t"].date() or bars[k]["t"].date() in used_days:
            continue
        te = bars[e]["t"].hour * 60 + bars[e]["t"].minute
        _te = sig["impulse"].get("t_end", sig["impulse"]["t"])
        ti = _te.hour * 60 + _te.minute
        if not (prof["from"] <= te < prof["to"]) or ti < prof["from"]:
            continue
        s = sig["side"]
        px = bars[e]["o"]
        st = sig["level"] * (1 - s * sig["buf"])
        R = abs(px - st)
        if R <= 0 or (st - px) * s >= 0 or (sig["t1"] - px) * s < 0.5 * R:
            continue
        used_days.add(bars[k]["t"].date())
        st0 = st
        lv1 = px + 0.5 * (sig["t1"] - px) if two else sig["t1"]
        lv2 = sig["t1"] if two else None
        stage, left, peak, exits, q, last, carried = 0, 1.0, px, [], e, e, 0
        dcur = bars[e]["t"].date()
        trail = sp.get("trail_r", 1.0) * R
        while q < len(bars):
            b = bars[q]
            m = b["t"].hour * 60 + b["t"].minute
            late_in = dcur == bars[e]["t"].date() and te >= prof["exit"]     # вход позже времени выхода — выход завтра
            if b["t"].date() != dcur or (m >= prof["exit"] and not late_in):
                pnl = s * (bars[last]["c"] / px - 1)
                if prof["carry"] and carry_all != "none" and (carry_all or (carried < MAX_CARRY_DAYS and (stage >= 1 or pnl > 0))):
                    carried += 1
                    exits.append((last, bars[last]["c"], 0, "перенос"))
                    nd = dcur
                    while q < len(bars) and (bars[q]["t"].date() <= nd or bars[q]["t"].hour * 60 + bars[q]["t"].minute < DAY_START):
                        q += 1
                    if q >= len(bars):
                        break
                    dcur = bars[q]["t"].date()
                    continue
                exits.append((last, bars[last]["c"], left, "конец дня")); left = 0
                break
            if stage >= 1:
                cand = peak - s * trail
                if (cand - st) * s > 0:
                    st = cand
            adv = b["l"] if s > 0 else b["h"]
            if (adv - st) * s <= 0:
                fill = st if (b["o"] - st) * s > 0 else b["o"]
                why = "стоп" if stage == 0 else ("б/у" if abs(st - px) < 1e-12 else "трейлинг")
                exits.append((q, fill, left, why)); left = 0
                break
            fav = b["h"] if s > 0 else b["l"]
            if stage == 0 and (fav - lv1) * s >= 0:
                part = w1 if w1 < 1 else 1.0
                exits.append((q, lv1, part, "тейк1")); left -= part; stage = 1; st = px; peak = lv1
                if left <= 1e-9:
                    break
            if stage == 1 and lv2 and (fav - lv2) * s >= 0:
                part = left / 2
                exits.append((q, lv2, part, "тейк2")); left -= part; stage = 2
            if stage >= 1 and (fav - peak) * s > 0:
                peak = fav
            last = q
            q += 1
        if left > 1e-9:
            last = min(last, len(bars) - 1)
            exits.append((last, bars[last]["c"], left, "открыта"))
        res = sum(f * s * (pp / px - 1) for _, pp, f, _ in exits)
        out.append({"side": s, "confirm": k, "entry_i": e, "entry": px, "stop": st0,
                    "t1": lv1, "t2": lv2, "impulse_t": sig["impulse"]["t"], "touch_t": sig["touch"]["t"],
                    "exits": exits, "result": res, "open": exits[-1][3] == "открыта", "kind": kind, "carried": carried})
        busy_until = exits[-1][0]
    return out


IDX_FLIP_BAND = 0.001          # разворот индекса считается, когда он ушёл от открытия больше чем на 0,1% в другую сторону
LOG_COLS = ["date", "ticker", "side", "lots", "entry", "exit", "reason", "result_pct", "time", "pnl_rub"]
BAL_COLS = ["time", "value", "deposit"]


def bal_log():
    return os.path.join(sc.HERE, "balance.csv")                 # стоимость портфеля во времени (для результатов за период)


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


class SandboxBroker:
    """Демо-счёт песочницы Т-Инвестиций."""
    def __init__(self, token, capital, log=print):
        self.ti = sc.TInvest(token, "sandbox")
        self.capital = capital
        self.log = log
        self.acc = None

    def prepare(self):
        self.acc = self.ti.sandbox_account()
        value = self.ti.sandbox_portfolio_value(self.acc)
        if value < self.capital * 0.5:
            self.log(f"На счёте песочницы {value:,.0f} ₽ — пополняю на {self.capital:,.0f} ₽".replace(",", " "))
            self.ti.sandbox_pay_in(self.acc, self.capital)
            sc.append_rows(bal_log(), BAL_COLS, [{"time": datetime.now(sc.MSK).strftime("%Y-%m-%d %H:%M"),
                                                  "value": round(value + self.capital, 2), "deposit": self.capital}])

    def tickers(self):
        return sc.imoex_tickers()

    def share(self, tk):
        return self.ti.share(tk)

    def candles(self, tk, day, frm=None, to=None, tf=1):
        return self.ti.candles(tk, day, frm, to, tf)

    def last_prices(self, tks):
        return self.ti.last_prices(tks)

    def all_shares(self):
        return self.ti.all_shares()

    def portfolio_value(self):
        """Текущая стоимость портфеля песочницы, ₽."""
        return self.ti.sandbox_portfolio_value(self.acc)

    def live_value(self):
        """Стоимость счёта с открытыми позициями по текущим ценам, ₽."""
        return self.ti.sandbox_live_value(self.acc)

    def universe(self, markets, index_list, log=print):
        """Список инструментов по выбранным в настройках группам."""
        out, shares = set(), None

        def get_shares():
            nonlocal shares
            if shares is None:
                shares = self.ti.all_shares()
            return shares
        for m in markets:
            try:
                if m == "all_shares":           # без 3-го эшелона: акции индекса широкого рынка (MOEXBMI) и индекса Мосбиржи
                    bmi = sc.index_members("MOEXBMI", None, 50)
                    out |= (set(get_shares()) & set(bmi)) | set(index_list) if bmi else set(get_shares())
                elif m == "imoex":
                    out |= set(index_list)
                elif m == "blue":
                    out |= set(sc.index_members("MOEXBC", sc.BLUE_CHIPS, 10))
                elif m == "third":
                    bmi = sc.index_members("MOEXBMI", None, 50)
                    out |= set(get_shares()) - set(bmi or index_list)
                elif m == "fx_metals":
                    out |= set(self.ti.all_currencies())
                elif m == "idx_comm":
                    out |= set(self.ti.nearest_futures({"TYPE_INDEX", "TYPE_COMMODITY"}))
                elif m == "futures":
                    out |= set(self.ti.nearest_futures())
            except sc.CertError:
                raise
            except Exception as e:
                log(f"Группа «{dict(MARKETS).get(m, m)}» не загружена: {e}")
        return sorted(out)

    def terms(self, tk):
        """(вид инструмента, ₽ за 1 единицу цены на 1 лот, обеспечение на 1 лот или None)."""
        sh = self.ti.share(tk)
        if sh.get("kind") == "future":
            pv, margin = self.ti.future_terms(tk)
            return "future", pv * sh["lot"], margin
        return sh.get("kind", "share"), float(sh["lot"]), None

    def order(self, tk, lots, side):
        """Рыночная заявка. Возвращает цену исполнения (или None, если брокер её не сообщил)."""
        js = self.ti.sandbox_order(self.acc, tk, lots, side)
        px = sc.TInvest.q(js.get("executedOrderPrice")) if isinstance(js, dict) else 0
        return px or None


class FinamBroker(SandboxBroker):
    """Торговля через Финам (Trade API): заявки и счёт — у Финама; свечи, цены и список бумаг — из Т-Инвестиций
    (на них робот проверен; токен песочницы Т-Инвестиций подходит для данных). Через Финам торгуются только акции Мосбиржи.
    ВНИМАНИЕ: если в Финаме выбран реальный (не демо) счёт — сделки идут на реальные деньги."""
    name = "Финам"

    def __init__(self, token, capital, finam_secret, finam_account="", log=print):
        super().__init__(token, capital, log)
        self.fn = sc.Finam(finam_secret, finam_account)

    def prepare(self):
        acc = self.fn.ensure_account()
        v = self.fn.equity()
        self.acc = acc
        self.log(f"Финам: счёт {acc}, стоимость {v:,.0f} ₽. Данные рынка — из Т-Инвестиций.".replace(",", " "))

    def portfolio_value(self):
        return self.fn.equity()

    def live_value(self):
        return self.fn.equity()

    def universe(self, markets, index_list, log=print):
        only = [m for m in markets if m in ("all_shares", "imoex", "blue", "third")]
        if len(only) < len(markets):
            log("Финам: валюта и фьючерсы через этого брокера роботом не торгуются — только акции.")
        return super().universe(only or ["all_shares"], index_list, log)

    def order(self, tk, lots, side):
        kind, _, _ = self.terms(tk)
        if kind != "share":
            raise RuntimeError("через Финам робот торгует только акциями")
        qty = int(lots) * int(self.ti.share(tk)["lot"])
        return self.fn.market_order(tk, qty, side)


class TInvestRealBroker(SandboxBroker):
    """Реальный брокерский счёт Т-Инвестиций: сделки на РЕАЛЬНЫЕ деньги. Нужен токен с полным доступом
    (личный кабинет → Настройки → Токены T-Invest API → «Полный доступ»). Свечи и цены — тем же токеном."""
    name = "Т-Инвестиции (реальный счёт)"

    def __init__(self, real_token, capital, account="", log=print):
        super().__init__(real_token, capital, log)
        self.ti = sc.TInvest(real_token, "prod")
        self.want = str(account or "").strip()

    def prepare(self):
        accs = self.ti.real_accounts()
        if not accs:
            raise RuntimeError("у токена нет открытых брокерских счетов")
        ids = [a["id"] for a in accs]
        if self.want and self.want not in ids:
            raise RuntimeError(f"счёт {self.want} не найден (доступны: {', '.join(ids)})")
        acc = next((a for a in accs if a["id"] == self.want), None) or \
            next((a for a in accs if a.get("type") == "ACCOUNT_TYPE_TINKOFF"), accs[0])
        if acc.get("accessLevel") not in (None, "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"):
            raise RuntimeError("у токена нет права торговать по этому счёту — создайте токен с полным доступом")
        self.acc = acc["id"]
        v = self.ti.real_portfolio_value(self.acc)
        self.log(f"РЕАЛЬНЫЙ счёт Т-Инвестиций {self.acc} ({acc.get('name', '')}): {v:,.0f} ₽. Сделки — на реальные деньги.".replace(",", " "))

    def portfolio_value(self):
        return self.ti.real_portfolio_value(self.acc)

    def live_value(self):
        return self.ti.real_live_value(self.acc)

    def order(self, tk, lots, side):
        js = self.ti.real_order(self.acc, tk, lots, side)
        st = js.get("executionReportStatus", "") if isinstance(js, dict) else ""
        if st in ("EXECUTION_REPORT_STATUS_REJECTED", "EXECUTION_REPORT_STATUS_CANCELLED"):
            raise RuntimeError(f"брокер отклонил заявку: {js.get('message') or st}")
        px = sc.TInvest.q(js.get("executedOrderPrice")) if isinstance(js, dict) else 0
        return px or None


REAL_BROKERS = ("tinvest_real", "finam")
BROKER_NAMES = {"tinvest": "Т-Инвестиции — песочница (демо-счёт)", "tinvest_real": "Т-Инвестиции — реальный счёт",
                "finam": "Финам — Trade API"}


def make_broker(s, log=print):
    """Брокер по настройкам: песочница Т-Инвестиций (по умолчанию), реальный счёт Т-Инвестиций или Финам."""
    if s.get("broker") == "tinvest_real":
        return TInvestRealBroker(s.get("real_token"), float(s["capital"]), s.get("real_account", ""), log=log)
    if s.get("broker") == "finam":
        return FinamBroker(s.get("token"), float(s["capital"]), s.get("finam_secret"), s.get("finam_account", ""), log=log)
    return SandboxBroker(s.get("token"), float(s["capital"]), log=log)


class AutoTrader:
    def __init__(self, broker, capital, log=print, on_update=None, clock=None, sleep=None,
                 stop_event=None, rescan=False, state_path=None, spike=None, day_on=True, on_event=None,
                 direction="both", trade=True):
        self.b = broker
        self.capital = capital
        self.log = log
        self.on_update = on_update or (lambda snap: None)
        self.clock = clock or (lambda: datetime.now(sc.MSK))
        self.stop_event = stop_event or threading.Event()
        self.on_event = on_event or (lambda kind, info: None)
        self.wake = threading.Event()            # будит робота раньше (ручное закрытие, «Стоп»)
        self.manual = []                         # тикеры, которые пользователь попросил закрыть ("*" — все)
        self.manual_open = []                    # заявки на открытие позиции вручную
        self._lock = threading.Lock()
        self._sleep = sleep or self._wait
        self.rescan = rescan
        self.day_on = day_on
        self.direction = direction            # "both" | "long" | "short"
        self.trade = trade                    # False — только наблюдение (формации, таблицы, Telegram), без новых сделок
        self.sp = dict(SPIKE_DEFAULTS, **(spike or {}))
        self.hist = {}               # тикер -> свечи 5 мин за несколько дней (из них строятся 15 мин и 1 час)
        self.hist_loaded = {}
        self.last_bar_check = None
        self.universe = []
        self.tier = {}
        self.waits = {}
        self.final_scan = None
        self._terms_cache = {}
        self.state_path = state_path or STATE_FILE
        self.status = "остановлен"
        self.scan_rows = []          # [(тикер, движение, статус)]
        self.spikes = []             # [время, тикер, ТФ, оборот, кратность, импульс, статус]
        self.market = None
        self.idx_dir, self.idx_ref = 0, None             # направление индекса (для закрытия при развороте)
        self.prices = {}
        self.opens = {}              # цена открытия дня 10:00
        self.pre_opens = {}          # цена открытия утренней сессии (индекс до 10:00)
        self.open_tries = {}
        self.seen_spikes = set()
        self.tickers = []
        self.day_tickers = []                    # чем торгует «Движение дня» (по умолчанию — акции индекса)
        self.last_scan = None
        self.balance = None
        self._bal_t = None
        self._bal_logged = None
        self.close_on_stop = False
        self.st = self._load()
        self._restore_found()

    # ------------------------------------------------------------------ состояние
    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            for k, v in (("positions", []), ("traded", []), ("spike_traded", []), ("results", [])):
                st.setdefault(k, v)
            return st
        except Exception:
            return self._fresh(None)

    @staticmethod
    def _fresh(day):
        return {"date": day, "entered": False, "done": False, "positions": [], "traded": [], "spike_traded": [], "results": []}

    def _restore_found(self):
        """Найденные сегодня всплески и формации переживают перезапуск программы/сервера (и не шлются в Telegram повторно)."""
        from datetime import date as _date
        if self.st.get("date") != self.clock().date().isoformat():
            return
        try:
            self.spikes = [list(r) for r in self.st.get("found_spikes", [])]
            self.seen_spikes = {tuple(k) for k in self.st.get("found_seen", [])}
            for k, w in self.st.get("found_waits", []):
                w = dict(w)
                w["date"] = _date.fromisoformat(w["date"])
                self.waits[tuple(k)] = w
        except Exception:
            self.spikes, self.seen_spikes, self.waits = [], set(), {}
        self._saved_found = len(self.spikes), len(self.seen_spikes)

    def _store_found(self):
        self.st["found_spikes"] = self.spikes[-300:]
        self.st["found_seen"] = [list(k) for k in self.seen_spikes]
        self.st["found_waits"] = [[list(k), dict(w, date=w["date"].isoformat())] for k, w in self.waits.items()]

    def _save(self):
        self._store_found()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    @property
    def positions(self):
        return self.st["positions"]

    def _carry_ok(self, p):
        if p.get("strat") == "15":                  # «движение дня»: перенос на заданное число дней (стоп 3% действует всё время)
            return bool(p.get("carry")) and p.get("carried", 0) < p.get("carry_max", 0)
        if p.get("strat") != "spike" or not p.get("carry"):
            return False
        if self.sp.get("carry_all", True) == "none":
            return False                           # «закрыть все» во время выхода
        if self.sp.get("carry_all", True):
            return True                            # переносится, пока не закроют стоп, тейк или трейлинг — без ограничения дней
        if p.get("carried", 0) >= MAX_CARRY_DAYS:
            return False
        lp = self.prices.get(p["ticker"])
        try:
            lp = self.b.last_prices([p["ticker"]]).get(p["ticker"]) or lp
        except Exception:
            pass
        if lp:
            self.prices[p["ticker"]] = lp
        return p.get("stage", 0) >= 1 or (lp is not None and p["side"] * (lp / p["entry"] - 1) > 0)

    def _set_status(self, text):
        self.status = text
        self._push()

    def _push(self):
        if (len(self.spikes), len(self.seen_spikes)) != getattr(self, "_saved_found", None) or \
                any(r[6] != getattr(self, "_last_status", {}).get(i) for i, r in enumerate(self.spikes[-50:])):
            self._saved_found = len(self.spikes), len(self.seen_spikes)
            self._last_status = {i: r[6] for i, r in enumerate(self.spikes[-50:])}
            try:
                self._save()
            except Exception:
                pass
        self.on_update({"status": self.status, "market": self.market, "market_open": getattr(self, "market_open", None),
                        "idx_ref": self.sp.get("idx_ref", "imoex"), "scan": list(self.scan_rows),
                        "spikes": list(self.spikes[-50:]),
                        "positions": [self._pos_view(p) for p in self.positions],
                        "last_scan": self.last_scan, "balance": self.balance})

    def _pos_view(self, p):
        """Позиция для экрана: текущая цена и результат в ₽ (без комиссий)."""
        last = self.prices.get(p["ticker"])
        per_lot = p.get("per_lot") or (self._terms_cache.get(p["ticker"]) or (None, None))[1]
        pnl = p["side"] * (last - p["entry"]) * p["lots"] * per_lot if last and per_lot else None
        return dict(p, last=last, pnl_rub=round(pnl, 2) if pnl is not None else None)

    def _balance(self, now):
        """Стоимость счёта по текущим ценам (с открытыми позициями) раз в минуту; в файл — при изменении (не чаще 10 мин) или раз в 30 минут."""
        if not hasattr(self.b, "portfolio_value"):
            return
        if self._bal_t and (now - self._bal_t).total_seconds() < 60:
            return
        self._bal_t = now
        try:
            v = float((self.b.live_value() if hasattr(self.b, "live_value") else self.b.portfolio_value()) or 0)
        except sc.CertError:
            raise
        except Exception:
            return
        if v <= 0:
            return
        self.balance = v
        if self._bal_logged is None or abs(v - self._bal_logged[1]) >= 0.01 and (now - self._bal_logged[0]).total_seconds() >= 600 \
                or (now - self._bal_logged[0]).total_seconds() >= 1800:
            sc.append_rows(bal_log(), BAL_COLS, [{"time": now.strftime("%Y-%m-%d %H:%M"), "value": round(v, 2), "deposit": ""}])
            self._bal_logged = (now, v)

    # ------------------------------------------------------------------ главный цикл
    def run(self):
        self.log(f"Робот запущен ({'ТОРГОВЛЯ' if self.trade else 'НАБЛЮДЕНИЕ'}). Мониторинг в торговые дни по календарю Мосбиржи{' (и в сессии выходного дня)' if self.sp.get('trade_weekends') else ''} {hm(DAY_START)}–{hm(DAY_END)} МСК, "
                 f"входы по формации: {trade_profile('index', 15, 'form', self.sp.get('window', '9-19'), self.sp.get('exit_late', True))['name']}; "
                 + ("во время выхода закрываются все позиции. " if self.sp.get("carry_all", True) == "none" else
                    "все позиции переносятся на следующий день, пока их не закроют стоп, тейк или трейлинг. "
                    if self.sp.get("carry_all", True) else
                    f"позиция в плюсе или с взятым тейком1 переносится на следующий день (до {MAX_CARRY_DAYS} дней). ")
                 + 
                 f"Направление: {self._dir_name()}.")
        if not self.day_on:
            self.log("Стратегия «движение дня» (вход в заданное время) выключена в настройках.")
        else:
            self.log(f"Стратегия «{hm(sc.ENTRY_MIN)}»: порог {sc.THRESHOLD * 100:g}%, стоп {sc.STOP * 100:g}%, выход {hm(self._day_exit())}"
                 + (", добор новых сигналов до 16:30" if self.rescan else "") + ".")
        sp = self.sp
        tfs = ", ".join(("1 час" if tf == 60 else f"{tf} мин") for tf in sp["tfs"]) or "нет"
        self.log(f"Всплески объёма + откат к EMA20 (таймфреймы: {tfs}): слежу всегда, торговля "
                 f"{'ВКЛЮЧЕНА' if sp['on'] else 'выключена'}. Касание EMA20 → свеча-подтверждение → вход; стоп под низом всего отката; "
                 f"тейк1 на половине пути до хая ({sp['half'] * 100:.0f}%) → стоп в б/у и трейлинг {sp['trail_r']:g}R; "
                 f"тейк2 на хае импульса (50% остатка); остаток — трейлинг; "
                 f"шорт только при падающем индексе.")
        if self.positions:
            self.log(f"Открытые позиции с прошлого запуска: {', '.join(p['ticker'] for p in self.positions)} — продолжаю сопровождать.")
        prepared_for = None
        self.cal = sc.TradingCalendar(getattr(self.b, "ti", None))
        while not self.stop_event.is_set():
            now = self.clock()
            t = now.hour * 60 + now.minute
            try:
                if self.manual or self.manual_open:
                    self._process_manual()
                kind = self.cal.kind(now.date())
                wk = bool(self.sp.get("trade_weekends"))
                if kind in ("holiday", "weekend") or (kind == "weekend_session" and not wk):
                    nxt = self.cal.next_trading(now.date(), wk)
                    why = sc.DAY_KINDS[kind] + (" (торговля в выходные выключена)" if kind == "weekend_session" else "")
                    self._set_status(f"{why} — жду " + (f"{nxt:%d.%m}" if nxt else "следующего торгового дня"))
                    self._sleep(300)
                    continue
                if t < DAY_START or t >= DAY_END:
                    self._set_status(f"биржа закрыта — жду {hm(DAY_START)}")
                    self._sleep(60)
                    continue
                if self.st.get("date") != now.date().isoformat():
                    self._new_day(now)
                if prepared_for != now.date():
                    self.b.prepare()
                    self.tickers = self.b.tickers()
                    self.universe = list(self.tickers)
                    if hasattr(self.b, "universe"):
                        u = self.b.universe(self.sp["markets"], self.tickers, self.log)
                        if u:
                            self.universe = u
                    dm = self.sp.get("day_markets") or ["imoex"]
                    self.day_tickers = list(self.tickers)
                    if dm != ["imoex"] and hasattr(self.b, "universe"):
                        u = self.b.universe(dm, self.tickers, self.log)
                        if u:
                            self.day_tickers = u
                    self.log("«Движение дня» торгует: " + ", ".join(dict(MARKETS).get(m, m) for m in dm)
                             + f" — {len(self.day_tickers)} инструментов.")
                    prepared_for = now.date()
                    names = ", ".join(dict(MARKETS).get(m, m) for m in self.sp["markets"])
                    self.log(f"{now:%d.%m} рабочий день начат: индекс — {len(self.tickers)} акций; формацию ищу в группах: "
                             f"{names} — всего {len(self.universe)} инструментов.")
                self._step(now)
            except sc.CertError:
                raise
            except Exception as e:
                self.log(f"Ошибка: {e}. Повтор через минуту.")
                self._sleep(60)
                continue
            self._sleep(STOP_POLL)
        if self.close_on_stop and self.positions:
            self.log("Робот остановлен — закрываю открытые позиции.")
            self._close_all("кнопка «Стоп»")
        elif self.positions:
            self.log(f"Робот остановлен. Позиции остаются открытыми: {', '.join(p['ticker'] for p in self.positions)}. "
                     "После «Старт» робот продолжит их сопровождать.")
        self._set_status("остановлен")

    def _new_day(self, now):
        keep = [p for p in self.positions if p.get("carried") or p.get("overnight")]
        other = [p for p in self.positions if not (p.get("carried") or p.get("overnight"))]
        if other:
            self.log("Остались позиции прошлого дня (без переноса) — закрываю.")
            self._close_all("позиция прошлого дня", only=other)
            if any(not p.get("carried") for p in self.positions):
                return        # не удалось закрыть — попробуем снова на следующем шаге
        if keep:
            self.log(f"Перенесённые позиции: {', '.join(p['ticker'] for p in keep)} — веду дальше.")
        self.st = self._fresh(now.date().isoformat())
        self.st["positions"] = keep
        self.opens, self.open_tries, self.scan_rows, self.market, self.prices = {}, {}, [], None, {}
        self.idx_dir, self.idx_ref = 0, None
        self.pre_opens = {}
        self.spikes, self.seen_spikes, self.hist, self.hist_loaded, self.waits = [], set(), {}, {}, {}
        self._save()

    def _step(self, now):
        t = now.hour * 60 + now.minute
        secs = t * 60 + now.second
        self._balance(now)
        # 1) сопровождение позиций: выход по времени, стоп, тейк
        if self.positions:
            today = now.date().isoformat()
            for p in [p for p in self.positions if secs >= p.get("exit_min", sc.EXIT_MIN) * 60 - 10
                      and p.get("carry_date") != today]:
                pick = p.get("carry_pick")              # выбор для этой позиции вручную: перенести / закрыть / по настройке
                if pick == "carry" or (pick != "close" and self._carry_ok(p)):
                    p["carried"] = p.get("carried", 0) + 1
                    p["carry_date"] = today
                    self._save()
                    lp = self.prices.get(p["ticker"]) or p["entry"]
                    res = p["side"] * (lp / p["entry"] - 1)
                    self.log(f"  {p['ticker']}: позиция {res * 100:+.2f}%"
                             + (", тейк1 взят" if p.get("stage", 0) >= 1 else "")
                             + (" (перенос выбран вручную)" if pick == "carry" else "") + f" — переношу на следующий день "
                             f"(перенос №{p['carried']}), стоп {p['stop']:.4g}")
                    self.on_event("carry", {"ticker": p["ticker"], "days": p["carried"], "result": res})
                else:
                    self._close_all(hm(p.get("exit_min", sc.EXIT_MIN)), only=[p])
            if self.positions:
                self._check_stops()
        if self.st["entered"] and secs >= self._day_exit() * 60 - 10 and not self.st["done"] \
                and not any(p.get("strat") == "15" for p in self.positions):
            self.st["done"] = True
            self._save()
            self._day_summary()
        # 2) до открытия основной сессии только ждём (и загружаем историю свечей для EMA20)
        if t < sc.OPEN_MIN + 1:
            if t >= DAY_START and (self.last_scan is None or (now - self.last_scan).total_seconds() >= SCAN_EVERY - 1):
                self._scan(now)                           # рынок от открытия утренней сессии — для фильтра индекса и входов с 9:00
            if self._day_signal_mode():
                self._enter_signal(now)
            elif self.trade and self.day_on and self._day_early() and not self.st["entered"] and sc.ENTRY_MIN <= t:
                self._scan(now)
                self._enter(now)                          # «Движение дня» с входом в 09:00
            mk = f"рынок {self.market * 100:+.2f}% · " if self.market is not None else ""
            self._set_status(f"{'ТОРГОВЛЯ' if self.trade else 'НАБЛЮДЕНИЕ'}: {mk}утренняя сессия, ищу формации")
            self._scan_pullbacks(now)
            return
        # 3) сканирование рынка раз в минуту (в момент входа — обязательно свежее)
        if not self.day_on and not self.st["entered"]:
            self.st.update(entered=True, done=True)      # стратегия «движение дня» выключена
            self._save()
        if self._day_signal_mode() and not self.st["entered"]:
            self.st["entered"] = True                    # вход не по времени, а сразу при сигнале
            self._save()
        entry_due = self.trade and (not self.st["entered"]) and sc.ENTRY_MIN <= t < sc.LATE_CUTOFF_MIN
        if t < DAY_END and (entry_due or self.last_scan is None or (now - self.last_scan).total_seconds() >= SCAN_EVERY - 1):
            self._scan(now)
        # 4) вход
        if self._day_signal_mode():
            self._enter_signal(now)
        elif entry_due:
            self._enter(now)
        elif self.trade and self.day_on and self.rescan and self.st["entered"] and t < sc.LATE_CUTOFF_MIN:
            self._enter_new(now)
        elif self.trade and not self.st["entered"] and t >= sc.LATE_CUTOFF_MIN and not self.st["done"]:
            self.log(f"Время входа пропущено (сейчас {hm(t)}, входить позже {hm(sc.LATE_CUTOFF_MIN)} нельзя) — сегодня без сделок по «{hm(sc.ENTRY_MIN)}».")
            self.st.update(entered=True, done=True)
            self._save()
        # 5) всплески объёма и откаты к EMA20
        self._scan_pullbacks(now)
        # 6) статус
        mk = f"рынок {self.market * 100:+.2f}% · " if self.market is not None else ""
        if not self.trade:
            self._set_status(f"НАБЛЮДЕНИЕ: {mk}ищу формации до {hm(DAY_END)}"
                             + (f" · ведёт позиции: {len(self.positions)}" if self.positions else "") + " — для торговли нажмите «Старт»")
        elif self.positions:
            self._set_status(f"{mk}в позициях: {len(self.positions)}")
        elif not self.st["entered"] and not (self.sp["on"] and self.sp["tfs"]):
            self._set_status(f"{mk}сканирую рынок — вход «движение дня» в {hm(sc.ENTRY_MIN)}")
        elif not self.st["entered"]:
            self._set_status(f"{mk}ищу формации (вход сразу при подтверждении); «движение дня» — в {hm(sc.ENTRY_MIN)}")
        elif self.sp["on"] and t < trade_profile("index", 15, "form", self.sp.get("window", "9-19"), self.sp.get("exit_late", True))["to"]:
            w = trade_profile("index", 15, "form", self.sp.get("window", "9-19"), self.sp.get("exit_late", True))
            self._set_status(f"ТОРГОВЛЯ: {mk}ищу формации, входы {hm(w['from'])}–{hm(w['to'])}")
        else:
            self._set_status(f"ТОРГОВЛЯ: {mk}входы на сегодня закончены — мониторинг до {hm(DAY_END)}")

    # ------------------------------------------------------------------ сканирование
    def _real_index(self, own):
        """Направление рынка для фильтров: по умолчанию — среднее движение бумаг индекса от открытия дня (как в
        проверке на истории); с настройкой idx_ref="imoex" — реальное изменение индекса IMOEX к вчерашнему закрытию."""
        self.market_open = own
        mode = self.sp.get("idx_ref", "imoex")
        if mode not in ("imoex", "imoex_open"):
            return own
        now = time.time()
        if not getattr(self, "_ixq", None) or now - self._ixq[0] > 30 or self._ixq[2] != mode:
            try:
                q = sc.index_quote()
                if q and mode == "imoex_open":      # IMOEX от открытия основной сессии (до 10:00 — от открытия утренней)
                    o = sc.index_open()
                    v = q["value"] / o - 1 if o else None
                else:
                    v = q["pct"] / 100 if q else None
                self._ixq = (now, v, mode)
            except Exception:
                self._ixq = (now, (getattr(self, "_ixq", None) or (0, None, mode))[1], mode)
        return self._ixq[1] if self._ixq[1] is not None else own

    def _scan_list(self):
        """Бумаги для «Движения дня» плюс акции индекса (по ним считается направление рынка «с открытия»)."""
        seen, out = set(), []
        for tk in list(self.day_tickers or self.tickers) + list(self.tickers):
            if tk not in seen:
                seen.add(tk); out.append(tk)
        return out

    def _own_market(self, moves):
        idx = [moves[tk] for tk in self.tickers if tk in moves]
        src = idx or list(moves.values())
        return sum(src) / len(src)

    def _day_only(self, moves):
        allow = set(self.day_tickers or self.tickers)
        return {tk: mv for tk, mv in moves.items() if tk in allow}

    def _rank(self, moves):
        rows = []
        for tk, mv in sorted(moves.items(), key=lambda x: -abs(x[1])):
            if abs(mv) < sc.THRESHOLD:
                why = "меньше порога"
            elif self.sp.get("idx_day", True) and (self.market == 0 or (mv > 0) != (self.market > 0)):
                why = "против рынка"
            else:
                why = "СИГНАЛ"
            rows.append((tk, mv, why))
        return rows

    def _scan(self, now):
        day = now.date()
        t = now.hour * 60 + now.minute
        for tk in self._scan_list():
            if tk in self.opens:
                continue
            last_try = self.open_tries.get(tk)
            if last_try and (now - last_try).total_seconds() < 300:
                continue
            self.open_tries[tk] = now
            try:   # с 07:00: открытие рынка — точка отсчёта до 10:00 (по умолчанию свечи берутся только с 09:55)
                rows = self.b.candles(tk, day, datetime(day.year, day.month, day.day, 6, 59, tzinfo=sc.MSK))
            except sc.CertError:
                raise
            except Exception as e:
                self.log(f"  {tk}: свечи не получены ({e})")
                continue
            pre9 = [r for r in rows if sc.minute_of(r["t"]) >= 7 * 60]
            if pre9 and tk not in self.pre_opens:
                self.pre_opens[tk] = pre9[0]["o"]          # открытие рынка (07:00) — до 10:00 и в режиме «от открытия рынка»
            first = [r for r in rows if sc.OPEN_MIN <= sc.minute_of(r["t"]) <= sc.OPEN_MIN + 5]
            if first:
                self.opens[tk] = first[0]["o"]
            elif t > sc.OPEN_MIN + 5:
                self.opens[tk] = None      # не торговалась в начале сессии — как в бэктесте, пропускаем
        if self.sp.get("day_ref", "main") == "market" or t < sc.OPEN_MIN + 1:
            ref = {tk: o for tk, o in self.pre_opens.items() if o}  # от открытия рынка 07:00 (до 10:00 — всегда так)
        else:
            ref = {tk: o for tk, o in self.opens.items() if o}
        live = list(ref)
        if not live:
            # до 10:00: движение индекса от открытия утренней сессии (для фильтра «шорт только при падающем индексе»)
            pre = [tk for tk in self._scan_list() if self.pre_opens.get(tk)]
            if pre and t < sc.OPEN_MIN + 6:
                try:
                    self.prices.update(self.b.last_prices(pre))
                except Exception:
                    return
                mvs = {tk: self.prices[tk] / self.pre_opens[tk] - 1 for tk in self._scan_list()
                       if self.pre_opens.get(tk) and self.prices.get(tk)}
                if mvs:
                    self.market = self._real_index(self._own_market(mvs))
                    self._idx_flip("утро")
                    self.scan_rows = self._rank(self._day_only(mvs))        # кандидаты с 09:01 — движение от открытия утренней сессии
                    self.last_scan = now
                    self._push()
            return
        self.prices.update(self.b.last_prices(live))
        moves = {tk: self.prices[tk] / ref[tk] - 1 for tk in live if self.prices.get(tk)}
        if not moves:
            return
        self.market = self._real_index(self._own_market(moves))
        self._idx_flip("сигнал" if self._day_signal_mode() else "день")
        self.scan_rows = self._rank(self._day_only(moves))
        self.last_scan = now
        self._push()

    def _signals_now(self):
        return [(tk, 1 if mv > 0 else -1, mv) for tk, mv, why in self.scan_rows if why == "СИГНАЛ"]

    # ------------------------------------------------------------------ всплески объёма
    def _load_hist(self, tk, now):
        """Свечи 5 мин за последние дни (все сессии) — для EMA20 и медианы объёма на 5 мин, 15 мин и 1 час."""
        out = []
        for d in range(HIST_DAYS, -1, -1):
            day = (now - timedelta(days=d)).date()
            if day.weekday() >= 5 and not self.sp.get("trade_weekends"):
                continue
            if hasattr(self, "cal") and self.cal.kind(day) in ("holiday", "weekend"):
                continue
            frm = datetime(day.year, day.month, day.day, 6, 50, tzinfo=sc.MSK)
            to = min(now, datetime(day.year, day.month, day.day, 23, 59, tzinfo=sc.MSK))
            out += self.b.candles(tk, day, frm, to, 5)
        return out

    def _bars5(self, tk, now):
        naive = now.replace(tzinfo=None)
        if self.hist_loaded.get(tk) != now.date():
            self.hist[tk] = self._load_hist(tk, now)
            self.hist_loaded[tk] = now.date()
        else:
            fresh = self.b.candles(tk, now.date(), now - timedelta(minutes=20), now, 5)
            known = {r["t"]: i for i, r in enumerate(self.hist[tk])}
            for r in fresh:
                if r["t"] in known:
                    self.hist[tk][known[r["t"]]] = r
                else:
                    self.hist[tk].append(r)
            self.hist[tk] = self.hist[tk][-1500:]
        kind, per_lot, _ = self._terms(tk)
        return [dict(r, val=r["v"] * per_lot * (r["h"] + r["l"] + r["c"]) / 3)
                for r in self.hist[tk] if r["t"] + timedelta(minutes=5) <= naive]      # только закрытые свечи

    def _terms(self, tk):
        if tk not in self._terms_cache:
            if hasattr(self.b, "terms"):
                self._terms_cache[tk] = self.b.terms(tk)
            else:
                self._terms_cache[tk] = ("share", float(self.b.share(tk)["lot"]), None)
        return self._terms_cache[tk]

    def _scan_pullbacks(self, now):
        """После закрытия каждой 5-минутки: всплески объёма и формация «откат к EMA20 + подтверждение» на 5м/15м/1ч."""
        sp = self.sp
        t = now.hour * 60 + now.minute
        if not sp["tfs"] or not (DAY_START <= t <= sp.get("scan_to", sp["to_min"]) + 15):
            return
        bar_end = (t // 5) * 5
        key = (now.date(), bar_end)
        if self.last_bar_check == key or (t - bar_end) * 60 + now.second < 5:
            return
        self.last_bar_check = key
        due = [tf for tf in sp["tfs"] if bar_end % tf == 0]            # у каких ТФ только что закрылась свеча
        session_over = SESSION_CLOSE + 5 <= t < 19 * 60 + 5        # перерыв между основной и вечерней сессией
        if session_over and self.final_scan != now.date():
            # основная сессия закрылась: незаконченные свечи 15 мин и 1 час (18:30, 18:00) считаем закрытыми — финальный проход
            self.final_scan = now.date()
            due = list(sp["tfs"])
            self.log("Основная сессия закрыта — финальная проверка формаций по последним свечам 15 мин и 1 час.")
        for n, tk in enumerate(self.universe):
            if self.stop_event.is_set():
                return
            if self.manual or self.manual_open:
                self._process_manual()
            if n and n % 25 == 0 and self.positions:
                self._check_stops()                                          # длинный проход не мешает стопам
            try:
                b5 = self._bars5(tk, self.clock())
            except sc.CertError:
                raise
            except Exception as e:
                self.log(f"  {tk}: свечи не получены ({e})")
                continue
            for tf in due:
                bars = aggregate([dict(b) for b in b5], tf)
                if tf != 5 and bars and bars[-1]["t"] + timedelta(minutes=tf) > now.replace(tzinfo=None) and not session_over:
                    bars = bars[:-1]                                        # последняя свеча ТФ ещё не закрыта
                if len(bars) < 25:
                    continue
                p = params(sp, tf)
                tier = self._tier(tk, b5)
                if tier == "2" and tf == 60:
                    p["min_value"] = 15e6                                   # 2-й эшелон: порог оборота ниже
                if tf == 60:
                    p["slot_mult"] = 2.0                                    # всплеск — и к соседним свечам, и к этому часу прошлых дней
                add_slot(bars)
                em = ema([b["c"] for b in bars])
                self._note_impulse(tk, bars, p, em)
                self._update_waits(tk, p, bars, em)
                sig = find_pullback(bars, p, em)
                kind = "form"
                if not sig and sp.get("trend_on", True):
                    pt = dict(p, **TIER2_TREND) if (tier == "2" and tf == 15 and sp.get("tier_profile", True)) else p
                    sig = find_trend_pullback(bars, pt, em)
                    kind = "trend"
                if sig:
                    sig["tf"] = tf
                    sig["tier"] = tier
                    if sp.get("tier_profile", True) and self._terms(tk)[0] == "share":
                        ok, why_no = tier_rules(tier, tf, kind)
                        if kind == "trend" and sp.get("trend_trade", False):
                            ok = True                                       # «торговать все трендовые откаты» в настройках
                        sig["tradeable"], sig["why_no"] = ok, why_no
                    self._pullback_signal(tk, sig)
        self._push()

    @staticmethod
    def _tf_name(tf):
        return "1ч" if tf == 60 else f"{tf}м"

    def _tier(self, tk, b5):
        """Эшелон: индекс / 2 (средний дневной оборот ≥ 30 млн ₽) / 3 — по загруженной истории 5 мин."""
        if tk in self.tier:
            return self.tier[tk]
        if tk in self.tickers:
            self.tier[tk] = "index"
            return "index"
        days = {}
        for b in b5:
            m = b["t"].hour * 60 + b["t"].minute
            if 600 <= m < 1120:
                days[b["t"].date()] = days.get(b["t"].date(), 0) + b["val"]
        med = _median(list(days.values())) if days else 0
        self.tier[tk] = "2" if med >= TIER2_TURNOVER else "3"
        return self.tier[tk]

    def _update_waits(self, tk, p, bars, em):
        """Строки «жду отката»: сколько осталось до EMA20, не отменён ли импульс, не вышло ли время."""
        tfn = self._tf_name(p["tf"])
        last = bars[-1]
        for row in self.spikes:
            if row[1] != tk or row[2] != tfn or not row[6].startswith("жду"):
                continue
            w = self.waits.get((tk, tfn, row[0]))
            if not w:
                continue
            s = w["side"]
            if last["t"].date() != w["date"] or (last["c"] - w["open"]) * s < 0:
                row[6] = "отменён (ушло за начало импульса)" if last["t"].date() == w["date"] else "время вышло"
                continue
            w["age"] += 1
            if w["age"] > p["win"]:
                row[6] = "время вышло"
                continue
            dist = (em[-1] / last["c"] - 1) * 100
            touched = (last["l"] <= em[-1]) if s > 0 else (last["h"] >= em[-1])
            row[6] = ("коснулся EMA20 — жду подтверждения" if touched or (last["c"] - em[-1]) * s <= 0
                      else f"жду отката к EMA20 ({dist:+.1f}%)")

    def _note_impulse(self, tk, bars, p, em=None):
        """Таблица «всплески объёма»: импульсная свеча, только что закрытая."""
        imp = _impulse(bars, len(bars) - 1, p, p["min_value"], em)
        if imp:
            a, s_, med, body, total = imp
            nb = len(bars) - a
            b = dict(bars[a], val=total)
            k = (tk, p["tf"], b["t"].isoformat())
            if k not in self.seen_spikes:
                self.seen_spikes.add(k)
                self.spikes.append([b["t"].strftime("%H:%M"), tk, self._tf_name(p["tf"]), b["val"], b["val"] / nb / med, body,
                                    "жду отката к EMA20", ""])
                self.waits[(tk, self._tf_name(p["tf"]), b["t"].strftime("%H:%M"))] = {
                    "side": 1 if body > 0 else -1, "open": bars[a]["o"], "date": b["t"].date(), "age": 0}
                self.log(f"Всплеск объёма {tk} ({self._tf_name(p['tf'])}, {b['t']:%H:%M}): оборот {b['val'] / 1e6:.0f} млн ₽ "
                         f"(×{b['val'] / nb / med:.1f}, свечей: {nb}), {'рост' if body > 0 else 'падение'} {abs(body) * 100:.2f}% — жду отката к EMA20.")
                self.on_event("impulse", {"ticker": tk, "tf": self._tf_name(p["tf"]), "t": b["t"].strftime("%H:%M"),
                                          "value": b["val"], "mult": b["val"] / nb / med, "bars": nb, "move": body,
                                          "price": bars[-1]["c"]})

    def _pullback_signal(self, tk, sig):
        sp = self.sp
        s = sig["side"]
        tfn = self._tf_name(sig["tf"])
        k = (tk, sig["tf"], sig["confirm"]["t"].isoformat(), "signal")
        if k in self.seen_spikes:
            return
        self.seen_spikes.add(k)
        imp = sig["impulse"]
        trend = sig.get("trend", False)
        if trend:                                   # трендовый откат — не чаще раза в день по бумаге и ТФ
            kd = (tk, sig["tf"], sig["confirm"]["t"].date().isoformat(), "trend")
            if kd in self.seen_spikes:
                return
            self.seen_spikes.add(kd)
        imp_t = imp["t"].strftime("%d.%m %H:%M" if trend else "%H:%M")
        txt = (f"{'Трендовый откат' if trend else 'Откат'} к EMA20 {tk} ({tfn}): импульс {imp_t} ({sig['body'] * 100:+.2f}%, "
               f"×{sig['mult']:.1f}), касание {sig['touch']['t']:%H:%M}, подтверждение {sig['confirm']['t']:%H:%M}")
        sig_t = (sig["confirm"]["t"] + timedelta(minutes=sig["tf"])).strftime("%H:%M")   # закрытие свечи-подтверждения = сигнал
        status = "ПОДТВЕРЖДЁН"
        if sp["index"] and self._terms(tk)[0] == "share" and \
                (self.market is None or self.market == 0 or (self.market > 0) != (s > 0)):
            status = "против индекса"
        row_ref = None
        if trend:
            row_ref = [sig["confirm"]["t"].strftime("%H:%M"), tk, tfn, sig["value"], sig["mult"], sig["body"],
                       "ТРЕНД: " + status + f" (импульс {imp_t})", sig_t]
            self.spikes.append(row_ref)
        else:
            for row in reversed(self.spikes):
                if row[1] == tk and row[2] == tfn and row[0] == imp["t"].strftime("%H:%M"):
                    row[6] = status
                    while len(row) < 8:
                        row.append("")
                    row[7] = sig_t
                    row_ref = row
                    break
            else:
                row_ref = [imp["t"].strftime("%H:%M"), tk, tfn, sig["value"], sig["mult"], sig["body"], status, sig_t]
                self.spikes.append(row_ref)
        enter, why = False, ""
        tc = sig["confirm"]["t"].hour * 60 + sig["confirm"]["t"].minute + sig["tf"]      # время закрытия свечи-подтверждения
        prof = trade_profile(sig.get("tier", "index"), sig["tf"], "trend" if trend else "form", sp.get("window", "9-19"),
                             sp.get("exit_late", True))
        te = imp.get("t_end", imp["t"])
        ti = te.hour * 60 + te.minute                       # время последней свечи импульса (как в проверке на истории)
        if not self.trade:
            why = "наблюдение — торговля выключена (кнопка «Старт»)"
        elif sig.get("tf") == 5:
            why = "5 мин — не торгую, только уведомление"
        elif sig.get("tier") == "3":
            why = "3-й эшелон — не торгую, только уведомление"
        elif not sig.get("tradeable", (not trend) or sp.get("trend_trade", False)):
            why = sig.get("why_no") or "трендовый откат (объём был раньше) — только уведомление, на истории такие входы в минусе"
        elif sig.get("late"):
            why = (f"поздний откат ({sig['age']} свечей после импульса, на истории в этом случае хуже) — только уведомление")
        elif not (prof["from"] <= tc < prof["to"]) or ti < prof["from"]:
            why = f"вне времени входа {hm(prof['from'])}–{hm(prof['to'])} для этого варианта — только уведомление"
        elif not self._side_ok(s):
            why = f"в настройках — {self._dir_name()}, не вхожу"
        elif sp.get("ema200") and not self._ema200_ok(tk, sig.get("tf") or 15, s, self.clock()):
            why = self._e200_why
        elif status != "ПОДТВЕРЖДЁН":
            why = "индекс идёт в другую сторону, не вхожу"
        elif not sp["on"]:
            why = "сигнал (торговля по формации выключена в настройках)"
        elif tk in self.st["spike_traded"] or any(p["ticker"] == tk for p in self.positions):
            why = "по бумаге сегодня уже была сделка, пропуск"
        elif sp["max_pos"] and sum(1 for p in self.positions if p.get("strat") == "spike") >= sp["max_pos"]:     # 0 — без ограничения
            why = f"уже {sp['max_pos']} позиций по формации, пропуск"
        else:
            enter, why = True, "вхожу"
        self.log(txt + " — " + why + ".")
        if row_ref is not None:                    # в таблице — итог: вошёл или почему нет
            row_ref[6] = ("ВХОД — " if enter else "") + (("ТРЕНД, " if trend else "") + why if not enter
                                                        else ("тренд" if trend else "формация") + f" ({tfn})")
            self._push()
        level = sig["level"] * (1 - s * sig["buf"])
        price = self.prices.get(tk) or sig["confirm"]["c"]
        two = sp.get("two_takes", True)
        self.on_event("formation", {
            "ticker": tk, "tf": tfn, "side": s, "impulse_t": imp_t, "move": sig["body"], "trend": trend,
            "mult": sig["mult"], "touch_t": sig["touch"]["t"].strftime("%H:%M"),
            "confirm_t": sig["confirm"]["t"].strftime("%H:%M"), "price": price, "stop": level,
            "take1": price + 0.5 * (sig["t1"] - price) if two else sig["t1"], "take2": sig["t1"] if two else None,
            "ema20": sig["ema"], "market": self.market, "decision": why, "enter": enter, "kind": self._terms(tk)[0]})
        if not enter:
            return
        self.st["spike_traded"].append(tk)
        base = self.capital
        if sp.get("from_portfolio", True) and hasattr(self.b, "portfolio_value"):
            try:
                base = self.b.portfolio_value() or self.capital
            except Exception:
                pass
        lev, lev_txt = self._leverage(tk, s)
        alloc = base * sp["share"] * lev
        self.log(f"  размер позиции: {sp['share'] * 100:g}% от {'портфеля' if base != self.capital else 'капитала'} "
                 f"{base:,.0f} ₽{lev_txt} = {alloc:,.0f} ₽".replace(",", " "))
        self._open(tk, side=s, alloc=alloc, strat="spike", stop_level=level, t1=sig["t1"],
                   exit_min=prof["exit"], carry=prof["carry"])

    # ------------------------------------------------------------------ вход по «15:00»
    def _enter(self, now):
        t = now.hour * 60 + now.minute
        sigs = self._signals_now()
        mk = f"{self.market * 100:+.2f}%" if self.market is not None else "нет данных"
        self.log(f"{hm(t)} Время входа «{hm(sc.ENTRY_MIN)}». Рынок с открытия {mk} (по {len(self.scan_rows)} бумагам). "
                 f"Сигналов: {len(sigs)}" + (f" — {', '.join(s[0] for s in sigs)}" if sigs else "") + ".")
        for tk, mv, why in self.scan_rows[:5]:
            self.log(f"   {tk:6s} {mv * 100:+.2f}%  {why}")
        if sigs and t - sc.ENTRY_MIN > sc.LATE_OK_MIN:
            sigs = self._check_late(now, sigs)
        for s in [s for s in sigs if not self._side_ok(s[1])]:
            self.log(f"  {s[0]}: {'лонг' if s[1] > 0 else 'шорт'} — в настройках {self._dir_name()}, не вхожу")
        sigs = [s for s in sigs if self._side_ok(s[1])]
        held = {p["ticker"] for p in self.positions}
        for s in [s for s in sigs if s[0] in held]:
            self.log(f"  {s[0]}: уже есть позиция по всплеску — не дублирую")
        sigs = [s for s in sigs if s[0] not in held]
        self.st["entered"] = True
        self._save()
        if not sigs:
            self.log("Сделок по «15:00» сегодня нет.")
            return
        if self.rescan:
            sigs = sigs[:MAX_POS_RESCAN]
        alloc = self.capital / (MAX_POS_RESCAN if self.rescan else len(sigs))
        for tk, side, mv in sigs:
            self.st["traded"].append(tk)
            self._open(tk, side, alloc)

    def _day_exit(self):
        v = str(self.sp.get("day_exit", "18:45"))
        try:
            hh, mm = (int(x) for x in v.split(":"))
            return hh * 60 + mm
        except ValueError:
            return DAY_EXIT

    def _sig_from(self):
        """С какого времени «Движение дня» может входить «сразу при сигнале» (настройка «время входов от…»)."""
        ef = str(self.sp.get("entry_from") or "")
        return 7 * 60 if ef == "7" else 10 * 60 if ef == "10" else DAY_SIG_FROM

    def _day_early(self):
        return sc.ENTRY_MIN < 11 * 60

    def _day_signal_mode(self):
        return self.day_on and self.sp.get("day_mode", "fixed") == "signal"

    def _enter_signal(self, now):
        """«Движение дня» — вход сразу, как только бумага прошла порог в сторону рынка (окно 09:00–19:00), один раз за день."""
        t = now.hour * 60 + now.minute
        if not self.trade or not (self._sig_from() <= t < DAY_SIG_TO) or self.market is None:
            return
        held = {p["ticker"] for p in self.positions}
        sigs = [(tk, 1 if mv > 0 else -1, mv) for tk, mv, why in self.scan_rows if why == "СИГНАЛ"]
        sigs = [x for x in sigs if x[0] not in held and x[0] not in self.st["traded"] and self._side_ok(x[1])]
        if not sigs:
            return
        base = self.capital
        try:
            base = self.b.portfolio_value() or self.capital
        except Exception:
            pass
        share = float(self.sp.get("day_share", self.sp.get("share", 0.10)) or 0.10)
        for tk, side, mv in sigs:
            self.log(f"«Движение дня»: {tk} {mv * 100:+.2f}% от открытия, рынок {self.market * 100:+.2f}% — вхожу "
                     f"{'в лонг' if side > 0 else 'в шорт'} ({share * 100:g}% портфеля).")
            self.st["traded"].append(tk)
            self._open(tk, side, base * share)
        self._save()

    def _check_late(self, now, sigs):
        """Запуск позже времени входа: сигнал должен быть и во время входа, и сейчас."""
        t = now.hour * 60 + now.minute
        self.log(f"Опоздание на {t - sc.ENTRY_MIN} мин: проверяю, были ли эти сигналы в {hm(sc.ENTRY_MIN)}…")
        data = {}
        for tk in self.day_tickers or self.tickers:
            try:
                data[tk] = self.b.candles(tk, now.date())
            except sc.CertError:
                raise
            except Exception:
                pass
        _, at_entry = sc.compute_day(data, with_result=False, entry_min=sc.ENTRY_MIN)
        ok = {(s["ticker"], s["side"]) for s in at_entry}
        keep = [s for s in sigs if (s[0], s[1]) in ok]
        for s in sigs:
            if (s[0], s[1]) not in ok:
                self.log(f"  {s[0]}: в {hm(sc.ENTRY_MIN)} сигнала не было — не вхожу")
        return keep

    def _enter_new(self, now):
        held = {p["ticker"] for p in self.positions}
        for tk, side, mv in [x for x in self._signals_now() if self._side_ok(x[1])]:
            if tk in self.st["traded"] or tk in held or \
                    sum(1 for p in self.positions if p.get("strat") == "15") >= MAX_POS_RESCAN:
                continue
            self.log(f"Новый сигнал {tk} ({mv * 100:+.2f}%) — добираю.")
            self.st["traded"].append(tk)
            self._open(tk, side, self.capital / MAX_POS_RESCAN)

    # ------------------------------------------------------------------ сделки
    def _open(self, tk, side, alloc, strat="15", stop_level=None, t1=None, exit_min=None, carry=False):
        self._save()
        try:
            sh = self.b.share(tk)
        except Exception as e:
            self.log(f"  {tk}: инструмент не найден ({e}), пропуск")
            return
        px = self.prices.get(tk)
        if strat == "spike" or not px:   # для отката — самая свежая цена
            try:
                px = self.b.last_prices([tk]).get(tk) or px
            except Exception:
                pass
        if not px:
            self.log(f"  {tk}: нет цены, пропуск")
            return
        if side < 0 and not sh["short"]:
            self.log(f"  {tk}: шорт по бумаге недоступен, пропуск")
            return
        if strat == "spike" and ((stop_level - px) * side >= 0 or (t1 - px) * side < 0.5 * abs(px - stop_level)):
            self.log(f"  {tk}: цена {px:.4g} уже слишком близко к стопу или тейку — вход невыгоден, пропуск")
            return
        kind, per_lot, margin = self._terms(tk)
        lots = int(alloc // (margin if margin else px * per_lot))
        if lots < 1:
            self.log(f"  {tk}: на долю капитала не хватает даже на один лот, пропуск")
            return
        try:
            fill = self.b.order(tk, lots, side)
        except sc.CertError:
            raise
        except Exception as e:
            self.log(f"  {tk}: заявка отклонена: {e}")
            return
        entry = fill or px
        stop = stop_level if stop_level is not None else entry * (1 - side * sc.STOP)
        if strat == "manual":
            take1, take2 = t1, None                                                # ручная позиция: один тейк — закрыть всё
        elif self.sp.get("two_takes", True):
            take1, take2 = (entry + 0.5 * (t1 - entry), t1) if t1 else (None, None)   # тейк1 — полпути до хая, тейк2 — хай
        else:
            take1, take2 = t1, None                                                # один тейк у хая импульса
        day_ex = {}
        if strat == "15":
            day_ex = {"dtp": float(self.sp.get("day_tp", 0) or 0), "dhalf": float(self.sp.get("day_tp_half", 1.0) or 1.0),
                      "dact": float(self.sp.get("day_act", 0) or 0), "dtrail": float(self.sp.get("day_trail", 0) or 0)}
        carry_max = 0
        if strat == "15" and int(self.sp.get("day_carry", 0) or 0) > 0:
            carry, carry_max = True, int(self.sp["day_carry"])
        pos = {"ticker": tk, "side": side, "lots": lots, "entry": entry, "stop": stop, "take": take1, "take2": take2,
               "strat": strat, "R": abs(entry - stop), "took": False, "per_lot": per_lot, "stage": 0, "peak": entry,
               "exit_min": exit_min or (self._day_exit() if strat == "15" else sc.EXIT_MIN), "time": self.clock().strftime("%H:%M:%S"), "carry": carry, "carry_max": carry_max,
               "date": self.clock().date().isoformat()}
        pos.update(day_ex)
        if day_ex.get("dtp"):                         # уровень тейка «движения дня» — для таблиц и графика
            pos["take"] = entry * (1 + side * day_ex["dtp"])
        now_t = self.clock()
        if now_t.hour * 60 + now_t.minute >= pos["exit_min"]:
            pos["carry_date"] = now_t.date().isoformat()     # вошли после времени выхода — выход завтра
            pos["overnight"] = True
        self.positions.append(pos)
        self._save()
        self.log(f"  {tk}: {'КУПЛЕНО' if side > 0 else 'ПРОДАНО (шорт)'} {lots} лот. по ~{entry:.4g}, стоп {pos['stop']:.4g}"
                 + (f", тейк1 {pos['take']:.4g} ({self.sp['half'] * 100:.0f}%, затем б/у и трейлинг)"
                    + (f", тейк2 {pos['take2']:.4g} (50% остатка)" if pos["take2"] else "")
                    if pos["take"]
                    else f", выход {hm(pos['exit_min'])}" + (f" через {carry_max} дн. (перенос)" if carry_max else ""))
                 + (" [всплеск]" if strat == "spike" else ""))
        self.on_event("open", dict(pos))
        self._push()

    def _check_stops(self):
        """
        Стоп, тейки и трейлинг. Схема для формации:
          тейк1 — половина пути от входа до хая импульса: закрыть 50%, стоп в безубыток, включить трейлинг;
          тейк2 — хай импульса: закрыть 50% остатка; остальное ведёт трейлинг-стоп.
        """
        lp = self.b.last_prices([p["ticker"] for p in self.positions])
        self.prices.update(lp)
        for p in list(self.positions):
            px = lp.get(p["ticker"])
            if not px:
                continue
            s = p["side"]
            if p.get("strat") in ("15", "manual") and (p.get("dtp") or p.get("dtrail")):
                self._day_exits(p, px, s)
                continue
            stage = p.get("stage", 1 if p.get("took") else 0)
            if stage >= 1:
                if (px - p["peak"]) * s > 0:
                    p["peak"] = px
                trail = p["peak"] - s * self.sp["trail_r"] * p["R"]
                if (trail - p["stop"]) * s > 0:
                    p["stop"] = trail
                    self._save()
            if (px - p["stop"]) * s <= 0:
                why = "стоп" if stage == 0 else ("б/у" if abs(p["stop"] - p["entry"]) < 1e-9 else "трейлинг-стоп")
                self._close(p, px, why)
                continue
            if stage == 0 and p.get("take") and (px - p["take"]) * s >= 0:
                part = int(p["lots"] * self.sp["half"]) if self.sp["half"] < 1 else p["lots"]
                if part >= p["lots"] or part < 1:
                    self._close(p, px, "тейк1")
                    continue
                if self._close(p, px, f"тейк1 {self.sp['half'] * 100:.0f}%", lots=part):
                    p.update(took=True, stage=1, stop=p["entry"], peak=px)
                    self._save()
                    self.log(f"  {p['ticker']}: стоп перенесён в безубыток {p['entry']:.4g}, включён трейлинг")
                continue
            if stage == 1 and p.get("take2") and (px - p["take2"]) * s >= 0:
                part = p["lots"] // 2
                if part < 1:
                    p["stage"] = 2
                    continue
                if self._close(p, px, "тейк2 (хай) 50% остатка", lots=part):
                    p["stage"] = 2
                    self._save()
                    self.log(f"  {p['ticker']}: тейк2 взят, остаток {p['lots']} лот. ведёт трейлинг-стоп")
        self._push()

    def _day_exits(self, p, px, s):
        """«Движение дня»: тейк (весь или часть + стоп в б/у) и трейлинг-стоп в % от цены; стоп 3% — как раньше."""
        e = p["entry"]
        if (px - p.get("best", e)) * s > 0:
            p["best"] = px
        best = p.get("best", e)
        tp, half, act, trail = p.get("dtp") or 0, p.get("dhalf", 1.0), p.get("dact") or 0, p.get("dtrail") or 0
        if tp and not p.get("dtook") and (px - e * (1 + s * tp)) * s >= 0:
            part = int(p["lots"] * half) if half < 1 else p["lots"]
            if part >= p["lots"] or part < 1:
                self._close(p, px, f"тейк {tp * 100:g}%")
                return
            if self._close(p, px, f"тейк {tp * 100:g}% ({half * 100:.0f}%)", lots=part):
                p["dtook"] = p["armed"] = True
                p["stage"] = 2                             # на экране вместо тейка — «трейл»
                if (e - p["stop"]) * s > 0:
                    p["stop"] = e
                self.log(f"  {p['ticker']}: стоп в безубыток {e:.4g}" + (f", трейлинг {trail * 100:g}%" if trail else ""))
                self._save()
        if act and not p.get("armed") and (px - e * (1 + s * act)) * s >= 0:
            p["armed"] = True
            if (e - p["stop"]) * s > 0:
                p["stop"] = e
            self.log(f"  {p['ticker']}: +{act * 100:g}% — стоп в безубыток, включён трейлинг {trail * 100:g}%")
            self._save()
        if p.get("armed") and trail:
            ts = best * (1 - s * trail)
            if (ts - p["stop"]) * s > 0:
                p["stop"] = ts
                self._save()
        if (px - p["stop"]) * s <= 0:
            why = "стоп" if not p.get("armed") else ("б/у" if abs(p["stop"] - e) < 1e-9 else "трейлинг-стоп")
            self._close(p, px, why)

    def _idx_flip(self, ref):
        """Индекс сменил направление (рост ↔ падение больше чем на IDX_FLIP_BAND от открытия) — закрыть позиции
        по настройке: все или только те, что в плюсе. Пока индекс у нуля, направление не считается сменившимся."""
        if ref != self.idx_ref:                       # сменилась точка отсчёта (утро → основная сессия) — начать заново
            self.idx_ref, self.idx_dir = ref, 0
        m = self.market
        d = 1 if m > IDX_FLIP_BAND else -1 if m < -IDX_FLIP_BAND else 0
        if not d:
            return
        prev, self.idx_dir = self.idx_dir, d
        mode = self.sp.get("idx_flip", "off")
        if not prev or prev == d or mode not in ("all", "profit") or not self.positions:
            return
        name = "вверх" if d > 0 else "вниз"
        targets = list(self.positions)
        if mode == "profit":
            targets = self._in_profit(targets)
        self.log(f"Индекс развернулся {name} ({m * 100:+.2f}% от открытия) — закрываю "
                 + ("все позиции" if mode == "all" else "позиции в плюсе") + (f": {', '.join(p['ticker'] for p in targets)}" if targets else ": таких нет"))
        if targets:
            self._close_all(f"разворот индекса {name}", only=targets)

    def _in_profit(self, positions):
        try:
            lp = self.b.last_prices([p["ticker"] for p in positions])
        except Exception:
            lp = {}
        out = []
        for p in positions:
            px = lp.get(p["ticker"]) or self.prices.get(p["ticker"])
            if px and p["side"] * (px / p["entry"] - 1) > 0:
                out.append(p)
        return out

    def _close_all(self, why, only=None):
        targets = list(only if only is not None else self.positions)
        try:
            lp = self.b.last_prices([p["ticker"] for p in targets])
        except Exception:
            lp = {}
        for p in targets:
            self._close(p, lp.get(p["ticker"]) or p["entry"], why)

    def _close(self, p, px, why, lots=None):
        lots = lots or p["lots"]
        try:
            fill = self.b.order(p["ticker"], lots, -p["side"])
        except sc.CertError:
            raise
        except Exception as e:
            self.log(f"  {p['ticker']}: не удалось закрыть ({e}) — повторю через {STOP_POLL} с")
            return False
        exit_px = fill or px
        res = p["side"] * (exit_px / p["entry"] - 1)
        if lots < p["lots"]:
            p["lots"] -= lots
        else:
            self.positions.remove(p)
        self.st["results"].append([p.get("strat", "15"), res])
        self._save()
        try:
            pnl_rub = p["side"] * (exit_px - p["entry"]) * lots * self._terms(p["ticker"])[1]     # ₽ без комиссий
        except Exception:
            pnl_rub = None
        self.log(f"  {p['ticker']}: закрыто {lots} лот. ({why}) по ~{exit_px:.4g}, результат {res * 100:+.2f}%"
                 + (" [всплеск]" if p.get("strat") == "spike" else " [вручную]" if p.get("strat") == "manual" else ""))
        sc.append_rows(sc.SPIKE_LOG if p.get("strat") == "spike" else sc.MANUAL_LOG if p.get("strat") == "manual" else sc.SANDBOX_LOG, LOG_COLS,
                       [{"date": self.st.get("date") or self.clock().date().isoformat(), "ticker": p["ticker"],
                         "side": "лонг" if p["side"] > 0 else "шорт", "lots": lots, "entry": round(p["entry"], 6),
                         "exit": round(exit_px, 6), "reason": why, "result_pct": round(res * 100, 4),
                         "time": self.clock().strftime("%Y-%m-%d %H:%M"), "pnl_rub": round(pnl_rub, 2) if pnl_rub is not None else ""}])
        self.on_event("close", {"ticker": p["ticker"], "why": why, "result": res, "lots": lots})
        self._push()
        return True

    def _day_summary(self):
        res = [r for s, r in self.st["results"] if s == "15"]
        if res:
            self.log(f"Итог дня по «{hm(sc.ENTRY_MIN)}»: сделок {len(res)}, в среднем {sum(res) / len(res) * 100:+.2f}% на сделку "
                     f"(до комиссий), в плюсе {sum(1 for r in res if r > 0)}.")

    def _leverage(self, tk, side):
        """Плечо: 1 — без маржи; число — фиксированное; "auto" — максимум брокера по ставке риска бумаги (1/dlong, 1/dshort)."""
        lev = self.sp.get("leverage", 1)
        if lev in (1, "1", None):
            return 1.0, ""
        if lev == "auto":
            try:
                sh = self.b.share(tk)
                d = sh.get("dlong" if side > 0 else "dshort") or 0
                if d > 0:
                    x = max(1.0, min(1 / d, 10.0))
                    return x, f" × плечо {x:.2f} (ставка риска {d * 100:.1f}%)"
            except Exception:
                pass
            return 1.0, " (ставка риска не получена — без плеча)"
        x = float(lev)
        return x, f" × плечо {x:g}"

    def set_trade(self, on):
        self.trade = bool(on)
        self.log("Торговля ВКЛЮЧЕНА — робот сам входит и выходит." if on else
                 "Торговля выключена — робот только наблюдает (открытые позиции продолжает вести до стопа/тейка/выхода).")
        self.wake.set()

    def _ema200_ok(self, tk, tf, s, now):
        """Фильтр тренда: лонг — только если цена выше EMA200 на ТФ формации, шорт — только ниже."""
        key = (tk, tf, now.strftime("%Y-%m-%d %H"))
        cache = self.__dict__.setdefault("_e200", {})
        if key not in cache:
            days = {5: 5, 15: 8, 60: 30}.get(tf, 8)
            try:
                rows = self.b.ti.candles_range(tk, now - timedelta(days=days), now, tf)
                naive = now.replace(tzinfo=None)
                cl = [r["c"] for r in rows if r["t"] + timedelta(minutes=tf) <= naive]
                cache[key] = ema(cl, 200)[-1] if len(cl) >= 200 else None
            except sc.CertError:
                raise
            except Exception as e:
                self.log(f"  {tk}: EMA200 не посчитана ({e})")
                cache[key] = None
        e200 = cache[key]
        tfn = {5: "5 мин", 15: "15 мин", 60: "1 час"}.get(tf, f"{tf} мин")
        if e200 is None:
            self._e200_why = f"мало истории для EMA200 ({tfn}) — не вхожу"
            return False
        px = self.prices.get(tk)
        if not px:
            try:
                px = self.b.last_prices([tk]).get(tk)
            except Exception:
                px = None
        ok = bool(px) and (px - e200) * s > 0
        self._e200_why = f"цена {px:.4g} {'ниже' if s > 0 else 'выше'} EMA200 ({tfn}) {e200:.4g} — не вхожу" if px else "нет цены для EMA200"
        return ok

    def _side_ok(self, side):
        return self.direction == "both" or (self.direction == "long") == (side > 0)

    def _dir_name(self):
        return {"long": "только лонг", "short": "только шорт"}.get(self.direction, "лонг и шорт")

    def _wait(self, sec):
        self.wake.wait(sec)
        self.wake.clear()

    def request_stop(self, close_positions):
        """Вызывается из окна: остановить робота (и при необходимости закрыть позиции)."""
        self.close_on_stop = close_positions
        self.stop_event.set()
        self.wake.set()

    def request_carry(self, ticker, mode):
        """Выборочный перенос: mode = "carry" (переносить), "close" (закрыть во время выхода), "" (по настройкам)."""
        mode = mode if mode in ("carry", "close") else None
        with self._lock:
            hit = [p for p in self.positions if p["ticker"] == ticker]
            for p in hit:
                if mode:
                    p["carry_pick"] = mode
                else:
                    p.pop("carry_pick", None)
        if hit:
            self._save()
            self.log(f"  {ticker}: во время выхода — " + {"carry": "перенести на следующий день", "close": "закрыть",
                                                            None: "по настройкам стратегии"}[mode])
            self._push()
        return bool(hit)

    def request_open(self, ticker, side, share=None, stop_pct=None, take_pct=0.0, trail_pct=0.0, exit_at="", carry=True):
        """Открыть позицию вручную (из окна или с телефона). share — доля портфеля (0.10 = 10%), stop/take/trail — в долях
        от цены входа (0 — нет). exit_at "ЧЧ:ММ" и carry: во время выхода перенести (True) или закрыть (False)."""
        with self._lock:
            self.manual_open.append({"ticker": str(ticker).upper().strip(), "side": 1 if side > 0 else -1,
                                     "share": share, "stop": stop_pct, "take": take_pct or 0.0, "trail": trail_pct or 0.0,
                                     "exit_at": exit_at, "carry": bool(carry)})
        self.wake.set()

    def _open_manual(self, r):
        tk = r["ticker"]
        if any(p["ticker"] == tk for p in self.positions):
            self.log(f"  {tk}: по этой бумаге уже есть позиция — вручную не открываю")
            return
        try:
            px = self.b.last_prices([tk]).get(tk)
        except Exception as e:
            self.log(f"  {tk}: нет цены ({e})")
            return
        if not px:
            self.log(f"  {tk}: нет цены — бумага не торгуется или не найдена")
            return
        share = float(r.get("share") or self.sp.get("share", 0.10))
        stop = float(r["stop"]) if r.get("stop") else sc.STOP
        s = r["side"]
        try:
            hh, mm = (int(x) for x in str(r.get("exit_at") or "").split(":"))
            exit_min = hh * 60 + mm
        except ValueError:
            exit_min = self._day_exit()
        self.log(f"Открываю вручную: {tk} {'лонг' if s > 0 else 'шорт'}, {share * 100:g}% портфеля, стоп {stop * 100:g}%"
                 + (f", тейк {r['take'] * 100:g}%" if r.get("take") else "") + (f", трейлинг {r['trail'] * 100:g}%" if r.get("trail") else "")
                 + f", во время выхода {hm(exit_min)} — " + ("перенести" if r.get("carry") else "закрыть"))
        n0 = len(self.positions)
        self._open(tk, s, self.capital * share, strat="manual", stop_level=px * (1 - s * stop), exit_min=exit_min)
        if len(self.positions) > n0:
            p = self.positions[-1]
            p["carry_pick"] = "carry" if r.get("carry") else "close"
            if r.get("take") or r.get("trail"):         # тейк (закрывает всё) и трейлинг в % — как у «Движения дня»
                p.update(dtp=float(r.get("take") or 0), dhalf=1.0, dtrail=float(r.get("trail") or 0), dact=0.0)
                p["take"] = p["entry"] * (1 + s * r["take"]) if r.get("take") else None
            p["stop"] = p["entry"] * (1 - s * stop)
            p["R"] = abs(p["entry"] - p["stop"])
            self._save()
            self._push()

    def request_close(self, ticker=None):
        """Вызывается из окна: закрыть позицию по рынку (ticker=None — все позиции)."""
        with self._lock:
            self.manual.append(ticker or "*")
        self.wake.set()

    def _process_manual(self):
        with self._lock:
            req, self.manual = self.manual, []
            opens, self.manual_open = self.manual_open, []
        for r in opens:
            try:
                self._open_manual(r)
            except sc.CertError:
                raise
            except Exception as e:
                self.log(f"  {r.get('ticker')}: открыть вручную не удалось: {e}")
        for tk in req:
            targets = (list(self.positions) if tk == "*" else self._in_profit(list(self.positions)) if tk == "+"
                       else [p for p in self.positions if p["ticker"] == tk])
            if tk == "+" and not targets:
                self.log("Закрыть позиции в плюсе: таких сейчас нет.")
            if targets:
                self.log(f"Закрываю вручную: {', '.join(p['ticker'] for p in targets)}")
                self._close_all("вручную", only=targets)
