# Генерация PDF-накладной ВЕТОП (обычной и черновика с водяным знаком).
import io
import logging
import os
import re
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (HRFlowable, Image, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from db import BISHKEK

log = logging.getLogger(__name__)

_FONTS = None  # кэш зарегистрированных шрифтов


def fmt_num(n) -> str:
    """Форматирует число с апострофом: 7800 -> 7'800"""
    try:
        n = int(round(float(n)))
    except (ValueError, TypeError):
        return str(n)
    return f"{n:,}".replace(",", "'")


def safe_filename(name: str) -> str:
    """Оставляет в имени файла только буквы, цифры, дефис и подчёркивание."""
    cleaned = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")
    return cleaned or "клиент"


def _register_fonts():
    global _FONTS
    if _FONTS:
        return _FONTS
    base = os.path.dirname(os.path.abspath(__file__))
    normal_candidates = [
        os.path.join(base, "DejaVuSans.ttf"),
        "/app/DejaVuSans.ttf",
        "/app/fonts/DejaVuSans.ttf",
    ]
    bold_candidates = [
        os.path.join(base, "DejaVuSans-Bold.ttf"),
        "/app/DejaVuSans-Bold.ttf",
        "/app/fonts/DejaVuSans-Bold.ttf",
    ]
    font_path = next((p for p in normal_candidates if os.path.exists(p)), None)
    font_bold_path = next((p for p in bold_candidates if os.path.exists(p)), None)
    if font_path and font_bold_path:
        try:
            pdfmetrics.registerFont(TTFont("DejaVu", font_path))
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", font_bold_path))
            _FONTS = ("DejaVu", "DejaVu-Bold")
            return _FONTS
        except Exception as e:
            log.error("Font load error: %s", e)
    _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


def _find_logo():
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "IMG_3248.jpeg"),
        os.path.join(base, "logo.jpg"),
        os.path.join(base, "logo.jpeg"),
        os.path.join(base, "logo.png"),
        "/app/IMG_3248.jpeg",
        "/app/logo.jpg",
        "/app/logo.png",
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def _find_qr():
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "qr_instagram.jpeg"),
        os.path.join(base, "qr_instagram.png"),
        "/app/qr_instagram.jpeg",
        "/app/qr_instagram.png",
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def _watermark(font_bold_name):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_bold_name, 60)
        canvas.setFillColor(colors.Color(0.75, 0.75, 0.75))
        try:
            canvas.setFillAlpha(0.3)
        except Exception:
            pass
        w, h = A5
        canvas.translate(w / 2, h / 2)
        canvas.rotate(35)
        canvas.drawCentredString(0, 0, "ЧЕРНОВИК")
        canvas.restoreState()
    return draw


def _report_brand(font_name):
    """Фирменная рамка отчётных PDF (просьба владельца 04.08.2026):
    маленькое лого в правом верхнем углу и контакты в подвале —
    на КАЖДОЙ странице."""
    def draw(canvas, doc):
        from reportlab.lib.utils import ImageReader
        page_w, page_h = doc.pagesize
        canvas.saveState()
        logo = _find_logo()
        if logo:
            try:
                img = ImageReader(logo)
                iw, ih = img.getSize()
                w = 22 * mm
                h = w * ih / iw
                canvas.drawImage(img, page_w - 14 * mm - w,
                                 page_h - 5 * mm - h,
                                 width=w, height=h, mask="auto")
            except Exception:
                pass
        canvas.setFont(font_name, 7.5)
        canvas.setFillColor(colors.HexColor("#1b5e20"))
        canvas.drawCentredString(
            page_w / 2, 6 * mm,
            "ОсОО «ВЕТОП» · +996 700 99 88 11 · +996 700 887 666 · vettop@inbox.ru")
        canvas.restoreState()
    return draw


def generate_report_pdf(title: str, subtitle: str, sections: list,
                        footer: str = "") -> io.BytesIO:
    """Универсальный фирменный PDF-отчёт (остатки, долги, сроки и т.п.).

    sections: [{"title": str, "headers": [str], "rows": [[str]],
                "footer": str (опц.), "widths": [мм] (опц.),
                "numbered": bool (опц.) — добавить колонку «№» с 1..N}]
    """
    from reportlab.lib.pagesizes import A4

    font_name, font_bold_name = _register_fonts()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=14*mm, leftMargin=14*mm,
                            topMargin=10*mm, bottomMargin=12*mm)

    HEADER_GREEN = colors.HexColor("#1b5e20")
    LIGHT_ROW = colors.HexColor("#f1f8e9")

    title_style = ParagraphStyle('title', fontName=font_bold_name, fontSize=15,
                                 leading=19, alignment=TA_CENTER, spaceAfter=2)
    sub_style = ParagraphStyle('sub', fontName=font_name, fontSize=9.5,
                               leading=13, alignment=TA_CENTER,
                               textColor=HEADER_GREEN, spaceAfter=4)
    sec_style = ParagraphStyle('sec', fontName=font_bold_name, fontSize=11.5,
                               leading=15, spaceBefore=5, spaceAfter=3,
                               textColor=HEADER_GREEN)
    cell_style = ParagraphStyle('cell', fontName=font_name, fontSize=8.5, leading=11)
    head_style = ParagraphStyle('cellb', fontName=font_bold_name, fontSize=9,
                                leading=12, textColor=colors.white, alignment=TA_CENTER)
    foot_style = ParagraphStyle('foot', fontName=font_bold_name, fontSize=10,
                                leading=14, spaceBefore=3)

    story = [Paragraph(xml_escape(title), title_style),
             Paragraph(xml_escape(subtitle), sub_style)]
    for sec in sections:
        if sec.get("title"):
            story.append(Paragraph(xml_escape(sec["title"]), sec_style))
        headers = sec["headers"]
        rows = sec["rows"]
        widths = sec.get("widths")
        if sec.get("numbered"):
            headers = ["№"] + list(headers)
            rows = [[str(i)] + list(r) for i, r in enumerate(rows, 1)]
            if widths:
                widths = [10] + list(widths)
        data = [[Paragraph(xml_escape(h), head_style) for h in headers]]
        for row in rows:
            cells = []
            for c in row:
                if isinstance(c, (list, tuple)):
                    # Список в ячейке = многострочный текст (позиции накладной)
                    text = "<br/>".join(xml_escape(str(x)) for x in c)
                else:
                    text = xml_escape(str(c))
                cells.append(Paragraph(text, cell_style))
            data.append(cells)
        col_widths = [w * mm for w in widths] if widths else None
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_GREEN),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_ROW]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8e6c9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        story.append(table)
        if sec.get("footer"):
            story.append(Paragraph(xml_escape(sec["footer"]), foot_style))
        story.append(Spacer(1, 3*mm))
    if footer:
        story.append(Paragraph(xml_escape(footer), foot_style))
    brand = _report_brand(font_name)
    doc.build(story, onFirstPage=brand, onLaterPages=brand)
    buffer.seek(0)
    return buffer


def generate_price_pdf(price_data) -> io.BytesIO:
    """Фирменный прайс-лист для рассылки клиентам."""
    from reportlab.lib.pagesizes import A4

    font_name, font_bold_name = _register_fonts()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=14*mm, leftMargin=14*mm,
                            topMargin=10*mm, bottomMargin=12*mm)

    HEADER_GREEN = colors.HexColor("#1b5e20")
    LIGHT_ROW = colors.HexColor("#f1f8e9")

    title_style = ParagraphStyle('title', fontName=font_bold_name, fontSize=16,
                                 leading=20, alignment=TA_CENTER, spaceAfter=2)
    slogan_style = ParagraphStyle('slogan', fontName=font_name, fontSize=9,
                                  leading=12, alignment=TA_CENTER,
                                  textColor=HEADER_GREEN, spaceAfter=2)
    sub_style = ParagraphStyle('sub', fontName=font_name, fontSize=10,
                               leading=14, alignment=TA_CENTER, spaceAfter=4)
    cell_style = ParagraphStyle('cell', fontName=font_name, fontSize=8.5, leading=11)
    cell_right = ParagraphStyle('cellr', fontName=font_bold_name, fontSize=8.5, leading=11)
    cell_bold = ParagraphStyle('cellb', fontName=font_bold_name, fontSize=9,
                               leading=12, textColor=colors.white, alignment=TA_CENTER)

    story = []
    story.append(Paragraph("Здоровье ваших животных — наш приоритет", slogan_style))
    logo = _find_logo()
    if logo:
        try:
            img = Image(logo)
            iw, ih = img.imageWidth, img.imageHeight
            img.drawWidth = 45*mm
            img.drawHeight = 45*mm * ih / iw
            img.hAlign = "CENTER"
            story.append(img)
        except Exception:
            pass
    story.append(Paragraph("ПРАЙС-ЛИСТ", title_style))
    story.append(Paragraph(
        f"ОсОО «ВЕТОП» · оптовые цены · от {datetime.now(BISHKEK).strftime('%d.%m.%Y')}",
        sub_style))
    story.append(Spacer(1, 2*mm))

    header = [
        Paragraph("№", cell_bold),
        Paragraph("Название", cell_bold),
        Paragraph("Фасовка", cell_bold),
        Paragraph("шт/кор", cell_bold),
        Paragraph("Цена, сом", cell_bold),
    ]
    table_data = [header]
    for p in price_data:
        table_data.append([
            Paragraph(str(p["id"]), cell_style),
            Paragraph(xml_escape(p["name"]), cell_style),
            Paragraph(xml_escape(p["volume"]), cell_style),
            Paragraph(str(p["box"]), cell_style),
            Paragraph(fmt_num(p["price"]), cell_right),
        ])

    table = Table(table_data, colWidths=[10*mm, 92*mm, 26*mm, 18*mm, 22*mm],
                  repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_GREEN),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (3, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for r in range(1, len(table_data)):
        if r % 2 == 0:
            style.append(("BACKGROUND", (0, r), (-1, r), LIGHT_ROW))
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 4*mm))

    story.append(HRFlowable(width="100%", thickness=0.6, color=HEADER_GREEN))
    story.append(Spacer(1, 2*mm))
    contact_style = ParagraphStyle('contact', fontName=font_name, fontSize=9,
                                   leading=13, alignment=TA_LEFT)
    qr_path = _find_qr()
    if qr_path:
        try:
            qr_img = Image(qr_path)
            qr_img.drawWidth = 20*mm
            qr_img.drawHeight = 20*mm
            contact_table = Table(
                [[Paragraph("Заказы: +996 700 99 88 11, +996 700 887 666<br/>"
                            "Эл. почта: vettop@inbox.ru", contact_style), qr_img]],
                colWidths=[None, 22*mm])
            contact_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]))
            story.append(contact_table)
        except Exception:
            story.append(Paragraph("Заказы: +996 700 99 88 11 | +996 700 887 666 | vettop@inbox.ru",
                                   contact_style))
    else:
        story.append(Paragraph("Заказы: +996 700 99 88 11 | +996 700 887 666 | vettop@inbox.ru",
                               contact_style))

    doc.build(story)
    buffer.seek(0)
    return buffer


def generate_act_pdf(client_name, warehouse_name, rows, start_debt, end_debt,
                     period_label, client_phone=None) -> io.BytesIO:
    """Акт сверки: дата, документ, товар (+), оплата (−), долг после."""
    from reportlab.lib.pagesizes import A4

    font_name, font_bold_name = _register_fonts()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=12*mm, bottomMargin=12*mm)

    HEADER_GREEN = colors.HexColor("#1b5e20")
    LIGHT_ROW = colors.HexColor("#f1f8e9")

    title_style = ParagraphStyle('title', fontName=font_bold_name, fontSize=15,
                                 leading=20, alignment=TA_CENTER, spaceAfter=2)
    sub_style = ParagraphStyle('sub', fontName=font_name, fontSize=10,
                               leading=14, alignment=TA_LEFT)
    cell_style = ParagraphStyle('cell', fontName=font_name, fontSize=9, leading=12)
    cell_bold = ParagraphStyle('cellb', fontName=font_bold_name, fontSize=9,
                               leading=12, textColor=colors.white, alignment=TA_CENTER)
    total_style = ParagraphStyle('total', fontName=font_bold_name, fontSize=11,
                                 leading=15, alignment=TA_LEFT)

    story = []
    logo = _find_logo()
    if logo:
        try:
            img = Image(logo)
            iw, ih = img.imageWidth, img.imageHeight
            img.drawWidth = 40*mm
            img.drawHeight = 40*mm * ih / iw
            img.hAlign = "CENTER"
            story.append(img)
        except Exception:
            pass

    story.append(Paragraph("АКТ СВЕРКИ ВЗАИМОРАСЧЁТОВ", title_style))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(f"ОсОО «ВЕТОП» — <b>{xml_escape(str(client_name))}</b>", sub_style))
    if client_phone:
        story.append(Paragraph(f"Тел. клиента: {xml_escape(str(client_phone))}", sub_style))
    story.append(Paragraph(f"Склад: {xml_escape(str(warehouse_name))}", sub_style))
    story.append(Paragraph(f"Период: {xml_escape(str(period_label))}", sub_style))
    story.append(Paragraph(
        f"Составлен: {datetime.now(BISHKEK).strftime('%d.%m.%Y')}", sub_style))
    story.append(Spacer(1, 3*mm))

    header = [
        Paragraph("Дата", cell_bold),
        Paragraph("Документ", cell_bold),
        Paragraph("Товар (+)", cell_bold),
        Paragraph("Оплата (−)", cell_bold),
        Paragraph("Долг", cell_bold),
    ]
    table_data = [header]
    table_data.append([
        Paragraph("", cell_style),
        Paragraph("Долг на начало периода", cell_style),
        Paragraph("", cell_style), Paragraph("", cell_style),
        Paragraph(fmt_num(start_debt), cell_style),
    ])
    for date_str, doc_str, plus, minus, balance in rows:
        table_data.append([
            Paragraph(xml_escape(str(date_str)), cell_style),
            Paragraph(xml_escape(str(doc_str)), cell_style),
            Paragraph(fmt_num(plus) if plus else "", cell_style),
            Paragraph(fmt_num(minus) if minus else "", cell_style),
            Paragraph(fmt_num(balance), cell_style),
        ])

    table = Table(table_data, colWidths=[25*mm, 75*mm, 27*mm, 27*mm, 26*mm],
                  repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_GREEN),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in range(1, len(table_data)):
        if r % 2 == 0:
            style.append(("BACKGROUND", (0, r), (-1, r), LIGHT_ROW))
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 4*mm))

    story.append(HRFlowable(width="100%", thickness=0.6, color=HEADER_GREEN))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Итого долг на {datetime.now(BISHKEK).strftime('%d.%m.%Y')}: "
        f"<b>{fmt_num(end_debt)} сом</b>", total_style))
    story.append(Spacer(1, 10*mm))
    sig_style = ParagraphStyle('sig', fontName=font_name, fontSize=10, leading=14)
    sig = Table([[Paragraph("ОсОО «ВЕТОП»: ________________", sig_style),
                  Paragraph("Клиент: ________________", sig_style)]],
                colWidths=[85*mm, 85*mm])
    story.append(sig)
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph("+996 700 99 88 11  |  +996 700 887 666  |  vettop@inbox.ru",
                           sub_style))

    doc.build(story)
    buffer.seek(0)
    return buffer


def generate_pdf_invoice(client_name, items, invoice_total, prev_debt=0,
                         payment=0, is_payment=False, warehouse_name=None,
                         draft=False, watermark=True, doc_title=None,
                         total_label=None, extra_totals=None,
                         doc_number=None, box_note=None) -> io.BytesIO:
    """Красивый PDF: логотип, таблица товаров, итоги.

    draft=True не меняет содержимое, только оформление: водяной знак и пометка
    в заголовке — и то лишь при watermark=True (переходный режим — без пометок).
    doc_title/total_label/extra_totals позволяют строить производные документы
    (например, возвратную накладную): extra_totals — список (подпись, сумма),
    он заменяет стандартный блок долгов и оплат.
    box_note — строка «сколько это коробок» мелким шрифтом в правом нижнем
    углу под итогами (просьба владельца 16.08.2026).
    """
    show_mark = draft and watermark
    font_name, font_bold_name = _register_fonts()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A5,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=8*mm, bottomMargin=8*mm
    )

    HEADER_GREEN = colors.HexColor("#1b5e20")
    LIGHT_ROW = colors.HexColor("#f1f8e9")

    title_style = ParagraphStyle('title', fontName=font_bold_name, fontSize=14,
                                 leading=18, alignment=TA_CENTER, spaceAfter=2)
    sub_style = ParagraphStyle('sub', fontName=font_name, fontSize=9,
                               leading=13, alignment=TA_LEFT)
    cell_style = ParagraphStyle('cell', fontName=font_name, fontSize=8, leading=10)
    cell_bold = ParagraphStyle('cellb', fontName=font_bold_name, fontSize=8,
                               leading=10, textColor=colors.white, alignment=TA_CENTER)
    total_style = ParagraphStyle('total', fontName=font_bold_name, fontSize=10,
                                 leading=14, alignment=TA_LEFT)
    slogan_style = ParagraphStyle('slogan', fontName=font_name, fontSize=9,
                                  leading=12, alignment=TA_CENTER,
                                  textColor=HEADER_GREEN, spaceAfter=3)
    # Коробки/штуки — мелко и в правом углу, чтобы не спорить с итогами денег
    box_style = ParagraphStyle('boxnote', fontName=font_name, fontSize=7,
                               leading=9, alignment=TA_RIGHT,
                               textColor=colors.HexColor("#555555"),
                               spaceBefore=1)

    story = []

    # Слоган над логотипом
    story.append(Paragraph("Здоровье ваших животных — наш приоритет", slogan_style))

    logo = _find_logo()
    if logo:
        try:
            img = Image(logo)
            iw, ih = img.imageWidth, img.imageHeight
            target_w = 45*mm
            img.drawWidth = target_w
            img.drawHeight = target_w * ih / iw
            img.hAlign = "CENTER"
            story.append(img)
        except Exception:
            pass

    title = doc_title or ("НАКЛАДНАЯ (ЧЕРНОВИК)" if show_mark else "НАКЛАДНАЯ")
    if doc_number:
        # Номер операции из журнала: по нему накладную находят в /op и /log
        # и заменяют («замени накладную №45: ...»).
        title += f" № {doc_number}"
    story.append(Paragraph(title, title_style))
    story.append(Paragraph(datetime.now(BISHKEK).strftime("%d.%m.%Y"), sub_style))
    story.append(Paragraph(f"Контрагент: <b>{xml_escape(str(client_name))}</b>", sub_style))
    if warehouse_name:
        story.append(Paragraph(f"Склад: {xml_escape(str(warehouse_name))}", sub_style))
    story.append(Spacer(1, 3*mm))

    header = [
        Paragraph("№", cell_bold),
        Paragraph("Название", cell_bold),
        Paragraph("Фасовка", cell_bold),
        Paragraph("Кол-во", cell_bold),
        Paragraph("Цена", cell_bold),
        Paragraph("Сумма", cell_bold),
    ]
    table_data = [header]
    for i, it in enumerate(items, 1):
        subtotal = it["qty"] * it["price"]
        box_qty = it.get("box_qty")
        qty_text = f"{it['qty']} шт"
        if box_qty:
            qty_text = f"{box_qty} кор<br/>({it['qty']} шт)"
        table_data.append([
            Paragraph(str(i), cell_style),
            Paragraph(xml_escape(str(it["name"])), cell_style),
            Paragraph(xml_escape(str(it["volume"])), cell_style),
            Paragraph(qty_text, cell_style),
            Paragraph(fmt_num(it["price"]), cell_style),
            Paragraph(fmt_num(subtotal), cell_style),
        ])

    col_widths = [8*mm, 48*mm, 18*mm, 18*mm, 16*mm, 18*mm]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_GREEN),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (3, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in range(1, len(table_data)):
        if r % 2 == 0:
            style.append(("BACKGROUND", (0, r), (-1, r), LIGHT_ROW))
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 4*mm))

    story.append(HRFlowable(width="100%", thickness=0.6, color=HEADER_GREEN))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"{total_label or 'Сумма накладной'}: <b>{fmt_num(invoice_total)} сом</b>",
        total_style))

    if extra_totals is not None:
        for label, value in extra_totals:
            story.append(Paragraph(f"{xml_escape(str(label))}: <b>{fmt_num(value)} сом</b>",
                                   total_style))
    elif is_payment:
        total_debt = invoice_total + prev_debt
        remainder = total_debt - payment
        if prev_debt > 0:
            story.append(Paragraph(f"Старый долг: <b>{fmt_num(prev_debt)} сом</b>", total_style))
        elif prev_debt < 0:
            # Переплата клиента зачитывается (вопрос владельца 14.08.2026:
            # «почему не видно, что у него переплата?»)
            story.append(Paragraph(f"Переплата клиента: <b>{fmt_num(-prev_debt)} сом</b> "
                                   f"(зачтена)", total_style))
        story.append(Paragraph(f"Итого долг: <b>{fmt_num(total_debt)} сом</b>", total_style))
        story.append(Paragraph(f"Приход: <b>{fmt_num(payment)} сом</b>", total_style))
        if remainder < 0:
            story.append(Paragraph(f"Долг полностью погашен! Переплата: "
                                   f"<b>{fmt_num(-remainder)} сом</b>", total_style))
        elif remainder == 0:
            story.append(Paragraph("Долг полностью погашен!", total_style))
        else:
            story.append(Paragraph(f"Остаток долга: <b>{fmt_num(remainder)} сом</b>", total_style))
    elif prev_debt > 0:
        grand_total = invoice_total + prev_debt
        story.append(Paragraph(f"Остаток долга: <b>{fmt_num(prev_debt)} сом</b>", total_style))
        story.append(Paragraph(f"Общий итоговый долг: <b>{fmt_num(grand_total)} сом</b>", total_style))
    elif prev_debt < 0:
        after = invoice_total + prev_debt
        story.append(Paragraph(f"Переплата клиента: <b>{fmt_num(-prev_debt)} сом</b> "
                               f"(зачтена)", total_style))
        if after > 0:
            story.append(Paragraph(f"Долг с учётом переплаты: <b>{fmt_num(after)} сом</b>",
                                   total_style))
        elif after < 0:
            story.append(Paragraph(f"Остаток переплаты: <b>{fmt_num(-after)} сом</b>",
                                   total_style))
        else:
            story.append(Paragraph("Переплата полностью зачтена.", total_style))

    if box_note:
        story.append(Paragraph(xml_escape(str(box_note)), box_style))

    story.append(Spacer(1, 2*mm))
    story.append(HRFlowable(width="100%", thickness=0.6, color=HEADER_GREEN))
    story.append(Spacer(1, 2*mm))

    qr_path = _find_qr()
    if qr_path:
        try:
            contact_style = ParagraphStyle('contact', fontName=font_name, fontSize=9,
                                           leading=13, alignment=TA_LEFT)
            qr_img = Image(qr_path)
            qr_size = 20*mm
            qr_img.drawWidth = qr_size
            qr_img.drawHeight = qr_size
            contact_table = Table(
                [[Paragraph("+996 700 99 88 11<br/>+996 700 887 666<br/>Эл. почта: vettop@inbox.ru", contact_style), qr_img]],
                colWidths=[None, 22*mm]
            )
            contact_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]))
            story.append(contact_table)
        except Exception:
            story.append(Paragraph("+996 700 99 88 11  |  +996 700 887 666", sub_style))
            story.append(Paragraph("Эл. почта: vettop@inbox.ru", sub_style))
    else:
        story.append(Paragraph("+996 700 99 88 11  |  +996 700 887 666", sub_style))
        story.append(Paragraph("Эл. почта: vettop@inbox.ru", sub_style))

    if show_mark:
        wm = _watermark(font_bold_name)
        doc.build(story, onFirstPage=wm, onLaterPages=wm)
    else:
        doc.build(story)
    buffer.seek(0)
    return buffer
