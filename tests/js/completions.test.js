// Runs editor.js completion logic against a fake Monaco model. Usage: node completions.test.js <static dir>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const dir = process.argv[2];
const stubEl = () => ({ append() {}, addEventListener() {}, classList: { toggle() {}, add() {}, remove() {} }, dataset: {}, setAttribute() {}, children: [] });
const ctx = {
  console, URL, setTimeout, clearTimeout,
  window: { addEventListener() {}, matchMedia: () => ({ matches: false }) },
  document: { createElement: stubEl, createTextNode: () => ({}), getElementById: stubEl, head: stubEl(), body: stubEl() },
  sessionStorage: { getItem() { return null; } }, localStorage: { getItem() { return null; } },
  location: { hash: "", href: "http://x/" },
};
vm.createContext(ctx);
for (const f of ["app.js", "editor.js"]) vm.runInContext(fs.readFileSync(path.join(dir, f), "utf8"), ctx, { filename: f });

vm.runInContext("App", ctx).schema = {
  views: {
    documents: [{ name: "id", type: "VARCHAR" }, { name: "path", type: "VARCHAR" }, { name: "metadata", type: "JSON" }],
    pages: [{ name: "page", type: "INTEGER" }, { name: "text", type: "VARCHAR" }],
    sales: [{ name: "region", type: "VARCHAR" }],
  },
  builtin_views: ["documents", "pages", "records", "document_versions"],
  metadata_keys: [
    { path: "author", types: ["str"], count: 5, sample: '"Ada"' },
    { path: "user", types: ["dict"], count: 2 },
    { path: "user.owner", types: ["str"], count: 2, sample: '"ops"' },
    { path: "user.tags.level", types: ["int"], count: 1 },
  ],
  record_keys: [{ path: "amount", types: ["int"], count: 3 }],
  api: {
    ctx: [{ name: "sql", kind: "method", signature: "sql(query)", doc: "" }, { name: "params", kind: "property", signature: "params", doc: "" }],
    doc: [{ name: "text", kind: "field", signature: "text: str", doc: "" }, { name: "metadata", kind: "field", signature: "metadata", doc: "" }],
  },
  sql_hints: [],
};

const monaco = { languages: { CompletionItemKind: { Field: 1, Class: 2, Keyword: 3, Snippet: 4, Method: 5, Property: 6, Variable: 7 }, CompletionItemInsertTextRule: { InsertAsSnippet: 4 } } };

function model(textWithCursor) {
  const i = textWithCursor.indexOf("|");
  const text = textWithCursor.slice(0, i) + textWithCursor.slice(i + 1);
  const before = text.slice(0, i);
  const lines = before.split("\n");
  const position = { lineNumber: lines.length, column: lines[lines.length - 1].length + 1 };
  const m = {
    getValue: () => text,
    getValueInRange: () => before,
    getWordUntilPosition: () => { const w = (before.match(/\w*$/) || [""])[0]; return { startColumn: position.column - w.length, endColumn: position.column }; },
  };
  return { m, position, before };
}

function py(src) { const { m, position } = model(src); return JSON.parse(JSON.stringify(ctx.pythonSuggestions(monaco, m, position).map((s) => s.label))); }
function sql(src) { const { m, position, before } = model(src); return JSON.parse(JSON.stringify(ctx.sqlSuggestions(monaco, before, m.getValue(), ctx.wordRange(m, position)).map((s) => s.label))); }

// Python: ctx members, doc members (loop var + transform param), metadata keys (nested), SQL in strings, table names
assert.deepStrictEqual(py("def run(ctx):\n    ctx.|"), ["sql", "params"]);
assert.deepStrictEqual(py("def run(ctx):\n    for d in ctx.documents():\n        d.|"), ["text", "metadata"]);
assert.deepStrictEqual(py("def transform(item, ctx):\n    item.|"), ["text", "metadata"]);
assert.deepStrictEqual(py("def transform(doc, ctx):\n    doc.metadata[\"|").sort(), ["author", "user"]);
assert.deepStrictEqual(py("def transform(doc, ctx):\n    doc.metadata[\"user\"][\"|"), ["owner"]);
assert.deepStrictEqual(py("def transform(doc, ctx):\n    doc.metadata.get(\"user\", {}).get(\"|"), ["owner"]);
assert.ok(py('def run(ctx):\n    ctx.sql("SELECT * FROM |').includes("pages"));
assert.ok(py('def run(ctx):\n    ctx.sql("SELECT metadata->>\'|').includes("author"));
assert.deepStrictEqual(py('def run(ctx):\n    ctx.read_table("|'), ["sales"]);
assert.deepStrictEqual(py("def run(ctx):\n    foo.|"), []);

// SQL: views after FROM, alias columns, metadata json paths, record keys
assert.deepStrictEqual(sql("SELECT * FROM |"), ["documents", "pages", "sales"]);
assert.deepStrictEqual(sql("SELECT d.| FROM documents d"), ["id", "path", "metadata"]);
assert.deepStrictEqual(sql("SELECT metadata->'user'->>'|"), ["owner"]);
assert.deepStrictEqual(sql("SELECT metadata->'user'->'tags'->>'|"), ["level"]);
assert.deepStrictEqual(sql("SELECT data->>'| FROM records"), ["amount"]);
assert.ok(sql("SELECT | FROM pages").includes("text"));
console.log("completions ok");
