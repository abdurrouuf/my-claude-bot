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
    m = qr.matrix
    n = len(m)
    cell = 4               # px на модуль
    border = 4 * cell
    qw = n * cell + 2 * border
    # ширина страницы — по самой длинной строке подписи (≈0.62 px на символ
    # при 13px), чтобы длинные названия не обрезались
    tw = int(max(len(l) for l in lines) * 13 * 0.62) + 2 * border
    w = max(qw, tw)
    ox = (w - qw) // 2
    h = qw + 18 * len(lines) + 10
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}">', f'<rect width="{w}" height="{h}" fill="#fff"/>']
    for y, row in enumerate(m):
        for x0, x1 in _runs(row):          # слитные полосы — без швов между модулями
            out.append(f'<rect x="{ox + border + x0 * cell}" y="{border + y * cell}" '
                       f'width="{(x1 - x0) * cell}" height="{cell}" fill="#000"/>')
    ty = qw + 4
    for i, line in enumerate(lines):
        out.append(f'<text x="{w / 2}" y="{ty + 14 + i * 18}" font-family="DejaVu Sans, '
                   f'Arial, sans-serif" font-size="{13 if i == 0 else 11}" '
                   f'text-anchor="middle">{line}</text>')
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def _pdf(path, qr, lines):
    pdfmetrics.registerFont(TTFont("DejaVu", FONT))
    m = qr.matrix
    n = len(m)
    cell = 1.0 * mm
    border = 4 * cell
    qsize = n * cell + 2 * border
    # ширина страницы — по самой длинной строке подписи (иначе длинные
    # названия обрезались краем страницы)
    fonts = [9 if i == 0 else 7.5 for i in range(len(lines))]
    tw = max(pdfmetrics.stringWidth(l, "DejaVu", f) for l, f in zip(lines, fonts)) + 2 * border
    size = max(qsize, tw)
    ox = (size - qsize) / 2
    h = qsize + 6 * mm * len(lines) + 3 * mm
    c = canvas.Canvas(path, pagesize=(size, h))
    for y, row in enumerate(m):
        for x0, x1 in _runs(row):
            c.rect(ox + border + x0 * cell, h - border - (y + 1) * cell,
                   (x1 - x0) * cell, cell, stroke=0, fill=1)
    ty = h - qsize - 4 * mm
    for i, line in enumerate(lines):
        c.setFont("DejaVu", fonts[i])
        c.drawCentredString(size / 2, ty - i * 6 * mm, line)
    c.save()


def make(product_id: int, lot: str | None, expiry: str | None = None,
         box: bool = False):
    """box=True — QR на БОЛЬШУЮ коробку: ссылка с хвостом -K, подпись
    «КОРОБКА N шт» (N — вместимость из прайса); бот подставит целую коробку."""
    url = qr_deeplink(product_id, lot, box=box)
    name, vol = _title(product_id)
    base = f"{product_id}" + (f"_{lot}" if lot else "") + ("_K" if box else "")
    os.makedirs(OUT, exist_ok=True)
    qr = segno.make(url, error="m")
    lines = [f"{name} {vol}".strip(), f"No.{product_id}" + (f"  Lot {lot}" if lot else "")]
    if expiry:
        lines.append(f"Exp {expiry}")
    if box:
        p = prices.BY_ID.get(product_id) or {}
        n = int(p.get("box") or 0)
        lines.insert(1, f"КОРОБКА {n} шт" if n > 1 else "КОРОБКА")
    _svg(os.path.join(OUT, base + ".svg"), qr, lines)
    _pdf(os.path.join(OUT, base + ".pdf"), qr, lines)
    print(f"{base}: {lines[0]} → {url}")


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
