import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const HEADERS_ZH = [
  "投递日期（可选）", "公司名称", "岗位名称", "部门（可选）", "投递渠道（可选）",
  "岗位描述（可选）", "岗位备注（可选）", "优先级（可选）", "当前阶段（可选）",
  "当前环节（可选）", "下一阶段（可选）", "下一环节（可选）",
  "下一环节的日期（可选）", "下一环节的时间（可选）", "下一步说明（可选）",
];
const HEADERS_EN = [
  "Application Date (optional)", "Company", "Role Title", "Department (optional)", "Source (optional)",
  "Job Description (optional)", "Role Notes (optional)", "Priority (optional)", "Current Stage (optional)",
  "Current Step (optional)", "Next Stage (optional)", "Next Step (optional)",
  "Next Step Date (optional)", "Next Step Time (optional)", "Next Step Notes (optional)",
];

const WIDTHS = [14, 18, 22, 18, 16, 34, 28, 16, 16, 18, 22, 16, 22, 22, 28];
const STAGES_ZH = ["待定", "已投递", "笔试中", "面试中", "Offer", "泡池子", "不再跟进", "已挂"];
const STAGES_EN = ["Considering", "Applied", "Assessment", "Interviewing", "Offer", "On Hold", "Withdrawn", "Rejected"];

function styleWorkbook(rows, tableName, { headers = HEADERS_ZH, stages = STAGES_ZH, priorities = ["高", "中", "低"], sheetName = "岗位导入" } = {}) {
  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add(sheetName);
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  sheet.getRange(`A1:O${rows.length + 1}`).values = [headers, ...rows];
  sheet.getRange("A1:O1").format = {
    fill: "#243447",
    font: { bold: true, color: "#FFFFFF", fontSize: 10 },
    borders: { bottom: { style: "medium", color: "#E98A3A" } },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  sheet.getRange("A1:O1").format.rowHeight = 42;
  sheet.getRange(`A2:O${rows.length + 1}`).format = {
    font: { color: "#25313C", fontSize: 10 },
    verticalAlignment: "top",
  };
  sheet.getRange(`A2:A${rows.length + 1}`).format.numberFormat = "yyyy-mm-dd";
  sheet.getRange(`M2:M${rows.length + 1}`).format.numberFormat = "yyyy-mm-dd";
  sheet.getRange(`N2:N${rows.length + 1}`).format.numberFormat = "hh:mm";
  sheet.getRange(`F2:G${rows.length + 1}`).format.wrapText = true;
  WIDTHS.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, rows.length + 1, 1).format.columnWidth = width;
  });
  sheet.getRange(`I2:I201`).dataValidation = { rule: { type: "list", values: stages } };
  sheet.getRange(`H2:H201`).dataValidation = { rule: { type: "list", values: priorities } };
  sheet.getRange(`K2:K201`).dataValidation = { rule: { type: "list", values: stages } };
  const table = sheet.tables.add(`A1:O${rows.length + 1}`, true, tableName);
  table.style = "TableStyleMedium2";
  table.showBandedRows = true;
  table.showFilterButton = true;
  return workbook;
}

const examples = [
  [new Date("2026-07-01T00:00:00"), "示例科技", "产品经理", "增长产品", "内推", "负责用户增长产品规划与数据分析", "重点准备增长实验案例", "高", "面试中", "二面", "面试中", "终面", new Date("2026-07-25T00:00:00"), 14 / 24, "与业务负责人线上沟通"],
  [new Date("2026-07-18T00:00:00"), "远景网络", "后端工程师", "平台研发", "官网", "熟悉 Python、SQL 与云服务", null, "中", "已投递", "简历筛选", "笔试中", "在线测评", null, null, "等待邮件通知"],
  [null, "新锐咨询", "行业研究员", null, "校招", null, null, null, "待定", null, null, null, null, null, null],
  [new Date("2026-06-12T00:00:00"), "云杉数据", "数据工程师", "数据平台", "猎头", "建设实时数据管道", "九月可再次联系", "低", "泡池子", "HR 沟通", "面试中", "重新联系 HR", new Date("2026-09-01T00:00:00"), null, "确认岗位是否重新开放"],
];

const examplesEn = [
  [new Date("2026-07-01T00:00:00"), "Northstar Labs", "Product Manager", "Growth", "Referral", "Own growth-product planning and analytics", "Prepare a strong experimentation case study", "High", "Interviewing", "Second interview", "Interviewing", "Final interview", new Date("2026-07-25T00:00:00"), 14 / 24, "Video call with the business lead"],
  [new Date("2026-07-18T00:00:00"), "Horizon Network", "Backend Engineer", "Platform Engineering", "Company site", "Python, SQL, and cloud-services experience", null, "Medium", "Applied", "Résumé review", "Assessment", "Online assessment", null, null, "Wait for the email"],
  [null, "Newbridge Consulting", "Industry Analyst", null, "Graduate programme", null, null, null, "Considering", null, null, null, null, null, null],
  [new Date("2026-06-12T00:00:00"), "Spruce Data", "Data Engineer", "Data Platform", "Recruiter", "Build real-time data pipelines", "Contact again in September", "Low", "On Hold", "Recruiter call", "Interviewing", "Contact recruiter", new Date("2026-09-01T00:00:00"), null, "Check whether the role has reopened"],
];

const templateZh = styleWorkbook(examples, "CareerDeskImportExampleZh");
const templateEn = styleWorkbook(examplesEn, "CareerDeskImportExampleEn", {
  headers: HEADERS_EN,
  stages: STAGES_EN,
  priorities: ["High", "Medium", "Low"],
  sheetName: "Role Import",
});
const templateZhFile = await SpreadsheetFile.exportXlsx(templateZh);
const templateEnFile = await SpreadsheetFile.exportXlsx(templateEn);
await templateZhFile.save("frontend/public/careerdesk-job-import-example-zh-CN.xlsx");
await templateEnFile.save("frontend/public/careerdesk-job-import-example-en.xlsx");

const inspected = await templateZh.inspect({
  kind: "table",
  range: "A1:O5",
  include: "values,formulas",
  tableMaxRows: 5,
  tableMaxCols: 15,
});
process.stdout.write(`${inspected.ndjson}\n`);
const errors = await templateZh.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 50 },
  summary: "template formula error scan",
});
process.stdout.write(`${errors.ndjson}\n`);
const inspectedEn = await templateEn.inspect({
  kind: "table",
  range: "A1:O5",
  include: "values,formulas",
  tableMaxRows: 5,
  tableMaxCols: 15,
});
process.stdout.write(`${inspectedEn.ndjson}\n`);
const errorsEn = await templateEn.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 50 },
  summary: "English template formula error scan",
});
process.stdout.write(`${errorsEn.ndjson}\n`);
const preview = await templateZh.render({ sheetName: "岗位导入", range: "A1:O5", scale: 1, format: "png" });
await fs.writeFile("/tmp/careerdesk-spreadsheet-qa/import-template.png", new Uint8Array(await preview.arrayBuffer()));
const previewEn = await templateEn.render({ sheetName: "Role Import", range: "A1:O5", scale: 1, format: "png" });
await fs.writeFile("/tmp/careerdesk-spreadsheet-qa/import-template-en.png", new Uint8Array(await previewEn.arrayBuffer()));
