"use strict";

const assert = require("node:assert/strict");
const model = require("../remote_control/static/app.js");

const catalog = model.buildAgentCatalog([
  {name: "qwen", transport: "acp", ready: false},
  {name: "kimi", transport: "acp+jsonl", ready: true},
  {name: "host", transport: "native-model", ready: false},
]);
assert.deepEqual(
  catalog.map((item) => [item.name, item.ready]),
  [["kimi", true], ["qwen", false], ["host", false]],
);

let value = "@";
let context = model.findMentionContext(value, value.length, catalog);
assert.deepEqual(context.items.map((item) => item.name), ["kimi", "qwen", "host"]);
let inserted = model.insertMention(value, context, context.items[0].name);
assert.equal(inserted.value, "@kimi ");

value = `${inserted.value}@`;
context = model.findMentionContext(value, value.length, catalog);
assert.deepEqual(context.items.map((item) => item.name), ["qwen", "host"]);
inserted = model.insertMention(value, context, context.items[0].name);
assert.equal(inserted.value, "@kimi @qwen ");

const completed = model.classifyHistoryPage({
  total_count: 3,
  items: [
    {kind: "running", created_at: "2026-09-02T01:00:00Z"},
    {kind: "partial", text: "正文"},
    {kind: "completed", created_at: "2026-09-02T01:00:02Z"},
  ],
});
assert.equal(completed.status, "completed");
assert.equal(completed.finished_at, "2026-09-02T01:00:02Z");

const interrupted = model.classifyHistoryPage({
  total_count: 2,
  items: [
    {kind: "running", created_at: "2026-09-02T02:00:00Z"},
    {kind: "status", text: "处理中"},
  ],
});
assert.equal(interrupted.status, "interrupted");
assert.equal(interrupted.finished_at, null);

const selected = model.selectVisibleTasks(
  [{command_id: "active", status: "running"}],
  [
    {command_id: "done", status: "completed", activity_at: "2026-09-02T01:00:00Z"},
    {command_id: "cut", status: "interrupted", activity_at: "2026-09-02T02:00:00Z"},
    {command_id: "unknown", status: "unknown", activity_at: "2026-09-02T03:00:00Z"},
  ],
);
assert.deepEqual(
  selected.visible.map((item) => item.command_id),
  ["active", "cut", "done"],
);

const detailEvents = model.visibleDetailEvents({
  items: [
    {kind: "running", text: "开始"},
    {kind: "partial", text: "不要重复正文"},
    {kind: "thought", text: "不要显示思考"},
    ...Array.from({length: 90}, (_, index) => ({
      kind: "status",
      text: `阶段 ${index}`,
    })),
    {kind: "completed", text: "结束"},
  ],
});
assert.equal(detailEvents.length, 80);
assert.equal(detailEvents.some((event) => event.kind === "partial"), false);
assert.equal(detailEvents.some((event) => event.kind === "thought"), false);
assert.equal(detailEvents.at(-1).kind, "completed");

console.log("ok  Web @ 连续点名、真实 readiness、历史终态与安全详情模型");
