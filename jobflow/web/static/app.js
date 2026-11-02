const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const KEYS = ["skills", "seniority", "location", "domain", "recency", "reach"];
const KIND = { easy: "quick apply", ats_form: "company form", external: "apply yourself" };

// Non-blocking notices in place of alert(); confirm() stays for destructive prompts.
function toast(msg, err) {
  const box = $("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), Math.min(9000, 3000 + String(msg).length * 40));
}
window.alert = (msg) => toast(msg, /fail|error|rejected|not |cannot|invalid/i.test(String(msg)));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
});

// --- logos: favicon from the company domain, initials when there is none -----
function logoEl(job, cls = "logo") {
  const el = document.createElement("div");
  el.className = cls;
  const initials = () => {
    const s = document.createElement("span");
    s.textContent = (job.company || "?").slice(0, 2).toUpperCase();
    el.appendChild(s);
  };
  if (!job.domain) { initials(); return el; }
  const img = new Image();
  img.alt = "";
  img.src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(job.domain)}&sz=64`;
  img.onerror = () => { img.remove(); initials(); };
  el.appendChild(img);
  return el;
}

// --- tabs -------------------------------------------------------------------
let current = "approve";
function show(tab) {
  if (!document.querySelector(`.tab[data-tab="${tab}"]`)) tab = "approve";
  current = tab;
  const site = tab.startsWith("site-") ? tab.slice(5) : null;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".pane").forEach((p) =>
    p.classList.toggle("hidden", p.id !== (site ? "pane-site" : `pane-${tab}`)));
  try { localStorage.setItem("tab", tab); } catch {}
  if (site) return loadSite(site);
  ({ setup: loadSetup, approve: loadReview, questions: loadQuestions, applied: loadApplied, browse: loadBrowse,
     sites: loadOverview })[tab]();
}
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => show(b.dataset.tab)));

// --- header -----------------------------------------------------------------
let overview = null;
async function loadOverview() {
  overview = await api("/api/overview");
  const c = overview.counts;
  $("s-today").textContent = overview.applied_today;
  $("s-review").textContent = c.review;
  $("s-questions").textContent = overview.questions;
  $("s-queued").textContent = c.queued + c.approved;
  const t = overview.tokens;
  $("s-tokens").textContent = t.limit ? `${Math.round((100 * t.used) / t.limit)}%` : "–";
  $("b-review").textContent = c.review || "";
  $("b-questions").textContent = overview.questions || "";
  for (const s of overview.sites) {
    const b = $(`b-${s.name}`);
    if (b) b.textContent = s.applied_today || "";
  }
  if (current === "sites") renderSites();
}

// --- approve ------------------------------------------------------------------
async function loadReview() {
  const q = encodeURIComponent($("q").value.trim());
  const d = await api(`/api/jobs?status=review&q=${q}`);
  const kind = $("kind").value;
  const rows = d.jobs.filter((j) => !kind || j.apply_kind === kind);
  const box = $("review-list");
  box.textContent = "";
  shownIds = rows.filter((j) => j.apply_kind !== "external" && j.platform !== "remote").map((j) => j.id);
  $("apply-all").textContent = `Apply to all shown (${shownIds.length})`;
  $("apply-all").disabled = !shownIds.length;
  if (!rows.length) {
    box.innerHTML = '<div class="empty">Nothing waiting for you.</div>';
    return;
  }
  for (const j of rows) box.appendChild(reviewCard(j));
}

let shownIds = [];
$("apply-all").onclick = async () => {
  if (!shownIds.length || !confirm(`Send the bot to apply to ${shownIds.length} job(s), each with a tailored resume?`)) return;
  $("apply-all").disabled = true;
  try {
    const r = await post("/api/apply-all", { ids: shownIds });
    loadOverview();
    loadReview();
    alert(`${r.approved} job(s) handed to the bot. It applies as the worker reaches each site.`);
  } catch (e) { alert(e.message); $("apply-all").disabled = false; }
};

// ATS: your resume's keyword coverage for this posting, and the edit made if any.
function atsBlock(score, after, rec) {
  if (score === null || score === undefined) return "";
  rec = rec || {};
  const edits = (rec.edits || []).map((e) =>
    `<div class="edit"><s>${esc(e.old)}</s><br>→ ${esc(e.new)}</div>`).join("");
  const notYours = (rec.not_yours || []).length
    ? `<div class="m">missing, not in your skills (left out): ${rec.not_yours.map(esc).join(" · ")}</div>` : "";
  return `<div class="ats">ATS match <b>${score}%</b>${after ? ` → <b>${after}%</b> with the edited resume` : ""}
    ${(rec.added || []).length ? `<div class="m">added: ${rec.added.map(esc).join(" · ")}</div>` : ""}
    ${edits}${notYours}</div>`;
}

function atsOf(j) {
  let rec = null;
  try { rec = j.tailored ? JSON.parse(j.tailored) : null; } catch {}
  return atsBlock(j.ats_score, j.ats_after, rec);
}

function bars(bd) {
  return `<div class="bars">${KEYS.map((k) => {
    const v = Math.max(0, Math.min(100, Number((bd || {})[k]) || 0));
    return `<div class="bar">${k}<i><b style="width:${v}%"></b></i></div>`;
  }).join("")}</div>`;
}

function reviewCard(j) {
  const el = document.createElement("div");
  el.className = "card";
  el.appendChild(logoEl(j));
  const body = document.createElement("div");
  const canApprove = j.apply_kind !== "external" && j.platform !== "remote" &&
    ["review", "skipped", "failed", "duplicate"].includes(j.status);
  body.innerHTML = `
    <div class="t">${esc(j.title)}</div>
    <div class="m">${esc(j.company)} · ${esc(j.location || "location n/a")}${j.salary ? " · " + esc(j.salary) : ""}${j.experience ? " · " + esc(j.experience) : ""}</div>
    ${j.reason ? `<div class="why">${esc(j.reason)}</div>` : ""}
    <div class="pills">
      <span class="pill">${esc(j.platform)}</span>
      <span class="pill">${esc(KIND[j.apply_kind] || j.apply_kind)}</span>
      ${j.note ? `<span class="pill">${esc(j.note)}</span>` : ""}
      ${j.scam_risk && j.scam_risk !== "low" ? `<span class="pill warn">${esc(j.scam_risk)} scam risk</span>` : ""}
    </div>
    ${bars(j.breakdown)}
    <div class="ats-box">${atsOf(j)}</div>
    <div class="actions">
      ${canApprove ? '<button class="btn primary" data-a="approve">Apply</button>' : ""}
      <button class="btn" data-ats>Check ATS</button>
      <a class="btn" href="/api/jobs/${encodeURIComponent(j.id)}/resume" target="_blank"
         title="the resume that will be sent: the edited copy if one was made, else yours">Resume</a>
      <a class="btn" href="${esc(j.url || "#")}" target="_blank" rel="noopener">Open</a>
      ${canApprove ? "" : '<button class="btn" data-a="manual">I applied</button>'}
      <button class="btn" data-a="skip">Skip</button>
    </div>`;
  el.appendChild(body);
  const sc = document.createElement("div");
  sc.className = "score" + (j.score >= 7 ? " hi" : j.score >= 6 ? " mid" : "");
  sc.textContent = j.score === null ? "–" : `${j.score}/10`;
  el.appendChild(sc);
  const atsBtn = body.querySelector("button[data-ats]");
  atsBtn.onclick = async () => {
    atsBtn.disabled = true;
    atsBtn.textContent = "Checking…";
    try {
      const r = await post(`/api/jobs/${encodeURIComponent(j.id)}/ats`);
      body.querySelector(".ats-box").innerHTML = atsBlock(r.ats, r.ats_after, r.record) ||
        `<div class="ats">${esc(r.log.join(" ") || "no keywords found in this posting")}</div>`;
      atsBtn.textContent = "Check ATS";
    } catch (e) { alert(e.message); atsBtn.textContent = "Check ATS"; }
    atsBtn.disabled = false;
  };
  body.querySelectorAll("button[data-a]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await post(`/api/jobs/${encodeURIComponent(j.id)}/${b.dataset.a}`);
        el.classList.add("gone");
        loadOverview();
      } catch (e) { alert(e.message); b.disabled = false; }
    };
  });
  return el;
}

let debounce = null;
$("q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(loadReview, 250); });
$("kind").addEventListener("change", loadReview);

// --- questions ------------------------------------------------------------------
async function loadQuestions() {
  const d = await api("/api/questions");
  const box = $("question-list");
  box.textContent = "";
  if (!d.questions.length) {
    box.innerHTML = '<div class="empty">No questions waiting.</div>';
    return;
  }
  for (const q of d.questions) {
    const el = document.createElement("div");
    el.className = "q";
    el.innerHTML = `
      <div class="qt">${esc(q.question)}</div>
      <div class="m">${q.blocked} job(s) waiting · field: ${esc(q.kind)}</div>
      ${q.options.length ? `<div class="opts">${q.options.map((o) =>
        `<button class="btn" data-o="${esc(o)}">${esc(o)}</button>`).join("")}</div>` : ""}
      <form><input name="a" placeholder="${q.options.length ? "or type an answer" : "your answer"}" autocomplete="off">
        <button class="btn primary">Save</button></form>`;
    const save = async (val) => {
      if (!val.trim()) return;
      try {
        const r = await post(`/api/questions/${encodeURIComponent(q.key)}`, { answer: val });
        el.innerHTML = `<div class="muted">Saved. ${r.released} job(s) back in the queue.</div>`;
        loadOverview();
      } catch (e) { alert(e.message); }
    };
    el.querySelectorAll("button[data-o]").forEach((b) => (b.onclick = () => save(b.dataset.o)));
    el.querySelector("form").onsubmit = (e) => { e.preventDefault(); save(e.target.a.value); };
    box.appendChild(el);
  }
}

// --- applied ----------------------------------------------------------------------
async function loadApplied(platform = "", box = $("app-list")) {
  const d = await api(`/api/applications?platform=${platform}`);
  box.textContent = "";
  if (!d.applications.length) {
    box.innerHTML = '<div class="empty">Nothing sent yet.</div>';
    return;
  }
  for (const a of d.applications) {
    const el = document.createElement("details");
    el.className = "app";
    el.innerHTML = `
      <summary><span class="res ${esc(a.result)}">${esc(a.result.replace("_", " "))}</span>
        <b>${esc(a.title || a.job_id)}</b><span class="m">${esc(a.company || "")} · ${esc(a.platform)} · ${esc(a.at)}</span></summary>
      ${a.note ? `<div class="m">${esc(a.note)}</div>` : ""}
      ${a.url ? `<div class="m"><a href="${esc(a.url)}" target="_blank" rel="noopener">open the posting</a></div>` : ""}
      ${a.qa.length ? `<ul class="qa">${a.qa.map((x) =>
        `<li>${esc(x.q)}<br><b>${esc(x.a)}</b> <span class="src">(${esc(x.from)})</span></li>`).join("")}</ul>` : ""}
      ${a.screenshot ? `<img class="shot" loading="lazy" alt="final page" src="/screens/${encodeURIComponent(a.screenshot)}">` : ""}`;
    box.appendChild(el);
  }
}

// --- browse -------------------------------------------------------------------------
async function loadBrowse() {
  const q = encodeURIComponent($("bq").value.trim());
  const status = $("bstatus").value;
  const d = await api(`/api/jobs?status=${status}&q=${q}&limit=3000`);
  const grid = $("grid");
  grid.textContent = "";
  for (const j of d.jobs) {
    const t = logoEl(j, "tile" + (j.score === null ? "" : " s" + Math.min(5, Math.max(1, Math.ceil(j.score / 2)))));
    t.title = `${j.title} — ${j.company} · ${j.platform} · ${j.status}` + (j.score !== null ? ` · ${j.score}/10` : "") +
      (j.reason ? `\n${j.reason}` : "");
    t.onclick = () => j.url && window.open(j.url, "_blank", "noopener");
    grid.appendChild(t);
  }
  $("gridnote").textContent = `${d.jobs.length} jobs · click a tile to open it`;
}
$("bq").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(loadBrowse, 250); });
$("bstatus").addEventListener("change", loadBrowse);

// --- sites ----------------------------------------------------------------------------
function renderSites() {
  const box = $("site-list");
  box.textContent = "";
  for (const s of overview.sites) {
    const pct = s.cap ? Math.min(100, (100 * s.applied_today) / s.cap) : 0;
    const el = document.createElement("div");
    el.className = "site";
    el.innerHTML = `
      <h3>${esc(s.name)}</h3>
      <div class="m">applied today ${s.applied_today}${s.cap ? " / " + s.cap : ""}</div>
      <div class="meter"><b style="width:${pct}%"></b></div>
      <div class="m">${s.auto ? `auto-apply at ${s.auto_min}+ · review at ${s.review_min}+` : `always review (${s.review_min}+)`}</div>
      <div class="m">last search: ${esc(s.last_discover || "never")}</div>
      ${s.logged_in === 0 ? '<div class="warn">not logged in — run <code>python main.py login</code></div>' : ""}
      ${s.paused_until ? `<div class="warn">paused until ${esc(s.paused_until)} (${esc(s.reason)})</div>
        <div class="actions"><button class="btn">Resume now</button></div>` : ""}`;
    const b = el.querySelector("button");
    if (b) b.onclick = async () => { await post(`/api/sites/${s.name}/resume`); loadOverview(); };
    box.appendChild(el);
  }
}

// --- one site: LinkedIn / Naukri / Unstop ----------------------------------------------
const SITE_LABEL = { linkedin: "LinkedIn", naukri: "Naukri", unstop: "Unstop" };
const ACTION_LABEL = { login: "log in", search: "search", dryrun: "test run", apply: "apply" };
let siteName = null, siteView = "review", siteTimer = null, siteLast = null;

async function loadSite(name) {
  if (siteName !== name) { siteName = name; siteLast = null; $("ps-log").textContent = ""; }
  clearTimeout(siteTimer);
  const s = await api(`/api/platform/${name}`);
  if (siteName !== name) return;
  $("ps-name").textContent = SITE_LABEL[name] || name;
  const login = s.logged_in === 0 ? '<span class="warn">not logged in - press Log in</span>'
    : s.logged_in === 1 ? "logged in" : "login not checked yet";
  $("ps-state").innerHTML = `${login} · applied today <b>${s.applied_today}</b>${s.cap ? " / " + s.cap : ""}` +
    (s.paused_until ? ` · <span class="warn">paused until ${esc(s.paused_until)} (${esc(s.reason)})</span>` : "");
  $("ps-meter").style.width = `${s.cap ? Math.min(100, (100 * s.applied_today) / s.cap) : 0}%`;
  $("ps-rules").textContent = (s.auto ? `applies by itself at ${s.auto_min}/10+, asks you at ${s.review_min}+`
    : `always asks you (${s.review_min}/10+)`) + ` · last search: ${s.last_discover || "never"}`;
  const kw = s.search.keywords || [], loc = s.search.locations || s.search.location || [];
  $("ps-search").textContent = kw.length ? `searches: ${kw.join(", ")}${loc.length ? " · in " + [].concat(loc).join(", ") : ""}` +
    " (change in config/settings.yaml)" : "";
  const c = s.counts;
  $("ps-nums").innerHTML = [["to approve", c.review], ["auto queue", c.queued + c.approved],
    ["needs answer", c.needs_input], ["applied", c.applied], ["found", c.total]]
    .map(([k, v]) => `<div><b>${v}</b><span>${k}</span></div>`).join("");

  // buttons: disabled while their run is going; the newest run's output in the log box
  let busy = false;
  document.querySelectorAll("button[data-site-action]").forEach((b) => {
    const a = s.actions[b.dataset.siteAction];
    b.disabled = a.running;
    busy = busy || a.running;
    if (a.running) siteLast = b.dataset.siteAction;
  });
  $("ps-login-done").classList.toggle("hidden", !(s.actions.login.running && s.login_waiting));
  if (siteLast) {
    const a = s.actions[siteLast];
    $("ps-log").textContent = `$ ${ACTION_LABEL[siteLast]}${a.running ? " (running…)" : ""}\n` + a.log.join("\n");
  }
  if (busy) siteTimer = setTimeout(() => current === `site-${name}` && loadSite(name), 2000);
  return loadSiteJobs();
}

async function loadSiteJobs() {
  const box = $("ps-list");
  if (siteView === "applied") return loadApplied(siteName, box);
  const q = encodeURIComponent($("ps-q").value.trim());
  const d = await api(`/api/jobs?platform=${siteName}&status=${siteView}&q=${q}&limit=300`);
  box.textContent = "";
  if (!d.jobs.length) {
    box.innerHTML = `<div class="empty">${siteView === "review" ? "Nothing waiting for you here." : "No jobs here yet."}</div>`;
    return;
  }
  for (const j of d.jobs) box.appendChild(reviewCard(j));
}

document.querySelectorAll("button[data-site-action]").forEach((b) => {
  b.onclick = async () => {
    const a = b.dataset.siteAction;
    if (a === "apply" && !confirm(`Apply now to up to 5 queued ${SITE_LABEL[siteName]} jobs? This really submits.`)) return;
    try {
      await post(`/api/actions/${a}-${siteName}`);
      siteLast = a;
      setTimeout(() => loadSite(siteName), 800);
    } catch (e) { alert(e.message); }
  };
});
$("ps-login-done").onclick = async () => {
  await post(`/api/platform/${siteName}/login-done`);
  setTimeout(() => loadSite(siteName), 1500);
};
$("ps-view").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    siteView = b.dataset.v;
    $("ps-view").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    loadSiteJobs();
  };
});
$("ps-q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(loadSiteJobs, 250); });

// --- live activity ----------------------------------------------------------------------
function startFeed() {
  const feed = $("feed");
  const es = new EventSource("/api/events");
  let first = true;
  es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (first) { feed.textContent = ""; first = false; }
    const li = document.createElement("li");
    const when = new Date(e.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    li.innerHTML = `<span class="when">${when}</span><span class="k ${esc(e.kind)}">${esc(e.kind)}</span>` +
      `${e.platform ? esc(e.platform) + " · " : ""}${esc(e.message)}`;
    feed.prepend(li);
    while (feed.children.length > 150) feed.lastChild.remove();
    if (["applied", "approve", "answer", "route", "pause"].includes(e.kind)) {
      loadOverview();
      if (current === `site-${e.platform}`) loadSite(e.platform);
    }
  };
}

// --- setup ----------------------------------------------------------------------------
let setupState = null;
function mark(id, ok, yes, no) {
  const el = $(id);
  el.textContent = ok ? `✓ ${yes}` : no;
  el.className = "ok" + (ok ? "" : " no");
}

async function loadSetup(keepProfile) {
  const s = (setupState = await api("/api/setup"));
  mark("key-ok", s.has_key, "saved", "needed");
  mark("resume-ok", !!s.resume, s.resume || "", "needed");
  mark("profile-ok", s.profile_exists && !!s.profile_name, `${s.profile_name || ""} · ${s.skills} skills`,
    s.profile_exists ? "fill in your name and skills" : "not created yet");
  mark("worker-ok", s.worker, "running", "stopped");
  $("st-key").classList.toggle("done", s.has_key);
  $("st-resume").classList.toggle("done", !!s.resume);
  $("st-profile").classList.toggle("done", s.profile_exists && !!s.profile_name);
  if (!keepProfile) $("profile-in").value = s.profile;
  const missing = [!s.has_key, !s.resume, !(s.profile_exists && s.profile_name)].filter(Boolean).length;
  $("b-setup").textContent = missing || "";
  const running = Object.entries(s.actions).filter(([, a]) => a.running).map(([k]) => k);
  const shown = running[0] || lastAction;
  if (shown && s.actions[shown]) {
    $("action-log").textContent = `$ ${shown}${s.actions[shown].running ? " (running…)" : ""}\n` +
      s.actions[shown].log.join("\n");
  }
  $("init-state").textContent = s.actions.init.running ? "drafting…" : "";
  if (running.length) setTimeout(() => current === "setup" && loadSetup(true), 2000);
  return s;
}

let lastAction = null;
document.querySelectorAll("button[data-action]").forEach((b) => {
  b.onclick = async () => {
    const name = b.dataset.action;
    if (name === "init" && setupState && setupState.profile_exists &&
        !confirm("Redraft profile.yaml from your resume? Your edits to it will be replaced.")) return;
    try {
      await post(`/api/actions/${name}`);
      lastAction = name;
      setTimeout(() => loadSetup(name !== "init"), 800);
    } catch (e) { alert(e.message); }
  };
});

$("key-form").onsubmit = async (e) => {
  e.preventDefault();
  const key = $("key-in").value.trim();
  if (!key) return;
  try {
    const r = await post("/api/setup/key", { key });
    $("key-in").value = "";
    alert(r.valid ? "Key saved and accepted by the provider." : `Key saved, but the provider rejected it (${r.detail}). Check it.`);
    loadSetup(true);
  } catch (err) { alert(err.message); }
};

$("resume-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = $("resume-in").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await api("/api/setup/resume", { method: "POST", body: fd });
    alert(`Uploaded ${r.saved} (${r.words} words).` + (r.warning ? `\n\n${r.warning}` : ""));
    loadSetup(true);
  } catch (err) { alert(err.message); }
};

$("profile-save").onclick = async () => {
  try {
    await post("/api/setup/profile", { text: $("profile-in").value });
    alert("Profile saved.");
    loadSetup(true);
  } catch (err) { alert(err.message); }
};

let start = "approve";
try { start = localStorage.getItem("tab") || "approve"; } catch {}
loadOverview().then(async () => {
  const s = await loadSetup();
  // Nothing works until the key and resume are in: open Setup first.
  show(!s.has_key || !s.resume ? "setup" : start);
});
startFeed();
setInterval(loadOverview, 30000);
