/* Monaco editor loader + schema-aware autocomplete for pipeline Python and lakehouse SQL. */
"use strict";

const MONACO_LOCAL = "/static/vendor/monaco/vs";
const MONACO_CDN = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs";

let monacoPromise = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = reject;
    document.head.append(s);
  });
}

function loadMonaco() {
  if (monacoPromise) return monacoPromise;
  monacoPromise = (async () => {
    let base = MONACO_LOCAL;
    try { await loadScript(`${base}/loader.js`); }
    catch (_) { base = MONACO_CDN; await loadScript(`${base}/loader.js`); }
    window.require.config({ paths: { vs: base } });
    const vsAbs = new URL(base + "/", location.href).href;
    window.MonacoEnvironment = {
      getWorkerUrl: () => `data:text/javascript;charset=utf-8,${encodeURIComponent(
        `self.MonacoEnvironment={baseUrl:'${new URL("..", vsAbs).href}'};importScripts('${vsAbs}base/worker/workerMain.js');`)}`,
    };
    await new Promise((resolve) => window.require(["vs/editor/editor.main"], resolve));
    registerCompletions(window.monaco);
    return window.monaco;
  })().catch((e) => { console.warn("Monaco unavailable, using textarea", e); return null; });
  return monacoPromise;
}

/* ---------------------------------------------------------------- schema helpers */

const SQL_KEYWORDS = ("SELECT FROM WHERE GROUP BY ORDER HAVING LIMIT OFFSET JOIN LEFT RIGHT INNER FULL OUTER ON AS AND OR NOT IN IS NULL " +
  "DISTINCT COUNT SUM AVG MIN MAX CASE WHEN THEN ELSE END CAST WITH UNION ALL EXCEPT INTERSECT LIKE ILIKE BETWEEN DESC ASC QUALIFY " +
  "OVER PARTITION UNNEST STRUCT LIST JSON VARCHAR INTEGER BIGINT DOUBLE BOOLEAN DATE TIMESTAMP").split(" ");

function schemaViews() { return (App.schema && App.schema.views) || {}; }

function childKeys(keys, prefix) {
  const out = new Map();
  for (const k of keys || []) {
    if (prefix && !k.path.startsWith(prefix + ".")) continue;
    const rest = prefix ? k.path.slice(prefix.length + 1) : k.path;
    if (!rest || rest.includes(".")) continue;
    out.set(rest, k);
  }
  return [...out.entries()];
}

function keyDetail(k) {
  return `${k.types.join(" | ")} · in ${k.count} doc(s)` + (k.sample ? ` · e.g. ${k.sample}` : "");
}

function wordRange(model, position) {
  const w = model.getWordUntilPosition(position);
  return { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: w.startColumn, endColumn: w.endColumn };
}

function textBefore(model, position) {
  return model.getValueInRange({ startLineNumber: 1, startColumn: 1, endLineNumber: position.lineNumber, endColumn: position.column });
}

/* ---------------------------------------------------------------- SQL completions */

function sqlAliases(sql) {
  const views = schemaViews();
  const map = {};
  const re = /\b(?:from|join)\s+"?([A-Za-z_][\w]*)"?(?:\s+(?:as\s+)?([A-Za-z_]\w*))?/gi;
  let m;
  while ((m = re.exec(sql))) {
    const t = m[1];
    if (!views[t]) continue;
    map[t] = t;
    if (m[2] && !SQL_KEYWORDS.includes(m[2].toUpperCase())) map[m[2]] = t;
  }
  return map;
}

function sqlSuggestions(monaco, before, fullSql, range) {
  const K = monaco.languages.CompletionItemKind;
  const views = schemaViews();
  const md = (App.schema && App.schema.metadata_keys) || [];
  const rk = (App.schema && App.schema.record_keys) || [];
  const s = [];

  // metadata->'a'->>'b|   (nested JSON paths into document metadata)
  let m = before.match(/\bmetadata((?:\s*->\s*'[^']+')*)\s*->>?\s*'([\w.\- ]*)$/);
  if (m) {
    const prefix = [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]).join(".");
    for (const [name, k] of childKeys(md, prefix))
      s.push({ label: name, kind: K.Field, insertText: name, detail: keyDetail(k), range, sortText: String(1e6 - k.count).padStart(7, "0") });
    return s;
  }
  // data->>'col|   (tabular records)
  m = before.match(/\bdata((?:\s*->\s*'[^']+')*)\s*->>?\s*'([\w.\- ]*)$/);
  if (m) {
    const prefix = [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]).join(".");
    for (const [name, k] of childKeys(rk, prefix)) s.push({ label: name, kind: K.Field, insertText: name, detail: keyDetail(k), range });
    return s;
  }
  // alias.col
  m = before.match(/([A-Za-z_]\w*)\.(\w*)$/);
  if (m) {
    const t = sqlAliases(fullSql)[m[1]] || (views[m[1]] ? m[1] : null);
    if (t) for (const c of views[t]) s.push({ label: c.name, kind: K.Field, insertText: c.name, detail: `${t}.${c.name}: ${c.type}`, range });
    return s;
  }
  // FROM / JOIN <table>
  if (/\b(from|join)\s+"?\w*$/i.test(before)) {
    for (const [v, cols] of Object.entries(views))
      s.push({ label: v, kind: K.Class, insertText: v, detail: `${cols.length} columns`, documentation: cols.map((c) => `${c.name} ${c.type}`).join("\n"), range });
    return s;
  }
  // general: columns of referenced tables, then tables, keywords, hints
  const used = new Set(Object.values(sqlAliases(fullSql)));
  for (const t of used) for (const c of views[t] || [])
    s.push({ label: c.name, kind: K.Field, insertText: c.name, detail: `${t}.${c.name}: ${c.type}`, range, sortText: "0" + c.name });
  for (const v of Object.keys(views)) s.push({ label: v, kind: K.Class, insertText: v, detail: "table/view", range, sortText: "1" + v });
  for (const kw of SQL_KEYWORDS) s.push({ label: kw, kind: K.Keyword, insertText: kw, range, sortText: "3" + kw });
  for (const hint of (App.schema && App.schema.sql_hints) || [])
    s.push({ label: hint.name, kind: K.Snippet, insertText: hint.name, detail: hint.doc, range, sortText: "2" + hint.name });
  return s;
}

/* ---------------------------------------------------------------- Python completions */

function docVariables(code) {
  const vars = new Set(["doc"]);
  for (const m of code.matchAll(/for\s+([A-Za-z_]\w*)\s+in\s+ctx\.documents\(/g)) vars.add(m[1]);
  for (const m of code.matchAll(/([A-Za-z_]\w*)\s*=\s*ctx\.document\(/g)) vars.add(m[1]);
  const t = code.match(/def\s+transform\(\s*([A-Za-z_]\w*)/);
  if (t) vars.add(t[1]);
  return vars;
}

function memberItems(monaco, members, range) {
  const K = monaco.languages.CompletionItemKind;
  return members.map((mb) => ({
    label: mb.name,
    kind: mb.kind === "method" ? K.Method : K.Property,
    insertText: mb.kind === "method" ? `${mb.name}($0)` : mb.name,
    insertTextRules: mb.kind === "method" ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
    detail: mb.signature,
    documentation: mb.doc,
    range,
  }));
}

/** Is the cursor inside a string literal passed to ctx.sql/df/arrow/documents? Returns the SQL text so far. */
function sqlStringContext(before) {
  const m = [...before.matchAll(/ctx\.(sql|df|arrow|documents)\(\s*[rf]?("""|'''|"|')/g)].pop();
  if (!m) return null;
  const start = m.index + m[0].length;
  const tail = before.slice(start);
  if (tail.includes(m[2])) return null;  // string already closed
  return { kind: m[1], sql: m[1] === "documents" ? "SELECT * FROM documents WHERE " + tail : tail };
}

function pythonSuggestions(monaco, model, position) {
  const K = monaco.languages.CompletionItemKind;
  const before = textBefore(model, position);
  const line = before.slice(before.lastIndexOf("\n") + 1);
  const code = model.getValue();
  const range = wordRange(model, position);
  const api = (App.schema && App.schema.api) || { ctx: [], doc: [] };
  const md = (App.schema && App.schema.metadata_keys) || [];

  // SQL inside ctx.sql("...")
  const sqlCtx = sqlStringContext(before);
  if (sqlCtx) return sqlSuggestions(monaco, sqlCtx.sql, sqlCtx.sql, range);

  // doc.metadata["a"]["b|   or   doc.metadata.get("a|
  let m = line.match(/\.metadata((?:\[\s*["'][^"']+["']\s*\]|\.get\(\s*["'][^"']+["'][^)]*\)|\.get\(\s*["'][^"']+["']\s*,\s*\{\}\s*\))*)(?:\[\s*|\.get\(\s*)["']([\w.\- ]*)$/);
  if (m) {
    const prefix = [...m[1].matchAll(/["']([^"']+)["']/g)].map((x) => x[1]).join(".");
    return childKeys(md, prefix).map(([name, k]) => ({
      label: name, kind: K.Field, insertText: name, detail: keyDetail(k), range, sortText: String(1e6 - k.count).padStart(7, "0"),
    }));
  }
  // ctx.read_table("|  ctx.write_table("|
  if (/ctx\.(read_table|write_table)\(\s*["'][\w]*$/.test(line)) {
    const builtin = new Set((App.schema && App.schema.builtin_views) || []);
    return Object.keys(schemaViews()).filter((v) => !builtin.has(v)).map((v) => ({ label: v, kind: K.Class, insertText: v, range }));
  }
  // ctx.<member>
  if (/\bctx\.\w*$/.test(line)) return memberItems(monaco, api.ctx, range);
  // <doc var>.<member>
  m = line.match(/\b([A-Za-z_]\w*)\.\w*$/);
  if (m && docVariables(code).has(m[1])) return memberItems(monaco, api.doc, range);
  if (m) return [];

  // top level: handy snippets + the two context objects
  const snip = monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet;
  return [
    { label: "ctx", kind: K.Variable, insertText: "ctx", detail: "pipeline context", range },
    ...[...docVariables(code)].map((v) => ({ label: v, kind: K.Variable, insertText: v, detail: "Doc", range })),
    { label: "def run(ctx)", kind: K.Snippet, insertTextRules: snip, insertText: "def run(ctx):\n    rows = ctx.sql(\"${1:SELECT * FROM documents}\")\n    return rows\n", range, detail: "manual pipeline entry point" },
    { label: "def transform(doc, ctx)", kind: K.Snippet, insertTextRules: snip, insertText: "def transform(doc, ctx):\n    return {\"${1:key}\": ${2:value}}\n", range, detail: "on_ingest entry point" },
    { label: "for doc in ctx.documents()", kind: K.Snippet, insertTextRules: snip, insertText: "for ${1:doc} in ctx.documents(${2}):\n    ${3:pass}", range },
  ];
}

function memberHover(monaco, model, position) {
  const word = model.getWordAtPosition(position);
  if (!word) return null;
  const lineText = model.getLineContent(position.lineNumber);
  const before = lineText.slice(0, word.startColumn - 1);
  const api = (App.schema && App.schema.api) || { ctx: [], doc: [] };
  let list = null;
  if (/\bctx\.$/.test(before)) list = api.ctx;
  else { const m = before.match(/\b([A-Za-z_]\w*)\.$/); if (m && docVariables(model.getValue()).has(m[1])) list = api.doc; }
  const mb = list && list.find((x) => x.name === word.word);
  if (!mb) return null;
  return { contents: [{ value: "```python\n" + mb.signature + "\n```" }, { value: mb.doc || "" }] };
}

function registerCompletions(monaco) {
  monaco.languages.registerCompletionItemProvider("sql", {
    triggerCharacters: [".", "'", ">", " "],
    provideCompletionItems(model, position) {
      const before = textBefore(model, position);
      return { suggestions: sqlSuggestions(monaco, before, model.getValue(), wordRange(model, position)) };
    },
  });
  monaco.languages.registerCompletionItemProvider("python", {
    triggerCharacters: [".", "\"", "'", "[", "(", " "],
    provideCompletionItems(model, position) { return { suggestions: pythonSuggestions(monaco, model, position) }; },
  });
  monaco.languages.registerHoverProvider("python", { provideHover: (model, position) => memberHover(monaco, model, position) });
}

/* ---------------------------------------------------------------- editor factory */

async function createEditor(container, { language, value = "", onChange, onRun, readOnly = false } = {}) {
  clear(container);
  const monaco = await loadMonaco();
  if (!monaco) {
    const ta = h("textarea", { class: "fallback", spellcheck: "false" });
    ta.value = value; ta.readOnly = readOnly;
    container.append(ta);
    if (onChange) ta.addEventListener("input", () => onChange(ta.value));
    if (onRun) ta.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") onRun(); });
    return { getValue: () => ta.value, setValue: (v) => { ta.value = v; }, setLanguage() {}, setMarkers() {}, focus: () => ta.focus(), dispose() {} };
  }
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const ed = monaco.editor.create(container, {
    value, language, readOnly, automaticLayout: true, minimap: { enabled: false }, fontSize: 13, tabSize: 4,
    theme: dark ? "vs-dark" : "vs", scrollBeyondLastLine: false, quickSuggestions: { other: true, strings: true },
    suggestOnTriggerCharacters: true, wordBasedSuggestions: "off",
  });
  if (onChange) ed.onDidChangeModelContent(() => onChange(ed.getValue()));
  if (onRun) ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, onRun);
  return {
    editor: ed,
    getValue: () => ed.getValue(),
    setValue: (v) => ed.setValue(v),
    setLanguage: (lang) => monaco.editor.setModelLanguage(ed.getModel(), lang),
    setMarkers: (problems) => monaco.editor.setModelMarkers(ed.getModel(), "dah", (problems || []).map((p) => ({
      startLineNumber: p.line, endLineNumber: p.line, startColumn: p.column || 1, endColumn: 1000, message: p.message,
      severity: p.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
    }))),
    focus: () => ed.focus(),
    dispose: () => ed.dispose(),
  };
}
