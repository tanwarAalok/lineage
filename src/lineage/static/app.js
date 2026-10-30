"use strict";
// Lineage web UI. No framework, no build. All data is inserted as text nodes (never innerHTML).

const $main = document.getElementById("main");
const $nav = document.getElementById("nav");
const TOKEN_KEY = "lineage.token";
let me = null;

// ---------- tiny DOM helper: strings become text nodes, so data can never become markup
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const badge = (status) => h("span", { class: `badge ${status}` }, status);
const roleOf = (project) => me.is_admin ? "admin" : me.maintains.includes(project) ? "maintainer" : "developer";

// Poll while an index job runs, so progress updates without a manual refresh.
let pollTimer = null;
function pollWhile(active) {
  clearTimeout(pollTimer);
  if (!active) return;
  pollTimer = setTimeout(() => {
    // Never wipe a half-typed form: skip this refresh and check again later.
    const typing = [...document.querySelectorAll("input[type=text], textarea")].some((e) => e.value);
    typing ? pollWhile(true) : render();
  }, 3000);
}
function jobLine(job) {
  if (!job) return null;
  const p = job.progress || {};
  if (job.status === "running" || job.status === "queued") {
    const done = (p.indexed || 0) + (p.unchanged || 0) + (p.skipped || 0) + (p.failed || 0);
    return h("div", { class: "muted" }, badge("proposed"), " indexing ",
      p.total ? `${done} / ${p.total} files` : "starting…", p.seconds ? ` · ${p.seconds}s` : "");
  }
  if (job.status === "failed" || job.status === "interrupted")
    return h("div", { class: "muted" }, badge("rejected"), ` last index ${job.status}`, job.error ? `: ${job.error}` : "");
  return null;
}
const jobActive = (job) => job && (job.status === "running" || job.status === "queued");
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "never");

function toast(msg, error = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (error ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = ""), error ? 5000 : 2500);
}

// ---------- API
const token = () => { try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; } };

async function api(path, body) {
  const headers = { Authorization: `Bearer ${token()}` };
  if (body) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) { logout(); throw new Error("Session expired, sign in again"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// Wrap a button action: disable while running, report errors, re-render on success.
function action(fn, done = "Saved") {
  return async (ev) => {
    ev?.preventDefault();
    const btn = ev?.submitter || ev?.currentTarget;
    if (btn?.tagName === "BUTTON") btn.disabled = true;
    try { const r = await fn(ev); if (done) toast(done); await render(); return r; }
    catch (e) { toast(e.message, true); }
    finally { if (btn?.tagName === "BUTTON") btn.disabled = false; }
  };
}

async function download(project, name) {
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/assets/${name}`,
      { headers: { Authorization: `Bearer ${token()}` } });
    if (!r.ok) throw new Error((await r.json()).error);
    const url = URL.createObjectURL(await r.blob());
    h("a", { href: url, download: name }).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { toast(e.message, true); }
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { toast("Copy failed: select the text and copy it manually", true); }
}

function logout() {
  try { sessionStorage.removeItem(TOKEN_KEY); } catch {}
  me = null;
  location.hash = "#/";
  render();
}

// ---------- views
function loginView() {
  $nav.replaceChildren();
  const input = h("input", { type: "password", autocomplete: "off", placeholder: "lin_…", required: true });
  return h("div", { class: "login panel stack" },
    h("h1", {}, "Sign in"),
    h("p", { class: "muted" }, "Paste the access token an admin gave you. It stays in this browser tab only."),
    h("form", { class: "inline", onsubmit: async (ev) => {
        ev.preventDefault();
        try { sessionStorage.setItem(TOKEN_KEY, input.value.trim()); } catch {}
        try { me = await api("/api/me"); render(); } catch (e) { toast(e.message, true); }
      } },
      h("label", {}, "Access token", input),
      h("button", { class: "primary", type: "submit" }, "Sign in")));
}

function navView() {
  $nav.replaceChildren(
    h("a", { href: "#/" }, "Projects"),
    me.is_admin && h("a", { href: "#/people" }, "People"),
    h("span", { class: "muted" }, me.name, me.is_admin ? " · admin" : ""),
    h("button", { class: "link", onclick: logout }, "Sign out"));
}

async function projectsView() {
  const projects = await api("/api/projects");
  pollWhile(projects.some((p) => jobActive(p.last_job)));
  const repo = h("input", { type: "text", placeholder: "/repos/billing", required: true });
  const pid = h("input", { type: "text", placeholder: "billing (optional)" });
  return h("div", { class: "stack" },
    h("div", {}, h("h1", {}, "Projects"),
      h("p", { class: "muted" }, "Pick the project your repository belongs to. Its rules, dictionary and code index are shared with your team.")),
    projects.length
      ? h("div", { class: "grid" }, projects.map((p) =>
          h("a", { class: "card", href: `#/p/${encodeURIComponent(p.id)}/dictionary` },
            h("div", { class: "row spread" }, h("span", { class: "title" }, p.id), h("span", { class: "badge" }, p.role)),
            h("div", { class: "muted" }, `${p.files} files · indexed ${when(p.indexed_at)}`),
            jobLine(p.last_job))))
      : h("div", { class: "panel empty" }, me.is_admin ? "No projects yet. Onboard one below." : "No projects yet. An admin onboards them."),
    me.is_admin && h("div", { class: "panel" },
      h("h2", {}, "Onboard a project"),
      h("form", { class: "inline", onsubmit: action(() =>
          api("/api/projects", { repo_path: repo.value.trim(), project: pid.value.trim() || null }), "Indexing started") },
        h("label", {}, "Repository path on the Lineage server", repo),
        h("label", {}, "Project id", pid),
        h("div", {}, h("button", { class: "primary", type: "submit" }, "Index repository")),
        h("p", { class: "muted" }, "Indexing runs in the background; large repositories take several minutes. Re-indexing only processes changed files."))));
}

const TABS = [
  ["dictionary", "Dictionary"], ["rules", "Rules"], ["links", "Code links"], ["connect", "Connect"], ["people", "Maintainers"],
];

async function projectView(project, tab) {
  const role = roleOf(project);
  projectsCache = await api("/api/projects");
  const maintainer = role === "admin" || role === "maintainer";
  const [body, jobs] = await Promise.all([
    ({ dictionary: dictionaryTab, rules: rulesTab, links: linksTab, connect: connectTab, people: maintainersTab }[tab]
      || dictionaryTab)(project, maintainer),
    api(`/api/projects/${encodeURIComponent(project)}/jobs`)]);
  pollWhile(jobActive(jobs[0]));
  return h("div", {},
    h("div", { class: "row spread" },
      h("div", {}, h("h1", {}, project), describe(project, maintainer),
        h("span", { class: "muted" }, "Your role: ", role), jobLine(jobs[0])),
      maintainer && h("button", { disabled: jobActive(jobs[0]), onclick: action(() =>
          api(`/api/projects/${encodeURIComponent(project)}/reindex`, {}), "Re-index started") }, "Re-index")),
    h("nav", { class: "tabs" }, TABS.map(([id, label]) =>
      h("a", { href: `#/p/${encodeURIComponent(project)}/${id}`, class: id === tab ? "on" : null }, label))),
    body);
}

// One line saying what the system is ("backend: orders, pricing"). Agents use it to tell systems apart.
let projectsCache = [];
function describe(project, maintainer) {
  const current = (projectsCache.find((p) => p.id === project) || {}).description || "";
  if (!maintainer) return h("p", { class: "muted" }, current || "No description yet.");
  const input = h("input", { type: "text", value: current, maxlength: 500,
    placeholder: "What this system is, e.g. backend: orders, pricing, payment authorisation" });
  return h("form", { class: "row", onsubmit: action(() =>
      api(`/api/projects/${encodeURIComponent(project)}/description`, { description: input.value })) },
    input, h("button", { type: "submit", class: "small" }, "Save"));
}

// Review buttons shared by dictionary, rules and code links.
function reviewButtons(project, kind, item) {
  const base = `/api/projects/${encodeURIComponent(project)}`;
  const set = (status) => action(() => api(`${base}/review`, { kind, id: item.id, status }), `Marked ${status}`);
  return [
    item.status !== "approved" && h("button", { class: "small", onclick: set("approved") }, "Approve"),
    item.status !== "rejected" && h("button", { class: "small", onclick: set("rejected") }, "Reject"),
    h("button", { class: "small danger", onclick: action(() => {
        if (!confirm(`Delete this ${kind}?`)) throw new Error("Cancelled");
        return api(`${base}/delete`, { kind, id: item.id });
      }, "Deleted") }, "Delete"),
  ];
}

let filter = "all";
function filters(items) {
  const count = (s) => items.filter((i) => s === "all" || i.status === s).length;
  return h("div", { class: "filters" }, ["all", "proposed", "approved", "rejected"].map((s) =>
    h("button", { class: "small" + (filter === s ? " on" : ""), onclick: () => { filter = s; render(); } },
      `${s === "proposed" ? "pending review" : s} (${count(s)})`)));
}
const visible = (items) => items.filter((i) => filter === "all" || i.status === filter);

async function dictionaryTab(project, maintainer) {
  const terms = await api(`/api/projects/${encodeURIComponent(project)}/terms`);
  const term = h("input", { type: "text", required: true, maxlength: 200 });
  const def = h("textarea", { required: true, maxlength: 8000 });
  const source = h("input", { type: "text", maxlength: 500, placeholder: "HLD v3 §2.1, CR-1042 meeting, Priya" });
  const approve = h("input", { type: "checkbox", checked: maintainer });
  const edit = (t) => { term.value = t.term; def.value = t.definition; source.value = t.source || ""; term.focus(); };
  return h("div", { class: "stack" },
    h("p", { class: "muted" }, "What design-document terms mean in this project. Requirements are often scattered or outdated, so every entry records its source and only approved entries reach agents."),
    filters(terms),
    h("div", { class: "panel table-wrap" }, visible(terms).length
      ? h("table", {},
          h("thead", {}, h("tr", {}, ["Term", "Definition", "Source", "Status", "By", ""].map((c) => h("th", {}, c)))),
          h("tbody", {}, visible(terms).map((t) => h("tr", {},
            h("td", {}, h("strong", {}, t.term)), h("td", {}, t.definition), h("td", { class: "muted" }, t.source || "-"),
            h("td", {}, badge(t.status)),
            h("td", { class: "muted" }, t.author, t.reviewer && t.reviewer !== t.author ? ` → ${t.reviewer}` : ""),
            h("td", { class: "actions" }, maintainer && [h("button", { class: "small", onclick: () => edit(t) }, "Edit"), reviewButtons(project, "term", t)])))))
      : h("div", { class: "empty" }, "No entries here yet.")),
    h("div", { class: "panel" },
      h("h2", {}, maintainer ? "Add or update an entry" : "Suggest an entry"),
      h("form", { class: "inline", onsubmit: action(() => api(`/api/projects/${encodeURIComponent(project)}/terms`,
          { term: term.value, definition: def.value, source: source.value, approve: maintainer && approve.checked }),
          maintainer && approve.checked ? "Saved" : "Suggested for review") },
        h("label", {}, "Term", term), h("label", {}, "Definition", def),
        h("label", {}, "Source (where this comes from, so reviewers can judge it)", source),
        maintainer && h("label", { class: "check" }, approve, "Approve now (otherwise it waits for review)"),
        h("div", {}, h("button", { class: "primary", type: "submit" }, maintainer ? "Save" : "Suggest")))));
}

async function rulesTab(project, maintainer) {
  const rules = await api(`/api/projects/${encodeURIComponent(project)}/rules`);
  const title = h("input", { type: "text", required: true, maxlength: 200, placeholder: "Use the shared HTTP client" });
  const body = h("textarea", { required: true, maxlength: 8000, placeholder: "All outbound calls go through http/client.ts; never call fetch directly." });
  const source = h("input", { type: "text", maxlength: 500, placeholder: "Architecture board 2026-09, coding standard v2" });
  const approve = h("input", { type: "checkbox", checked: maintainer });
  let editing = null;
  const edit = (r) => { editing = r.id; title.value = r.title; body.value = r.body; source.value = r.source || ""; title.focus(); };
  return h("div", { class: "stack" },
    h("p", { class: "muted" }, "Project rules agents must follow: conventions, required libraries, things never to do."),
    filters(rules),
    visible(rules).length
      ? visible(rules).map((r) => h("div", { class: "panel" },
          h("div", { class: "row spread" }, h("div", { class: "row" }, h("strong", {}, r.title), badge(r.status)),
            h("div", { class: "row" }, maintainer && [h("button", { class: "small", onclick: () => edit(r) }, "Edit"), reviewButtons(project, "rule", r)])),
          h("p", { class: "rule-body" }, r.body),
          h("div", { class: "muted" }, `by ${r.author}`, r.reviewer ? ` · reviewed by ${r.reviewer}` : "",
            r.source ? ` · source: ${r.source}` : "")))
      : h("div", { class: "panel empty" }, "No rules here yet."),
    h("div", { class: "panel" },
      h("h2", {}, maintainer ? "Add or edit a rule" : "Suggest a rule"),
      h("form", { class: "inline", onsubmit: action(() => api(`/api/projects/${encodeURIComponent(project)}/rules`,
          { title: title.value, body: body.value, source: source.value, id: editing, approve: maintainer && approve.checked })) },
        h("label", {}, "Title", title), h("label", {}, "Rule", body), h("label", {}, "Source", source),
        maintainer && h("label", { class: "check" }, approve, "Approve now"),
        h("div", {}, h("button", { class: "primary", type: "submit" }, maintainer ? "Save" : "Suggest")))));
}

async function linksTab(project, maintainer) {
  const links = await api(`/api/projects/${encodeURIComponent(project)}/mappings`);
  const term = h("input", { type: "text", required: true });
  const symbol = h("input", { type: "text", required: true, placeholder: "InvoiceService" });
  const path = h("input", { type: "text", required: true, placeholder: "billing/invoice.py" });
  return h("div", { class: "stack" },
    h("p", { class: "muted" }, "Which code implements each term. Lineage infers candidates when agents ask; maintainers approve the right ones."),
    filters(links),
    h("div", { class: "panel table-wrap" }, visible(links).length
      ? h("table", {},
          h("thead", {}, h("tr", {}, ["Term", "Symbol", "File", "Status", "Source", ""].map((c) => h("th", {}, c)))),
          h("tbody", {}, visible(links).map((m) => h("tr", {},
            h("td", {}, h("strong", {}, m.term)), h("td", {}, h("code", {}, m.symbol_name)), h("td", {}, h("code", {}, m.path)),
            h("td", {}, badge(m.status)), h("td", { class: "muted" }, m.origin === "inferred" ? `inferred (${m.score ?? "-"})` : m.author || "human"),
            h("td", { class: "actions" }, maintainer && reviewButtons(project, "mapping", m))))))
      : h("div", { class: "empty" }, "No code links here yet.")),
    h("div", { class: "panel" },
      h("h2", {}, "Suggest a code link"),
      h("form", { class: "inline", onsubmit: action(() => api(`/api/projects/${encodeURIComponent(project)}/mappings`,
          { term: term.value, symbol: symbol.value, path: path.value }), "Suggested for review") },
        h("label", {}, "Term", term), h("label", {}, "Symbol", symbol), h("label", {}, "File path", path),
        h("div", {}, h("button", { class: "primary", type: "submit" }, "Suggest")))));
}

// Setup for each AI tool. Tokens never go in committed files: each developer supplies their own.
const CLIENTS = {
  vscode: {
    label: "VS Code (Copilot)",
    file: ".vscode/mcp.json",
    note: "Commit it. VS Code asks each developer for their token once.",
    instructions: [["copilot-instructions.md", ".github/copilot-instructions.md"]],
    config: (url, project) => JSON.stringify({
      inputs: [{ type: "promptString", id: "lineage-token", description: "Lineage access token", password: true }],
      servers: { lineage: { type: "http", url, headers: { Authorization: "Bearer ${input:lineage-token}", "X-Lineage-Project": project } } },
    }, null, 2),
  },
  claude: {
    label: "Claude Code",
    file: "run once in the repository",
    note: "--scope local keeps your token in your own Claude config, never in the repository.",
    instructions: [["CLAUDE.md", "CLAUDE.md (repository root)"]],
    config: (url, project) =>
      `claude mcp add --transport http --scope local lineage "${url}?project=${project}" \\\n  --header "Authorization: Bearer <your Lineage token>"`,
  },
  codex: {
    label: "Codex",
    file: ".codex/config.toml (or ~/.codex/config.toml)",
    note: "Set LINEAGE_TOKEN in your environment; Codex sends it as the bearer token.",
    instructions: [["AGENTS.md", "AGENTS.md (repository root)"]],
    config: (url, project) =>
      `[mcp_servers.lineage]\nurl = "${url}"\nbearer_token_env_var = "LINEAGE_TOKEN"\nhttp_headers = { "X-Lineage-Project" = "${project}" }`,
  },
  cursor: {
    label: "Cursor",
    file: ".cursor/mcp.json",
    note: "Commit it. Each developer sets LINEAGE_TOKEN in the environment Cursor starts from.",
    instructions: [["AGENTS.md", "AGENTS.md (repository root)"]],
    config: (url, project) => JSON.stringify({
      mcpServers: { lineage: { url: `${url}?project=${project}`, headers: { Authorization: "Bearer ${env:LINEAGE_TOKEN}" } } },
    }, null, 2),
  },
  gemini: {
    label: "Gemini CLI",
    file: ".gemini/settings.json",
    note: "Commit it. Each developer sets LINEAGE_TOKEN in their environment.",
    instructions: [["GEMINI.md", "GEMINI.md (repository root)"]],
    config: (url, project) => JSON.stringify({
      mcpServers: { lineage: { httpUrl: `${url}?project=${project}`, headers: { Authorization: "Bearer $LINEAGE_TOKEN" } } },
    }, null, 2),
  },
  other: {
    label: "Other MCP clients",
    file: "your client's MCP settings",
    note: "Any client with streamable HTTP works. For stdio-only clients, bridge with: npx mcp-remote <url> --header \"Authorization: Bearer <token>\"",
    instructions: [["AGENTS.md", "AGENTS.md (repository root)"]],
    config: (url, project) =>
      `Transport:  streamable HTTP\nURL:        ${url}?project=${project}\nHeader:     Authorization: Bearer <your Lineage token>\n(optional)  X-Lineage-Project: ${project}`,
  },
};
let connectClient = "vscode";

function connectTab(project) {
  const client = CLIENTS[connectClient];
  const config = client.config(`${location.origin}/mcp`, project);
  return h("div", { class: "stack" },
    h("div", { class: "filters" }, Object.entries(CLIENTS).map(([id, c]) =>
      h("button", { class: "small" + (id === connectClient ? " on" : ""), onclick: () => { connectClient = id; render(); } }, c.label))),
    h("div", { class: "panel stack" },
      h("h2", {}, "1. Connect ", client.label, " to this project"),
      h("p", {}, h("code", {}, client.file), ". ", client.note),
      h("pre", {}, config),
      h("div", {}, h("button", { onclick: () => copy(config) }, "Copy"))),
    h("div", { class: "panel stack" },
      h("h2", {}, "2. Add the agent instructions"),
      h("p", {}, "Generated from this project's approved rules, dictionary and system map, so the agent knows the other systems exist and how to reach them. Re-download after rules change."),
      h("div", { class: "row" }, client.instructions.map(([name, where]) =>
        h("button", { onclick: () => download(project, name) }, `Download ${name}`)),
        h("button", { onclick: () => download(project, "glossary.yaml") }, "glossary.yaml")),
      h("p", { class: "muted" }, "Save it as ", client.instructions.map(([, where]) => where).join(", "), ".")));
}

async function maintainersTab(project) {
  const people = await api(`/api/projects/${encodeURIComponent(project)}/maintainers`);
  const user = h("input", { type: "text", required: true });
  const set = (name, on) => action(() => api(`/api/projects/${encodeURIComponent(project)}/maintainers`, { user: name, maintainer: on }));
  return h("div", { class: "stack" },
    h("p", { class: "muted" }, "Everyone signed in is a developer on every project: they use it and suggest entries. Maintainers also approve suggestions and re-index. Admins choose them."),
    h("div", { class: "panel table-wrap" }, people.length
      ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Maintainer"), h("th", {}))),
          h("tbody", {}, people.map((m) => h("tr", {}, h("td", {}, m.name),
            h("td", { class: "actions" }, me.is_admin && h("button", { class: "small danger", onclick: set(m.name, false) }, "Make developer"))))))
      : h("div", { class: "empty" }, "No maintainers: only admins approve suggestions for this project.")),
    me.is_admin && h("div", { class: "panel" }, h("h2", {}, "Add a maintainer"),
      h("form", { class: "inline", onsubmit: action(() => api(`/api/projects/${encodeURIComponent(project)}/maintainers`,
          { user: user.value.trim(), maintainer: true })) },
        h("label", {}, "User name", user),
        h("div", {}, h("button", { class: "primary", type: "submit" }, "Make maintainer")))));
}

let revealed = null; // a freshly issued token, shown once
async function peopleView() {
  const users = await api("/api/users");
  const name = h("input", { type: "text", required: true });
  const admin = h("input", { type: "checkbox" });
  const issue = (fn) => action(async () => { revealed = await fn(); }, null);
  return h("div", { class: "stack" },
    h("h1", {}, "People"),
    revealed && h("div", { class: "secret stack" },
      h("strong", {}, `Token for ${revealed.name}. Copy it now; it is not shown again.`),
      h("div", {}, h("code", {}, revealed.token)),
      h("div", { class: "row" }, h("button", { onclick: () => copy(revealed.token) }, "Copy"),
        h("button", { onclick: () => { revealed = null; render(); } }, "Done"))),
    h("div", { class: "panel table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["User", "Maintains", "Created", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, users.map((u) => h("tr", {},
        h("td", {}, u.name, u.is_admin ? " " : "", u.is_admin && h("span", { class: "badge" }, "admin"),
          u.disabled ? " " : "", u.disabled && h("span", { class: "badge rejected" }, "disabled")),
        h("td", { class: "muted" }, u.maintains || "-"), h("td", { class: "muted" }, when(u.created_at)),
        h("td", { class: "actions" },
          h("button", { class: "small", onclick: issue(() => api(`/api/users/${encodeURIComponent(u.name)}/token`, {})) }, "New token"),
          u.name !== me.name && h("button", { class: "small danger", onclick: action(() =>
            api(`/api/users/${encodeURIComponent(u.name)}/disabled`, { disabled: !u.disabled })) }, u.disabled ? "Enable" : "Disable"))))))),
    h("div", { class: "panel" }, h("h2", {}, "Add a person"),
      h("form", { class: "inline", onsubmit: issue(() => api("/api/users", { name: name.value.trim(), is_admin: admin.checked })) },
        h("label", {}, "User name", name),
        h("label", { class: "check" }, admin, "Admin (onboards projects, sets rules, manages people)"),
        h("p", { class: "muted" }, "New people are developers on every project straight away."),
        h("div", {}, h("button", { class: "primary", type: "submit" }, "Create and show token")))));
}

// ---------- router
async function render() {
  if (!token()) { $main.replaceChildren(loginView()); return; }
  try {
    if (!me) me = await api("/api/me");
    navView();
    const [, section, project, tab] = location.hash.split("/").map(decodeURIComponent);
    const view = section === "p" && project ? await projectView(project, tab || "dictionary")
      : section === "people" && me.is_admin ? await peopleView()
      : await projectsView();
    $main.replaceChildren(view);
  } catch (e) {
    if (token()) $main.replaceChildren(h("div", { class: "panel empty" }, e.message));
  }
}
window.addEventListener("hashchange", () => { filter = "all"; clearTimeout(pollTimer); render(); });
render();
