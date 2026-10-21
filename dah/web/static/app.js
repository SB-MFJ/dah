/* Core: API client, auth, routing and small DOM helpers. All user data is rendered via textContent. */
"use strict";

const App = {
  views: {},          // name -> {title, order, render(root, params)}
  schema: null,       // /api/schema, used by editor autocomplete
  token: null,
};

/* ---------------------------------------------------------------- DOM */

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

function toast(msg, bad = false) {
  const t = h("div", { class: "t" + (bad ? " bad" : ""), text: msg });
  document.getElementById("toast").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}

function fmtBytes(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleString();
}

function badge(status) { return h("span", { class: `badge ${status || ""}`, text: status || "" }); }

function dataTable(columns, rows, { onRow, render } = {}) {
  const thead = h("thead", {}, h("tr", {}, columns.map(c => h("th", { text: typeof c === "string" ? c : c.label }))));
  const tbody = h("tbody");
  for (const r of rows) {
    const tr = h("tr", { class: onRow ? "clickable" : "" });
    for (const c of columns) {
      const key = typeof c === "string" ? c : c.key;
      let v = render && render[key] ? render[key](r[key], r) : r[key];
      if (!(v instanceof Node)) {
        if (v !== null && typeof v === "object") v = JSON.stringify(v);
        v = h("div", { class: "cell", title: v == null ? "" : String(v), text: v == null ? "" : String(v) });
      }
      tr.append(h("td", { class: c.num ? "num" : "" }, v));
    }
    if (onRow) tr.addEventListener("click", () => onRow(r));
    tbody.append(tr);
  }
  return h("table", { class: "data" }, thead, tbody);
}

function lineDiff(a, b) {
  // Simple LCS line diff for version comparisons.
  const A = (a || "").split("\n"), B = (b || "").split("\n");
  const n = A.length, m = B.length;
  if (n * m > 4e6) return [{ t: "hunk", s: "(too large to diff in browser)" }];
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push({ t: "eq", s: A[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ t: "del", s: A[i++] });
    else out.push({ t: "add", s: B[j++] });
  }
  while (i < n) out.push({ t: "del", s: A[i++] });
  while (j < m) out.push({ t: "add", s: B[j++] });
  return out;
}

function diffView(a, b) {
  const lines = lineDiff(a, b);
  return h("div", { class: "diff" }, lines.map(l =>
    h("div", { class: l.t, text: (l.t === "add" ? "+ " : l.t === "del" ? "- " : l.t === "hunk" ? "" : "  ") + l.s })));
}

/* ---------------------------------------------------------------- API */

function loadToken() {
  App.token = sessionStorage.getItem("dah.token");
  if (!App.token) { try { App.token = localStorage.getItem("dah.token"); } catch (_) { App.token = null; } }
}

function saveToken(tok, remember) {
  App.token = tok;
  sessionStorage.setItem("dah.token", tok);
  try { if (remember) localStorage.setItem("dah.token", tok); else localStorage.removeItem("dah.token"); } catch (_) {}
}

function logout() {
  App.token = null;
  sessionStorage.removeItem("dah.token");
  try { localStorage.removeItem("dah.token"); } catch (_) {}
  showLogin();
}

class ApiError extends Error {
  constructor(status, detail) { super(typeof detail === "string" ? detail : JSON.stringify(detail)); this.status = status; }
}

async function api(path, { method = "GET", body, form, raw } = {}) {
  const headers = {};
  if (App.token) headers["Authorization"] = `Bearer ${App.token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(path, { method, headers, body: payload });
  if (res.status === 401) { showLogin(); throw new ApiError(401, "sign in required"); }
  if (raw) { if (!res.ok) throw new ApiError(res.status, await res.text()); return res; }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, (data && data.detail) || data);
  return data;
}

async function guarded(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { if (e.status !== 401) toast(e.message || String(e), true); throw e; }
}

async function refreshSchema() {
  try { App.schema = await api("/api/schema"); } catch (_) { /* editor falls back to no completions */ }
  return App.schema;
}

/* ---------------------------------------------------------------- auth */

function showLogin() {
  const dlg = document.getElementById("login");
  if (!dlg.open) dlg.showModal();
  document.getElementById("logout").hidden = true;
  document.getElementById("whoami").textContent = "";
}

async function tryLogin(tok, remember) {
  const res = await fetch("/api/schema", { headers: { Authorization: `Bearer ${tok}` } });
  if (!res.ok) return false;
  saveToken(tok, remember);
  App.schema = await res.json();
  return true;
}

/* ---------------------------------------------------------------- routing */

function parseHash() {
  const [name, ...rest] = (location.hash.slice(1) || "documents").split("/");
  return { name, params: rest.map(decodeURIComponent) };
}

function go(name, ...params) { location.hash = [name, ...params.map(encodeURIComponent)].join("/"); }

function renderNav() {
  const nav = clear(document.getElementById("nav"));
  const { name } = parseHash();
  Object.entries(App.views).sort((a, b) => a[1].order - b[1].order).forEach(([key, v]) =>
    nav.append(h("a", { href: `#${key}`, class: key === name ? "active" : "", text: v.title })));
}

async function route() {
  if (!App.token) return showLogin();
  renderNav();
  const { name, params } = parseHash();
  const view = App.views[name] || App.views.documents;
  const root = clear(document.getElementById("view"));
  try { await view.render(root, params); }
  catch (e) { if (e.status !== 401) root.append(h("p", { class: "error", text: e.message })); }
}

function registerView(name, def) { App.views[name] = def; }

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", async () => {
  loadToken();
  document.getElementById("logout").addEventListener("click", logout);
  document.getElementById("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const tok = document.getElementById("login-token").value.trim();
    const ok = await tryLogin(tok, document.getElementById("login-remember").checked);
    const err = document.getElementById("login-error");
    if (!ok) { err.textContent = "Token rejected."; err.hidden = false; return; }
    err.hidden = true;
    document.getElementById("login").close();
    document.getElementById("logout").hidden = false;
    document.getElementById("whoami").textContent = "admin";
    route();
  });
  let remembered = false;
  try { remembered = !!localStorage.getItem("dah.token"); } catch (_) {}
  if (App.token && await tryLogin(App.token, remembered)) {
    document.getElementById("logout").hidden = false;
    document.getElementById("whoami").textContent = "admin";
    route();
  } else {
    App.token = null;
    showLogin();
  }
});
