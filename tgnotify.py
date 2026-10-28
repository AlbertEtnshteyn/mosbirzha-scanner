# -*- coding: utf-8 -*-
"""Уведомления в Telegram через своего бота (токен от @BotFather и ваш chat id)."""
import threading

import scanner as sc

API = "https://api.telegram.org/bot{token}/{method}"


def _call(token, method, payload, timeout=15):
    r = sc.http().post(API.format(token=token, method=method), json=payload, timeout=timeout)
    js = r.json()
    if not js.get("ok"):
        raise RuntimeError(js.get("description") or f"HTTP {r.status_code}")
    return js.get("result")


def send(token, chat_id, text, log=None):
    """Отправка в фоне: робот не ждёт Telegram."""
    if not token or not chat_id:
        return

    def job():
        try:
            _call(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        except Exception as e:
            if log:
                log(f"Telegram: сообщение не отправлено ({e})")
    threading.Thread(target=job, daemon=True).start()


def send_now(token, chat_id, text):
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text})


def find_chat_id(token):
    """chat id из последнего сообщения, которое пользователь написал боту."""
    upd = _call(token, "getUpdates", {"limit": 20}) or []
    for u in reversed(upd):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"]), chat.get("first_name") or chat.get("title") or ""
    return None, None


def fmt(v):
    return f"{v:.1f}" if v >= 1000 else (f"{v:.2f}" if v >= 10 else (f"{v:.3f}" if v >= 1 else f"{v:.5f}"))


def formation_text(i):
    side = "ЛОНГ ⬆" if i["side"] > 0 else "ШОРТ ⬇"
    mk = f"{i['market'] * 100:+.2f}%" if i.get("market") is not None else "—"
    lines = [f"📊 Формация: {i['ticker']} {i['tf']} — {side}",
             f"Импульс {i['impulse_t']}: {i['move'] * 100:+.2f}%, объём ×{i['mult']:.1f}",
             f"Касание EMA20 {i['touch_t']}, подтверждение {i['confirm_t']}",
             f"Цена {fmt(i['price'])} · стоп {fmt(i['stop'])} · тейк1 {fmt(i['take1'])}"
             + (f" · тейк2 {fmt(i['take2'])}" if i.get("take2") else ""),
             f"Индекс с открытия: {mk}",
             f"Робот: {i['decision']}"]
    return "\n".join(lines)


def trade_text(kind, i):
    if kind == "open":
        side = "КУПЛЕНО" if i["side"] > 0 else "ПРОДАНО (шорт)"
        s = f"✅ {i['ticker']}: {side} {i['lots']} лот. по {fmt(i['entry'])}, стоп {fmt(i['stop'])}"
        if i.get("take"):
            s += f", тейк1 {fmt(i['take'])}" + (f", тейк2 {fmt(i['take2'])}" if i.get("take2") else "")
        return s
    return f"{'💰' if i['result'] > 0 else '🔻'} {i['ticker']}: закрыто {i['lots']} лот. ({i['why']}), {i['result'] * 100:+.2f}%"


def impulse_text(i):
    return (f"⚡ Всплеск объёма: {i['ticker']} {i['tf']} — {'рост' if i['move'] > 0 else 'падение'} {abs(i['move']) * 100:.2f}% "
            f"с {i['t']} ({i['bars']} св.), оборот {i['value'] / 1e6:.0f} млн ₽ (×{i['mult']:.1f}), цена {fmt(i['price'])}. "
            f"Жду отката к EMA20.")
