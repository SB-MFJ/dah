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
