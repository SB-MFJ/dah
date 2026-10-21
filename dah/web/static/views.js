/* Views: documents, query console, pipeline editor, lake tables. */
"use strict";

/* ================================================================ Documents */

registerView("documents", {
  title: "Documents", order: 10,
  async render(root, params) {
    if (params[0]) return renderDocument(root, params[0]);

    const pathIn = h("input", { placeholder: "folder/ (optional prefix or full path)", style: "flex:1" });
    const commentIn = h("input", { placeholder: "version comment (optional)", style: "flex:1" });
    const mdIn = h("input", { placeholder: '{"team": "finance"} (optional metadata)', class: "mono", style: "flex:2" });
    const fileIn = h("input", { type: "file", multiple: true, hidden: true });
    const drop = h("div", { class: "drop" }, "Drop files here or ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); fileIn.click(); }, text: "browse" }),
      h("div", { class: "muted", text: "PDF, Office (doc/docx/xls/xlsx/ppt/pptx/odt/rtf), images (OCR), text, CSV, JSON, HTML, email…" }));

    async function send(files) {
      if (!files.length) return;
      const fd = new FormData();
      for (const f of files) fd.append("files", f, f.name);
      if (pathIn.value.trim()) fd.append("path", pathIn.value.trim());
      if (commentIn.value.trim()) fd.append("comment", commentIn.value.trim());
      if (mdIn.value.trim()) fd.append("metadata", mdIn.value.trim());
      const r = await guarded(() => api("/api/documents", { method: "POST", form: fd }));
      const n = r.items.filter((i) => !i.unchanged).length;
      toast(`${n} new version(s) queued, ${r.items.length - n} unchanged`);
      setTimeout(load, 600);
    }
    fileIn.addEventListener("change", () => send([...fileIn.files]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); send([...e.dataTransfer.files]); });

    const q = h("input", { type: "search", placeholder: "Filter by path…", style: "flex:1" });
    const ft = h("input", { type: "search", placeholder: "Full-text search…", style: "flex:1" });
    const listBox = h("div", { class: "scroll" });
    const hits = h("div");

    async function load() {
      const data = await api(`/api/documents?limit=500${q.value ? `&q=${encodeURIComponent(q.value)}` : ""}`);
      clear(listBox).append(
        h("p", { class: "muted", text: `${data.total} document(s)` }),
        dataTable(
          [{ key: "path", label: "Path" }, { key: "mime", label: "Type" }, { key: "version", label: "Version", num: true },
            { key: "status", label: "Status" }, { key: "size", label: "Size", num: true }, { key: "updated_at", label: "Updated" }],
          data.items.map((d) => ({
            id: d.id, path: d.path, mime: d.current ? d.current.mime : "", status: d.latest_status,
            version: d.current_version === d.latest_version ? `v${d.current_version}` : `v${d.current_version} (latest v${d.latest_version})`,
            size: d.current ? d.current.size : null, updated_at: d.updated_at,
          })),
          { onRow: (r) => go("documents", r.id), render: { status: (v) => badge(v), size: (v) => fmtBytes(v), updated_at: (v) => fmtDate(v) } },
        ),
      );
    }
    async function search() {
      clear(hits);
      if (!ft.value.trim()) return;
      const r = await guarded(() => api(`/api/documents/search?q=${encodeURIComponent(ft.value.trim())}`));
      hits.append(h("h3", { text: `${r.rows.length} matching page(s)` }),
        dataTable([{ key: "path", label: "Path" }, { key: "page", label: "Page", num: true }, { key: "snippet", label: "Snippet" }], r.rows,
          { onRow: (row) => go("documents", row.document_id) }));
    }
    q.addEventListener("input", () => { clearTimeout(q._t); q._t = setTimeout(load, 250); });
    ft.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });

    root.append(h("div", { class: "stack" },
      h("section", { class: "panel stack" }, h("h2", { text: "Ingest" }), h("div", { class: "row" }, pathIn, commentIn, mdIn), drop, fileIn),
      h("section", { class: "panel stack" },
        h("div", { class: "row" }, h("h2", { text: "Documents" }), h("span", { class: "spacer" }), q, ft, h("button", { onclick: search, text: "Search text" }), h("button", { onclick: load, text: "Refresh" })),
        hits, listBox),
    ));
    await load();
  },
});

/** Extension points so later features can add panels to the document page. */
const DocumentPanels = [];

async function renderDocument(root, id) {
  const d = await api(`/api/documents/${id}?text=true`);
  const cur = d.current;
  const tabs = h("div", { class: "tabs" });
  const body = h("div");
  const panels = {
    Text: () => h("pre", { text: cur ? (cur.text || "(no text extracted)") : "(not processed yet)" }),
    Metadata: () => h("pre", { text: JSON.stringify(cur ? cur.metadata : {}, null, 2) }),
    Pages: async () => {
      const p = await api(`/api/documents/${id}/pages`);
      return h("div", { class: "stack" }, p.pages.map((pg) => h("div", {},
        h("h3", {}, `Page ${pg.page} `, pg.ocr ? badge("ocr") : null), h("pre", { text: pg.text || "" }))));
    },
  };
  for (const p of DocumentPanels) panels[p.title] = () => p.render(d, () => renderDocument(clear(root), id));
  async function show(name) {
    [...tabs.children].forEach((b) => b.classList.toggle("active", b.textContent === name));
    clear(body).append(h("p", { class: "muted", text: "Loading…" }));
    const node = await panels[name]();
    clear(body).append(node);
  }
  Object.keys(panels).forEach((name) => tabs.append(h("button", { text: name, onclick: () => show(name) })));

  async function act(fn, msg) { await guarded(fn, msg); renderDocument(clear(root), id); }
  root.append(h("div", { class: "stack" },
    h("div", { class: "row" }, h("a", { href: "#documents", text: "← Documents" })),
    h("section", { class: "panel stack" },
      h("div", { class: "row" },
        h("h2", { text: d.path }), cur ? badge(cur.status) : null, h("span", { class: "muted", text: `v${d.current_version} of ${d.latest_version}` }),
        h("span", { class: "spacer" }),
        h("button", { text: "Download", onclick: () => downloadDocument(id, null, cur && cur.filename) }),
        h("button", { text: "Reprocess", onclick: () => act(() => api(`/api/documents/${id}/reprocess`, { method: "POST" }), "Reprocessing") }),
        h("button", { class: "danger", text: "Delete…", onclick: () => confirmDelete(d, root) })),
      cur ? h("dl", { class: "kv" },
        h("dt", { text: "Type" }), h("dd", { text: cur.mime }),
        h("dt", { text: "Size" }), h("dd", { text: fmtBytes(cur.size) }),
        h("dt", { text: "SHA-256" }), h("dd", { class: "mono", text: cur.sha256 }),
        h("dt", { text: "Pages" }), h("dd", { text: String(cur.page_count) }),
        h("dt", { text: "Extraction" }), h("dd", { text: (cur.metadata && cur.metadata.extraction_method) || "" }),
        cur.error ? [h("dt", { text: "Error" }), h("dd", { class: "error", text: cur.error })] : null,
      ) : h("p", { class: "muted", text: "Processing…" })),
    h("section", { class: "panel" }, tabs, body),
  ));
  show("Text");
}

async function downloadDocument(id, version, filename) {
  const res = await guarded(() => api(`/api/documents/${id}/download${version ? `?version=${version}` : ""}`, { raw: true }));
  const url = URL.createObjectURL(await res.blob());
  const a = h("a", { href: url, download: filename || "download" });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function confirmDelete(d, root) {
  const input = h("input", { placeholder: d.path });
  const dlg = h("dialog", {}, h("form", { method: "dialog" },
    h("h2", { text: "Delete document?" }),
    h("p", { text: `This removes all ${d.version_count} version(s) of ${d.path} from the catalog. Type the path to confirm.` }),
    input,
    h("menu", {}, h("button", { value: "cancel", text: "Cancel" }), h("button", { class: "danger", value: "ok", text: "Delete" }))));
  document.body.append(dlg);
  dlg.addEventListener("close", async () => {
    if (dlg.returnValue === "ok" && input.value === d.path) {
      await guarded(() => api(`/api/documents/${d.id}`, { method: "DELETE" }), "Deleted");
      go("documents");
    } else if (dlg.returnValue === "ok") toast("Path did not match; nothing deleted", true);
    dlg.remove();
  });
  dlg.showModal();
}

/* ================================================================ Query console */

registerView("query", {
  title: "Query", order: 20,
  async render(root) {
    await refreshSchema();
    const edBox = h("div", { class: "editor" });
    const result = h("div", { class: "scroll" });
    const status = h("span", { class: "muted" });
    const saved = sessionStorage.getItem("dah.query") ||
      "-- Ctrl/Cmd+Enter to run. Try typing metadata->>' for metadata keys.\nSELECT path, mime, metadata->>'author' AS author, page_count\nFROM documents\nORDER BY path\nLIMIT 100";
    let ed;
    async function run() {
      const sql = ed.getValue();
      sessionStorage.setItem("dah.query", sql);
      status.textContent = "Running…";
      const t0 = performance.now();
      try {
        const r = await api("/api/query", { method: "POST", body: { sql, limit: 5000 } });
        status.textContent = `${r.rows.length}${r.truncated ? "+" : ""} row(s) in ${Math.round(performance.now() - t0)} ms`;
        clear(result).append(dataTable(r.columns, r.rows));
      } catch (e) { status.textContent = ""; clear(result).append(h("pre", { class: "error", text: e.message })); }
    }
    const side = h("div", { class: "panel stack" }, h("h3", { text: "Tables & views" }), schemaTree((name) => ed && ed.editor && ed.editor.trigger("kb", "type", { text: name })));
    root.append(h("div", { class: "split" }, side,
      h("div", { class: "stack" },
        h("section", { class: "panel stack" }, edBox, h("div", { class: "row" }, h("button", { class: "primary", text: "Run", onclick: run }), status)),
        h("section", { class: "panel" }, result))));
    ed = await createEditor(edBox, { language: "sql", value: saved, onRun: run });
  },
});

function schemaTree(onPick) {
  const views = (App.schema && App.schema.views) || {};
  const ul = h("ul", { class: "list" });
  for (const [name, cols] of Object.entries(views)) {
    const li = h("li", {}, h("div", { class: "mono", text: name, onclick: () => onPick(name) }),
      h("div", { class: "sub" }, cols.map((c, i) => [i ? ", " : "", h("span", { title: c.type, text: c.name, style: "cursor:pointer", onclick: (e) => { e.stopPropagation(); onPick(c.name); } })])));
    ul.append(li);
  }
  const keys = (App.schema && App.schema.metadata_keys) || [];
  return h("div", {}, ul,
    h("h3", { text: "Metadata keys", style: "margin-top:12px" }),
    h("ul", { class: "list" }, keys.slice(0, 80).map((k) => h("li", { title: `${k.types.join("|")} ${k.sample || ""}`, onclick: () => onPick(k.path.includes(".") ? `metadata->${k.path.split(".").slice(0, -1).map((p) => `'${p}'`).join("->")}->>'${k.path.split(".").pop()}'` : `metadata->>'${k.path}'`) },
      h("span", { class: "mono", text: k.path }), h("span", { class: "sub", text: ` ${k.count}` })))));
}

/* ================================================================ Pipelines */

/** Extension points for later features (version history, AI assist). */
const PipelinePanels = [];

registerView("pipelines", {
  title: "Pipelines", order: 30,
  async render(root, params) {
    await refreshSchema();
    const [list, templates] = await Promise.all([api("/api/pipelines"), api("/api/pipelines/templates")]);
    const sel = params[0];
    const ul = h("ul", { class: "list" }, list.items.map((p) => h("li", { class: p.name === sel ? "active" : "", onclick: () => go("pipelines", p.name) },
      h("div", { text: p.name }),
      h("div", { class: "sub", text: `${p.current.kind} · ${p.current.trigger}${p.current.output_table ? " → " + p.current.output_table : ""} · v${p.current_version}${p.enabled ? "" : " · disabled"}` }))));
    const side = h("div", { class: "panel stack" }, h("div", { class: "row" }, h("h3", { text: "Pipelines" }), h("span", { class: "spacer" }),
      h("button", { class: "primary", text: "New", onclick: () => go("pipelines", "_new") })), ul);
    const main = h("div", { class: "stack" });
    root.append(h("div", { class: "split" }, side, main));
    if (sel) await pipelineEditor(main, sel === "_new" ? null : sel, templates);
    else main.append(h("section", { class: "panel" }, h("p", { class: "muted", text: "Select a pipeline or create a new one." })));
  },
});

async function pipelineEditor(main, name, templates) {
  const p = name ? await api(`/api/pipelines/${encodeURIComponent(name)}`) : null;
  const cur = p ? p.current : { ...templates.python_run, output_table: "", description: "", params: {} };
  const nameIn = h("input", { value: p ? p.name : "", placeholder: "pipeline-name", disabled: !!p });
  const kindIn = h("select", {}, ["python", "sql"].map((k) => h("option", { value: k, text: k, selected: cur.kind === k })));
  const trigIn = h("select", {}, ["manual", "on_ingest"].map((k) => h("option", { value: k, text: k, selected: cur.trigger === k })));
  const outIn = h("input", { value: cur.output_table || "", placeholder: "output table (optional for python)" });
  const descIn = h("input", { value: cur.description || "", placeholder: "description" });
  const paramsIn = h("input", { class: "mono", value: JSON.stringify(cur.params || {}), placeholder: "{}" });
  const commentIn = h("input", { placeholder: "version comment", style: "flex:1" });
  const enabledIn = h("input", { type: "checkbox", checked: p ? p.enabled : true });
  const edBox = h("div", { class: "editor" });
  const output = h("div", { class: "stack" });

  const ed = await createEditor(edBox, {
    language: cur.kind, value: cur.code,
    onChange: () => { clearTimeout(ed._t); ed._t = setTimeout(validate, 500); },
    onRun: () => save().then(run),
  });
  async function validate() {
    try { const r = await api("/api/pipelines/validate", { method: "POST", body: { kind: kindIn.value, code: ed.getValue() } }); ed.setMarkers(r.problems); } catch (_) {}
  }
  kindIn.addEventListener("change", () => {
    ed.setLanguage(kindIn.value);
    if (!p && ed.getValue() === cur.code) ed.setValue((kindIn.value === "sql" ? templates.sql : templates.python_run).code);
    if (kindIn.value === "sql") trigIn.value = "manual";
    validate();
  });
  trigIn.addEventListener("change", () => {
    if (!p && trigIn.value === "on_ingest" && kindIn.value === "python") ed.setValue(templates.python_transform.code);
  });

  function body() {
    let params = {};
    try { params = JSON.parse(paramsIn.value || "{}"); } catch (_) { throw new Error("params must be JSON"); }
    return { kind: kindIn.value, trigger: trigIn.value, output_table: outIn.value.trim() || null, description: descIn.value, params, code: ed.getValue(), comment: commentIn.value || null };
  }
  async function save() {
    if (!p) {
      const created = await guarded(() => api("/api/pipelines", { method: "POST", body: { name: nameIn.value.trim(), ...body() } }), "Created");
      go("pipelines", created.name);
      return created;
    }
    const r = await guarded(() => api(`/api/pipelines/${encodeURIComponent(p.name)}`, { method: "PUT", body: { ...body(), enabled: enabledIn.checked } }));
    toast(r.current_version !== p.current_version ? `Saved as v${r.current_version}` : "No changes");
    commentIn.value = "";
    if (r.current_version !== p.current_version) route();
    return r;
  }
  async function run() {
    if (!p) return;
    clear(output).append(h("p", { class: "muted", text: "Running…" }));
    const r = await guarded(() => api(`/api/pipelines/${encodeURIComponent(p.name)}/${trigIn.value === "on_ingest" ? "backfill" : "run"}`, { method: "POST", body: {} }));
    showRun(r);
    refreshSchema();
  }
  function showRun(r) {
    clear(output).append(
      h("div", { class: "row" }, h("h3", { text: `Run #${r.id}` }), badge(r.status), h("span", { class: "muted", text: fmtDate(r.finished_at) })),
      r.error ? h("pre", { class: "error", text: r.error }) : null,
      Object.keys(r.outputs || {}).length ? h("pre", { text: JSON.stringify(r.outputs, null, 2) }) : null,
      r.log ? h("pre", { text: r.log }) : null);
  }
  async function runs() {
    const r = await api(`/api/pipelines/${encodeURIComponent(p.name)}/runs?limit=20`);
    clear(output).append(h("h3", { text: "Recent runs" }), dataTable(
      [{ key: "id", label: "#" }, { key: "pipeline_version", label: "Version" }, { key: "status", label: "Status" }, { key: "started_at", label: "Started" }, { key: "error", label: "Error" }],
      r.items, { onRow: showRun, render: { status: (v) => badge(v), started_at: (v) => fmtDate(v) } }));
  }
  async function del() {
    const dlg = h("dialog", {}, h("form", { method: "dialog" }, h("h2", { text: `Delete ${p.name}?` }),
      h("p", { text: `All ${p.version_count} versions and run history are removed. Output tables are kept.` }),
      h("menu", {}, h("button", { value: "cancel", text: "Cancel" }), h("button", { class: "danger", value: "ok", text: "Delete" }))));
    document.body.append(dlg);
    dlg.addEventListener("close", async () => { if (dlg.returnValue === "ok") { await guarded(() => api(`/api/pipelines/${encodeURIComponent(p.name)}`, { method: "DELETE" }), "Deleted"); go("pipelines"); } dlg.remove(); });
    dlg.showModal();
  }

  const extra = h("div", { class: "stack" });
  main.append(
    h("section", { class: "panel stack" },
      h("div", { class: "row" }, h("h2", { text: p ? p.name : "New pipeline" }), p ? h("span", { class: "muted", text: `v${p.current_version}` }) : null, h("span", { class: "spacer" }),
        p ? h("label", { class: "inline" }, enabledIn, "enabled") : null),
      h("div", { class: "grid-form" },
        h("label", {}, "Name", nameIn), h("label", {}, "Kind", kindIn), h("label", {}, "Trigger", trigIn),
        h("label", {}, "Output table", outIn), h("label", {}, "Description", descIn), h("label", {}, "Default params (JSON)", paramsIn)),
      h("p", { class: "muted", text: "Autocomplete: ctx. · doc. · doc.metadata[\" · ctx.sql(\"…\") SQL · metadata->>' — Ctrl/Cmd+Enter saves & runs." }),
      edBox,
      h("div", { class: "row" }, commentIn,
        h("button", { class: "primary", text: p ? "Save version" : "Create", onclick: save }),
        p ? h("button", { text: trigIn.value === "on_ingest" ? "Backfill all documents" : "Run", onclick: run }) : null,
        p ? h("button", { text: "Runs", onclick: runs }) : null,
        p ? h("button", { class: "danger", text: "Delete…", onclick: del }) : null)),
    h("section", { class: "panel" }, output),
    extra,
  );
  for (const panel of PipelinePanels) if (p || panel.forNew) extra.append(await panel.render({ pipeline: p, editor: ed, form: body, reload: () => route() }));
  validate();
}

/* ================================================================ Lake tables */

const TablePanels = [];

registerView("tables", {
  title: "Tables", order: 40,
  async render(root, params) {
    const data = await api("/api/tables");
    const sel = params[0];
    const side = h("div", { class: "panel stack" }, h("h3", { text: "Lake tables" }),
      data.items.length ? h("ul", { class: "list" }, data.items.map((t) => h("li", { class: t.name === sel ? "active" : "", onclick: () => go("tables", t.name) },
        h("div", { class: "mono", text: t.name }), h("div", { class: "sub", text: `${t.rows} rows · v${t.current_version} · ${t.version_count} snapshot(s)` }))))
        : h("p", { class: "muted", text: "Run a pipeline to create tables." }));
    const main = h("div", { class: "stack" });
    root.append(h("div", { class: "split" }, side, main));
    const t = data.items.find((x) => x.name === sel);
    if (!t) return;
    const r = await api("/api/query", { method: "POST", body: { sql: `SELECT * FROM "${t.name}" LIMIT 200` } });
    main.append(h("section", { class: "panel stack" },
      h("div", { class: "row" }, h("h2", { text: t.name }), h("span", { class: "muted", text: `${t.rows} rows · version ${t.current_version}` })),
      h("div", { class: "row" }, t.schema.map((c) => h("span", { class: "badge", text: `${c.name}: ${c.type}` }))),
      h("div", { class: "scroll" }, dataTable(r.columns, r.rows))));
    for (const panel of TablePanels) main.append(await panel.render(t, () => route()));
  },
});

/* ================================================================ Versioning panels */

function restoreDialog(title, text, onConfirm) {
  const comment = h("input", { placeholder: "comment (optional)" });
  const dlg = h("dialog", {}, h("form", { method: "dialog" }, h("h2", { text: title }), h("p", { text }), comment,
    h("menu", {}, h("button", { value: "cancel", text: "Cancel" }), h("button", { class: "primary", value: "ok", text: "Restore" }))));
  document.body.append(dlg);
  dlg.addEventListener("close", async () => { if (dlg.returnValue === "ok") await onConfirm(comment.value || null); dlg.remove(); });
  dlg.showModal();
}

DocumentPanels.push({
  title: "Versions",
  async render(d, reload) {
    const r = await api(`/api/documents/${d.id}/versions`);
    const detail = h("div", { class: "stack" });
    async function compare(v) {
      const df = await api(`/api/documents/${d.id}/diff?a=${v.version}&b=${r.current_version}`);
      const md = df.metadata_diff;
      clear(detail).append(
        h("h3", { text: `v${v.version} → current v${r.current_version}${df.same_content ? " (identical bytes)" : ""}` }),
        df.text_diff ? h("pre", { text: df.text_diff }) : h("p", { class: "muted", text: "Text is identical." }),
        h("h3", { text: "Metadata changes" }),
        h("pre", { text: JSON.stringify(md, null, 2) }));
    }
    async function view(v) {
      const full = await api(`/api/documents/${d.id}/versions/${v.version}`);
      clear(detail).append(h("h3", { text: `v${v.version} text` }), h("pre", { text: full.text || "(no text)" }));
    }
    const actions = (v) => h("div", { class: "row" },
      h("button", { text: "View", onclick: (e) => { e.stopPropagation(); view(v); } }),
      h("button", { text: "Diff", disabled: v.is_current, onclick: (e) => { e.stopPropagation(); compare(v); } }),
      h("button", { text: "Download", onclick: (e) => { e.stopPropagation(); downloadDocument(d.id, v.version, v.filename); } }),
      h("button", { class: "primary", text: "Restore", disabled: v.is_current, onclick: (e) => {
        e.stopPropagation();
        restoreDialog(`Restore v${v.version}?`, `Creates v${d.latest_version + 1} as a copy of v${v.version} and makes it current. Nothing is deleted.`,
          async (comment) => { await guarded(() => api(`/api/documents/${d.id}/versions/${v.version}/restore`, { method: "POST", body: { comment } }), `Restored v${v.version}`); reload(); });
      } }));
    return h("div", { class: "stack" },
      dataTable([{ key: "version", label: "Version" }, { key: "status", label: "Status" }, { key: "filename", label: "File" }, { key: "size", label: "Size", num: true },
        { key: "comment", label: "Comment" }, { key: "created_at", label: "Created" }, { key: "actions", label: "" }],
        r.items.map((v) => ({ ...v, actions: actions(v) })),
        { render: {
          version: (x, v) => h("span", {}, `v${x} `, v.is_current ? badge("current") : null, v.restored_from ? h("span", { class: "muted", text: `(from v${v.restored_from})` }) : null),
          status: (x) => badge(x), size: (x) => fmtBytes(x), created_at: (x) => fmtDate(x), actions: (x) => x } }),
      detail);
  },
});

PipelinePanels.push({
  async render({ pipeline: p, editor, reload }) {
    const r = await api(`/api/pipelines/${encodeURIComponent(p.name)}/versions`);
    const detail = h("div", { class: "stack" });
    function show(v) {
      clear(detail).append(
        h("div", { class: "row" }, h("h3", { text: `v${v.version} vs editor` }), h("span", { class: "spacer" }),
          h("button", { text: "Load into editor (unsaved)", onclick: () => { editor.setValue(v.code); toast(`Loaded v${v.version}; save to make it a new version`); } }),
          h("button", { class: "primary", text: "Restore as current", disabled: v.is_current, onclick: () =>
            restoreDialog(`Restore v${v.version}?`, `Creates a new version identical to v${v.version} (code, kind, trigger, output table, params).`,
              async (comment) => { await guarded(() => api(`/api/pipelines/${encodeURIComponent(p.name)}/versions/${v.version}/restore`, { method: "POST", body: { comment } }), `Restored v${v.version}`); reload(); }) })),
        h("p", { class: "muted", text: `${v.kind} · ${v.trigger}${v.output_table ? " → " + v.output_table : ""} · ${fmtDate(v.created_at)}${v.comment ? " · " + v.comment : ""}` }),
        diffView(editor.getValue(), v.code));
    }
    return h("section", { class: "panel stack" }, h("h3", { text: `Version history (${r.items.length})` }),
      dataTable([{ key: "version", label: "Version" }, { key: "comment", label: "Comment" }, { key: "created_by", label: "By" }, { key: "created_at", label: "Saved" }],
        r.items, { onRow: show, render: {
          version: (x, v) => h("span", {}, `v${x} `, v.is_current ? badge("current") : null, v.restored_from ? h("span", { class: "muted", text: `(from v${v.restored_from})` }) : null),
          created_at: (x) => fmtDate(x) } }),
      detail);
  },
});

TablePanels.push({
  async render(t, reload) {
    const r = await api(`/api/tables/${encodeURIComponent(t.name)}/versions`);
    const preview = h("div", { class: "scroll" });
    async function show(v) {
      const q = await api("/api/query", { method: "POST", body: { sql: `SELECT * FROM "${t.name}" LIMIT 200`, table_versions: { [t.name]: v.version } } });
      clear(preview).append(h("h3", { text: `Snapshot v${v.version}` }), dataTable(q.columns, q.rows));
    }
    return h("section", { class: "panel stack" }, h("h3", { text: "Snapshots (time travel)" }),
      dataTable([{ key: "version", label: "Version" }, { key: "rows", label: "Rows", num: true }, { key: "pipeline_run_id", label: "Run" }, { key: "created_at", label: "Created" }, { key: "act", label: "" }],
        r.items.map((v) => ({ ...v, act: v })), { onRow: show, render: {
          version: (x, v) => h("span", {}, `v${x} `, v.is_current ? badge("current") : null, v.restored_from ? h("span", { class: "muted", text: `(from v${v.restored_from})` }) : null),
          created_at: (x) => fmtDate(x),
          act: (v) => h("button", { text: "Restore", disabled: v.is_current, onclick: (e) => { e.stopPropagation();
            restoreDialog(`Restore ${t.name} v${v.version}?`, "Creates a new snapshot copying this one and makes it current.",
              async () => { await guarded(() => api(`/api/tables/${encodeURIComponent(t.name)}/versions/${v.version}/restore`, { method: "POST" }), "Restored"); reload(); }); } }) } }),
      preview);
  },
});

/* ================================================================ ML assistant */

PipelinePanels.unshift({
  forNew: true,
  async render({ pipeline: p, editor, form }) {
    const instr = h("textarea", { rows: 3, placeholder: "e.g. only include PDFs with more than 5 pages and add the author's email domain as a column" });
    const out = h("div", { class: "stack" });
    const btn = h("button", { class: "primary", text: "Propose change" });
    btn.addEventListener("click", async () => {
      if (!instr.value.trim()) return;
      btn.disabled = true; btn.textContent = "Thinking…";
      clear(out);
      try {
        const f = form();
        const r = await api("/api/assist/pipeline", { method: "POST", body: { kind: f.kind, code: editor.getValue(), instruction: instr.value, pipeline: p ? p.name : null } });
        const errs = r.problems.filter((x) => x.severity === "error");
        out.append(
          h("div", { class: "row" }, h("h3", { text: "Proposed change" }), h("span", { class: "muted", text: r.usage && r.usage.model ? r.usage.model : "" }), h("span", { class: "spacer" }),
            errs.length ? h("span", { class: "error", text: `${errs.length} validation error(s)` }) : badge("valid"),
            h("button", { class: "primary", text: "Apply to editor", onclick: () => { editor.setValue(r.code); toast("Applied to editor. Review, then Save version."); } })),
          r.explanation ? h("pre", { text: r.explanation }) : null,
          diffView(editor.getValue(), r.code));
      } catch (e) {
        out.append(h("p", { class: "error", text: e.message }), e.status === 400 ? h("a", { href: "#settings", text: "Open ML settings" }) : null);
      } finally { btn.disabled = false; btn.textContent = "Propose change"; }
    });
    return h("section", { class: "panel stack" }, h("h3", { text: "Ask AI to edit this pipeline" }),
      h("p", { class: "muted", text: "Uses the ML provider configured in Settings. The proposal is shown as a diff and is never saved automatically." }),
      instr, h("div", { class: "row" }, btn), out);
  },
});

registerView("settings", {
  title: "Settings", order: 70,
  async render(root) {
    const s = await api("/api/settings/ml");
    const provider = h("select", {}, [["anthropic", "Anthropic (Claude)"], ["openai_compatible", "OpenAI-compatible (self-hosted)"]].map(([v, t]) => h("option", { value: v, text: t, selected: s.provider === v })));
    const model = h("input", { value: s.model });
    const baseUrl = h("input", { value: s.base_url, placeholder: "optional; required for openai_compatible" });
    const effort = h("select", {}, ["low", "medium", "high", "xhigh", "max"].map((v) => h("option", { value: v, text: v, selected: s.effort === v })));
    const maxTokens = h("input", { type: "number", value: s.max_tokens, min: 1024 });
    const fallbacks = h("input", { type: "checkbox", checked: s.fallbacks });
    const key = h("input", { type: "password", autocomplete: "new-password", placeholder: s.api_key_set ? `stored (${s.api_key_hint}); leave blank to keep` : "paste API key" });
    async function save(clearKey) {
      const body = { provider: provider.value, model: model.value.trim(), base_url: baseUrl.value.trim(), effort: effort.value, max_tokens: Number(maxTokens.value), fallbacks: fallbacks.checked };
      if (clearKey) body.api_key = ""; else if (key.value) body.api_key = key.value;
      await guarded(() => api("/api/settings/ml", { method: "PUT", body }), clearKey ? "API key removed" : "Saved");
      route();
    }
    root.append(h("section", { class: "panel stack", style: "max-width:760px" },
      h("h2", { text: "ML assistant" }),
      h("p", { class: "muted", text: "Used by the pipeline editor's “Ask AI” panel and the MCP tool assist_edit_pipeline. The key is encrypted at rest with DAH_SECRET_KEY and never sent back to the browser." }),
      h("div", { class: "grid-form" },
        h("label", {}, "Provider", provider), h("label", {}, "Model", model), h("label", {}, "Base URL", baseUrl),
        h("label", {}, "Effort", effort), h("label", {}, "Max output tokens", maxTokens), h("label", {}, "API key", key)),
      h("label", { class: "inline" }, fallbacks, "Anthropic server-side fallback when a request is declined (beta)"),
      h("div", { class: "row" }, h("button", { class: "primary", text: "Save", onclick: () => save(false) }),
        s.api_key_set ? h("button", { class: "danger", text: "Remove key", onclick: () => save(true) }) : null)),
      h("section", { class: "panel stack", style: "max-width:760px" }, h("h2", { text: "MCP server" }),
        h("p", { text: "Connect MCP clients to the dah MCP server (docker-compose service “mcp”)." }),
        h("pre", { text: "Streamable HTTP: http://<host>:8001/mcp\nHeader:        Authorization: Bearer <DAH_MCP_TOKEN or DAH_ADMIN_TOKEN>\n\nstdio (local):  DAH_API_URL=http://localhost:8000 DAH_ADMIN_TOKEN=… dah-mcp" })));
  },
});

/* ================================================================ SSO + data endpoints */

const OIDC_FIELDS = [["issuer", "Issuer URL"], ["client_id", "Client ID"], ["client_secret", "Client secret", "password"], ["discovery_url", "Discovery URL (optional)"],
  ["scopes", "Scopes (space separated)", "list"], ["groups_claim", "Groups claim"], ["email_claim", "Email claim"], ["attribute_claims", "Extra claims → attributes", "list"],
  ["audience", "Bearer token audience (default client ID)"], ["allow_bearer", "Accept IdP bearer tokens on /data", "bool"]];
const SAML_FIELDS = [["idp_entity_id", "IdP entity ID"], ["idp_sso_url", "IdP SSO URL (redirect binding)"], ["idp_x509_cert", "IdP signing certificate (PEM)", "text"],
  ["idp_slo_url", "IdP logout URL (optional)"], ["sp_entity_id", "SP entity ID (default: metadata URL)"], ["groups_attribute", "Groups attribute"],
  ["email_attribute", "Email attribute"], ["name_attribute", "Display-name attribute"], ["attribute_names", "Extra attributes → attributes", "list"],
  ["name_id_format", "NameID format"], ["want_assertions_signed", "Require signed assertions", "bool"], ["sp_x509_cert", "SP certificate (optional, PEM)", "text"],
  ["sp_private_key", "SP private key (optional, PEM)", "secret-text"]];

function providerForm(existing, onDone) {
  const kind = h("select", { disabled: !!existing }, ["oidc", "saml"].map((k) => h("option", { value: k, text: k.toUpperCase(), selected: existing ? existing.kind === k : k === "oidc" })));
  const id = h("input", { value: existing ? existing.id : "", placeholder: "okta", disabled: !!existing });
  const name = h("input", { value: existing ? existing.display_name : "", placeholder: "Okta (staff)" });
  const enabled = h("input", { type: "checkbox", checked: existing ? existing.enabled : true });
  const fieldsBox = h("div", { class: "grid-form" });
  const inputs = {};
  function draw() {
    clear(fieldsBox);
    const cfg = existing ? existing.config : {};
    for (const [key, label, type] of kind.value === "oidc" ? OIDC_FIELDS : SAML_FIELDS) {
      const v = cfg[key];
      let el;
      if (type === "bool") el = h("input", { type: "checkbox", checked: v === undefined ? true : !!v });
      else if (type === "text" || type === "secret-text") { el = h("textarea", { rows: 4, placeholder: type === "secret-text" && v && v.set ? "stored; leave blank to keep" : "" }); if (type === "text") el.value = v || ""; }
      else if (type === "password") el = h("input", { type: "password", autocomplete: "new-password", placeholder: v && v.set ? "stored; leave blank to keep" : "" });
      else el = h("input", { value: type === "list" ? (v || []).join(" ") : (v || "") });
      inputs[key] = [el, type];
      fieldsBox.append(h("label", { class: type === "bool" ? "inline" : "", style: type === "text" || type === "secret-text" ? "grid-column:1/-1" : "" }, type === "bool" ? [el, label] : [label, el]));
    }
  }
  kind.addEventListener("change", draw);
  draw();
  async function save() {
    const config = {};
    for (const [key, [el, type]] of Object.entries(inputs)) {
      if (type === "bool") config[key] = el.checked;
      else if (type === "list") config[key] = el.value.split(/[\s,]+/).filter(Boolean);
      else if ((type === "password" || type === "secret-text") && !el.value) continue;
      else config[key] = el.value.trim();
    }
    if (existing) await guarded(() => api(`/api/sso/providers/${existing.id}`, { method: "PUT", body: { display_name: name.value, enabled: enabled.checked, config } }), "Saved");
    else await guarded(() => api("/api/sso/providers", { method: "POST", body: { id: id.value.trim(), kind: kind.value, display_name: name.value, enabled: enabled.checked, config } }), "Created");
    onDone();
  }
  return h("div", { class: "stack" },
    h("div", { class: "grid-form" }, h("label", {}, "ID (URL slug)", id), h("label", {}, "Protocol", kind), h("label", {}, "Display name", name), h("label", { class: "inline" }, enabled, "enabled")),
    fieldsBox,
    existing ? h("dl", { class: "kv" },
      existing.redirect_uri ? [h("dt", { text: "Redirect URI" }), h("dd", { class: "mono", text: existing.redirect_uri })] : null,
      existing.acs_url ? [h("dt", { text: "ACS URL" }), h("dd", { class: "mono", text: existing.acs_url }), h("dt", { text: "SP metadata" }), h("dd", {}, h("a", { href: existing.metadata_url, target: "_blank", text: existing.metadata_url }))] : null,
      h("dt", { text: "Login URL" }), h("dd", { class: "mono", text: existing.login_url })) : null,
    h("div", { class: "row" }, h("button", { class: "primary", text: existing ? "Save" : "Create", onclick: save }),
      existing ? h("button", { class: "danger", text: "Delete", onclick: async () => { await guarded(() => api(`/api/sso/providers/${existing.id}`, { method: "DELETE" }), "Deleted"); onDone(); } }) : null));
}

const POLICY_EXAMPLE = {
  providers: [], allowed_groups: ["finance"], allowed_users: [],
  row_filters: [{ column: "region", claim: "groups", op: "in" }],
  unrestricted_groups: ["auditors"], columns: [], column_groups: { salary: ["hr"] }, max_rows: 10000, allow_user_filters: true,
};

async function endpointForm(existing, onDone) {
  const slug = h("input", { value: existing ? existing.slug : "", disabled: !!existing, placeholder: "sales" });
  const desc = h("input", { value: existing ? existing.description : "" });
  const srcKind = h("select", {}, [["table", "Lake table / view"], ["sql", "SQL query"]].map(([v, t]) => h("option", { value: v, text: t, selected: existing ? (existing.source_sql ? v === "sql" : v === "table") : v === "table" })));
  const views = Object.keys((App.schema && App.schema.views) || {});
  const table = h("select", {}, views.map((v) => h("option", { value: v, text: v, selected: existing && existing.source_table === v })));
  const sqlBox = h("div", { class: "editor small" });
  const polBox = h("div", { class: "editor small" });
  const enabled = h("input", { type: "checkbox", checked: existing ? existing.enabled : true });
  const tableLabel = h("label", {}, "Source table", table);
  const sqlLabel = h("label", { style: "grid-column:1/-1" }, "Source SQL", sqlBox);
  const toggle = () => { tableLabel.hidden = srcKind.value !== "table"; sqlLabel.hidden = srcKind.value !== "sql"; };
  srcKind.addEventListener("change", toggle);
  const wrap = h("div", { class: "stack" },
    h("div", { class: "grid-form" }, h("label", {}, "Slug", slug), h("label", {}, "Description", desc), h("label", {}, "Source", srcKind), tableLabel, h("label", { class: "inline" }, enabled, "enabled")),
    sqlLabel,
    h("label", {}, "Access policy (JSON) — see docs: providers, allowed_users, allowed_groups, row_filters, unrestricted_groups, columns, column_groups, max_rows", polBox));
  toggle();
  const sqlEd = await createEditor(sqlBox, { language: "sql", value: existing && existing.source_sql ? existing.source_sql : "SELECT * FROM documents" });
  const polEd = await createEditor(polBox, { language: "json", value: JSON.stringify(existing ? existing.policy : POLICY_EXAMPLE, null, 2) });
  async function save() {
    let policy;
    try { policy = JSON.parse(polEd.getValue()); } catch (e) { return toast("Policy is not valid JSON", true); }
    const body = { description: desc.value, policy, enabled: enabled.checked,
      source_table: srcKind.value === "table" ? table.value : null, source_sql: srcKind.value === "sql" ? sqlEd.getValue() : null };
    if (existing) await guarded(() => api(`/api/data-endpoints/${existing.slug}`, { method: "PUT", body }), "Saved");
    else await guarded(() => api("/api/data-endpoints", { method: "POST", body: { slug: slug.value.trim(), ...body } }), "Created");
    onDone();
  }
  wrap.append(h("div", { class: "row" }, h("button", { class: "primary", text: existing ? "Save" : "Create", onclick: save }),
    existing ? h("button", { class: "danger", text: "Delete", onclick: async () => { await guarded(() => api(`/api/data-endpoints/${existing.slug}`, { method: "DELETE" }), "Deleted"); onDone(); } }) : null));

  if (existing) {
    const who = h("textarea", { rows: 4, class: "mono" });
    who.value = JSON.stringify({ provider: "okta", email: "ann@example.com", groups: ["finance", "eu"], attributes: {} }, null, 2);
    const out = h("div");
    wrap.append(h("h3", { text: "Simulate a caller" }), who, h("div", { class: "row" }, h("button", { text: "Evaluate policy", onclick: async () => {
      let principal; try { principal = JSON.parse(who.value); } catch (_) { return toast("Principal must be JSON", true); }
      const r = await guarded(() => api(`/api/data-endpoints/${existing.slug}/simulate`, { method: "POST", body: { principal } }));
      clear(out).append(h("p", {}, badge(r.allowed ? "succeeded" : "failed"), " ", r.reason),
        r.sql ? h("pre", { text: `${r.sql}\nparams: ${JSON.stringify(r.params)}` }) : null,
        r.rows ? dataTable(r.columns, r.rows) : null);
    } })), out,
    h("p", { class: "muted" }, "Callers: ", h("code", { text: `curl -H 'Authorization: Bearer <token>' ${existing.url}?limit=100&f.<column>=<value>&format=json|csv|ndjson` })));
  }
  return wrap;
}

registerView("access", {
  title: "Access", order: 60,
  async render(root, params) {
    await refreshSchema();
    const [provs, eps] = await Promise.all([api("/api/sso/providers"), api("/api/data-endpoints")]);
    const [section, sel] = params;
    const main = h("div", { class: "stack" });
    const side = h("div", { class: "panel stack" },
      h("div", { class: "row" }, h("h3", { text: "Identity providers" }), h("span", { class: "spacer" }), h("button", { text: "Add", onclick: () => go("access", "provider", "_new") })),
      h("ul", { class: "list" }, provs.items.map((p) => h("li", { class: section === "provider" && sel === p.id ? "active" : "", onclick: () => go("access", "provider", p.id) },
        h("div", { text: p.display_name }), h("div", { class: "sub", text: `${p.kind.toUpperCase()} · ${p.id}${p.enabled ? "" : " · disabled"}` })))),
      h("div", { class: "row" }, h("h3", { text: "Data endpoints" }), h("span", { class: "spacer" }), h("button", { text: "Add", onclick: () => go("access", "endpoint", "_new") })),
      h("ul", { class: "list" }, eps.items.map((e) => h("li", { class: section === "endpoint" && sel === e.slug ? "active" : "", onclick: () => go("access", "endpoint", e.slug) },
        h("div", { class: "mono", text: `/data/${e.slug}` }), h("div", { class: "sub", text: e.description || (e.source_table ? `table ${e.source_table}` : "SQL") })))),
      h("button", { text: "Access log", onclick: () => go("access", "log") }));
    root.append(h("div", { class: "split" }, side, main));
    const back = () => route();
    if (section === "provider") {
      const p = provs.items.find((x) => x.id === sel);
      main.append(h("section", { class: "panel stack" }, h("h2", { text: p ? p.display_name : "New identity provider" }), providerForm(p, () => go("access"))));
    } else if (section === "endpoint") {
      const e = eps.items.find((x) => x.slug === sel);
      main.append(h("section", { class: "panel stack" }, h("h2", { text: e ? `/data/${e.slug}` : "New data endpoint" }), await endpointForm(e, back)));
    } else if (section === "log") {
      const log = await api("/api/access-log?limit=500");
      main.append(h("section", { class: "panel stack" }, h("h2", { text: "Access log" }), h("div", { class: "scroll" },
        dataTable([{ key: "at", label: "When" }, { key: "endpoint", label: "Endpoint" }, { key: "subject", label: "Who" }, { key: "provider", label: "IdP" },
          { key: "allowed", label: "Result" }, { key: "rows", label: "Rows", num: true }, { key: "reason", label: "Reason" }], log.items,
          { render: { at: (v) => fmtDate(v), allowed: (v) => badge(v ? "succeeded" : "failed") } }))));
    } else {
      main.append(h("section", { class: "panel stack" }, h("h2", { text: "SSO-protected data API" }),
        h("p", { text: "Configure any number of SAML 2.0 and OpenID Connect identity providers, then publish data endpoints with per-endpoint access policies (users, groups, row filters from user claims, column restrictions)." }),
        h("p", {}, "Users sign in at ", h("a", { href: "/sso", target: "_blank", text: "/sso" }), "; API clients use the issued bearer token or, for OIDC providers, the IdP's own JWT.")));
    }
  },
});

/* ================================================================ Licenses */

registerView("licenses", {
  title: "Licenses", order: 90,
  async render(root) {
    const inv = await api("/api/licenses");
    const link = (text, url) => url ? h("a", { href: url, target: "_blank", rel: "noopener", text }) : text;
    root.append(h("div", { class: "stack" },
      h("section", { class: "panel stack" }, h("h2", { text: "Open-source acknowledgements" }),
        h("p", { text: `dah ${inv.project.version} is licensed under ${inv.project.license} and built entirely on open-source software. Thank you to every project listed here.` }),
        h("p", {}, "Public page: ", h("a", { href: "/licenses", target: "_blank", text: "/licenses" }))),
      h("section", { class: "panel stack" }, h("h3", { text: "System and frontend components" }),
        dataTable([{ key: "name", label: "Component" }, { key: "license", label: "License" }, { key: "used_for", label: "Used for" }], inv.system_components,
          { render: { name: (v, r) => link(v, r.url) } })),
      h("section", { class: "panel stack" }, h("h3", { text: `Python packages (${inv.python_packages.length})` }),
        dataTable([{ key: "name", label: "Package" }, { key: "version", label: "Version" }, { key: "license", label: "License" }, { key: "direct", label: "Direct" }, { key: "text", label: "" }],
          inv.python_packages, { render: {
            name: (v, r) => link(v, r.url), direct: (v) => (v ? "yes" : ""),
            license: (v, r) => h("span", {}, v, r.open_source ? null : [" ", badge("failed")], r.note ? h("div", { class: "sub muted", text: r.note }) : null),
            text: (_, r) => r.has_license_text ? link("license text", `/api/licenses/${encodeURIComponent(r.name)}/text`) : "" } }))));
  },
});
