# -*- coding: utf-8 -*-
"""Установка/обновление сервера робота на VPS прямо из программы: по SSH (адрес, логин, пароль от VPS)."""
import io
import json
import os
import shlex
import sys
import zipfile

import scanner as sc

BUNDLE_FILES = ["server.py", "scanner.py", "autotrader.py", "robotcore.py", "tgnotify.py", "history_dl.py", "version.txt"]
BUNDLE_DIRS = ["web", "tools"]
TRANSFER = ["auto_state.json", "sandbox_trades.csv", "sandbox_spikes.csv", "forward_journal.csv", "forward_days.csv"]
REMOTE = "/tmp/moexrobot_setup"


def _src_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def bundle_bytes():
    """Архив с программой для сервера: из сборки (exe) или из папки с исходниками."""
    packed = os.path.join(_src_dir(), "server_bundle.zip")
    if os.path.exists(packed):
        return open(packed, "rb").read()
    base = _src_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in BUNDLE_FILES:
            p = os.path.join(base, f)
            if os.path.exists(p):
                z.write(p, f)
        for d in BUNDLE_DIRS:
            for root, _, files in os.walk(os.path.join(base, d)):
                for f in files:
                    if f.endswith((".pyc",)):
                        continue
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, base))
        sh = open(os.path.join(base, "deploy", "install.sh"), "rb").read().replace(b"\r\n", b"\n")
        z.writestr("install.sh", sh)
    return buf.getvalue()


def install(host, user, password, settings, transfer=True, log=print, port=22):
    """Ставит/обновляет сервер. Возвращает код подключения moexrobot://…"""
    import paramiko
    log(f"Подключаюсь к {host} по SSH…")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, port=port, username=user, password=password, timeout=20, banner_timeout=30,
                auth_timeout=30, look_for_keys=False, allow_agent=False)
    try:
        def run(cmd, show=True):
            sudo = "" if user == "root" else "sudo -S -p '' "
            stdin, stdout, _ = cli.exec_command(sudo + "bash -lc " + shlex.quote(cmd), get_pty=True, timeout=1800)
            if sudo:
                stdin.write(password + "\n")
                stdin.flush()
            out = []
            for line in iter(stdout.readline, ""):
                line = line.rstrip("\r\n")
                if line.strip() == password:
                    continue
                out.append(line)
                if show and line.strip():
                    log("  " + line)
            return stdout.channel.recv_exit_status(), out

        log("Передаю программу на сервер…")
        run(f"rm -rf {REMOTE} && mkdir -p {REMOTE} && chmod 777 {REMOTE}", show=False)
        sftp = cli.open_sftp()
        sftp.putfo(io.BytesIO(bundle_bytes()), f"{REMOTE}/bundle.zip")
        if transfer:
            sftp.mkdir(f"{REMOTE}/transfer")
            s = dict(settings)
            s["token"] = s.get("token") or os.environ.get("TINVEST_TOKEN", "")
            s["remember_token"] = True
            s["autostart"] = False                      # торговлю на сервере включают кнопкой «Старт» с телефона
            sftp.putfo(io.BytesIO(json.dumps(s, ensure_ascii=False, indent=1).encode("utf-8")), f"{REMOTE}/transfer/settings.json")
            for f in TRANSFER:
                p = os.path.join(sc.HERE, f)
                if os.path.exists(p):
                    sftp.put(p, f"{REMOTE}/transfer/{f}")
        sftp.close()
        code, _ = run(f"cd {REMOTE} && (command -v unzip >/dev/null || (apt-get update -qq && apt-get install -y -qq unzip >/dev/null)) "
                      f"&& unzip -oq bundle.zip && bash install.sh {shlex.quote(host)}")
        _, out = run("cat /opt/moexrobot/connect_code.txt", show=False)
        run(f"rm -rf {REMOTE}", show=False)
        conn = next((ln.strip() for ln in out if ln.strip().startswith("moexrobot://")), None)
        if code != 0 or not conn:
            raise RuntimeError("установка не завершилась — подробности выше в журнале")
        log("Сервер установлен и работает.")
        return conn
    finally:
        cli.close()


def connect_code(host, user, password, port=22):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, port=port, username=user, password=password, timeout=20, look_for_keys=False, allow_agent=False)
    try:
        sudo = "" if user == "root" else "sudo -S -p '' "
        stdin, stdout, _ = cli.exec_command(sudo + "cat /opt/moexrobot/connect_code.txt", get_pty=bool(sudo))
        if sudo:
            stdin.write(password + "\n")
            stdin.flush()
        for ln in stdout.read().decode("utf-8", "ignore").splitlines():
            if ln.strip().startswith("moexrobot://"):
                return ln.strip()
    finally:
        cli.close()
    return None


def qr_matrix(text):
    import qrcode
    q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    return q.get_matrix()
