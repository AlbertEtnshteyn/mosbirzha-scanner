"""Свечи индекса IMOEX (10 мин) с ISS Мосбиржи за период — для проверки стратегий на истории (запускается в GitHub Actions)."""
import csv
import sys
import time
from datetime import date, timedelta

import requests

frm = date.fromisoformat(sys.argv[1])
till = date.fromisoformat(sys.argv[2])
out = sys.argv[3]
rows = []
d = frm
while d <= till:
    e = min(d + timedelta(days=20), till)
    start = 0
    while True:
        url = (f"https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX/candles.json"
               f"?from={d}&till={e}&interval=10&start={start}")
        for k in range(5):
            try:
                js = requests.get(url, timeout=30).json()
                break
            except Exception:
                time.sleep(3)
        c = js["candles"]
        data = c["data"]
        cols = c["columns"]
        for r in data:
            x = dict(zip(cols, r))
            rows.append((x["begin"], x["open"], x["high"], x["low"], x["close"]))
        if len(data) < 500:
            break
        start += len(data)
    d = e + timedelta(days=1)
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["begin", "open", "high", "low", "close"])
    w.writerows(sorted(set(rows)))
print(len(rows), "свечей")
