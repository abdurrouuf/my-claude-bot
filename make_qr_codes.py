"""Генератор QR-кодов для этикеток и коробок (заказ Shimu TQ20260924C).

    python3 make_qr_codes.py 104-2606920 103-2606921 ...
    python3 make_qr_codes.py --lots tq20260924c_lots_data   # из таблицы серий

На каждую пару «товар-серия» в out_qr/ создаются <№>_<серия>.svg и .pdf
(вектор): QR со ссылкой t.me/vetop_helper_bot?start=<№>-<серия> и подпись —
название/фасовка по-русски, латиницей № товара и серия, срок. Нужна
библиотека segno (pip install segno). Товар вне прайса (новые №105/№106)
подписывается из NEW_NAMES.
"""
import importlib
import os
import sys

import segno
from reportlab.lib.pagesizes import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import prices


def qr_deeplink(product_id, lot=None, bot_name="vetop_helper_bot", box=False):
    """Та же ссылка, что в bot.qr_deeplink (без импорта бота — ему нужны токены)."""
    tail = f"{product_id}-{lot}" if lot else str(product_id)
    if box:
        tail += "-K"                       # QR коробки — см. bot.qr_parse_start
    return f"https://t.me/{bot_name}?start={tail}"

OUT = "out_qr"
# Позиции заказа, которых ещё нет в прайсе (владелец добавит): № → (имя, фасовка)
NEW_NAMES = {105: ("ДОКЦИЛИН 200", "50 мл"), 106: ("ФЛОРФЕН ПЛЮС 300", "50 мл")}
# Английские названия — как в проформе TQ20260924C (Shimu), чтобы завод понял,
# на какой препарат клеить код. У «Альбен плюс 100» латинского бренда в
# проформе нет — только состав.
EN_NAMES = {
    69: ("Avertop", "Avermectin solution pour on 0.5%"),
    70: ("Avertop", "Avermectin solution pour on 0.5%"),
    71: ("Avertop", "Avermectin solution pour on 0.5%"),
    72: ("Alben plus 100", "Albendazole 10% Oral Solution"),
    74: ("Alben plus 100", "Albendazole 10% Oral Solution"),
    75: ("Alben plus 100", "Albendazole 10% Oral Solution"),
    76: ("Albeniver", "Albendazole 10% + Ivermectin 0.4% Oral Solution"),
    77: ("Albeniver", "Albendazole 10% + Ivermectin 0.4% Oral Solution"),
    78: ("Albeniver", "Albendazole 10% + Ivermectin 0.4% Oral Solution"),
    79: ("Albeniver", "Albendazole 10% + Ivermectin 0.4% Oral Solution"),
    105: ("Doxyline 200", "Doxycycline Hyclate 20% Injection"),
    80: ("Doxyline 200", "Doxycycline Hyclate 20% Injection"),
    89: ("Closanplus 100 LA", "Closantel Sodium 10% Injection"),
    90: ("Closanplus 100 LA", "Closantel Sodium 10% Injection"),
    91: ("Oxyline 300 LA", "Oxytetracycline 30% Injection"),
    92: ("Oxyline 300 LA", "Oxytetracycline 30% Injection"),
    93: ("Oxyline 300 LA", "Oxytetracycline 30% Injection"),
    94: ("Penstrep Plus LA", "Procaine Penicillin G + Dihydrostreptomycin"),
    95: ("Penstrep Plus LA", "Procaine Penicillin G + Dihydrostreptomycin"),
    96: ("Penstrep Plus LA", "Procaine Penicillin G + Dihydrostreptomycin"),
    106: ("Florfen Plus 300", "Florfenicol Injection 30%"),
    101: ("Florfen Plus 300", "Florfenicol Injection 30%"),
}


def _vol_en(vol: str) -> str:
    v = vol.replace("(10 фл)", "x 10 bottles").replace("мл", "ml")
    v = v.replace(" л", " L").replace("кг", "kg").replace(" г", " g").replace("таб", "tab")
    return v.strip()


def _en_lines(product_id, vol, box_n=0):
    """[(текст, кегль)] — английские строки подписи; пусто, если названия нет."""
    en = EN_NAMES.get(product_id)
    if not en:
        return []
    brand, comp = en
    if "фл" in vol:                       # «10 мл (10 фл)» — пачка из 10 флаконов
        # термины завода (проформа): «box» = коробочка флакона,
        # «middle box» = 10 флаконов = наша единица прайса
        pack = f"{_vol_en(vol.split('(')[0])}, middle box of 10 bottles"
    else:
        pack = f"{_vol_en(vol)}/bottle"
    out = [(f"{brand} — {pack}", 8), (comp, 6.5)]
    if box_n > 1:
        if "фл" in vol:
            out.append((f"CARTON {box_n} middle boxes = {box_n * 10} bottles", 8))
        else:
            out.append((f"CARTON {box_n} bottles", 8))
    return out
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _title(product_id):
    p = prices.BY_ID.get(product_id)
    if p:
        return prices._base_name(p["name"]).upper(), p["volume"]
    return NEW_NAMES.get(product_id, (f"товар №{product_id}", ""))


def _runs(row):
    """Отрезки [x0, x1) чёрных модулей в строке — рисуем полосами, чтобы при
    печати/рендере между соседними квадратиками не было белых швов."""
    out, start = [], None
    for x, v in enumerate(list(row) + [0]):
        if v and start is None:
            start = x
        elif not v and start is not None:
            out.append((start, x))
            start = None
    return out


def _svg(path, qr, lines):
    """lines — [(текст, кегль pt)]. Ширина страницы — по самой длинной строке."""
    m = qr.matrix
    n = len(m)
    cell = 4               # px на модуль
    border = 4 * cell
    qw = n * cell + 2 * border
    px = lambda pt: pt * 4 / 3            # pt → px (SVG 96 dpi)
    tw = int(max(len(t) * px(sz) * 0.62 for t, sz in lines)) + 2 * border
    w = max(qw, tw)
    ox = (w - qw) // 2
    step = lambda sz: int(px(sz) * 1.45)
    h = qw + sum(step(sz) for _, sz in lines) + 12
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}">', f'<rect width="{w}" height="{h}" fill="#fff"/>']
    for y, row in enumerate(m):
        for x0, x1 in _runs(row):          # слитные полосы — без швов между модулями
            out.append(f'<rect x="{ox + border + x0 * cell}" y="{border + y * cell}" '
                       f'width="{(x1 - x0) * cell}" height="{cell}" fill="#000"/>')
    ty = qw + 4
    for t, sz in lines:
        ty += step(sz)
        out.append(f'<text x="{w / 2}" y="{ty}" font-family="DejaVu Sans, '
                   f'Arial, sans-serif" font-size="{px(sz):.1f}" '
                   f'text-anchor="middle">{t}</text>')
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def _pdf(path, qr, lines):
    """lines — [(текст, кегль pt)]. Ширина страницы — по самой длинной строке
    (иначе длинные названия обрезались краем страницы)."""
    pdfmetrics.registerFont(TTFont("DejaVu", FONT))
    m = qr.matrix
    n = len(m)
    cell = 1.0 * mm
    border = 4 * cell
    qsize = n * cell + 2 * border
    tw = max(pdfmetrics.stringWidth(t, "DejaVu", sz) for t, sz in lines) + 2 * border
    size = max(qsize, tw)
    ox = (size - qsize) / 2
    step = lambda sz: sz * 1.45 / 72 * 25.4 * mm      # интервал строки в мм
    h = qsize + sum(step(sz) for _, sz in lines) + 4 * mm
    c = canvas.Canvas(path, pagesize=(size, h))
    for y, row in enumerate(m):
        for x0, x1 in _runs(row):
            c.rect(ox + border + x0 * cell, h - border - (y + 1) * cell,
                   (x1 - x0) * cell, cell, stroke=0, fill=1)
    ty = h - qsize - 1 * mm
    for t, sz in lines:
        ty -= step(sz)
        c.setFont("DejaVu", sz)
        c.drawCentredString(size / 2, ty, t)
    c.save()


def make(product_id: int, lot: str | None, expiry: str | None = None,
         box: bool = False):
    """box=True — QR на БОЛЬШУЮ коробку: ссылка с хвостом -K, подпись
    «КОРОБКА N шт / CARTON N bottles»; бот подставит целую коробку.
    Под русским названием — английское (из проформы), чтобы завод понял,
    на какой препарат клеить код."""
    url = qr_deeplink(product_id, lot, box=box)
    name, vol = _title(product_id)
    base = f"{product_id}" + (f"_{lot}" if lot else "") + ("_K" if box else "")
    os.makedirs(OUT, exist_ok=True)
    qr = segno.make(url, error="m")
    p = prices.BY_ID.get(product_id) or {}
    box_n = int(p.get("box") or 0)
    lines = [(f"{name} {vol}".strip(), 9)]
    if box:
        if "фл" in vol and box_n > 1:      # единица прайса — упаковка 10 флаконов
            lines.append((f"КОРОБКА {box_n} уп × 10 фл = {box_n * 10} фл", 9))
        else:
            lines.append((f"КОРОБКА {box_n} шт" if box_n > 1 else "КОРОБКА", 9))
    lines += _en_lines(product_id, vol, box_n if box else 0)
    lines.append((f"No.{product_id}" + (f"  Lot {lot}" if lot else ""), 7.5))
    if expiry:
        lines.append((f"Exp {expiry}", 7.5))
    _svg(os.path.join(OUT, base + ".svg"), qr, lines)
    _pdf(os.path.join(OUT, base + ".pdf"), qr, lines)
    print(f"{base}: {lines[0][0]} → {url}")


def main(argv):
    box = False
    if argv[:1] == ["--boxes"]:            # QR коробок (снаружи, не вскрывая)
        box, argv = True, argv[1:]
    if argv[:1] == ["--lots"]:
        mod = importlib.import_module(argv[1])
        for pid, lot, exp, _qty in mod.LOTS:
            make(pid, lot, exp, box=box)
        return
    for a in argv:
        pid, _, lot = a.partition("-")
        make(int(pid), lot or None, box=box)


if __name__ == "__main__":
    main(sys.argv[1:])
