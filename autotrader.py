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
from datetime import datetime, timedelta

import scanner as sc

DAY_START = 7 * 60            # 07:00 — робот начинает рабочий день
DAY_END = 18 * 60 + 30        # 18:30 — заканчивает
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
                  "markets": ["all_shares"], "max_pos": 5, "share": 0.10,
                  "from_min": 10 * 60, "to_min": 18 * 60, "exit_min": 18 * 60 + 25}
# Какими инструментами торгует формация (выбор в настройках)
MARKETS = [
    ("all_shares", "Все российские акции"),
    ("blue", "Голубые фишки Мосбиржи"),
    ("imoex", "Акции индекса Мосбиржи"),
    ("third", "Третий эшелон"),
    ("fx_metals", "Валюты и металлы"),
    ("idx_comm", "Индексы и товары (фьючерсы)"),
    ("futures", "Фьючерсы (все, ближайшие контракты)"),
]
HIST_DAYS = 5                 # сколько календарных дней истории грузить для EMA20 и медианы объёма


def params(sp, tf):
    p = dict(sp)
    p.update({k: v for k, v in SPIKE_PRESETS[tf].items() if k != "bt"})
    p["tf"] = tf
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


def _impulse(bars, i, sp, minv):
    """
    Импульс — серия из 1..K свечей подряд, заканчивающаяся свечой i (свеча i — по направлению импульса):
    движение от открытия первой свечи ≥ порога, средний оборот серии ≥ mult × медиана оборота 20 свечей до неё.
    Возвращает (начало, сторона, медиана, движение, оборот) или None.
    """
    bi = bars[i]
    tod = bi["t"].hour * 60 + bi["t"].minute
    if not (sp["from_min"] <= tod < sp["to_min"]):
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
        if med > 0 and abs(mv) >= sp["body"] and total / k >= sp["mult"] * med and total >= minv:
            s = 1 if mv > 0 else -1
            if (bi["c"] - bi["o"]) * s > 0:
                return a, s, med, mv, total
    return None


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
        imp = _impulse(bars, i, sp, minv)
        if not imp:
            continue
        a, s, med, body, total = imp
        bi = dict(bars[i], o=bars[a]["o"], val=total, t=bars[a]["t"], n=i - a + 1)   # импульс как одна «свеча»
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
                                "buf": sp["buf"]}
                    break
            tch = (bj["l"] <= em[j]) if s > 0 else (bj["h"] >= em[j])
            if tch and (not sp.get("touch_close", True) or (bj["c"] - em[j] * (1 - s * 0.003)) * s >= 0):
                touch = j
            pl = (bj["l"] if pl is None else min(pl, bj["l"])) if s > 0 else (bj["h"] if pl is None else max(pl, bj["h"]))
            ext = max(ext, bj["h"]) if s > 0 else min(ext, bj["l"])
    return None


def history_trades(bars, sp):
    """
    Формации на истории графика и как бы их отработал робот (без фильтра индекса): вход по открытию свечи
    после подтверждения, стоп под откатом, тейк1/тейк2/б/у/трейлинг как в настройках, выход не позже 18:25.
    Проверка внутри свечи — худший случай (сначала стоп). Возвращает список сделок.
    """
    em = ema([b["c"] for b in bars])
    out, used_days, busy_until = [], set(), -1
    for k in range(25, len(bars) - 1):
        if k <= busy_until:
            continue
        sig = find_pullback(bars, sp, em, k)
        if not sig:
            continue
        e = k + 1
        if bars[e]["t"].date() != bars[k]["t"].date() or bars[k]["t"].date() in used_days:
            continue
        s = sig["side"]
        px = bars[e]["o"]
        st = sig["level"] * (1 - s * sig["buf"])
        R = abs(px - st)
        if (st - px) * s >= 0 or (sig["t1"] - px) * s < 0.5 * R:
            continue
        used_days.add(bars[k]["t"].date())
        two = sp.get("two_takes", True)
        lv1 = px + 0.5 * (sig["t1"] - px) if two else sig["t1"]
        lv2 = sig["t1"] if two else None
        w1 = sp.get("half", 0.5)
        stage, left, peak, exits, q = 0, 1.0, px, [], e
        trail = sp.get("trail_r", 1.0) * R
        while q < len(bars) and bars[q]["t"].date() == bars[e]["t"].date():
            b = bars[q]
            if (b["t"].hour * 60 + b["t"].minute) >= sp.get("exit_min", 18 * 60 + 25):
                exits.append((q, b["o"], left, "18:25")); left = 0
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
            q += 1
        if left > 1e-9:
            last = min(q, len(bars) - 1)
            exits.append((last, bars[last]["c"], left, "открыта" if last == len(bars) - 1 else "конец дня"))
        res = sum(f * s * (p / px - 1) for _, p, f, _ in exits)
        out.append({"side": s, "confirm": k, "entry_i": e, "entry": px, "stop": sig["level"] * (1 - s * sig["buf"]),
                    "t1": lv1, "t2": lv2, "impulse_t": sig["impulse"]["t"], "touch_t": sig["touch"]["t"],
                    "exits": exits, "result": res, "open": exits[-1][3] == "открыта"})
        busy_until = exits[-1][0]
    return out


LOG_COLS = ["date", "ticker", "side", "lots", "entry", "exit", "reason", "result_pct"]


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
                if m == "all_shares":
                    out |= set(get_shares())
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


class AutoTrader:
    def __init__(self, broker, capital, log=print, on_update=None, clock=None, sleep=None,
                 stop_event=None, rescan=False, state_path=None, spike=None, day_on=True, on_event=None):
        self.b = broker
        self.capital = capital
        self.log = log
        self.on_update = on_update or (lambda snap: None)
        self.clock = clock or (lambda: datetime.now(sc.MSK))
        self.stop_event = stop_event or threading.Event()
        self.on_event = on_event or (lambda kind, info: None)
        self.wake = threading.Event()            # будит робота раньше (ручное закрытие, «Стоп»)
        self.manual = []                         # тикеры, которые пользователь попросил закрыть ("*" — все)
        self._lock = threading.Lock()
        self._sleep = sleep or self._wait
        self.rescan = rescan
        self.day_on = day_on
        self.sp = dict(SPIKE_DEFAULTS, **(spike or {}))
        self.hist = {}               # тикер -> свечи 5 мин за несколько дней (из них строятся 15 мин и 1 час)
        self.hist_loaded = {}
        self.last_bar_check = None
        self.universe = []
        self._terms_cache = {}
        self.state_path = state_path or STATE_FILE
        self.status = "остановлен"
        self.scan_rows = []          # [(тикер, движение, статус)]
        self.spikes = []             # [время, тикер, ТФ, оборот, кратность, импульс, статус]
        self.market = None
        self.prices = {}
        self.opens = {}              # цена открытия дня 10:00
        self.open_tries = {}
        self.seen_spikes = set()
        self.tickers = []
        self.last_scan = None
        self.close_on_stop = False
        self.st = self._load()

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

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    @property
    def positions(self):
        return self.st["positions"]

    def _set_status(self, text):
        self.status = text
        self._push()

    def _push(self):
        self.on_update({"status": self.status, "market": self.market, "scan": list(self.scan_rows),
                        "spikes": list(self.spikes[-50:]),
                        "positions": [dict(p, last=self.prices.get(p["ticker"])) for p in self.positions],
                        "last_scan": self.last_scan})

    # ------------------------------------------------------------------ главный цикл
    def run(self):
        self.log(f"Робот запущен. Рабочее время по будням {hm(DAY_START)}–{hm(DAY_END)} МСК.")
        if not self.day_on:
            self.log("Стратегия «движение дня» (вход в заданное время) выключена в настройках.")
        else:
            self.log(f"Стратегия «{hm(sc.ENTRY_MIN)}»: порог {sc.THRESHOLD * 100:g}%, стоп {sc.STOP * 100:g}%, выход {hm(sc.EXIT_MIN)}"
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
        while not self.stop_event.is_set():
            now = self.clock()
            t = now.hour * 60 + now.minute
            try:
                if self.manual:
                    self._process_manual()
                if now.weekday() >= 5:
                    self._set_status("выходной — жду понедельника")
                    self._sleep(300)
                    continue
                if t < DAY_START or t >= DAY_END:
                    if self.positions and t >= DAY_END:
                        self._close_all("конец рабочего дня 18:30")
                    self._set_status(f"вне рабочего времени — жду {hm(DAY_START)}")
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
        if self.positions:
            self.log("Остались позиции прошлого дня — закрываю.")
            self._close_all("позиция прошлого дня")
            if self.positions:
                return        # не удалось закрыть — попробуем снова на следующем шаге
        self.st = self._fresh(now.date().isoformat())
        self.opens, self.open_tries, self.scan_rows, self.market, self.prices = {}, {}, [], None, {}
        self.spikes, self.seen_spikes, self.hist, self.hist_loaded = [], set(), {}, {}
        self._save()

    def _step(self, now):
        t = now.hour * 60 + now.minute
        secs = t * 60 + now.second
        # 1) сопровождение позиций: выход по времени, стоп, тейк
        if self.positions:
            for p in [p for p in self.positions if secs >= p.get("exit_min", sc.EXIT_MIN) * 60 - 10]:
                self._close_all(hm(p.get("exit_min", sc.EXIT_MIN)), only=[p])
            if self.positions:
                self._check_stops()
        if self.st["entered"] and secs >= sc.EXIT_MIN * 60 - 10 and not self.st["done"] \
                and not any(p.get("strat") == "15" for p in self.positions):
            self.st["done"] = True
            self._save()
            self._day_summary()
        # 2) до открытия основной сессии только ждём (и загружаем историю свечей для EMA20)
        if t < sc.OPEN_MIN + 1:
            self._set_status(f"утренняя сессия — загружаю историю, жду открытия основной в {hm(sc.OPEN_MIN)}")
            self._scan_pullbacks(now)
            return
        # 3) сканирование рынка раз в минуту (в момент входа — обязательно свежее)
        if not self.day_on and not self.st["entered"]:
            self.st.update(entered=True, done=True)      # стратегия «движение дня» выключена
            self._save()
        entry_due = (not self.st["entered"]) and sc.ENTRY_MIN <= t < sc.LATE_CUTOFF_MIN
        if entry_due or self.last_scan is None or (now - self.last_scan).total_seconds() >= SCAN_EVERY - 1:
            self._scan(now)
        # 4) вход
        if entry_due:
            self._enter(now)
        elif self.day_on and self.rescan and self.st["entered"] and t < sc.LATE_CUTOFF_MIN:
            self._enter_new(now)
        elif not self.st["entered"] and t >= sc.LATE_CUTOFF_MIN and not self.st["done"]:
            self.log(f"Время входа пропущено (сейчас {hm(t)}, входить позже {hm(sc.LATE_CUTOFF_MIN)} нельзя) — сегодня без сделок по «{hm(sc.ENTRY_MIN)}».")
            self.st.update(entered=True, done=True)
            self._save()
        # 5) всплески объёма и откаты к EMA20
        self._scan_pullbacks(now)
        # 6) статус
        mk = f"рынок {self.market * 100:+.2f}% · " if self.market is not None else ""
        if self.positions:
            self._set_status(f"{mk}в позициях: {len(self.positions)}")
        elif not self.st["entered"] and not (self.sp["on"] and self.sp["tfs"]):
            self._set_status(f"{mk}сканирую рынок — вход «движение дня» в {hm(sc.ENTRY_MIN)}")
        elif not self.st["entered"]:
            self._set_status(f"{mk}ищу формации (вход сразу при подтверждении); «движение дня» — в {hm(sc.ENTRY_MIN)}")
        elif self.sp["on"] and t < self.sp["to_min"]:
            self._set_status(f"{mk}ищу всплески объёма и откаты к EMA20 до {hm(self.sp['to_min'])}")
        else:
            self._set_status(f"{mk}сделки на сегодня завершены — работаю до {hm(DAY_END)}")

    # ------------------------------------------------------------------ сканирование
    def _scan(self, now):
        day = now.date()
        t = now.hour * 60 + now.minute
        for tk in self.tickers:
            if tk in self.opens:
                continue
            last_try = self.open_tries.get(tk)
            if last_try and (now - last_try).total_seconds() < 300:
                continue
            self.open_tries[tk] = now
            try:
                rows = self.b.candles(tk, day)
            except sc.CertError:
                raise
            except Exception as e:
                self.log(f"  {tk}: свечи не получены ({e})")
                continue
            first = [r for r in rows if sc.OPEN_MIN <= sc.minute_of(r["t"]) <= sc.OPEN_MIN + 5]
            if first:
                self.opens[tk] = first[0]["o"]
            elif t > sc.OPEN_MIN + 5:
                self.opens[tk] = None      # не торговалась в начале сессии — как в бэктесте, пропускаем
        live = [tk for tk, o in self.opens.items() if o]
        if not live:
            return
        self.prices.update(self.b.last_prices(live))
        moves = {tk: self.prices[tk] / self.opens[tk] - 1 for tk in live if self.prices.get(tk)}
        if not moves:
            return
        self.market = sum(moves.values()) / len(moves)
        rows = []
        for tk, mv in sorted(moves.items(), key=lambda x: -abs(x[1])):
            if abs(mv) < sc.THRESHOLD:
                why = "меньше порога"
            elif self.market == 0 or (mv > 0) != (self.market > 0):
                why = "против рынка"
            else:
                why = "СИГНАЛ"
            rows.append((tk, mv, why))
        self.scan_rows = rows
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
            if day.weekday() >= 5:
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
        if not sp["tfs"] or not (DAY_START <= t <= sp["to_min"] + 60):
            return
        bar_end = (t // 5) * 5
        key = (now.date(), bar_end)
        if self.last_bar_check == key or (t - bar_end) * 60 + now.second < 5:
            return
        self.last_bar_check = key
        due = [tf for tf in sp["tfs"] if bar_end % tf == 0]            # у каких ТФ только что закрылась свеча
        for n, tk in enumerate(self.universe):
            if self.stop_event.is_set():
                return
            if self.manual:
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
                if tf != 5 and bars and bars[-1]["t"] + timedelta(minutes=tf) > now.replace(tzinfo=None):
                    bars = bars[:-1]                                        # последняя свеча ТФ ещё не закрыта
                if len(bars) < 25:
                    continue
                p = params(sp, tf)
                self._note_impulse(tk, bars, p)
                sig = find_pullback(bars, p)
                if sig:
                    sig["tf"] = tf
                    self._pullback_signal(tk, sig)
        self._push()

    @staticmethod
    def _tf_name(tf):
        return "1ч" if tf == 60 else f"{tf}м"

    def _note_impulse(self, tk, bars, p):
        """Таблица «всплески объёма»: импульсная свеча, только что закрытая."""
        imp = _impulse(bars, len(bars) - 1, p, p["min_value"])
        if imp:
            a, s_, med, body, total = imp
            nb = len(bars) - a
            b = dict(bars[a], val=total)
            k = (tk, p["tf"], b["t"].isoformat())
            if k not in self.seen_spikes:
                self.seen_spikes.add(k)
                self.spikes.append([b["t"].strftime("%H:%M"), tk, self._tf_name(p["tf"]), b["val"], b["val"] / nb / med, body,
                                    "жду отката к EMA20"])
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
        txt = (f"Откат к EMA20 {tk} ({tfn}): импульс {imp['t']:%H:%M} ({sig['body'] * 100:+.2f}%, ×{sig['mult']:.1f}), касание "
               f"{sig['touch']['t']:%H:%M}, подтверждение {sig['confirm']['t']:%H:%M}")
        status = "ПОДТВЕРЖДЁН"
        if sp["index"] and self._terms(tk)[0] == "share" and \
                (self.market is None or self.market == 0 or (self.market > 0) != (s > 0)):
            status = "против индекса"
        for row in reversed(self.spikes):
            if row[1] == tk and row[2] == tfn and row[0] == imp["t"].strftime("%H:%M"):
                row[6] = status
                break
        else:
            self.spikes.append([imp["t"].strftime("%H:%M"), tk, tfn, sig["value"], sig["mult"], sig["body"], status])
        enter, why = False, ""
        if status != "ПОДТВЕРЖДЁН":
            why = "индекс идёт в другую сторону, не вхожу"
        elif not sp["on"]:
            why = "сигнал (торговля по формации выключена в настройках)"
        elif tk in self.st["spike_traded"] or any(p["ticker"] == tk for p in self.positions):
            why = "по бумаге сегодня уже была сделка, пропуск"
        elif sum(1 for p in self.positions if p.get("strat") == "spike") >= sp["max_pos"]:
            why = f"уже {sp['max_pos']} позиций по формации, пропуск"
        else:
            enter, why = True, "вхожу"
        self.log(txt + " — " + why + ".")
        level = sig["level"] * (1 - s * sig["buf"])
        price = self.prices.get(tk) or sig["confirm"]["c"]
        two = sp.get("two_takes", True)
        self.on_event("formation", {
            "ticker": tk, "tf": tfn, "side": s, "impulse_t": imp["t"].strftime("%H:%M"), "move": sig["body"],
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
        self.log(f"  размер позиции: {sp['share'] * 100:g}% от {'портфеля' if base != self.capital else 'капитала'} "
                 f"{base:,.0f} ₽ = {base * sp['share']:,.0f} ₽".replace(",", " "))
        self._open(tk, side=s, alloc=base * sp["share"], strat="spike", stop_level=level, t1=sig["t1"],
                   exit_min=sp["exit_min"])

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

    def _check_late(self, now, sigs):
        """Запуск позже времени входа: сигнал должен быть и во время входа, и сейчас."""
        t = now.hour * 60 + now.minute
        self.log(f"Опоздание на {t - sc.ENTRY_MIN} мин: проверяю, были ли эти сигналы в {hm(sc.ENTRY_MIN)}…")
        data = {}
        for tk in self.tickers:
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
        for tk, side, mv in self._signals_now():
            if tk in self.st["traded"] or tk in held or \
                    sum(1 for p in self.positions if p.get("strat") != "spike") >= MAX_POS_RESCAN:
                continue
            self.log(f"Новый сигнал {tk} ({mv * 100:+.2f}%) — добираю.")
            self.st["traded"].append(tk)
            self._open(tk, side, self.capital / MAX_POS_RESCAN)

    # ------------------------------------------------------------------ сделки
    def _open(self, tk, side, alloc, strat="15", stop_level=None, t1=None, exit_min=None):
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
        if self.sp.get("two_takes", True):
            take1, take2 = (entry + 0.5 * (t1 - entry), t1) if t1 else (None, None)   # тейк1 — полпути до хая, тейк2 — хай
        else:
            take1, take2 = t1, None                                                # один тейк у хая импульса
        pos = {"ticker": tk, "side": side, "lots": lots, "entry": entry, "stop": stop, "take": take1, "take2": take2,
               "strat": strat, "R": abs(entry - stop), "took": False, "stage": 0, "peak": entry,
               "exit_min": exit_min or sc.EXIT_MIN, "time": self.clock().strftime("%H:%M:%S")}
        self.positions.append(pos)
        self._save()
        self.log(f"  {tk}: {'КУПЛЕНО' if side > 0 else 'ПРОДАНО (шорт)'} {lots} лот. по ~{entry:.4g}, стоп {pos['stop']:.4g}"
                 + (f", тейк1 {pos['take']:.4g} ({self.sp['half'] * 100:.0f}%, затем б/у и трейлинг)"
                    + (f", тейк2 {pos['take2']:.4g} (50% остатка)" if pos["take2"] else "")
                    if pos["take"]
                    else f", выход {hm(pos['exit_min'])}")
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
        self.log(f"  {p['ticker']}: закрыто {lots} лот. ({why}) по ~{exit_px:.4g}, результат {res * 100:+.2f}%"
                 + (" [всплеск]" if p.get("strat") == "spike" else ""))
        sc.append_rows(sc.SPIKE_LOG if p.get("strat") == "spike" else sc.SANDBOX_LOG, LOG_COLS,
                       [{"date": self.st.get("date") or self.clock().date().isoformat(), "ticker": p["ticker"],
                         "side": "лонг" if p["side"] > 0 else "шорт", "lots": lots, "entry": round(p["entry"], 6),
                         "exit": round(exit_px, 6), "reason": why, "result_pct": round(res * 100, 4)}])
        self.on_event("close", {"ticker": p["ticker"], "why": why, "result": res, "lots": lots})
        self._push()
        return True

    def _day_summary(self):
        res = [r for s, r in self.st["results"] if s == "15"]
        if res:
            self.log(f"Итог дня по «{hm(sc.ENTRY_MIN)}»: сделок {len(res)}, в среднем {sum(res) / len(res) * 100:+.2f}% на сделку "
                     f"(до комиссий), в плюсе {sum(1 for r in res if r > 0)}.")

    def _wait(self, sec):
        self.wake.wait(sec)
        self.wake.clear()

    def request_stop(self, close_positions):
        """Вызывается из окна: остановить робота (и при необходимости закрыть позиции)."""
        self.close_on_stop = close_positions
        self.stop_event.set()
        self.wake.set()

    def request_close(self, ticker=None):
        """Вызывается из окна: закрыть позицию по рынку (ticker=None — все позиции)."""
        with self._lock:
            self.manual.append(ticker or "*")
        self.wake.set()

    def _process_manual(self):
        with self._lock:
            req, self.manual = self.manual, []
        for tk in req:
            targets = list(self.positions) if tk == "*" else [p for p in self.positions if p["ticker"] == tk]
            if targets:
                self.log(f"Закрываю вручную: {', '.join(p['ticker'] for p in targets)}")
                self._close_all("вручную", only=targets)
