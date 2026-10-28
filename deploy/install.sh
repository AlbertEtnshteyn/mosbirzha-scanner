#!/usr/bin/env bash
# Установка/обновление сервера робота Мосбиржи на VPS (Ubuntu 22.04/24.04, Debian 12).
# Запуск от root:  bash install.sh <публичный-IP-сервера>
# Программа ставится в /opt/moexrobot и запускается службой systemd (сама поднимается после перезагрузки и сбоев).
# Сертификаты Минцифры для связи с Т-Инвестициями кладутся только в папку программы, в систему не ставятся.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/moexrobot
IP="${1:-}"
export DEBIAN_FRONTEND=noninteractive

echo "== 1/6 Пакеты системы"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl tzdata >/dev/null

echo "== 2/6 Пользователь и папка $DST"
id moexrobot >/dev/null 2>&1 || useradd --system --home "$DST" --shell /usr/sbin/nologin moexrobot
mkdir -p "$DST"
# код программы обновляется, данные (настройки, журналы, ключ и сертификат сервера) остаются
for f in server.py scanner.py autotrader.py robotcore.py tgnotify.py history_dl.py version.txt; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DST/"
done
rm -rf "$DST/web" "$DST/tools"; cp -r "$SRC/web" "$DST/web"; cp -r "$SRC/tools" "$DST/tools"
# перенос настроек и журналов с компьютера (если переданы)
if [ -d "$SRC/transfer" ]; then
  cp "$SRC/transfer/"* "$DST/" 2>/dev/null || true
  echo "   перенесены настройки и журналы с компьютера"
fi

echo "== 3/6 Python и библиотеки"
[ -x "$DST/venv/bin/python" ] || python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" install -q --upgrade pip
"$DST/venv/bin/pip" install -q "requests>=2.31" certifi cryptography

echo "== 4/6 Сертификаты НУЦ Минцифры (только в папку программы)"
mkdir -p "$DST/certs"
ls "$DST/certs/"*.pem >/dev/null 2>&1 || "$DST/venv/bin/python" "$DST/tools/get_ru_certs.py" "$DST/certs" || echo "   не скачались — программа попробует сама при первом запросе"

if [ -z "$IP" ]; then IP="$(curl -s -4 --max-time 8 https://ifconfig.me || hostname -I | awk '{print $1}')"; fi
chown -R moexrobot:moexrobot "$DST"
chmod 700 "$DST"

echo "== 5/6 Служба moexrobot (адрес $IP, порт 8443)"
cat > /etc/systemd/system/moexrobot.service <<UNIT
[Unit]
Description=Робот Мосбиржи (сервер)
After=network-online.target
Wants=network-online.target

[Service]
User=moexrobot
WorkingDirectory=$DST
ExecStart=$DST/venv/bin/python -u $DST/server.py --port 8443 --host $IP
Restart=always
RestartSec=10
Environment=PYTHONIOENCODING=utf-8

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable moexrobot >/dev/null 2>&1
rm -f "$DST/connect_code.txt"
systemctl restart moexrobot
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 8443/tcp >/dev/null; fi

echo "== 6/6 Проверка"
for i in $(seq 1 40); do [ -s "$DST/connect_code.txt" ] && break; sleep 1; done
if [ ! -s "$DST/connect_code.txt" ]; then
  echo "ОШИБКА: сервер не запустился. Последние строки журнала:"; journalctl -u moexrobot -n 40 --no-pager; exit 1
fi
systemctl is-active --quiet moexrobot && echo "Служба работает."
echo "CONNECT: $(cat $DST/connect_code.txt)"
