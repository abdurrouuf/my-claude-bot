const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType, AlignmentType, HeadingLevel, PageOrientation, ShadingType, BorderStyle } = require("docx");
const rows = JSON.parse(fs.readFileSync("/tmp/label_rows.json", "utf8"));
const STORAGE = "Хранить в сухом, тёмном и недоступном для детей месте при температуре от 0 до 30 °C.";
const P = (t, o = {}) => new Paragraph({ children: [new TextRun({ text: t, bold: o.b, size: o.s || 22, font: "Arial" })], spacing: { after: o.after ?? 100 }, alignment: o.al });
const H = (t) => new Paragraph({ text: t, heading: HeadingLevel.HEADING_2, spacing: { before: 200, after: 100 } });
const cell = (t, w, bold = false, shade = null) => new TableCell({ width: { size: w, type: WidthType.DXA }, shading: shade ? { type: ShadingType.CLEAR, fill: shade, color: "auto" } : undefined,
  children: [new Paragraph({ children: [new TextRun({ text: t, bold, size: 18, font: "Arial" })] })] });
const cols = [500, 2600, 1300, 1700, 1200, 2300, 1500, 2600]; const total = cols.reduce((a, b) => a + b, 0);
const header = new TableRow({ tableHeader: true, children: ["№", "Product name (RU)", "Package size", "Batch", "Expiry", "Quantity per carton", "Registration No.", "Carton QR file"].map((t, i) => cell(t, cols[i], true, "E8F0E8")) });
const body = rows.map(r => new TableRow({ children: [String(r.n), r.name, r.vol, r.lot, r.exp, r.qty, "РУ № " + r.reg, r.qr + ".svg / .pdf"].map((t, i) => cell(t, cols[i])) }));
const doc = new Document({ sections: [{
  properties: { page: { size: { width: 11906, height: 16838, orientation: PageOrientation.LANDSCAPE }, margin: { top: 720, bottom: 720, left: 720, right: 720 } } },
  children: [
    new Paragraph({ text: "Outer carton labels — order TQ20260924C (VETOP, Kyrgyzstan)", heading: HeadingLevel.HEADING_1 }),
    P("Label size: A5 landscape, 210 × 148 mm, white paper, glued on BOTH blank sides of the carton (two labels per carton). The PDF file «carton_labels_TQ20260924C_all22.pdf» contains all 22 labels ready to print (1:1); this document gives the same text in editable form.", { s: 20 }),
    H("Layout of one label (top to bottom)"),
    P("1. VETOP logo, centered (original proportions, green).", { s: 20 }),
    P("2. Product name in Russian — the largest text (e.g. ОКСИЛИН 300 LA).", { s: 20 }),
    P("3. Package size (e.g. 10 мл, 100 мл, 1 л).", { s: 20 }),
    P("4. «Серия:» + batch number.        5. «Годен до:» + expiry (MM.YYYY).", { s: 20 }),
    P("6. Quantity per carton (see table).        7. Carton QR code on the right, minimum 40 × 40 mm (file from QR_TQ20260924C_BOXES, name ends with _K).", { s: 20 }),
    P("8. «РУ № KG …» — registration certificate number (see table).", { s: 20 }),
    P("9. Storage line, same for all products: " + STORAGE, { s: 20 }),
    P("10. Small line at the bottom: «Только для применения в ветеринарии».", { s: 20 }),
    P("No manufacturer / distributor lines on the label — they stay in the printed carton design.", { s: 20, b: true }),
    H("Fixed Russian words (copy as they are)"),
    P("Серия:   |   Годен до:   |   В коробке:   |   уп × 10 фл =   |   шт   |   РУ №   |   Только для применения в ветеринарии", { s: 20 }),
    P(STORAGE, { s: 20 }),
    H("All 22 labels"),
    new Table({ width: { size: total, type: WidthType.DXA }, columnWidths: cols, rows: [header, ...body] }),
    P(""),
    P("Prepared by VETOP LLC, 30.09.2026. Before printing please send us the artwork of each label and a photo of one test print — we will scan the QR and confirm.", { s: 18 }),
  ] }] });
Packer.toBuffer(doc).then(b => fs.writeFileSync("/home/user/my-claude-bot/deliverables/carton_labels_TQ20260924C.docx", b));
