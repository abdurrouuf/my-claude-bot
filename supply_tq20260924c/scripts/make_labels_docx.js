const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, AlignmentType, PageOrientation, PageBreak } = require("docx");
const rows = JSON.parse(fs.readFileSync("/tmp/label_rows.json", "utf8"));
const STORAGE = "Хранить в сухом, тёмном и недоступном для детей месте при температуре от 0 до 30 °C.";
const P = (t, size, bold = true, center = false, after = 120) => new Paragraph({ alignment: center ? AlignmentType.CENTER : AlignmentType.LEFT, spacing: { after },
  children: [new TextRun({ text: t, bold, size, font: "Arial" })] });
const labelParas = (r) => [
  P(r.name, 96, true, true, 0), P(r.vol, 72, true, true, 300),
  P("Серия: " + r.lot, 40), P("Дата производства: " + r.mfg, 40), P("Годен до: " + r.exp, 40), P(r.qty, 28, true, false, 240),
  P("РУ № " + r.reg, 32, true, false, 240), P(STORAGE, 22, false), P("Только для применения в ветеринарии", 16, false),
];
const section = (children) => ({ properties: { page: { size: { width: 8391, height: 11906, orientation: PageOrientation.LANDSCAPE }, margin: { top: 500, bottom: 400, left: 500, right: 500 } } }, children });
const outDir = "/home/user/my-claude-bot/deliverables/carton_labels_docx"; fs.mkdirSync(outDir, { recursive: true });
(async () => {
  for (const r of rows) {
    const doc = new Document({ sections: [section(labelParas(r))] });
    const fn = `${String(r.n).padStart(2, "0")}_${r.qr.replace(/_K$/, "")}_${r.name.replace(/ /g, "_")}_${r.vol.replace(/ /g, "")}.docx`;
    fs.writeFileSync(`${outDir}/${fn}`, await Packer.toBuffer(doc));
  }
  const all = new Document({ sections: rows.map(r => section(labelParas(r))) });
  fs.writeFileSync("/home/user/my-claude-bot/deliverables/carton_labels_TQ20260924C_words_all22.docx", await Packer.toBuffer(all));
  console.log("ok");
})();
