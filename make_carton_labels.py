"""Наклейки на БОЛЬШИЕ коробки поставки Shimu TQ20260924C (30.09.2026).

A5 горизонтально, 1:1, по одной странице на позицию/серию + страница
требований для завода. Состав (решения владельца 30.09.2026): лого,
название и фасовка крупно, «Серия:», «Годен до:» (месяц.год), количество
в коробке, «РУ № KG …» (регистрационное удостоверение КР), условия
хранения, мелко «Только для применения в ветеринарии», справа коробочный
QR (-K). БЕЗ производителя/дистрибьютора — они в печати самой коробки.

    python3 make_carton_labels.py            # → out_carton/…all22.pdf + sample

Тексты для Word-версий — supply_tq20260924c/scripts/*.js (docx-js).
"""
import os, sys, textwrap
import segno
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prices, make_qr_codes as mq, tq20260924c_lots_data as L

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Рег. удостоверения КР (Shimu_RU_TQ20260924C.pdf, сверено с оригиналами
# удостоверений 30.09.2026; действуют 18.06.2024–18.06.2029)
REG = {"АВЕРТОП": "KG 1674", "АЛЬБЕН ПЛЮС 100": "KG 1673", "АЛБЕНИВЕР": "KG 1681",
       "ДОКЦИЛИН 200": "KG 1679", "КЛОЗАН ПЛЮС 100": "KG 1671", "ОКСИЛИН 300 LA": "KG 1669",
       "ПЕНСТРЕП ПЛЮС LA": "KG 1672", "ФЛОРФЕН ПЛЮС 300": "KG 1684"}
# Из инструкции Иверклоза (KG 1680); для остальных препаратов Shimu принято
# одинаковым — сверить с инструкцией каждого при печати.
STORAGE = "Хранить в сухом, тёмном и недоступном для детей месте при температуре от 0 до 30 °C."
W, H = 210 * mm, 148 * mm
OUT = "out_carton"


def fit(c, font, size, text, maxw):
    while size > 6 and pdfmetrics.stringWidth(text, font, size) > maxw:
        size -= 1
    c.setFont(font, size)
    return size


def draw_qr(c, url, x, y, size_mm):
    q = segno.make(url, error="m"); m = q.matrix; n = len(m)
    cell = size_mm * mm / (n + 8); border = 4 * cell
    for yy, row in enumerate(m):
        for x0, x1 in mq._runs(row):
            c.rect(x + border + x0 * cell, y + size_mm * mm - border - (yy + 1) * cell,
                   (x1 - x0) * cell, cell, stroke=0, fill=1)


def qty_line(p):
    b = int(p["box"])
    return f"{b} уп × 10 фл = {b * 10} фл" if "фл" in p["volume"] else f"В коробке: {b} шт"


def label(c, pid, lot, exp):
    p = prices.BY_ID[pid]; ru = prices._base_name(p["name"]).upper()
    vol = p["volume"].split("(")[0].strip(); reg = REG[ru]
    c.setLineWidth(0.5); c.setStrokeColorRGB(0.6, 0.6, 0.6); c.rect(3 * mm, 3 * mm, W - 6 * mm, H - 6 * mm)
    iw, ih = Image.open("IMG_3248.jpeg").size; lh = 26 * mm; lw = lh * iw / ih
    c.drawImage(ImageReader("IMG_3248.jpeg"), (W - lw) / 2, H - 6 * mm - lh, width=lw, height=lh, mask="auto")
    x = 8 * mm
    s1 = fit(c, "DVB", 68, ru, W - 16 * mm); c.drawCentredString(W / 2, H - 58 * mm, ru)
    s2 = fit(c, "DVB", 48, vol, W - 16 * mm); c.drawCentredString(W / 2, H - 74 * mm, vol)
    tw = 135 * mm
    t = f"Серия:  {lot}"; fit(c, "DVB", 20, t, tw); c.drawString(x, H - 88 * mm, t)
    t = f"Годен до:  {exp}"; fit(c, "DVB", 20, t, tw); c.drawString(x, H - 98 * mm, t)
    t = qty_line(p); fit(c, "DVB", 14, t, tw); c.drawString(x, H - 107 * mm, t)
    qs = 40; draw_qr(c, mq.qr_deeplink(pid, lot, box=True), W - qs * mm - 8 * mm, H - 116 * mm, qs)
    t = f"РУ № {reg}"; fit(c, "DVB", 16, t, tw); c.drawString(x, H - 121 * mm, t)
    s3 = fit(c, "DV", 11, STORAGE, W - 16 * mm); c.drawString(x, H - 130 * mm, STORAGE)
    c.setFont("DV", 8); c.drawString(x, H - 138 * mm, "Только для применения в ветеринарии")
    return s1, s2, s3


def spec(c, s1, s2, s3):
    c.setFont("DVB", 15); c.drawString(12 * mm, H - 16 * mm, "Outer carton label — specification (order TQ20260924C)")
    blocks = [
        ("Size", "A5 landscape, 210 × 148 mm (labels are 1:1). Paper label glued on the carton. All text in Russian. Keep the printed carton design as it is (handling marks, weight, size, manufacturer, distributor) — that is why the label has no manufacturer/distributor lines."),
        ("Placement", "The carton has two printed sides and two blank sides. Put one label on EACH blank side (two labels per carton), centered."),
        ("Content", "1. VETOP logo (original proportions). 2. Product name and package size — the largest text. 3. «Серия:» batch number. 4. «Годен до:» expiry (MM.YYYY). 5. Quantity per carton. 6. «РУ № KG …» — Kyrgyz registration certificate number of the product. 7. Storage conditions. 8. Carton QR code. Small line «Только для применения в ветеринарии»."),
        ("QR code", "Use the CARTON code from archive QR_TQ20260924C_BOXES (file name ends with _K), one code per product/batch — it is already placed on each label in the file. Minimum 40 × 40 mm, black on white, keep the white margin, do not redraw or stretch. Do NOT use the bottle code (archive QR_TQ20260924C_VETOP) on the carton."),
        ("Font sizes", f"product name up to {s1} pt, package size {s2} pt, batch / expiry 20 pt, registration 16 pt, storage {s3} pt."),
        ("Before printing", "send us the artwork of each label and a photo of one test print — we will scan and confirm."),
    ]
    y = H - 27 * mm
    for head, body in blocks:
        c.setFont("DVB", 10.5); c.drawString(12 * mm, y, head + ":"); y -= 5.5 * mm
        c.setFont("DV", 9.5)
        for ln in textwrap.wrap(body, 105):
            c.drawString(15 * mm, y, ln); y -= 4.8 * mm
        y -= 2 * mm
    c.setFont("DV", 8); c.drawString(12 * mm, 8 * mm, "Подготовлено ОсОО «ВЕТОП» · 30.09.2026")


def main():
    pdfmetrics.registerFont(TTFont("DV", FONT)); pdfmetrics.registerFont(TTFont("DVB", FONTB))
    os.makedirs(OUT, exist_ok=True)
    c = canvas.Canvas(os.path.join(OUT, "carton_label_sample_A5.pdf"), pagesize=(W, H))
    s = label(c, 91, "20261115C", "11.2029"); c.showPage(); spec(c, *s); c.save()
    c = canvas.Canvas(os.path.join(OUT, "carton_labels_TQ20260924C_all22.pdf"), pagesize=(W, H))
    for pid, lot, exp, _qty in L.LOTS:
        label(c, pid, lot, exp); c.showPage()
    spec(c, *s); c.save()
    print("готово:", OUT)


if __name__ == "__main__":
    main()
