"""Генератор QR-кодов для этикеток и коробок (заказ Shimu TQ20260924C).

Использование:
    python3 make_qr_codes.py 104-2606920 103-2606921 ...
    python3 make_qr_codes.py --file lots.txt      # строки «№товара серия»

Для каждой пары создаёт out_qr/<№>_<серия>.svg и .pdf (вектор): QR со ссылкой
t.me/vetop_helper_bot?start=<№>-<серия> и подпись — название/фасовка
по-русски и номер товара с серией латиницей (заводу так проще). Без серии
(«104») — QR только по товару. Нужна библиотека segno (pip install segno).
"""
import os
import sys

import segno

import prices
from bot import qr_deeplink

OUT = "out_qr"


def make(product_id: int, lot: str | None):
    p = prices.BY_ID[product_id]
    url = qr_deeplink(product_id, lot)
    base = f"{product_id}" + (f"_{lot}" if lot else "")
    os.makedirs(OUT, exist_ok=True)
    qr = segno.make(url, error="m")
    title = f"{prices._base_name(p['name']).upper()} {p['volume']}"
    qr.save(os.path.join(OUT, base + ".svg"), scale=10, border=2)
    qr.save(os.path.join(OUT, base + ".pdf"), scale=10, border=2)
    print(f"{base}: {title}" + (f" · серия {lot}" if lot else "") + f" → {url}")


def main(argv):
    pairs = []
    if argv[:1] == ["--file"]:
        with open(argv[1], encoding="utf-8") as f:
            for line in f:
                parts = line.replace("-", " ").split()
                if parts:
                    pairs.append((int(parts[0]), parts[1] if len(parts) > 1 else None))
    else:
        for a in argv:
            pid, _, lot = a.partition("-")
            pairs.append((int(pid), lot or None))
    for pid, lot in pairs:
        make(pid, lot)


if __name__ == "__main__":
    main(sys.argv[1:])
