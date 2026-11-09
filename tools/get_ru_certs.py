# -*- coding: utf-8 -*-
"""
Скачивает корневой и промежуточный сертификаты НУЦ Минцифры (Russian Trusted Root CA / Sub CA)
с официального сервера госуслуг и кладёт их в certs/ для встраивания в программу.
Запускается при сборке на GitHub. Проверяет, что это действительно нужные сертификаты.
"""
import hashlib
import os
import ssl
import sys
import tempfile
import urllib.request

TARGETS = {
    "russian_trusted_root_ca.pem": {
        "cn": "Russian Trusted Root CA",
        "urls": ["https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
                 "https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer"],
    },
    "russian_trusted_sub_ca.pem": {
        "cn": "Russian Trusted Sub CA",
        "urls": ["https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
                 "https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer"],
    },
}


def to_pem(data):
    if b"-----BEGIN CERTIFICATE-----" in data:
        return data.decode("ascii", "ignore")
    return ssl.DER_cert_to_PEM_cert(data)


def decode(pem):
    fd, p = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(fd, "w") as f:
        f.write(pem)
    try:
        return ssl._ssl._test_decode_cert(p)
    finally:
        os.remove(p)


def cn(name):
    return " ".join(v for rdn in name for k, v in rdn if k == "commonName")


def main(out="certs"):
    os.makedirs(out, exist_ok=True)
    ok = 0
    for fname, t in TARGETS.items():
        for url in t["urls"]:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=30).read()
                pem = to_pem(data)
                d = decode(pem)
                subj, iss = cn(d.get("subject", ())), cn(d.get("issuer", ()))
                if t["cn"] not in subj:
                    print(f"{url}: неожиданный сертификат «{subj}», пропуск")
                    continue
                der = ssl.PEM_cert_to_DER_cert(pem.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n")
                with open(os.path.join(out, fname), "w", encoding="ascii") as f:
                    f.write(pem)
                print(f"OK {fname}: {subj} | выдан: {iss} | до {d.get('notAfter')} | SHA-256 {hashlib.sha256(der).hexdigest().upper()}")
                ok += 1
                break
            except Exception as e:
                print(f"{url}: {e}")
    if ok != len(TARGETS):
        sys.exit("Не удалось скачать сертификаты Минцифры")


if __name__ == "__main__":
    main(*sys.argv[1:])
