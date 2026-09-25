/* LabCare — mobile web app (complaints & breakdowns for lab equipment) */
"use strict";

// ---------------------------------------------------------------- Safe storage
// localStorage can throw (e.g. sandboxed iframes, private mode); degrade to memory.
const store = (() => {
  let mem = {};
  let ok = false;
  try {
    const t = "__labcare_test__";
    localStorage.setItem(t, "1");
    localStorage.removeItem(t);
    ok = true;
  } catch (e) { ok = false; }
  return {
    get(k) {
      try { return ok ? localStorage.getItem(k) : (k in mem ? mem[k] : null); }
      catch (e) { return k in mem ? mem[k] : null; }
    },
    set(k, v) {
      try { if (ok) localStorage.setItem(k, v); } catch (e) {}
      mem[k] = v;
    },
    remove(k) {
      try { if (ok) localStorage.removeItem(k); } catch (e) {}
      delete mem[k];
    },
  };
})();

// ---------------------------------------------------------------- API client
// The preview may run in a sandboxed iframe with an opaque origin: cookies
// can't be set and reverse proxies may strip the Authorization header. To keep
// sessions working everywhere we send the token through three channels at once
// (header, cookie, and ?token= query — the server accepts any of them).
function withToken(path, token) {
  const t = token != null ? token : API.token;
  if (!t) return path;
  const sep = path.includes("?") ? "&" : "?";
  return path + sep + "token=" + encodeURIComponent(t);
}

const API = {
  token: store.get("labcare_token"),
  async req(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    if (this.token) headers["Authorization"] = "Bearer " + this.token;
    // The backend may be waking from scale-to-zero (a few seconds). Retry
    // transient failures a couple of times so a cold-start blip never surfaces
    // as a full-page "Couldn't load …" error.
    let res, lastErr;
    let attempts = (method === "GET") ? 3 : 1;  // never repeat writes
    for (let i = 0; i < attempts; i++) {
      try {
        res = await fetch(withToken(path), {
          method,
          headers,
          credentials: "same-origin", // send the auth cookie (belt & suspenders)
          body: body ? JSON.stringify(body) : undefined,
        });
        lastErr = null;
        // only retry on gateway blips / unavailable-backend statuses
        if (!(res.status === 502 || res.status === 503 || res.status === 504) || i === attempts - 1) {
          break;
        }
      } catch (e) {
        lastErr = e;
      }
      await new Promise((r) => setTimeout(r, 600 * (i + 1)));
    }
    if (!res) {
      throw new Error(lastErr ? "Cannot reach the server. Check your internet connection." : "Backend not connected.");
    }
    // 502/503/504 = the frontend is up but the API backend behind it is not
    // (e.g. an unset/wrong netlify.toml /api proxy target).
    if (!res.ok || res.status >= 500) {
      if (res.status === 502 || res.status === 503 || res.status === 504) {
        throw new Error("Backend not connected (error " + res.status + "). The API server is unreachable — check that the /api proxy points to a running backend.");
      }
    }
    let data = {};
    const ct = (res.headers.get("content-type") || "");
    if (ct.includes("application/json")) {
      try { data = await res.json(); } catch (e) { /* keep {} */ }
    } else {
      // Netlify error page or proxy HTML — don't dump it into the toast
      data = { error: "Unexpected response from server (" + res.status + ")." };
    }
    if (!res.ok) {
      throw new Error((typeof data.error === "string" && data.error) || "Request failed (" + res.status + ")");
    }
    return data;
  },
  get(p) { return this.req("GET", p); },
  post(p, b) { return this.req("POST", p, b); },
  put(p, b) { return this.req("PUT", p, b); },
  patch(p, b) { return this.req("PATCH", p, b); },
  del(p) { return this.req("DELETE", p); },
};

// ---------------------------------------------------------------- State
const state = {
  user: null,
  view: "dashboard",
  history: [],
  // caches
  complaints: null,
  breakdowns: null,
  equipment: null,
  customers: null,
  users: null,
  locations: null,
  departments: null,
  orgTab: "customers",
  complaintFilter: "open",
  breakdownFilter: "open",
  pmFilter: "due",
  complaintDetail: null,
  breakdownDetail: null,
  signup: null,
  pm: null,
  portals: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

// Normalize a phone number into a dial-able tel: href (keep digits + leading +).
const telHref = (s) => "tel:" + String(s == null ? "" : s).replace(/[^\d+]/g, "");

// Build a WhatsApp click-to-chat link. Reporters often type a local number like
// "012-345 6789"; WhatsApp needs the full international form (60123456789).
// Default country code is Malaysia (+60): drop a leading 0 and prepend 60.
// Numbers that already carry a "+" or a longer country code are left untouched.
const waNumber = (s) => {
  let d = String(s == null ? "" : s).replace(/[^\d+]/g, ""); // keep digits + leading +
  if (!d) return "";
  if (d.startsWith("+")) return d.slice(1);                // +6012… -> 6012…
  if (d.length >= 9 && d.length <= 11 && d.startsWith("0")) d = "60" + d.slice(1); // 0112… / 012… -> 6011… / 6012…
  return d;
};
const waChatHref = (s) => { const n = waNumber(s); return n ? "https://wa.me/" + n : ""; };

const ROLE_LABELS = { admin: "Admin", engineer: "Engineer", application: "Application", customer: "Customer" };
const roleChip = (role) => `<span class="chip chip-${role === "admin" ? "admin" : (role === "engineer" || role === "application") ? "tech" : "cust"}">${ROLE_LABELS[role] || role}</span>`;
// a user's human-readable role (distinguishes Master admin from Tenant admin)
const roleLabel = (u) => {
  if (!u) return "";
  if (u.role === "admin") return (u.email || "").toLowerCase() === "admin@labcare.com" ? "Master admin" : "Tenant admin";
  return ROLE_LABELS[u.role] || u.role;
};
// coloured chip that separates Master admin, Tenant admin, Engineer and Customer
const roleBadge = (u) => {
  if (!u || !u.role) return "";
  if (u.role === "admin") {
    return (u.email || "").toLowerCase() === "admin@labcare.com"
      ? `<span class="chip chip-master">Master admin</span>`
      : `<span class="chip chip-tenant">Tenant admin</span>`;
  }
  return roleChip(u.role);
};

const STATUS_META = {
  open: { label: "Open", cls: "b-open" },
  in_progress: { label: "In Progress", cls: "b-in_progress" },
  resolved: { label: "Resolved", cls: "b-resolved" },
  closed: { label: "Closed", cls: "b-closed" },
  reported: { label: "Reported", cls: "b-reported" },
  diagnosed: { label: "Diagnosed", cls: "b-diagnosed" },
  on_hold: { label: "On Hold", cls: "b-on_hold" },
};
const PRIORITY_META = {
  low: { label: "Low", cls: "b-low" },
  medium: { label: "Medium", cls: "b-medium" },
  high: { label: "High", cls: "b-high" },
  critical: { label: "Critical", cls: "b-critical" },
};
const AUDIT_META = {
  created: { t: "Opened ticket", ico: "➕" },
  assigned: { t: "Changed assignee", ico: "👤" },
  status: { t: "Changed status", ico: "🔁" },
  accepted: { t: "Accepted", ico: "✔️" },
  resolution: { t: "Resolution", ico: "✅" },
  updated: { t: "Updated", ico: "✏️" },
  comment: { t: "Commented", ico: "💬" },
  attachment: { t: "Added file", ico: "📎" },
};
const badge = (type, value) => {
  const m = (type === "status" ? STATUS_META : PRIORITY_META)[value] || { label: value, cls: "" };
  return `<span class="badge ${m.cls}">${m.label}</span>`;
};

// ----------------------------------------------------------------------------
// Malaysia time (MYT = UTC+8, no daylight saving).
// Timestamps from the API (e.g. "2026-09-25 14:30:00") are stored in Malaysian
// wall-clock time. We parse them as MYT instants and always render in MYT, so a
// device in any timezone sees the same local time the record was created in.
const parseMYT = (s) => {
  if (!s) return null;
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/);
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3], h = +(m[4] || 0), mi = +(m[5] || 0), sec = +(m[6] || 0);
  return new Date(Date.UTC(y, mo - 1, d, h - 8, mi, sec)); // wall-clock MYT -> instant
};
const inMYT = (d, opts) => !d || isNaN(d) ? "—" : d.toLocaleString("en-MY", { timeZone: "Asia/Kuala_Lumpur", ...opts });
const mytHour = () => Number(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kuala_Lumpur", hour: "numeric", hour12: false }).format(new Date()));
const mytToday = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kuala_Lumpur", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const mytDatePlus = (days) => {
  const [y, mo, d] = mytToday().split("-").map(Number);
  return new Date(Date.UTC(y, mo - 1, d + days)).toISOString().slice(0, 10);
};

const fmtDate = (s) => {
  if (!s) return "—";
  const d = parseMYT(s);
  if (!d || isNaN(d)) return s;
  return inMYT(d, { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
};
const fmtDateShort = (s) => {
  if (!s) return "—";
  const d = parseMYT(s);
  if (!d || isNaN(d)) return s;
  return inMYT(d, { day: "numeric", month: "short", year: "numeric" });
};
const initials = (name) => (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const timeAgo = (s) => {
  if (!s) return "";
  const d = parseMYT(s);
  if (!d || isNaN(d)) return s;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + "d ago";
  return fmtDateShort(s);
};

// ---------------------------------------------------------------- Toasts & modals & loading
function toast(msg, type = "") {
  const t = document.createElement("div");
  t.className = "toast " + type;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

function showLoading() { $("#loading").classList.remove("hidden"); }
function hideLoading() { $("#loading").classList.add("hidden"); }

function openSheet(html) {
  const host = $("#modalHost");
  host.innerHTML = `<div class="sheet">${html}</div>`;
  host.classList.remove("hidden");
  host.querySelector(".sheet").addEventListener("click", (e) => e.stopPropagation());
}
function closeSheet() {
  $("#modalHost").classList.add("hidden");
  $("#modalHost").innerHTML = "";
}
$("#modalHost").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeSheet(); });

function confirmDialog(title, message, dangerText, onConfirm) {
  openSheet(`
    <div class="sheet-head"><h3>${esc(title)}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body"><p style="font-size:14.5px;color:var(--ink-soft)">${esc(message)}</p></div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn ${dangerText ? "btn-danger" : "btn-primary"}" id="confirmYes">${esc(dangerText || "Confirm")}</button>
    </div>
  `);
  $("#confirmYes").addEventListener("click", async () => {
    closeSheet();
    onConfirm && onConfirm();
  });
}

// ---------------------------------------------------------------- Auth
async function login(email, password) {
  const data = await API.post("/api/login", { email, password });
  API.token = data.token;
  store.set("labcare_token", data.token);   // best effort; the HttpOnly cookie
                                            // is the reliable fallback path
  state.user = data.user;
}

function logout() {
  API.post("/api/logout", {}).catch(() => {});
  API.token = null;
  store.remove("labcare_token");
  state.user = null;
  state.view = "dashboard";
  state.history = [];
  state.complaints = state.breakdowns = state.equipment = state.customers = state.users = null;
  render();
}

// ---------------------------------------------------------------- Router
const MAIN_VIEWS = ["dashboard", "complaints", "breakdowns", "equipment", "more"];

function navigate(view, params) {
  if (state.view !== view) {
    // only push history when drilling into a sub-view from a main view
    if (!MAIN_VIEWS.includes(view)) {
      state.history.push({ view: state.view, params: state.viewParams });
      if (state.history.length > 20) state.history.shift();
    } else {
      state.history = []; // switching main tabs resets the stack
    }
  }
  state.view = view;
  state.viewParams = params || {};
  render();
}

function goBack() {
  const prev = state.history.pop();
  if (prev) {
    state.view = prev.view;
    state.viewParams = prev.params || {};
  } else {
    state.view = "dashboard";
    state.viewParams = {};
  }
  render();
}

const isAdmin = () => state.user && state.user.role === "admin";
const isTech = () => state.user && (state.user.role === "engineer" || state.user.role === "application" || state.user.role === "admin");
const isCust = () => state.user && state.user.role === "customer";
const isMaster = () => state.user && state.user.role === "admin" && !state.user.customer_id && (state.user.email || "").toLowerCase() === "admin@labcare.com";
// staff who are not bound to a customer (master admin or a provider engineer)
const isUnboundStaff = () => state.user && (state.user.role === "admin" || state.user.role === "engineer" || state.user.role === "application") && !state.user.customer_id;
// cache of tenant admins per customer for the "Responsible tenant admin" picker
const _adminsCache = {};

// ---------------------------------------------------------------- Render root
function render() {
  const signedIn = !!state.user;
  $("#loginScreen").classList.toggle("hidden", signedIn);
  $("#app").classList.toggle("hidden", !signedIn);
  if (!signedIn) return;

  $("#avatarInitials").textContent = initials(state.user.name);
  const backVisible = state.history.length > 0;
  $("#backBtn").classList.toggle("hidden", !backVisible);

  // bottom nav active state
  const navViews = ["dashboard", "complaints", "breakdowns", "equipment", "more"];
  $$(".bn-item").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  const fabViews = isCust() ? ["dashboard", "complaints", "breakdowns"] : ["dashboard", "complaints", "breakdowns", "equipment", "pm"];
  $("#fab").classList.toggle("hidden", !fabViews.includes(state.view));

  // detail-like screens (single records) are kept to a readable width on desktop
  const narrowViews = ["complaintDetail", "breakdownDetail", "equipmentDetail", "customerDetail", "profile", "pmDetail"];
  $("#view").classList.toggle("narrow", narrowViews.includes(state.view));

  // titles
  const titles = {
    dashboard: "Dashboard", complaints: "Complaints", breakdowns: "Breakdowns",
    equipment: "Equipment", equipmentDetail: "Equipment", more: "Menu",
    customers: "Organizations", customerDetail: "Customer", users: "Team",
    complaintDetail: "Complaint", breakdownDetail: "Breakdown", profile: "My Account",
    pm: "Maintenance", pmDetail: "Maintenance", portals: "QR Portal",
    locations: "Organizations", departments: "Organizations", org: "Organizations",
    onboarding: "Join requests", categories: "Categories",
  };
  $("#tbTitle").textContent = titles[state.view] || "LabCare";

  renderView();
}

async function renderView() {
  const v = $("#view");
  switch (state.view) {
    case "dashboard": await viewDashboard(v); break;
    case "complaints": await viewComplaints(v); break;
    case "complaintDetail": await viewComplaintDetail(v); break;
    case "breakdowns": await viewBreakdowns(v); break;
    case "breakdownDetail": await viewBreakdownDetail(v); break;
    case "equipment": await viewEquipment(v); break;
    case "equipmentDetail": await viewEquipmentDetail(v); break;
    case "org": await viewOrg(v); break;
    case "customers": await viewOrg(v, "customers"); break;
    case "customerDetail": await viewCustomerDetail(v); break;
    case "users": await viewUsers(v); break;
    case "locations": await viewOrg(v, "locations"); break;
    case "departments": await viewOrg(v, "locations"); break;
    case "onboarding": await viewOnboarding(v); break;
    case "categories": await viewCategories(v); break;
    case "profile": viewProfile(v); break;
    case "pm": await viewPM(v); break;
    case "pmDetail": await viewPMDetail(v); break;
    case "portals": await viewPortals(v); break;
    case "more": viewMore(v); break;
    default: v.innerHTML = "";
  }
}

// ---------------------------------------------------------------- Dashboard
async function viewDashboard(v) {
  try {
    const d = await API.get("/api/dashboard");
    const c = d.counts;
    const nowHour = mytHour();
    const greet = nowHour < 12 ? "Good morning" : nowHour < 18 ? "Good afternoon" : "Good evening";
    const firstName = (state.user.name || "").split(" ")[0];

    v.innerHTML = `
      <div class="hero">
        <h2>${greet}, ${esc(firstName)} 👋</h2>
        <p>${isCust() ? "Track your equipment complaints and breakdowns below." : "Here's what needs your attention today."}</p>
      </div>

      <div class="section-title">At a glance <span style="font-weight:400;text-transform:none">· tap to open</span></div>
      <div class="stats-grid">
        <div class="stat tone-red clickable" onclick="goComplaints('active')"><span class="stat-num">${c.open_complaints}</span><span class="stat-label">Open complaints ›</span></div>
        <div class="stat tone-amber clickable" onclick="goBreakdowns('active')"><span class="stat-num">${c.open_breakdowns}</span><span class="stat-label">Active breakdowns ›</span></div>
        <div class="stat ${c.critical_complaints ? "tone-red" : "tone-green"} clickable" onclick="goComplaints('critical')"><span class="stat-num">${c.critical_complaints}</span><span class="stat-label">Critical complaints ›</span></div>
        <div class="stat tone-blue clickable" onclick="goEquipment()"><span class="stat-num">${c.total_equipment}</span><span class="stat-label">Equipment tracked ›</span></div>
      </div>

      ${!isCust() ? `
      <div class="section-title">Customer base &amp; maintenance</div>
      <div class="stats-grid">
        <div class="stat tone-brand"><span class="stat-num">${c.total_customers}</span><span class="stat-label">Customers</span></div>
        <div class="stat tone-green"><span class="stat-num">${c.resolved_complaints}</span><span class="stat-label">Resolved complaints</span></div>
        <div class="stat ${c.pm_due ? "tone-amber" : "tone-green"}" onclick="navigate('pm')" style="cursor:pointer"><span class="stat-num">${c.pm_due}</span><span class="stat-label">PM due</span></div>
        <div class="stat tone-blue" onclick="navigate('pm')" style="cursor:pointer"><span class="stat-num">${c.pm_total}</span><span class="stat-label">PM schedules</span></div>
      </div>` : ""}

      ${renderBarChart("Complaints — last 3 months", d.monthly_complaints, "month", "n", "var(--brand-2)")}
      ${renderHBars("Open complaints by priority", d.complaints_by_priority, "priority", prioColorKey)}
      ${!isCust() ? renderHBars("Most problem-prone equipment", d.top_equipment, "name", () => "#d97706") : ""}

      ${renderDashboardBreakdown("Complaints by status", d.complaints_by_status)}
      ${renderDashboardBreakdown("Breakdowns by status", d.breakdowns_by_status)}

      ${renderTimeline(d)}
    `;
  } catch (e) {
    v.innerHTML = `<div class="empty">
      <div class="e-ico">⚠️</div>
      <h3>Couldn't load dashboard</h3>
      <p>${esc(e.message)}</p>
      <button class="btn btn-primary-2 btn-sm" style="margin-top:12px" onclick="render()">↻ Retry</button>
    </div>`;
  }
}

const prioColorKey = {
  low: "#4338ca", medium: "#a16207", high: "#c2410c", critical: "#dc2626",
};
const prioLabelKey = { low: "Low", medium: "Medium", high: "High", critical: "Critical" };

function renderBarChart(title, rows, keyField, valField, color) {
  if (!rows || !rows.length) return "";
  const max = Math.max(...rows.map((r) => r[valField])) || 1;
  return `
    <div class="section-title">${esc(title)}</div>
    <div class="card">
      <div class="chart">
        ${rows.map((r) => `
          <div class="bar-col">
            <span class="bar-tick">${r[valField]}</span>
            <div class="bar" style="height:${Math.max(6, Math.round((r[valField] / max) * 76))}px;background:${color}"></div>
            <span class="bar-lab">${esc(monthShort(r[keyField]))}</span>
          </div>`).join("")}
      </div>
    </div>`;
}

function renderHBars(title, rows, keyField, colorFn) {
  if (!rows || !rows.length) return "";
  const max = Math.max(...rows.map((r) => r.n)) || 1;
  // colorFn may be a function (e.g. top equipment) or a key→color lookup
  // object (e.g. priority) — support both.
  const colorFor = typeof colorFn === "function"
    ? colorFn
    : (k) => (colorFn && colorFn[k]) || "#64748b";
  return `
    <div class="section-title">${esc(title)}</div>
    <div class="card">
      ${rows.map((r) => `
        <div class="hbar-row">
          <span class="hbar-lab">${esc(hbarLabel(keyField, r))}</span>
          <div class="hbar-track"><div class="hbar-fill" style="width:${Math.max(6, Math.round((r.n / max) * 100))}%;background:${colorFor(r[keyField])}"></div></div>
          <span class="hbar-num">${r.n}</span>
        </div>`).join("")}
    </div>`;
}

const monthShort = (m) => {
  if (!m) return "";
  const [y, mo] = String(m).split("-");
  return new Date(Number(y), Number(mo) - 1, 1).toLocaleDateString("en-MY", { month: "short" }) + " " + y.slice(2);
};
const hbarLabel = (key, r) => {
  if (key === "priority") return prioLabelKey[r.priority] || r.priority;
  if (key === "name") return r.name;
  return r[key];
};

function renderDashboardBreakdown(title, rows) {
  const total = rows.reduce((s, r) => s + r.n, 0) || 1;
  const bars = rows
    .map((r) => {
      const m = STATUS_META[r.status] || { label: r.status };
      const pct = Math.round((r.n / total) * 100);
      const color = r.status === "resolved" || r.status === "closed" ? "var(--ok)" : r.status === "open" || r.status === "reported" ? "var(--danger)" : "var(--info)";
      return `
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px">
            <span style="font-weight:600">${esc(m.label || r.status)}</span><span style="color:var(--ink-soft)">${r.n}</span>
          </div>
          <div style="height:8px;background:var(--bg);border-radius:99px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${color};border-radius:99px"></div>
          </div>
        </div>`;
    })
    .join("");
  if (!rows.length) bars = `<p style="color:var(--ink-soft);font-size:12.5px">No records yet.</p>`;
  return `
    <div class="section-title">${esc(title)}</div>
    <div class="card">${bars}</div>`;
}

function renderTimeline(d) {
  const items = [];
  (d.recent_complaints || []).slice(0, 4).forEach((r) => {
    items.push(`
      <li class="tl-item" onclick="navigate('complaintDetail',{id:${r.id}})">
        <div class="tl-title"><span style="color:var(--brand)">Complaint</span> · ${esc(r.subject)}</div>
        <div class="tl-time">${esc(r.code)} · ${timeAgo(r.created_at)} · ${badge("status", r.status)} ${badge("priority", r.priority)} ${r.accepted_by_name ? `<span class="badge b-accepted">✔ Accepted</span>` : ""}</div>
      </li>`);
  });
  (d.recent_breakdowns || []).slice(0, 4).forEach((r) => {
    items.push(`
      <li class="tl-item" onclick="navigate('breakdownDetail',{id:${r.id}})">
        <div class="tl-title"><span style="color:#d97706">Breakdown</span> · ${esc(truncate(r.fault_description, 70))}</div>
        <div class="tl-time">${esc(r.code)} · ${timeAgo(r.created_at)} · ${badge("status", r.status)} ${r.accepted_by_name ? `<span class="badge b-accepted">✔ Accepted</span>` : ""}</div>
      </li>`);
  });
  if (!items.length) items.push(`<li class="tl-item"><div class="tl-title">No recent activity</div></li>`);

  return `
    <div class="section-title">Recent activity</div>
    <div class="card"><ul class="timeline" style="padding-top:6px">${items.join("")}</ul></div>`;
}

const truncate = (s, n) => (s && s.length > n ? s.slice(0, n) + "…" : s);

// ---------------------------------------------------------------- Complaints list
async function viewComplaints(v) {
  const f = state.complaintFilter;
  v.innerHTML = `
    <div class="filter-row">
      ${["active", "open", "in_progress", "critical", "resolved", "closed", "all"].map((k) => `
        <button class="filter-chip ${f === k ? "active" : ""}" onclick="setComplaintFilter('${k}')">
          ${({ "active": "Active", "critical": "Critical", "all": "All" }[k] || (STATUS_META[k] || {}).label || k)}
        </button>`).join("")}
    </div>
    <div id="cmpList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  await refreshComplaints();
}

function setComplaintFilter(k) {
  state.complaintFilter = k;
  viewComplaints($("#view"));
}

// Dashboard "At a glance" shortcuts
function goComplaints(f) {
  state.history = [];
  state.complaintFilter = f;
  navigate("complaints");
}
function goBreakdowns(f) {
  state.history = [];
  state.breakdownFilter = f;
  navigate("breakdowns");
}
function goEquipment() {
  state.history = [];
  navigate("equipment");
}

async function refreshComplaints() {
  const box = $("#cmpList");
  try {
    let list = await API.get("/api/complaints");
    const f = state.complaintFilter;
    if (f === "active") list = list.filter((c) => c.status === "open" || c.status === "in_progress");
    else if (f === "critical") list = list.filter((c) => c.priority === "critical" && (c.status === "open" || c.status === "in_progress"));
    else if (f !== "all") list = list.filter((c) => c.status === f);
    state.complaints = list;
    box.innerHTML = list.length
      ? `<div class="list">${list.map(complaintCard).join("")}</div>`
      : emptyState("📭", "No complaints here", 'Tap the ＋ button to log a new complaint.', isCust() ? "Log complaint" : "New complaint");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function complaintCard(c) {
  const passed = isCust() ? "" : `<span class="mono">${esc(c.code)}</span>`;
  const scope = Array.from(new Set([c.location_name, c.department_name].filter(Boolean))).join(" · ");
  return `
    <div class="item" onclick="navigate('complaintDetail',{id:${c.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title">${esc(c.subject)}</div>
          <div class="item-sub">${isCust() ? "" : "<b>" + esc(c.customer_name || "") + "</b> · "}${esc(c.equipment_name || "General")}</div>
          ${scope ? `<div class="item-sub">📍 ${esc(scope)}</div>` : ""}
        </div>
      </div>
      <div class="item-meta">
        ${passed}
        ${badge("status", c.status)}
        ${badge("priority", c.priority)}
        ${c.accepted_by_name ? `<span class="badge b-accepted">✔ Accepted</span>` : ""}
        <span class="item-time">${timeAgo(c.created_at)}</span>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- Complaint detail
async function viewComplaintDetail(v) {
  const id = state.viewParams.id;
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const c = await API.get("/api/complaints/" + id);
    state.complaintDetail = c;
    v.innerHTML = complaintDetailHtml(c);
    loadPhotos("complaint", id);
    loadHistory("complaint", id);
  } catch (e) {
    v.innerHTML = `<div class="empty"><div class="e-ico">⚠️</div><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function complaintDetailHtml(c) {
  const canEdit = isTech() || isCust();
  const statusActions = statusButtons("complaint", c);
  const locDept = Array.from(new Set([c.location_name, c.department_name].filter(Boolean))).join(" · ");

  return `
    <div class="detail-head">
      <button class="back-link" onclick="goBack()">‹ Back</button>
      <div class="card" style="margin-top:8px">
        <div class="item-meta" style="margin:0 0 8px">
          <span class="mono">${esc(c.code)}</span>
          ${badge("status", c.status)}
          ${badge("priority", c.priority)}
        </div>
        <h2 style="font-size:17px;line-height:1.35">${esc(c.subject)}</h2>
        <div class="item-sub" style="margin-top:6px">
          ${esc(c.customer_name || "")} · ${esc(c.equipment_name || "General equipment")}
        </div>
      </div>
    </div>

    <div class="section-title">Details</div>
    <div class="card">
      <div class="kv"><span class="k">Category</span><span class="v">${esc(c.category || "General")}</span></div>
      <div class="kv"><span class="k">Location/Department</span><span class="v">${esc(locDept || "—")}</span></div>
      <div class="kv"><span class="k">Opened by</span><span class="v">${esc(c.reporter_name || c.created_by_name || "—")}</span></div>
      ${c.reporter_phone ? `<div class="kv"><span class="k">Contact</span><span class="v phone-actions"><a class="wa-link" href="${waChatHref(c.reporter_phone)}" target="_blank" rel="noopener">💬 WhatsApp ${esc(c.reporter_phone)}</a><a class="tel-link" href="${telHref(c.reporter_phone)}">📞</a></span></div>` : ""}
      <div class="kv"><span class="k">Assigned to</span><span class="v">${esc(c.assigned_to_name || "Unassigned")}</span></div>
      ${c.accepted_by_name ? `<div class="kv"><span class="k">Accepted by</span><span class="v">${esc(c.accepted_by_name)}${c.accepted_at ? " · " + fmtDate(c.accepted_at) : ""}</span></div>` : ""}
      ${c.accept_reply ? `<div class="kv"><span class="k">Reply to sender</span><span class="v">“${esc(c.accept_reply)}”</span></div>` : ""}
      ${c.responsible_admin_name ? `<div class="kv"><span class="k">Tenant admin in charge</span><span class="v">${esc(c.responsible_admin_name)}</span></div>` : ""}
      ${c.closed_by_name ? `<div class="kv"><span class="k">${c.status === "closed" ? "Closed by" : "Resolved by"}</span><span class="v">${esc(c.closed_by_name)}</span></div>` : ""}
      <div class="kv"><span class="k">Created</span><span class="v">${fmtDate(c.created_at)}</span></div>
      ${c.resolved_at ? `<div class="kv"><span class="k">Resolved</span><span class="v">${fmtDate(c.resolved_at)}</span></div>` : ""}
    </div>

    <div class="section-title">Description</div>
    <div class="card"><div class="desc-box">${esc(c.description || "No description provided.")}</div></div>

    <div class="section-title">Actions</div>
    <div class="card">
      <div class="action-panel">
        ${statusActions}
      </div>
      ${canEdit ? `<div class="action-panel" style="margin-top:10px">
        <button class="btn btn-ghost btn-sm" onclick="openComplaintEditor(true)">✏️ Edit</button>
      </div>` : ""}
      ${isMaster() ? `<div class="action-panel" style="margin-top:10px">
        <button class="btn btn-danger btn-sm" onclick="deleteTicket('complaint',${c.id})">🗑 Delete ticket</button>
      </div>` : ""}
    </div>

    <div class="section-title">Photos &amp; files</div>
    <div class="card" id="photosBox"><div class="empty" style="padding:12px"><div class="spinner" style="margin:0 auto"></div></div></div>

    <div class="section-title">History</div>
    <div class="card" id="historyBox"><div class="empty" style="padding:12px"><div class="spinner" style="margin:0 auto"></div></div></div>

    <div class="section-title">Conversation</div>
    <div class="card" id="commentsBox">
      ${(c.comments || []).map(commentHtml).join("") || '<p style="color:var(--ink-soft);font-size:13px">No comments yet.</p>'}
      <div style="display:flex;gap:8px;margin-top:12px">
        <input id="commentInput" placeholder="Add a comment…" onkeydown="if(event.key==='Enter')addComment()">
        <button class="btn btn-primary btn-sm" onclick="addComment()">Send</button>
      </div>
    </div>

    <div class="section-title">Report</div>
    <div class="card">
      <div class="action-panel">
        <button class="btn btn-ghost btn-sm" onclick="downloadReport('/api/complaints/${c.id}/report.pdf')">📄 Service report (PDF)</button>
      </div>
    </div>`;
}

function commentHtml(cm) {
  return `
    <div class="comment">
      <div class="c-avatar">${esc(initials(cm.user_name))}</div>
      <div class="c-body">
        <div class="c-head"><span class="c-name">${esc(cm.user_name)}</span><span class="c-time">${timeAgo(cm.created_at)}</span></div>
        <div class="c-text">${esc(cm.text)}</div>
      </div>
    </div>`;
}

async function addComment() {
  const input = $("#commentInput");
  const text = (input.value || "").trim();
  if (!text) return;
  const c = state.complaintDetail;
  const entity = state.view === "complaintDetail" ? "complaint" : "breakdown";
  const id = state.viewParams.id;
  showLoading();
  try {
    await API.post("/api/comments", { entity_type: entity, entity_id: id, text });
    input.value = "";
    await viewComplaintDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

function statusButtons(entity, rec) {
  const b = [];
  if (entity === "complaint") {
    if (isTech() && rec.status === "open") {
      if (!rec.accepted_by) {
        b.push(`<button class="btn btn-primary-2 btn-sm" onclick="openAcceptSheet(${rec.id})">✔ Accept</button>`);
      }
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="setComplaintStatus(${rec.id},'in_progress')">▶ Start work</button>`);
      if (!rec.accepted_by) {
        b.push(`<button class="btn btn-ghost btn-sm" onclick="openAssignSheet('complaint',${rec.id})">👤 Assign</button>`);
      }
    }
    if (isTech() && rec.status === "in_progress") {
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="setComplaintStatus(${rec.id},'resolved')">✔ Mark resolved</button>`);
    }
    if (rec.status === "resolved" && (isTech() || isCust())) {
      b.push(`<button class="btn btn-ghost btn-sm" onclick="setComplaintStatus(${rec.id},'closed')">Close complaint</button>`);
    }
    if ((rec.status === "resolved" || rec.status === "closed") && isTech()) {
      b.push(`<button class="btn btn-ghost btn-sm" onclick="setComplaintStatus(${rec.id},'open')">↺ Reopen</button>`);
    }
    if (isTech() && ["open", "in_progress"].includes(rec.status)) {
      b.push(`<button class="btn btn-ghost btn-sm" onclick="linkBreakdown(${rec.id})">⚠ Create breakdown</button>`);
    }
  } else {
    // breakdown statuses
    if (isTech() && ["reported", "diagnosed", "in_progress", "on_hold"].includes(rec.status) && !rec.accepted_by) {
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="openBreakdownAcceptSheet(${rec.id})">✔ Accept</button>`);
    }
    if (isTech() && rec.status === "reported") {
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="setBreakdownStatus(${rec.id},'diagnosed')">🔍 Diagnosing</button>`);
      if (!rec.accepted_by) {
        b.push(`<button class="btn btn-ghost btn-sm" onclick="openAssignSheet('breakdown',${rec.id})">👤 Assign</button>`);
      }
    }
    if (isTech() && rec.status === "diagnosed") {
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="setBreakdownStatus(${rec.id},'in_progress')">🛠 Repair in progress</button>`);
    }
    if (isTech() && ["reported", "diagnosed", "in_progress"].includes(rec.status)) {
      b.push(`<button class="btn btn-ghost btn-sm" onclick="setBreakdownStatus(${rec.id},'on_hold')">⏸ On hold</button>`);
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="openResolveSheet(${rec.id})">✔ Resolve</button>`);
    }
    if (isTech() && rec.status === "on_hold") {
      b.push(`<button class="btn btn-primary-2 btn-sm" onclick="setBreakdownStatus(${rec.id},'in_progress')">▶ Resume</button>`);
      b.push(`<button class="btn btn-ghost btn-sm" onclick="openResolveSheet(${rec.id})">✔ Resolve</button>`);
    }
  }
  if (rec.accepted_by_name) {
    b.unshift(`<div class="accept-banner">✔ Accepted by <b>${esc(rec.accepted_by_name)}</b>${rec.accepted_at ? ` · ${fmtDate(rec.accepted_at)}` : ""}</div>`);
  }
  if (!b.length) return `<p style="color:var(--ink-soft);font-size:13px">No actions available for the current status${isCust() ? " — the support team will update this" : ""}.</p>`;
  return b.join("");
}

async function setComplaintStatus(id, status) {
  showLoading();
  try {
    await API.patch("/api/complaints/" + id, { status });
    toast("Complaint updated", "success");
    state.complaints = null;
    await viewComplaintDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

function openAcceptSheet(id) {
  const c = state.complaintDetail;
  const cur = c.status || "open";
  const opts = ["open", "in_progress", "resolved", "closed"]
    .map((s) => `<option value="${s}" ${s === cur ? "selected" : ""}>${["Open", "In Progress", "Resolved", "Closed"][["open", "in_progress", "resolved", "closed"].indexOf(s)]}</option>`).join("");
  openSheet(`
    <div class="sheet-head"><h3>Accept complaint</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <p style="font-size:14px;color:var(--ink-soft);margin:0 0 4px">
        Accepting <b>${esc(c.code)}</b> tells the reporter (via the QR portal) that
        <b>${esc(state.user?.name || "you")}</b> has taken it on and will show your reply
        when they re-scan the QR code.
      </p>
      <label class="field" style="margin-top:12px"><span>Status after accepting</span>
        <select id="acceptStatus">${opts}</select></label>
      <label class="field"><span>Reply to the sender (optional — shown in the portal)</span>
        <textarea id="acceptReply" placeholder="e.g. We have received your report and an engineer will contact you today."></textarea></label>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="submitAccept(${id})">✔ Accept &amp; notify sender</button>
    </div>`);
}

async function submitAccept(id) {
  const reply = $("#acceptReply") ? $("#acceptReply").value.trim() : "";
  const status = $("#acceptStatus") ? $("#acceptStatus").value : "";
  closeSheet();
  showLoading();
  try {
    await API.post("/api/complaints/" + id + "/accept", { reply, status });
    toast("Complaint accepted — the sender has been notified", "success");
    state.complaints = null;
    await viewComplaintDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

function openBreakdownAcceptSheet(id) {
  const b = state.breakdownDetail;
  const cur = b.status || "reported";
  const labels = { reported: "Reported", diagnosed: "Diagnosed", in_progress: "In Progress", on_hold: "On Hold", resolved: "Resolved" };
  const opts = ["reported", "diagnosed", "in_progress", "on_hold", "resolved"]
    .map((s) => `<option value="${s}" ${s === cur ? "selected" : ""}>${labels[s]}</option>`).join("");
  openSheet(`
    <div class="sheet-head"><h3>Accept breakdown</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <p style="font-size:14px;color:var(--ink-soft);margin:0 0 4px">
        Accepting <b>${esc(b.code)}</b> tells the reporter (via the QR portal) that
        <b>${esc(state.user?.name || "you")}</b> has taken it on and will show your reply
        when they re-scan the QR code. You will also be assigned this breakdown.
      </p>
      <label class="field" style="margin-top:12px"><span>Status after accepting</span>
        <select id="acceptStatus">${opts}</select></label>
      <label class="field"><span>Reply to the sender (optional — shown in the portal)</span>
        <textarea id="acceptReply" placeholder="e.g. We have received your report and an engineer will contact you today."></textarea></label>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="submitBreakdownAccept(${id})">✔ Accept &amp; notify sender</button>
    </div>`);
}

async function submitBreakdownAccept(id) {
  const reply = $("#acceptReply") ? $("#acceptReply").value.trim() : "";
  const status = $("#acceptStatus") ? $("#acceptStatus").value : "";
  closeSheet();
  showLoading();
  try {
    await API.post("/api/breakdowns/" + id + "/accept", { reply, status });
    toast("Breakdown accepted — the sender has been notified", "success");
    state.breakdowns = null;
    await viewBreakdownDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function setBreakdownStatus(id, status) {
  showLoading();
  try {
    await API.patch("/api/breakdowns/" + id, { status });
    toast("Breakdown updated", "success");
    state.breakdowns = null;
    await viewBreakdownDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function deleteTicket(entity, id) {
  confirmDialog(
    `Delete ${entity}?`,
    "This permanently deletes the ticket and all its comments, photos and activity. This cannot be undone.",
    "Delete",
    async () => {
      showLoading();
      try {
        await API.del(`/api/${entity === "complaint" ? "complaints" : "breakdowns"}/` + id);
        toast(entity === "complaint" ? "Complaint deleted" : "Breakdown deleted", "success");
        state.complaints = state.breakdowns = null;
        state.history = [];
        navigate(entity === "complaint" ? "complaints" : "breakdowns");
      } catch (e) { toast(e.message, "error"); }
      hideLoading();
    }
  );
}

async function openAssignSheet(entity, id) {
  let techs = [];
  try { techs = await API.get("/api/engineers"); } catch (e) {}
  openSheet(`
    <div class="sheet-head"><h3>Assign to engineer</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body" style="padding-bottom:24px">
      ${techs.map((t) => `
        <button class="menu-item" onclick="assignTech('${entity}',${id},${t.id})">
          <span class="c-avatar">${esc(initials(t.name))}</span> ${esc(t.name)}
          <span class="mi-arrow">›</span>
        </button>`).join("") || "<p>No engineers found.</p>"}
    </div>`);
}

async function assignTech(entity, id, techId) {
  closeSheet();
  showLoading();
  try {
    if (entity === "complaint") await API.patch("/api/complaints/" + id, { assigned_to: techId });
    else await API.patch("/api/breakdowns/" + id, { assigned_to: techId });
    toast("Assigned", "success");
    state.view === "complaintDetail" ? await viewComplaintDetail($("#view")) : await viewBreakdownDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function linkBreakdown(complaintId) {
  const c = state.complaintDetail;
  openBreakdownEditor(true, { complaint_id: complaintId, customer_id: c.customer_id, equipment_id: c.equipment_id, subject: c.subject });
}

// ---------------------------------------------------------------- Breakdowns list
async function viewBreakdowns(v) {
  const f = state.breakdownFilter;
  v.innerHTML = `
    <div class="filter-row">
      ${["active", "reported", "diagnosed", "in_progress", "on_hold", "resolved", "all"].map((k) => `
        <button class="filter-chip ${f === k ? "active" : ""}" onclick="setBreakdownFilter('${k}')">
          ${({ "active": "Active", "all": "All" }[k] || (STATUS_META[k] || {}).label || k)}
        </button>`).join("")}
    </div>
    <div id="brkList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  await refreshBreakdowns();
}

function setBreakdownFilter(k) {
  state.breakdownFilter = k;
  viewBreakdowns($("#view"));
}

async function refreshBreakdowns() {
  const box = $("#brkList");
  try {
    let list = await API.get("/api/breakdowns");
    const f = state.breakdownFilter;
    if (f === "active") list = list.filter((b) => b.status !== "resolved");
    else if (f !== "all") list = list.filter((b) => b.status === f);
    state.breakdowns = list;
    box.innerHTML = list.length
      ? `<div class="list">${list.map(breakdownCard).join("")}</div>`
      : emptyState("🔧", "No breakdowns here", "Equipment faults appear here when reported.", "Report breakdown");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function breakdownCard(b) {
  const passed = isCust() ? "" : `<span class="mono">${esc(b.code)}</span>`;
  return `
    <div class="item" onclick="navigate('breakdownDetail',{id:${b.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title">${esc(b.equipment_name || "Equipment")}</div>
          <div class="item-sub">${esc(truncate(b.fault_description, 90))}</div>
        </div>
      </div>
      <div class="item-meta">
        ${passed}
        ${badge("status", b.status)}
        ${badge("priority", b.priority)}
        ${b.accepted_by_name ? `<span class="badge b-accepted">✔ Accepted</span>` : ""}
        <span class="item-time">${timeAgo(b.created_at)}</span>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- Breakdown detail
async function viewBreakdownDetail(v) {
  const id = state.viewParams.id;
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const b = await API.get("/api/breakdowns/" + id);
    state.breakdownDetail = b;
    v.innerHTML = breakdownDetailHtml(b);
    loadPhotos("breakdown", id);
    loadHistory("breakdown", id);
  } catch (e) {
    v.innerHTML = `<div class="empty"><div class="e-ico">⚠️</div><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function breakdownDetailHtml(b) {
  const statusActions = statusButtons("breakdown", b);
  return `
    <div class="detail-head">
      <button class="back-link" onclick="goBack()">‹ Back</button>
      <div class="card" style="margin-top:8px">
        <div class="item-meta" style="margin:0 0 8px">
          <span class="mono">${esc(b.code)}</span>
          ${badge("status", b.status)}
          ${badge("priority", b.priority)}
        </div>
        <h2 style="font-size:17px;line-height:1.35">${esc(b.equipment_name || "Equipment")}</h2>
        <div class="item-sub" style="margin-top:6px">${esc(b.customer_name || "")}</div>
      </div>
    </div>

    <div class="section-title">Fault description</div>
    <div class="card"><div class="desc-box">${esc(b.fault_description)}</div></div>

    <div class="section-title">Details</div>
    <div class="card">
      ${b.complaint_id ? `<div class="kv"><span class="k">Source complaint</span><span class="v" style="color:var(--brand);text-decoration:underline" onclick="navigate('complaintDetail',{id:${b.complaint_id}})">${esc("View")}</span></div>` : ""}
      <div class="kv"><span class="k">Opened by</span><span class="v">${esc(b.reported_by_name || "—")}</span></div>
      ${b.reporter_name ? `<div class="kv"><span class="k">Reporter</span><span class="v">${esc(b.reporter_name)}</span></div>` : ""}
      ${b.reporter_phone ? `<div class="kv"><span class="k">Contact</span><span class="v phone-actions"><a class="wa-link" href="${waChatHref(b.reporter_phone)}" target="_blank" rel="noopener">💬 WhatsApp ${esc(b.reporter_phone)}</a><a class="tel-link" href="${telHref(b.reporter_phone)}">📞</a></span></div>` : ""}
      <div class="kv"><span class="k">Assigned to</span><span class="v">${esc(b.assigned_to_name || "Unassigned")}</span></div>
      ${b.accepted_by_name ? `<div class="kv"><span class="k">Accepted by</span><span class="v">${esc(b.accepted_by_name)}${b.accepted_at ? " · " + fmtDate(b.accepted_at) : ""}</span></div>` : ""}
      ${b.accept_reply ? `<div class="kv"><span class="k">Reply to sender</span><span class="v">“${esc(b.accept_reply)}”</span></div>` : ""}
      ${b.responsible_admin_name ? `<div class="kv"><span class="k">Tenant admin in charge</span><span class="v">${esc(b.responsible_admin_name)}</span></div>` : ""}
      ${b.closed_by_name ? `<div class="kv"><span class="k">Resolved by</span><span class="v">${esc(b.closed_by_name)}</span></div>` : ""}
      <div class="kv"><span class="k">Reported</span><span class="v">${fmtDate(b.created_at)}</span></div>
      ${b.resolved_at ? `<div class="kv"><span class="k">Resolved</span><span class="v">${fmtDate(b.resolved_at)}</span></div>` : ""}
      ${b.root_cause ? `<div class="kv"><span class="k">Root cause</span><span class="v">${esc(b.root_cause)}</span></div>` : ""}
    </div>

    ${b.resolution_notes ? `
    <div class="section-title">Resolution notes</div>
    <div class="card"><div class="desc-box">${esc(b.resolution_notes)}</div></div>` : ""}

    <div class="section-title">Actions</div>
    <div class="card">
      <div class="action-panel">${statusActions}</div>
      ${isTech() ? `<div class="action-panel" style="margin-top:10px">
        <button class="btn btn-ghost btn-sm" onclick="openBreakdownEditor(true)">✏️ Edit</button>
      </div>` : ""}
      ${isMaster() ? `<div class="action-panel" style="margin-top:10px">
        <button class="btn btn-danger btn-sm" onclick="deleteTicket('breakdown',${b.id})">🗑 Delete ticket</button>
      </div>` : ""}
    </div>

    <div class="section-title">Photos &amp; files</div>
    <div class="card" id="photosBox"><div class="empty" style="padding:12px"><div class="spinner" style="margin:0 auto"></div></div></div>

    <div class="section-title">History</div>
    <div class="card" id="historyBox"><div class="empty" style="padding:12px"><div class="spinner" style="margin:0 auto"></div></div></div>

    <div class="section-title">Work log</div>
    <div class="card" id="commentsBox">
      ${(b.comments || []).map(commentHtml).join("") || '<p style="color:var(--ink-soft);font-size:13px">No updates yet.</p>'}
      <div style="display:flex;gap:8px;margin-top:12px">
        <input id="commentInput" placeholder="Add an update…" onkeydown="if(event.key==='Enter')addBrokComment()">
        <button class="btn btn-primary btn-sm" onclick="addBrokComment()">Send</button>
      </div>
    </div>`;
}

async function addBrokComment() {
  const input = $("#commentInput");
  const text = (input.value || "").trim();
  if (!text) return;
  const id = state.viewParams.id;
  showLoading();
  try {
    await API.post("/api/comments", { entity_type: "breakdown", entity_id: id, text });
    input.value = "";
    await viewBreakdownDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

function openResolveSheet(id) {
  openSheet(`
    <div class="sheet-head"><h3>Resolve breakdown</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Root cause</span><input id="resRootCause" placeholder="e.g. Failed power supply unit"></label>
      <label class="field"><span>Resolution notes</span><textarea id="resNotes" placeholder="What was done to fix it?"></textarea></label>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="confirmResolve(${id})">Mark resolved</button>
    </div>`);
}

async function confirmResolve(id) {
  const root_cause = $("#resRootCause").value.trim();
  const resolution_notes = $("#resNotes").value.trim();
  closeSheet();
  showLoading();
  try {
    await API.patch("/api/breakdowns/" + id, { status: "resolved", root_cause, resolution_notes });
    toast("Breakdown resolved", "success");
    state.breakdowns = null;
    await viewBreakdownDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

// ---------------------------------------------------------------- Equipment
async function viewEquipment(v) {
  v.innerHTML = `
    ${isTech() ? `<div class="btn-row" style="margin-bottom:12px">
      <button class="btn btn-primary" onclick="openEquipmentEditor(false)">＋ Add equipment</button>
    </div>` : ""}
    <div id="eqList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  await refreshEquipment();
}

async function refreshEquipment() {
  const box = $("#eqList");
  try {
    const list = await API.get("/api/equipment");
    state.equipment = list;
    box.innerHTML = list.length
      ? renderEquipmentGrouped(list)
      : emptyState("⚙️", "No equipment", "Register lab equipment to start linking complaints.", "Add equipment");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

// Group equipment by Location/Department — each asset identified by its
// own serial number within its location/department.
function renderEquipmentGrouped(list) {
  const groups = new Map();
  for (const e of list) {
    const loc = e.location_name || e.department_name || "No location/department";
    const key = loc;
    if (!groups.has(key)) groups.set(key, { loc, items: [] });
    groups.get(key).items.push(e);
  }
  let html = "";
  for (const g of groups.values()) {
    html += `
      <div class="section-title" style="margin-top:14px">📍 ${esc(g.loc)} <span style="color:var(--ink-soft);font-weight:400">(${g.items.length})</span></div>
      <div class="list">${g.items.map(equipmentCard).join("")}</div>`;
  }
  return html;
}

function equipmentCard(e) {
  const warranty = e.warranty_expiry ? parseMYT(e.warranty_expiry) : null;
  const expired = warranty && warranty < new Date();
  const nearExp = warranty && !expired && (warranty - new Date()) < 1000 * 60 * 60 * 24 * 90;
  const scope = isCust()
    ? ""
    : `${esc(e.customer_name || "")} · ${esc(e.location_name || e.department_name || "—")}`;
  return `
    <div class="item" onclick="navigate('equipmentDetail',{id:${e.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title">${esc(e.name)}</div>
          <div class="item-sub mono">S/N ${esc(e.serial_number || "not assigned")} · ${esc(e.model || "No model")}</div>
          ${scope ? `<div class="item-sub">${scope}</div>` : ""}
        </div>
      </div>
      <div class="item-meta">
        ${e.category ? `<span class="badge" style="background:var(--bg);color:var(--ink-soft)">${esc(e.category)}</span>` : ""}
        ${warranty ? `<span class="badge ${expired ? "b-closed" : nearExp ? "b-high" : "b-resolved"}">${expired ? "Warranty expired" : nearExp ? "Warranty expiring" : "In warranty"}</span>` : ""}
        ${e.status === "retired" ? `<span class="badge b-closed">Retired</span>` : ""}
      </div>
    </div>`;
}

// equipment detail inline view
async function viewEquipmentDetail(v) {
  const id = state.viewParams.id;
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const list = await API.get("/api/equipment");
    state.equipment = list;
    const e = list.find((x) => x.id === id);
    if (!e) throw new Error("Equipment not found");
    v.innerHTML = `
      <div class="detail-head">
        <button class="back-link" onclick="goBack()">‹ Back</button>
        <div class="card" style="margin-top:8px">
          <h2 style="font-size:18px">${esc(e.name)}</h2>
          <div class="item-sub">${esc(e.model || "—")}${e.serial_number ? " · S/N " + esc(e.serial_number) : ""}</div>
          <div class="item-meta" style="margin-top:8px">
            ${e.category ? `<span class="badge" style="background:var(--bg);color:var(--ink-soft)">${esc(e.category)}</span>` : ""}
            ${e.status === "retired" ? '<span class="badge b-closed">Retired</span>' : '<span class="badge b-resolved">Active</span>'}
          </div>
        </div>
      </div>
      <div class="section-title">Asset details</div>
      <div class="card">
        <div class="kv"><span class="k">Customer</span><span class="v">${esc(e.customer_name || "—")}</span></div>
        <div class="kv"><span class="k">Location/Department</span><span class="v">${esc(e.location_name || e.department_name || "—")}</span></div>
        ${e.responsible_admin_name ? `<div class="kv"><span class="k">Tenant admin in charge</span><span class="v">${esc(e.responsible_admin_name)}</span></div>` : ""}
        <div class="kv"><span class="k">Installed</span><span class="v">${fmtDateShort(e.installed_date)}</span></div>
        <div class="kv"><span class="k">Warranty until</span><span class="v">${fmtDateShort(e.warranty_expiry)}</span></div>
      </div>
      ${e.notes ? `<div class="section-title">Notes</div><div class="card"><div class="desc-box">${esc(e.notes)}</div></div>` : ""}
      ${isTech() ? `<div class="action-panel" style="margin-top:14px"><button class="btn btn-ghost" onclick="openEquipmentEditor(true)">✏️ Edit</button></div>` : ""}
    `;
  } catch (err) {
    v.innerHTML = `<div class="empty"><h3>Could not load</h3><p>${esc(err.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------- Organizations (Customers + Locations/Departments)
async function viewOrg(v, tab) {
  if (tab) state.orgTab = tab;
  if (state.orgTab === "departments") state.orgTab = "locations";
  const t = state.orgTab;
  const tabs = isAdmin() || isTech()
    ? [["customers", "🏢 Customers"], ["locations", "📍 Location/Department"]]
    : [["customers", "🏢 My organisation"]];
  const addBtn = t === "customers"
    ? (isAdmin() ? `<button class="btn btn-primary" onclick="openCustomerEditor(false)">＋ Add customer</button>` : "")
    : (isTech() ? `<button class="btn btn-primary" onclick="openLocationEditor(false)">＋ Add location/department</button>` : "");
  v.innerHTML = `
    <div class="hero" style="background:linear-gradient(135deg,#134e4a,#0f766e)">
      <h2>Organizations</h2>
      <p>Customers and locations/departments in one place.</p>
    </div>
    <div class="seg" style="margin:14px 0 12px">
      ${tabs.map(([k, label]) => `<button class="${t === k ? "active" : ""}" onclick="setOrgTab('${k}')">${label}</button>`).join("")}
    </div>
    ${addBtn ? `<div class="btn-row" style="margin-bottom:12px">${addBtn}</div>` : ""}
    <div id="locList" class="${t === "locations" ? "" : "hidden"}"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>
    <div id="custList" class="${t === "customers" ? "" : "hidden"}"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  if (t === "customers") await refreshCustomers();
  else await refreshLocations();
}

function setOrgTab(tab) {
  state.orgTab = tab;
  viewOrg($("#view"));
}

// ---------------------------------------------------------------- Customers
async function viewCustomers(v) {
  await viewOrg(v, "customers");
}

async function refreshCustomers() {
  const box = $("#custList");
  try {
    const list = await API.get("/api/customers");
    state.customers = list;
    box.innerHTML = list.length
      ? `<div class="list">${list.map((cu) => `
        <div class="item" onclick="navigate('customerDetail',{id:${cu.id}})">
          <div class="item-top">
            <div class="c-avatar" style="width:40px;height:40px;font-size:14px">${esc(initials(cu.name))}</div>
            <div class="item-main">
              <div class="item-title">${esc(cu.name)}</div>
              <div class="item-sub">${esc(cu.contact_name || "—")} · ${esc(cu.city || "")}</div>
            </div>
          </div>
          <div class="item-meta">
            <span class="badge" style="background:var(--bg);color:var(--ink-soft)">${cu.equipment_count} equipment</span>
            <span class="badge b-open">${cu.open_complaints} open complaints</span>
          </div>
        </div>`).join("")}</div>`
      : emptyState("🏢", "No customers yet", "Add your first customer to start tracking.", "Add customer");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

async function viewCustomerDetail(v) {
  const id = state.viewParams.id;
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const list = await API.get("/api/customers");
    state.customers = list;
    const cu = list.find((x) => x.id === id);
    const eq = await API.get("/api/equipment?customer_id=" + id);
    const cmp = await API.get("/api/complaints?customer_id=" + id);
    const brk = await API.get("/api/breakdowns?customer_id=" + id);
    if (!cu) throw new Error("Customer not found");
    v.innerHTML = `
      <div class="detail-head">
        <button class="back-link" onclick="goBack()">‹ Back</button>
        <div class="card" style="margin-top:8px">
          <div style="display:flex;align-items:center;gap:12px">
            <div class="c-avatar" style="width:48px;height:48px;font-size:16px">${esc(initials(cu.name))}</div>
            <div>
              <h2 style="font-size:18px">${esc(cu.name)}</h2>
              <div class="item-sub">${esc(cu.city || "")}</div>
            </div>
          </div>
        </div>
      </div>
      <div class="section-title">Contact</div>
      <div class="card">
        <div class="kv"><span class="k">Contact person</span><span class="v">${esc(cu.contact_name || "—")}</span></div>
        <div class="kv"><span class="k">Email</span><span class="v">${esc(cu.email || "—")}</span></div>
        <div class="kv"><span class="k">Phone</span><span class="v">${esc(cu.phone || "—")}</span></div>
        <div class="kv"><span class="k">Address</span><span class="v">${esc(cu.address || "—")}</span></div>
      </div>
      <div class="section-title">Equipment (${eq.length})</div>
      ${eq.length ? `<div class="list">${eq.map(equipmentCard).join("")}</div>` : `<div class="card"><p style="color:var(--ink-soft);font-size:13px">No equipment registered.</p></div>`}
      <div class="section-title">Recent complaints (${cmp.length})</div>
      ${cmp.length ? `<div class="list">${cmp.slice(0, 3).map(complaintCard).join("")}</div>` : `<div class="card"><p style="color:var(--ink-soft);font-size:13px">None.</p></div>`}
      ${isAdmin() ? `<div class="action-panel" style="margin-top:14px"><button class="btn btn-ghost" onclick="openCustomerEditor(true)">✏️ Edit customer</button></div>` : ""}
    `;
  } catch (e) {
    v.innerHTML = `<div class="empty"><h3>Could not load</h3><p>${esc(e.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------- Locations/Departments
async function viewLocations(v) {
  v.innerHTML = `
    ${isTech() ? `<div class="btn-row" style="margin-bottom:12px">
      <button class="btn btn-primary" onclick="openLocationEditor(false)">＋ Add location/department</button>
    </div>` : ""}
    <div id="locList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  await refreshLocations();
}

async function refreshLocations() {
  const box = $("#locList");
  try {
    const list = await API.get("/api/locations");
    state.locations = list;
    box.innerHTML = list.length
      ? `<div class="list">${list.map((l) => `
        <div class="item" onclick="${isTech() ? `openLocationEditor(true, ${l.id})` : ""}">
          <div class="item-top">
            <div class="c-avatar">📍</div>
            <div class="item-main">
              <div class="item-title">${esc(l.name)}</div>
              <div class="item-sub">${esc(l.customer_name || "")}${l.city ? " · " + esc(l.city) : ""}</div>
            </div>
          </div>
          <div class="item-meta">
            <span class="badge" style="background:var(--bg);color:var(--ink-soft)">${l.equipment_count} equipment</span>
          </div>
        </div>`).join("")}</div>`
      : emptyState("📍", "No locations/departments", "Add your first location/department.", "Add location/department");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

async function openLocationEditor(edit, id) {
  let customers = [];
  try { customers = await API.get("/api/customers"); } catch (e) {}
  const l = edit ? state.locations?.find((x) => x.id === id) : null;
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit location/department" : "Add location/department"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Location/Department name *</span><input id="locName" value="${esc(l ? l.name : "")}" placeholder="e.g. Molecular Diagnostics"></label>
      <label class="field"><span>Customer *</span>
        <select id="locCustomer">
          ${customers.map((x) => `<option value="${x.id}" ${l && l.customer_id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>
      <label class="field"><span>City</span><input id="locCity" value="${esc(l ? l.city : "")}" placeholder="e.g. Kuala Lumpur"></label>
      <label class="field"><span>Address</span><input id="locAddress" value="${esc(l ? l.address : "")}"></label>
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deleteLocation(${l.id})">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveLocation(${edit ? l.id : "null"})">${edit ? "Save" : "Add location/department"}</button>
    </div>`);
}

async function saveLocation(id) {
  const body = {
    name: $("#locName").value.trim(),
    customer_id: $("#locCustomer").value,
    city: $("#locCity").value.trim(),
    address: $("#locAddress").value.trim(),
  };
  if (!body.name) { toast("Location/department name is required", "error"); return; }
  if (!body.customer_id) { toast("Customer is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.put("/api/locations/" + id, body);
    else await API.post("/api/locations", body);
    toast(id ? "Location/department updated" : "Location/department added", "success");
    await refreshLocations();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function deleteLocation(id) {
  confirmDialog("Delete location/department?", "Locations/departments with equipment cannot be deleted.", "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/locations/" + id);
      toast("Location/department deleted", "success");
      await refreshLocations();
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

async function viewDepartments(v) {
  await viewLocations(v);
}

async function refreshDepartments() {
  try {
    state.departments = await API.get("/api/departments");
  } catch (e) {}
}

async function openDepartmentEditor(edit, id) {
  openLocationEditor(edit, id);
}

async function saveDepartment(id) {
  await saveLocation(id);
}

async function deleteDepartment(id) {
  await deleteLocation(id);
}

// ---------------------------------------------------------------- Users
async function viewUsers(v) {
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const list = await API.get("/api/users");
    state.users = list;
    const canEdit = (usr) => isMaster()
      || (isAdmin() && usr.role !== "admin" && usr.customer_id === state.user.customer_id);
    v.innerHTML = `
      ${isAdmin() ? `<div class="btn-row" style="margin-bottom:12px"><button class="btn btn-primary" onclick="openUserEditor(false)">＋ Add user</button></div>` : ""}
      <div class="list list-grid">${list.map((u) => `
        <div class="item" onclick="${canEdit(u) ? `openUserEditor(true, ${u.id})` : ""}">
          <div class="item-top">
            <div class="c-avatar">${esc(initials(u.name))}</div>
            <div class="item-main">
              <div class="item-title">${esc(u.name)}</div>
              <div class="item-sub">${esc(u.email)}${u.customer_name ? " · " + esc(u.customer_name) : ""}${u.location_name || u.department_name ? " · " + esc(u.location_name || u.department_name) : ""}${u.responsible_admin_name ? " · 👤 " + esc(u.responsible_admin_name) : ""}</div>
            </div>
            ${roleBadge(u)}
          </div>
        </div>`).join("")}</div>`;
  } catch (e) {
    v.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------- Categories
async function viewCategories(v) {
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const cats = await API.get("/api/categories");
    v.innerHTML = `
      <div class="hero" style="background:linear-gradient(135deg,#3f6212,#4d7c0f)">
        <h2>Equipment categories</h2>
        <p>Manage the categories used when adding equipment.</p>
      </div>
      ${isMaster() ? `<div class="btn-row" style="margin-bottom:12px">
        <button class="btn btn-primary" onclick="openCategoryEditor(false)">＋ Add category</button>
      </div>` : ""}
      <div class="list list-grid">${cats.map((c) => `
        <div class="item" onclick="${isMaster() ? `openCategoryEditor(true, ${c.id}, '${esc(c.name)}')` : ""}">
          <div class="item-top">
            <div class="c-avatar">🏷️</div>
            <div class="item-main">
              <div class="item-title">${esc(c.name)}</div>
              <div class="item-sub">${c.equipment_count} equipment</div>
            </div>
          </div>
        </div>`).join("")}</div>`;
  } catch (e) {
    v.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function openCategoryEditor(edit, id, name) {
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit category" : "Add category"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Category name *</span><input id="catName" value="${esc(edit ? name : "")}" placeholder="e.g. Incubators"></label>
      ${edit ? `<label class="field"><span style="color:var(--ink-soft)">Renaming keeps existing equipment linked.</span></label>` : ""}
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deleteCategory(${id}, '${esc(name)}')">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveCategory(${edit ? id : "null"})">${edit ? "Save" : "Add category"}</button>
    </div>`);
}

async function saveCategory(id) {
  const name = $("#catName").value.trim();
  if (!name) { toast("Category name is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.put("/api/categories/" + id, { name });
    else await API.post("/api/categories", { name });
    toast(id ? "Category updated" : "Category added", "success");
    await viewCategories($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function deleteCategory(id, name) {
  confirmDialog("Delete category?", `Equipment in “${name}” will move to “Other”.`, "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/categories/" + id);
      toast("Category deleted", "success");
      await viewCategories($("#view"));
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

// ---------------------------------------------------------------- Onboarding
async function viewOnboarding(v) {
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const apps = await API.get("/api/onboarding");
    const pending = apps.filter((a) => a.status === "pending").length;
    v.innerHTML = `
      <div class="hero" style="background:linear-gradient(135deg,#164e63,#155e75)">
        <h2>Join requests</h2>
        <p>${pending ? `<b>${pending}</b> awaiting approval` : "Nothing waiting — you're all caught up"}</p>
      </div>
      ${apps.length ? `<div class="list list-grid">${apps.map(onboardingCard).join("")}</div>`
        : emptyState("📥", "No join requests", "When someone requests a customer or engineer account, it will appear here.", "")}`;
  } catch (e) {
    v.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function onboardingCard(a) {
  const s = a.status;
  const badgeCls = s === "pending" ? "b-open" : s === "approved" ? "b-resolved" : "b-closed";
  const badgeLabel = s === "pending" ? "Pending" : s === "approved" ? "Approved" : "Rejected";
  const scope = a.customer_name
    ? `${esc(a.customer_name)} › ${esc(a.location_name || a.department_name || "—")}`
    : "—";
  const actions = s === "pending"
    ? `<div class="btn-row" style="margin-top:12px">
        <button class="btn btn-danger btn-sm" onclick="reviewJoin(${a.id},'reject')">✕ Reject</button>
        <button class="btn btn-primary-2 btn-sm" onclick="reviewJoin(${a.id},'approve')">✔ Approve</button>
      </div>`
    : "";
  return `
    <div class="item">
      <div class="item-top">
        <div class="c-avatar">${esc(initials(a.name))}</div>
        <div class="item-main">
          <div class="item-title">${esc(a.name)} ${roleChip(a.role)}</div>
          <div class="item-sub">${esc(a.email)}${a.phone ? " · " + esc(a.phone) : ""}</div>
          <div class="item-sub">${a.role === "customer" ? "Scope: " + scope : "LabCare engineer"}</div>
        </div>
        <span class="badge ${badgeCls}">${badgeLabel}</span>
      </div>
      ${actions}
    </div>`;
}

async function reviewJoin(id, decision) {
  showLoading();
  try {
    await API.post(`/api/onboarding/${id}/review`, { decision });
    toast(decision === "approve" ? "Request approved — the user can now sign in" : "Request rejected", "success");
    await viewOnboarding($("#view"));
    refreshBell();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

// ---------------------------------------------------------------- Profile & More
function viewProfile(v) {
  const u = state.user;
  const isTenantAdmin = u && u.role === "admin" && u.customer_id;
  v.innerHTML = `
    <div class="card" style="text-align:center;padding:26px 16px">
      <div class="c-avatar" style="width:72px;height:72px;font-size:26px;margin:0 auto">${esc(initials(u.name))}</div>
      <h2 style="font-size:20px;margin-top:10px">${esc(u.name)}</h2>
      <p style="color:var(--ink-soft);font-size:13.5px">${esc(u.email)}</p>
      <div style="margin-top:10px">${roleBadge(u)}</div>
    </div>
    <div class="section-title">Account</div>
    <div class="card">
      <div class="kv"><span class="k">Phone</span><span class="v">${esc(u.phone || "—")}</span></div>
      <div class="kv"><span class="k">Role</span><span class="v">${roleLabel(u)}</span></div>
      ${u.customer_name ? `<div class="kv"><span class="k">Organisation</span><span class="v">${esc(u.customer_name)}</span></div>` : ""}
      ${(u.location_name || u.department_name) ? `<div class="kv"><span class="k">Location/Department</span><span class="v">${esc(u.location_name || u.department_name)}</span></div>` : ""}
    </div>
    ${isTenantAdmin ? `
    <div class="section-title">Customer organisations I care for</div>
    <div class="card">
      <p style="font-size:13px;color:var(--ink-soft);margin:0 0 10px">Pick which customer organisations you manage. You can add engineers and view tickets, equipment and reports for every organisation on your list.</p>
      <div id="careListHost"></div>
    </div>` : ""}
    <div class="action-panel" style="margin-top:16px">
      <button class="btn btn-danger" onclick="logout()">Sign out</button>
    </div>`;
  if (isTenantAdmin) renderCareList();
}

// ---- Tenant-admin self-select care list ----
async function renderCareList() {
  const host = $("#careListHost");
  if (!host) return;
  host.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  let mine = null, dir = null;
  try {
    [mine, dir] = await Promise.all([
      API.get("/api/my-customers"), API.get("/api/customer-directory"),
    ]);
  } catch (e) {
    host.innerHTML = `<div class="empty"><p>Couldn't load your organisations: ${esc(e.message)}</p></div>`;
    return;
  }
  const linked = mine.customers || [];
  const myIds = new Set((mine.customer_ids || []).map(String));
  const others = (dir || []).filter((x) => !myIds.has(String(x.id)));
  host.innerHTML = `
    <div class="chip-row" style="margin-bottom:10px;display:flex;flex-wrap:wrap;gap:8px">
      ${linked.map((x) => `
        <span class="chip" style="display:inline-flex;align-items:center;gap:8px;background:#ede9fe;color:#6d28d9;padding:6px 12px;font-size:12px">
          ${esc(x.name)}
          ${String(x.id) === String(mine.primary_id)
            ? `<span class="badge" style="background:#f1eefc;color:#7c6af0">primary</span>`
            : `<button class="chip-x" onclick="removeCareCustomer(${x.id})">✕</button>`}
        </span>`).join("")}
    </div>
    ${others.length ? `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <select id="careAddSel" style="flex:1;min-width:200px">
          ${others.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}
        </select>
        <button class="btn btn-primary-2" style="padding:9px 14px" onclick="addCareCustomer(document.getElementById('careAddSel').value)">＋ Add to my list</button>
      </div>`
    : `<p style="font-size:13px;color:var(--ink-soft);margin:0">You already care for every customer organisation.</p>`}`;
}

async function addCareCustomer(id) {
  if (!id) return;
  showLoading();
  try {
    await API.post("/api/my-customers/" + id);
    toast("Added to your customer list", "success");
    state.user = await API.get("/api/me");
    await renderCareList();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function removeCareCustomer(id) {
  confirmDialog("Remove organisation", "Deselecting this organisation stops you from managing its users, tickets and equipment.", "Remove", async () => {
    showLoading();
    try {
      await API.del("/api/my-customers/" + id);
      toast("Removed from your customer list", "success");
      state.user = await API.get("/api/me");
      await renderCareList();
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

function viewMore(v) {
  const items = [];
  items.push(`<button class="menu-item" onclick="navigate('profile')"><span class="mi-ico">👤</span> My account <span class="mi-arrow">›</span></button>`);
  if (isAdmin() || isTech()) items.push(`<button class="menu-item" onclick="navigate('org')"><span class="mi-ico">🏢</span> Customers &amp; locations/departments <span class="mi-arrow">›</span></button>`);
  if (isMaster()) items.push(`<button class="menu-item" onclick="navigate('categories')"><span class="mi-ico">🏷️</span> Categories <span class="mi-arrow">›</span></button>`);
  if (isAdmin()) items.push(`<button class="menu-item" onclick="navigate('users')"><span class="mi-ico">👥</span> Team & users <span class="mi-arrow">›</span></button>`);
  if (isMaster()) items.push(`<button class="menu-item" onclick="navigate('onboarding')"><span class="mi-ico">📥</span> Join requests <span class="mi-arrow">›</span></button>`);

  // Create new: quick access from the menu (same as the ＋ button)
  const createItems = [];
  if (isCust() || isTech() || isAdmin()) createItems.push(`<button class="menu-item create-new" onclick="openComplaintEditor(false)"><span class="mi-ico">✉️</span> New complaint <span class="mi-arrow">＋</span></button>`);
  if (isCust() || isTech() || isAdmin()) createItems.push(`<button class="menu-item create-new" onclick="openBreakdownEditor(false)"><span class="mi-ico">⚠️</span> New breakdown <span class="mi-arrow">＋</span></button>`);

  // Maintenance & portal
  items.push(`<button class="menu-item" onclick="navigate('pm')"><span class="mi-ico">🗓</span> Preventive maintenance <span class="mi-arrow">›</span></button>`);
  if (isTech()) {
    items.push(`<button class="menu-item" onclick="navigate('portals')"><span class="mi-ico">📱</span> Customer portal &amp; QR <span class="mi-arrow">›</span></button>`);
  }

  // Reports & export
  items.push(`<button class="menu-item" onclick="downloadReport('/api/reports/trend.pdf')"><span class="mi-ico">📈</span> Trend report (PDF) <span class="mi-arrow">›</span></button>`);
  if (isTech()) {
    items.push(`<button class="menu-item" onclick="openExportSheet()"><span class="mi-ico">📊</span> Export data (CSV) <span class="mi-arrow">›</span></button>`);
  }

  // Sound alerts
  const alertOn = store.get("labcare_alert_sound") !== "off";
  items.push(`<button class="menu-item" id="alertSoundToggle" onclick="openAlertSheet()"><span class="mi-ico">${alertOn ? "🔊" : "🔇"}</span> Alerts &amp; sound ${alertOn ? "" : "· muted"} <span class="mi-arrow">›</span></button>`);

  items.push(`<button class="menu-item danger" onclick="logout()"><span class="mi-ico">🚪</span> Sign out <span class="mi-arrow">›</span></button>`);

  v.innerHTML = `
    <div class="hero" style="background:linear-gradient(135deg,#365314,#3f6212)">
      <h2>${esc(state.user.name)}</h2>
      <p>${roleLabel(state.user)} · ${esc(state.user.email)}</p>
    </div>
    <div class="section-title">Create new</div>
    <div class="menu-group">${createItems.join("")}</div>
    <div class="section-title">Menu</div>
    <div class="menu-group">${items.join("")}</div>
    <div class="card" style="margin-top:6px">
      <p style="font-size:12.5px;color:var(--ink-soft)">LabCare v1.1 — complaints &amp; breakdowns, photos, reports &amp; notifications.</p>
    </div>`;
}

function openExportSheet() {
  openSheet(`
    <div class="sheet-head"><h3>Export data</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <p style="font-size:13.5px;color:var(--ink-soft);margin-bottom:14px">Download all records as CSV to open in Excel / Google Sheets.</p>
      <button class="menu-item" onclick="downloadReport('/api/export.csv?type=complaints')">✉ Complaints CSV <span class="mi-arrow">›</span></button>
      <button class="menu-item" onclick="downloadReport('/api/export.csv?type=breakdowns')">⚠ Breakdowns CSV <span class="mi-arrow">›</span></button>
    </div>`);
}

// ---------------------------------------------------------------- Editors (sheets)
async function openComplaintEditor(edit) {
  let customers = [], equipment = [], techs = [];
  if (isTech()) {
    try {
      [customers, equipment, techs, state.locations, state.departments] = await Promise.all([
        API.get("/api/customers"), API.get("/api/equipment"), API.get("/api/engineers"),
        API.get("/api/locations"), API.get("/api/departments"),
      ]);
    } catch (e) {}
  } else if (isCust()) {
    try { equipment = await API.get("/api/equipment"); } catch (e) {}
  }
  const c = edit ? state.complaintDetail : null;
  const defCust = c && c.customer_id ? c.customer_id : (customers.length ? customers[0].id : "");
  const respAdmins = isTech() && isUnboundStaff() && defCust ? await adminsForCustomer(defCust) : [];

  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit complaint" : "Log complaint"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Subject *</span><input id="fSubject" value="${esc(c ? c.subject : "")}" placeholder="What went wrong?"></label>
      <label class="field"><span>Description</span><textarea id="fDesc" placeholder="Details, symptoms, when it started…">${esc(c ? c.description : "")}</textarea></label>
      ${isTech() ? `
      <label class="field"><span>Customer *</span>
        <select id="fCustomer" onchange="onCustPickComplaint()">
          ${customers.map((x) => `<option value="${x.id}" ${c && c.customer_id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>
      ${isUnboundStaff() ? `
      <label class="field" id="fRespAdminField" style="${defCust ? "" : "display:none"}"><span>Responsible tenant admin</span>
        <select id="fRespAdmin">
          ${respAdminOpts(respAdmins, c && c.responsible_admin_id, true)}
        </select></label>` : ""}
      <label class="field"><span>Location/Department</span>
        <select id="fLocation" onchange="onLocPickComplaint()">
          ${locOpts(state.locations || [], c && c.location_id, defCust)}
        </select></label>
      <select id="fDepartment" style="display:none">
        ${deptOpts(state.departments || [], c && c.department_id, c && c.location_id)}
      </select>` : ""}
      <label class="field"><span>Related equipment</span>
        <select id="fEquipment">
          <option value="">— None / general —</option>
          ${equipment.map((x) => `<option value="${x.id}" ${c && c.equipment_id === x.id ? "selected" : ""}>${esc(x.name)} (${esc(x.serial_number || "n/a")})</option>`).join("")}
        </select></label>
      <label class="field"><span>Category</span>
        <select id="fCategory">
          ${["General", "Centrifuges", "PCR", "Cold Storage", "Chromatography", "Spectroscopy", "Sterilization", "Analyzers", "Histology", "Other"].map((x) => `<option ${c && c.category === x ? "selected" : ""}>${x}</option>`).join("")}
        </select></label>
      <label class="field"><span>Priority</span>
        <div class="priority-pick" id="fPriority">
          ${["low", "medium", "high", "critical"].map((p) => `<button class="${(c ? c.priority === p : p === "medium") ? "active" : ""}" style="color:${prioColor(p)}" data-p="${p}">${PRIORITY_META[p].label}</button>`).join("")}
        </div></label>
      ${isTech() ? `
      <label class="field"><span>Assign to</span>
        <select id="fAssignee">
          <option value="">Unassigned</option>
          ${techs.map((t) => `<option value="${t.id}" ${c && c.assigned_to === t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}
        </select></label>` : ""}
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveComplaint(${edit ? c.id : "null"})">${edit ? "Save changes" : "Log complaint"}</button>
    </div>`);

  $$("#fPriority button").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#fPriority button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    })
  );
}

const prioColor = (p) => ({ low: "#4338ca", medium: "#a16207", high: "#c2410c", critical: "#dc2626" }[p]);

async function saveComplaint(id) {
  const body = {
    subject: $("#fSubject").value.trim(),
    description: $("#fDesc").value.trim(),
    equipment_id: $("#fEquipment").value || null,
    category: $("#fCategory").value,
    priority: ($("#fPriority button.active")?.dataset.p || "medium"),
  };
  if (isTech()) {
    body.customer_id = $("#fCustomer").value;
    body.location_id = $("#fLocation").value || null;
    const deptMatch = (state.departments || []).find((d) => String(d.location_id) === String(body.location_id));
    body.department_id = $("#fDepartment")?.value || deptMatch?.id || body.location_id || null;
    body.assigned_to = ($("#fAssignee").value || null);
    if (isUnboundStaff() && $("#fRespAdmin")) body.responsible_admin_id = $("#fRespAdmin").value || null;
  }
  if (!body.subject) { toast("Subject is required", "error"); return; }
  if (isTech() && !body.customer_id) { toast("Customer is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.patch("/api/complaints/" + id, body);
    else await API.post("/api/complaints", body);
    toast(id ? "Complaint updated" : "Complaint logged", "success");
    state.complaints = null;
    if (state.view === "complaintDetail" && id) await viewComplaintDetail($("#view"));
    else await refreshComplaintsInside();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function refreshComplaintsInside() {
  if (state.view === "complaints") await refreshComplaints();
}

async function openBreakdownEditor(edit, prefill) {
  let customers = [], equipment = [], techs = [], complaints = [];
  if (isTech()) {
    try {
      [customers, equipment, techs, complaints, state.locations, state.departments] = await Promise.all([
        API.get("/api/customers"), API.get("/api/equipment"), API.get("/api/engineers"), API.get("/api/complaints"),
        API.get("/api/locations"), API.get("/api/departments"),
      ]);
    } catch (e) {}
  } else if (isCust()) {
    try { equipment = await API.get("/api/equipment"); } catch (e) {}
  }
  const b = edit && !prefill ? state.breakdownDetail : null;
  const p = prefill || {};
  const defCust = (b ? b.customer_id : p.customer_id) || (isTech() && customers.length ? customers[0].id : "");
  const respAdmins = isTech() && isUnboundStaff() && defCust ? await adminsForCustomer(defCust) : [];

  openSheet(`
    <div class="sheet-head"><h3>${edit && !prefill ? "Edit breakdown" : "Report breakdown"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Affected equipment</span>
        <select id="bEquipment">
          <option value="">— Select —</option>
          ${equipment.map((x) => `<option value="${x.id}" ${(b ? b.equipment_id === x.id : p.equipment_id === x.id) ? "selected" : ""}>${esc(x.name)} (${esc(x.serial_number || "n/a")})</option>`).join("")}
        </select></label>
      ${isTech() ? `
      <label class="field"><span>Customer *</span>
        <select id="bCustomer" onchange="onCustPickBreakdown()">
          ${customers.map((x) => `<option value="${x.id}" ${(b ? b.customer_id === x.id : p.customer_id === x.id) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>
      ${isUnboundStaff() ? `
      <label class="field" id="bRespAdminField" style="${defCust ? "" : "display:none"}"><span>Responsible tenant admin</span>
        <select id="bRespAdmin">
          ${respAdminOpts(respAdmins, b && b.responsible_admin_id, true)}
        </select></label>` : ""}
      <label class="field"><span>Location/Department</span>
        <select id="bLocation" onchange="onLocPickBreakdown()">
          ${locOpts(state.locations || [], (b && b.location_id) || null, defCust)}
        </select></label>
      <select id="bDepartment" style="display:none">
        ${deptOpts(state.departments || [], (b && b.department_id) || null, (b && b.location_id) || null)}
      </select>
      <label class="field"><span>Linked complaint (optional)</span>
        <select id="bComplaint">
          <option value="">— None —</option>
          ${complaints.map((x) => `<option value="${x.id}" ${(b ? b.complaint_id === x.id : p.complaint_id === x.id) ? "selected" : ""}>${esc(x.code)} — ${esc(x.subject)}</option>`).join("")}
        </select></label>` : ""}
      <label class="field"><span>Fault description *</span><textarea id="bFault" placeholder="Symptom, error code, affected usage…">${esc(b ? b.fault_description : p.subject ? "Linked to complaint: " + p.subject + "\n" : "")}</textarea></label>
      <label class="field"><span>Priority</span>
        <div class="priority-pick" id="bPriority">
          ${["low", "medium", "high", "critical"].map((x) => `<button class="${(b && b.priority === x) || (!b && x === "medium") ? "active" : ""}" style="color:${prioColor(x)}" data-p="${x}">${PRIORITY_META[x].label}</button>`).join("")}
        </div></label>
      ${isTech() ? `
      <label class="field"><span>Assign to</span>
        <select id="bAssignee">
          <option value="">Unassigned</option>
          ${techs.map((t) => `<option value="${t.id}" ${b && b.assigned_to === t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}
        </select></label>` : ""}
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveBreakdown(${edit && !prefill ? b.id : "null"})">${edit && !prefill ? "Save changes" : "Report breakdown"}</button>
    </div>`);

  $$("#bPriority button").forEach((b2) =>
    b2.addEventListener("click", () => {
      $$("#bPriority button").forEach((x) => x.classList.remove("active"));
      b2.classList.add("active");
    })
  );
}

async function saveBreakdown(id) {
  const body = {
    equipment_id: $("#bEquipment").value || null,
    fault_description: $("#bFault").value.trim(),
    priority: ($("#bPriority button.active")?.dataset.p || "medium"),
  };
  if (isTech()) {
    body.customer_id = $("#bCustomer").value;
    body.location_id = $("#bLocation").value || null;
    const deptMatch = (state.departments || []).find((d) => String(d.location_id) === String(body.location_id));
    body.department_id = $("#bDepartment")?.value || deptMatch?.id || body.location_id || null;
    body.complaint_id = $("#bComplaint").value || null;
    body.assigned_to = ($("#bAssignee").value || null);
    if (isUnboundStaff() && $("#bRespAdmin")) body.responsible_admin_id = $("#bRespAdmin").value || null;
  }
  if (!body.fault_description) { toast("Fault description is required", "error"); return; }
  if (isTech() && !body.customer_id) { toast("Customer is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.patch("/api/breakdowns/" + id, body);
    else await API.post("/api/breakdowns", body);
    toast(id ? "Breakdown updated" : "Breakdown reported", "success");
    state.breakdowns = null;
    if (state.view === "breakdownDetail" && id) await viewBreakdownDetail($("#view"));
    else if (state.view === "breakdowns") await refreshBreakdowns();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function openEquipmentEditor(edit) {
  let cats = [];
  try {
    const p = [API.get("/api/categories")];
    if (isTech()) p.push(API.get("/api/locations"), API.get("/api/departments"), API.get("/api/customers"));
    const [c, ...rest] = await Promise.all(p);
    cats = c;
    if (isTech()) {
      [state.locations, state.departments, state.customers] = rest;
    }
  } catch (e) {}
  const e = edit ? state.equipment?.find((x) => x.id === state.viewParams.id) : null;
  const currentCat = (e && e.category) || "";
  // pick the category from the list, otherwise add the current value as an option
  let catNames = cats.map((x) => x.name);
  if (currentCat && !catNames.includes(currentCat)) catNames.push(currentCat);
  const defEqCust = (e && e.customer_id) || (isTech() && (state.customers || []).length ? state.customers[0].id : "");
  const eqRespAdmins = isTech() && defEqCust ? await adminsForCustomer(defEqCust) : [];
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit equipment" : "Add equipment"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      ${isTech() ? `
      <div class="section-label">Customer</div>
      <label class="field"><span>Customer *</span>
        <select id="eqCustomer" onchange="onCustPick('eqCustomer')">
          ${(state.customers || []).map((x) => `<option value="${x.id}" ${e && e.customer_id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>
      ${isUnboundStaff() ? `
      <label class="field" id="eqRespAdminField" style="${defEqCust ? "" : "display:none"}"><span>Responsible tenant admin</span>
        <select id="eqRespAdmin">
          ${respAdminOpts(eqRespAdmins, e && e.responsible_admin_id, true)}
        </select></label>` : ""}
      <div class="section-label">Location/Department</div>
      <label class="field"><span>Location/Department *</span>
        <select id="eqLocation" onchange="onLocPick('eqLocation')">
          ${locOpts(state.locations || [], e && e.location_id, defEqCust)}
        </select></label>
      <select id="eqDepartment" style="display:none">
        ${deptOpts(state.departments || [], e && e.department_id, e && e.location_id)}
      </select>` : ""}
      <div class="section-label">Equipment details</div>
      <label class="field"><span>Equipment name *</span><input id="eqName" value="${esc(e ? e.name : "")}" placeholder="e.g. HPLC System"></label>
      <label class="field"><span>Model</span><input id="eqModel" value="${esc(e ? e.model : "")}" placeholder="e.g. Agilent 1260"></label>
      <label class="field"><span>Serial number</span><input id="eqSerial" value="${esc(e ? e.serial_number : "")}" placeholder="S/N"></label>
      <label class="field"><span>Category</span>
        <select id="eqCategory" onchange="eqCategoryPick()">
          <option value="">— Select —</option>
          ${catNames.map((x) => `<option value="${esc(x)}" ${currentCat === x ? "selected" : ""}>${esc(x)}</option>`).join("")}
          ${isMaster() ? `<option value="__custom__">＋ New category…</option>` : ""}
        </select></label>
      <label class="field" id="eqCategoryCustomWrap" style="display:none"><span>New category name</span><input id="eqCategoryCustom" placeholder="Type a new category"></label>
      <label class="field"><span>Warranty expiry</span><input id="eqWarranty" type="date" value="${e && e.warranty_expiry ? e.warranty_expiry.slice(0, 10) : ""}"></label>
      <label class="field"><span>Notes</span><textarea id="eqNotes">${esc(e ? e.notes : "")}</textarea></label>
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deleteEquipment(${e.id})">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveEquipment(${edit ? e.id : "null"})">${edit ? "Save changes" : "Add equipment"}</button>
    </div>`);
}

function eqCategoryPick() {
  const sel = $("#eqCategory");
  const custom = sel.value === "__custom__";
  $("#eqCategoryCustomWrap").style.display = custom ? "" : "none";
}

async function deleteEquipment(id) {
  confirmDialog("Delete equipment?", "Equipment referenced by complaints or breakdowns cannot be deleted.", "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/equipment/" + id);
      toast("Equipment deleted", "success");
      closeSheet();
      state.equipment = null;
      if (state.view === "equipmentDetail") navigate("equipment");
      else await refreshEquipment();
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

// --- shared cascading helpers for the customer → location → department chain ---
async function adminsForCustomer(customerId) {
  if (!customerId) return [];
  if (_adminsCache[customerId]) return _adminsCache[customerId];
  try {
    const list = await API.get("/api/tenant-admins?customer_id=" + encodeURIComponent(customerId));
    _adminsCache[customerId] = list;
    return list;
  } catch (e) { return []; }
}

// Options for the "Responsible tenant admin" picker. Master/provider staff see an
// explicit "— None —" choice so they can leave it unset when a customer has no
// tenant admin yet; tenant staff are always defaulted by the backend.
function respAdminOpts(admins, selectedId, allowNone) {
  return (allowNone ? `<option value="">— None / not assigned —</option>` : "")
    + admins.map((a) => `<option value="${a.id}" ${String(selectedId) === String(a.id) ? "selected" : ""}>${esc(a.name)} · ${esc(a.email)}</option>`).join("");
}

async function onRespCustomerPick(customerSelId, respSelId) {
  const cust = $("#" + customerSelId).value;
  const admins = await adminsForCustomer(cust);
  const box = $("#" + respSelId);
  if (box) box.innerHTML = respAdminOpts(admins, null, isUnboundStaff());
  const field = $("#" + respSelId + "Field");
  if (field) field.style.display = cust ? "" : "none";
}

function locOpts(locations, selectedId, customerId) {
  const list = customerId ? locations.filter((l) => String(l.customer_id) === String(customerId)) : locations;
  return `<option value="">— Select location/department —</option>` + list.map((l) =>
    `<option value="${l.id}" ${String(selectedId) === String(l.id) ? "selected" : ""}>${esc(l.name)}</option>`).join("");
}

function deptOpts(departments, selectedId, locationId) {
  const list = locationId ? departments.filter((d) => String(d.location_id) === String(locationId)) : departments;
  return `<option value="">— Select department —</option>` + list.map((d) =>
    `<option value="${d.id}" ${String(selectedId) === String(d.id) ? "selected" : ""}>${esc(d.name)}</option>`).join("");
}

function onCustPick(custSelId) {
  const cust = $("#" + custSelId).value;
  $("#eqLocation").innerHTML = locOpts(state.locations || [], null, cust);
  $("#eqLocation").value = "";
  if ($("#eqDepartment")) {
    $("#eqDepartment").innerHTML = deptOpts(state.departments || [], null, null);
    $("#eqDepartment").value = "";
  }
  if ($("#eqRespAdmin")) onRespCustomerPick(custSelId, "eqRespAdmin");
}

function onLocPick(locSelId) {
  const loc = $("#" + locSelId).value;
  const depts = (state.departments || []).filter((d) => String(d.location_id) === String(loc));
  if ($("#eqDepartment")) {
    $("#eqDepartment").innerHTML = deptOpts(state.departments || [], depts[0]?.id || null, loc);
    $("#eqDepartment").value = depts[0]?.id ? String(depts[0].id) : (loc || "");
  }
}

function onCustPickComplaint() {
  const cust = $("#fCustomer").value;
  $("#fLocation").innerHTML = locOpts(state.locations || [], null, cust);
  $("#fLocation").value = "";
  if ($("#fDepartment")) {
    $("#fDepartment").innerHTML = deptOpts(state.departments || [], null, null);
    $("#fDepartment").value = "";
  }
  if ($("#fRespAdmin")) onRespCustomerPick("fCustomer", "fRespAdmin");
}

function onLocPickComplaint() {
  const loc = $("#fLocation").value;
  const depts = (state.departments || []).filter((d) => String(d.location_id) === String(loc));
  if ($("#fDepartment")) {
    $("#fDepartment").innerHTML = deptOpts(state.departments || [], depts[0]?.id || null, loc);
    $("#fDepartment").value = depts[0]?.id ? String(depts[0].id) : (loc || "");
  }
}

function onCustPickBreakdown() {
  const cust = $("#bCustomer").value;
  $("#bLocation").innerHTML = locOpts(state.locations || [], null, cust);
  $("#bLocation").value = "";
  if ($("#bDepartment")) {
    $("#bDepartment").innerHTML = deptOpts(state.departments || [], null, null);
    $("#bDepartment").value = "";
  }
  if ($("#bRespAdmin")) onRespCustomerPick("bCustomer", "bRespAdmin");
}

function onLocPickBreakdown() {
  const loc = $("#bLocation").value;
  const depts = (state.departments || []).filter((d) => String(d.location_id) === String(loc));
  if ($("#bDepartment")) {
    $("#bDepartment").innerHTML = deptOpts(state.departments || [], depts[0]?.id || null, loc);
    $("#bDepartment").value = depts[0]?.id ? String(depts[0].id) : (loc || "");
  }
}

async function saveEquipment(id) {
  let category = $("#eqCategory").value;
  if (category === "__custom__") {
    category = ($("#eqCategoryCustom").value || "").trim();
    if (!category) { toast("Please name the new category", "error"); return; }
    // register the new category so it shows in the list for everyone
    try { await API.post("/api/categories", { name: category }); } catch (e) { /* duplicate — fine */ }
  }
  const locVal = $("#eqLocation")?.value || null;
  const deptMatch = (state.departments || []).find((d) => String(d.location_id) === String(locVal));
  const deptVal = $("#eqDepartment")?.value || deptMatch?.id || locVal || null;
  const body = {
    name: $("#eqName").value.trim(),
    model: $("#eqModel").value.trim(),
    serial_number: $("#eqSerial").value.trim(),
    category,
    location_id: locVal,
    department_id: deptVal,
    warranty_expiry: $("#eqWarranty").value || "",
    notes: $("#eqNotes").value.trim(),
  };
  if (isTech()) body.customer_id = $("#eqCustomer").value;
  if (isTech() && isUnboundStaff() && $("#eqRespAdmin")) body.responsible_admin_id = $("#eqRespAdmin").value || null;
  if (!body.name) { toast("Equipment name is required", "error"); return; }
  if (isTech() && !body.customer_id) { toast("Customer is required", "error"); return; }
  if (isTech() && !body.location_id) { toast("Location/department is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.put("/api/equipment/" + id, body);
    else await API.post("/api/equipment", body);
    toast(id ? "Equipment updated" : "Equipment added", "success");
    state.equipment = null;
    if (state.view === "equipmentDetail" && id) await viewEquipmentDetail($("#view"));
    else await refreshEquipment();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function openCustomerEditor(edit) {
  const cu = edit ? state.customers?.find((x) => x.id === state.viewParams.id) : null;
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit customer" : "Add customer"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Organisation name *</span><input id="cuName" value="${esc(cu ? cu.name : "")}" placeholder="e.g. BioReference Labs"></label>
      <label class="field"><span>Contact person</span><input id="cuContact" value="${esc(cu ? cu.contact_name : "")}"></label>
      <label class="field"><span>Email</span><input id="cuEmail" type="email" value="${esc(cu ? cu.email : "")}"></label>
      <label class="field"><span>Phone</span><input id="cuPhone" value="${esc(cu ? cu.phone : "")}"></label>
      <label class="field"><span>City</span><input id="cuCity" value="${esc(cu ? cu.city : "")}"></label>
      <label class="field"><span>Address</span><input id="cuAddress" value="${esc(cu ? cu.address : "")}"></label>
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deleteCustomer(${cu.id})">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveCustomer(${edit ? cu.id : "null"})">${edit ? "Save changes" : "Add customer"}</button>
    </div>`);
}

async function deleteCustomer(id) {
  confirmDialog("Delete customer?", "Customers with linked locations, equipment, complaints or breakdowns cannot be deleted.", "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/customers/" + id);
      toast("Customer deleted", "success");
      closeSheet();
      state.customers = null;
      if (state.view === "customerDetail") navigate("customers");
      else await refreshCustomers();
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

async function saveCustomer(id) {
  const body = {
    name: $("#cuName").value.trim(),
    contact_name: $("#cuContact").value.trim(),
    email: $("#cuEmail").value.trim(),
    phone: $("#cuPhone").value.trim(),
    city: $("#cuCity").value.trim(),
    address: $("#cuAddress").value.trim(),
  };
  if (!body.name) { toast("Organisation name is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.put("/api/customers/" + id, body);
    else await API.post("/api/customers", body);
    toast(id ? "Customer updated" : "Customer added", "success");
    state.customers = null;
    if (state.view === "customerDetail" && id) await viewCustomerDetail($("#view"));
    else if (state.view === "customers" || state.view === "org") await refreshCustomers();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function openUserEditor(edit, id) {
  let customers = [];
  try {
    [customers, state.locations, state.departments] = await Promise.all([
      API.get("/api/customers"), API.get("/api/locations"), API.get("/api/departments"),
    ]);
  } catch (e) {}
  const u = edit ? state.users?.find((x) => x.id === id) : null;
  const master = isMaster();
  const startRole = u ? u.role : "engineer";
  const startCust = startRole === "customer";
  // who sees which fields: customers always get the full pickers; the master may
  // bind engineers to a customer or leave them LabCare-wide, and may create
  // tenant admins linked to a customer or entirely unlinked (they create their
  // own organisations after first login); non-master tenant admins always pick
  // which of their care-list customers the new account belongs to.
  const showCustFields = master || startCust || (isAdmin() && !master);
  const showLocDept = startCust;
  // responsible tenant admin: only the master picks it (tenant staff are auto-assigned by the backend)
  const startCustId = u && u.customer_id ? u.customer_id : (state.user && !master ? state.user.customer_id : "");
  const respAdmins = (u && u.customer_id) || (state.user && !master && state.user.customer_id)
    ? await adminsForCustomer(startCustId || null)
    : [];
  const respOptions = master
    ? respAdminOpts(respAdmins, u && u.responsible_admin_id, true)
    : "";
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit user" : "Add user"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Full name *</span><input id="uName" value="${esc(u ? u.name : "")}"></label>
      <label class="field"><span>Email *</span><input id="uEmail" type="email" value="${esc(u ? u.email : "")}"></label>
      <label class="field"><span>Phone</span><input id="uPhone" value="${esc(u ? u.phone : "")}"></label>
      <label class="field"><span>Role</span>
        <select id="uRole" onchange="toggleCustomerSelect()">
          ${(master ? [["admin", "Tenant admin"], ["engineer", "Engineer"], ["application", "Application"], ["customer", "Customer"]] : [["engineer", "Engineer"], ["application", "Application"], ["customer", "Customer"]])
            .map(([r, lbl]) => `<option value="${r}" ${startRole === r ? "selected" : ""}>${lbl}</option>`).join("")}
        </select></label>
      <div id="uCustomerFields" style="${showCustFields ? "" : "display:none"}">
        <label class="field"><span id="uCustomerLabel">${master && !startCust ? "Linked customer (optional — leave empty for LabCare-wide)" : "Linked customer"}</span>
          <select id="uCustomer" onchange="onUserCustPick()">
            ${master ? `<option value="" ${!u || !u.customer_id ? "selected" : ""}>— LabCare-wide (no customer) —</option>` : ""}
            ${customers.map((x) => `<option value="${x.id}" ${String(x.id) === String(u && u.customer_id ? u.customer_id : (!master ? startCustId : null)) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
          </select></label>
        <label class="field" id="uLocationField" style="${showLocDept ? "" : "display:none"}"><span>Linked location/department</span>
          <select id="uLocation" onchange="onUserLocPick()">
            ${locOpts(state.locations || [], u && u.location_id, u && u.customer_id)}
          </select></label>
        <select id="uDepartment" style="display:none">
          ${deptOpts(state.departments || [], u && u.department_id, u && u.location_id)}
        </select>
        ${master ? `<label class="field" id="uRespAdminField" style="${startCustId ? "" : "display:none"}"><span>Responsible tenant admin</span>
          <select id="uRespAdmin">${respOptions}</select></label>` : ""}
      </div>
      <label class="field"><span>${edit ? "New password (leave blank to keep)" : "Password *"}</span><input id="uPassword" type="password" placeholder="${edit ? "••••••••" : "Set a password"}"></label>
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deleteUser(${id})">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="saveUser(${edit ? id : "null"})">${edit ? "Save changes" : "Add user"}</button>
    </div>`);
  // set labels/visibility correctly for the initially-selected role
  toggleCustomerSelect();
}

function toggleCustomerSelect() {
  const role = $("#uRole").value;
  const isCustRole = role === "customer";
  const isTenantAdminRole = role === "admin";
  if (isMaster()) {
    // customer accounts always need the full customer/location/department picker;
    // a tenant admin MAY stay unlinked — after first login they create their own
    // organisations, which land in their care list automatically.
    $("#uCustomerFields").style.display = (isCustRole || isTenantAdminRole) ? "" : "none";
    const lbl = $("#uCustomerLabel");
    if (lbl) lbl.textContent = isTenantAdminRole
      ? "Linked customer (optional — they create their own after login)"
      : "Linked customer (optional — leave empty for LabCare-wide)";
    const emptyOpt = document.querySelector("#uCustomer option[value='']");
    if (emptyOpt) emptyOpt.textContent = isTenantAdminRole
      ? "— Not linked yet (they create their own later) —"
      : "— LabCare-wide (no customer) —";
  } else {
    // tenant staff can create for any organisation in their care list
    // (which the backend scopes /api/customers to). Default to their primary.
    $("#uCustomerFields").style.display = "";
    const sel = $("#uCustomer");
    if (sel && !sel.value && state.user && state.user.customer_id) sel.value = String(state.user.customer_id);
  }
  $("#uLocationField").style.display = isCustRole ? "" : "none";
  if ($("#uDepartmentField")) $("#uDepartmentField").style.display = "none";
  onUserCustPick();
}

function onUserCustPick() {
  const cust = $("#uCustomer").value;
  $("#uLocation").innerHTML = locOpts(state.locations || [], null, cust);
  $("#uLocation").value = "";
  if ($("#uDepartment")) {
    $("#uDepartment").innerHTML = deptOpts(state.departments || [], null, null);
    $("#uDepartment").value = "";
  }
  // refresh the responsible tenant admin picker for the newly chosen customer
  if (isMaster() && $("#uRespAdmin")) onRespCustomerPick("uCustomer", "uRespAdmin");
}

function onUserLocPick() {
  const loc = $("#uLocation").value;
  const depts = (state.departments || []).filter((d) => String(d.location_id) === String(loc));
  if ($("#uDepartment")) {
    $("#uDepartment").innerHTML = deptOpts(state.departments || [], depts[0]?.id || null, loc);
    $("#uDepartment").value = depts[0]?.id ? String(depts[0].id) : (loc || "");
  }
}

async function saveUser(id) {
  const body = {
    name: $("#uName").value.trim(),
    email: $("#uEmail").value.trim(),
    phone: $("#uPhone").value.trim(),
    role: $("#uRole").value,
  };
  if (body.role === "customer") {
    body.customer_id = $("#uCustomer").value;
    body.location_id = $("#uLocation").value || null;
    const deptMatch = (state.departments || []).find((d) => String(d.location_id) === String(body.location_id));
    body.department_id = $("#uDepartment")?.value || deptMatch?.id || body.location_id || null;
    if (!body.customer_id) { toast("Linked customer is required for customer accounts", "error"); return; }
    if (isMaster() && $("#uRespAdmin")) body.responsible_admin_id = $("#uRespAdmin").value || null;
  } else if (isMaster()) {
    // a tenant admin may be created WITHOUT a customer — they build their own
    // organisation after first login; techs/customers may be global or bound
    body.customer_id = $("#uCustomer").value || null;
    body.location_id = null;
    body.department_id = null;
    if ($("#uRespAdmin")) body.responsible_admin_id = $("#uRespAdmin").value || null;
  } else {
    // tenant staff: engineers/customer users go under the customer they picked
    // (defaults to their primary customer)
    body.customer_id = $("#uCustomer") ? $("#uCustomer").value || (state.user && state.user.customer_id) || null : null;
    body.location_id = null;
    body.department_id = null;
  }
  const pw = $("#uPassword").value;
  if (pw) body.password = pw;
  if (!body.name || !body.email || (!id && !pw)) { toast("Name, email and password required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.patch("/api/users/" + id, body);
    else await API.post("/api/users", body);
    toast(id ? "User updated" : "User added", "success");
    await viewUsers($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function deleteUser(id) {
  confirmDialog("Delete user?", "The account is removed permanently. Their tickets, comments, equipment and history are kept — everything just becomes unassigned/unlinked from this user.", "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/users/" + id);
      toast("User deleted", "success");
      await viewUsers($("#view"));
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

// ---------------------------------------------------------------- Preventive maintenance
async function viewPM(v) {
  v.innerHTML = `
    <div class="filter-row">
      ${["due", "all"].map((k) => `
        <button class="filter-chip ${state.pmFilter === k ? "active" : ""}" onclick="setPMFilter('${k}')">
          ${k === "due" ? "Due / overdue" : "All schedules"}
        </button>`).join("")}
    </div>
    ${isTech() ? `<div class="btn-row" style="margin-bottom:10px">
      <button class="btn btn-primary" onclick="openPMEditor(false)">＋ New schedule</button>
    </div>` : ""}
    <div id="pmList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  await refreshPM();
}

function setPMFilter(k) {
  state.pmFilter = k;
  viewPM($("#view"));
}

function pmDueState(p) {
  if (!p.next_due_at) return { label: "Not scheduled", cls: "b-closed", urgent: false };
  const due = parseMYT(p.next_due_at);
  const now = new Date();
  const diffDays = Math.ceil((due - now) / 86400000);
  if (diffDays < 0) return { label: `Overdue ${Math.abs(diffDays)}d`, cls: "b-critical", urgent: true };
  if (diffDays <= 14) return { label: `Due in ${diffDays}d`, cls: "b-high", urgent: true };
  if (diffDays <= 30) return { label: `Due in ${diffDays}d`, cls: "b-medium", urgent: false };
  return { label: "Scheduled", cls: "b-resolved", urgent: false };
}

async function refreshPM() {
  const box = $("#pmList");
  try {
    let list = await API.get("/api/pms");
    state.pm = list;
    if (state.pmFilter === "due") list = list.filter((p) => pmDueState(p).urgent || !p.next_due_at);
    box.innerHTML = list.length
      ? `<div class="list">${list.map(pmCard).join("")}</div>`
      : emptyState("🗓", "Nothing due", isCust() ? "Your scheduled maintenance will appear here." : "Create a maintenance schedule with the ＋ button.", "New schedule");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function pmCard(p) {
  const due = pmDueState(p);
  return `
    <div class="item" onclick="navigate('pmDetail',{id:${p.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title">${esc(p.title)}</div>
          <div class="item-sub">${esc(p.equipment_name || "—")}${isCust() ? "" : " · " + esc(p.customer_name || "")}</div>
        </div>
      </div>
      <div class="item-meta">
        <span class="badge ${due.cls}">${esc(due.label)}</span>
        <span class="badge" style="background:var(--bg);color:var(--ink-soft)">${p.interval_days}d cycle</span>
        ${p.assigned_to_name ? `<span style="font-size:11.5px;color:var(--ink-soft)">👤 ${esc(p.assigned_to_name)}</span>` : ""}
        <span class="item-time">${p.next_due_at ? fmtDateShort(p.next_due_at) : ""}</span>
      </div>
    </div>`;
}

async function viewPMDetail(v) {
  const id = state.viewParams.id;
  v.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const list = await API.get("/api/pms");
    const p = list.find((x) => x.id === id);
    if (!p) throw new Error("Schedule not found");
    const logs = isTech() || isCust() ? await API.get("/api/pms/" + id + "/logs") : [];
    const due = pmDueState(p);
    v.innerHTML = `
      <div class="detail-head">
        <button class="back-link" onclick="goBack()">‹ Back</button>
        <div class="card" style="margin-top:8px">
          <div class="item-meta" style="margin:0 0 8px"><span class="badge ${due.cls}">${esc(due.label)}</span> ${p.active ? '<span class="badge b-resolved">Active</span>' : '<span class="badge b-closed">Paused</span>'}</div>
          <h2 style="font-size:17px">${esc(p.title)}</h2>
          <div class="item-sub" style="margin-top:6px">${esc(p.equipment_name || "—")} · ${esc(p.customer_name || "")}</div>
        </div>
      </div>
      <div class="section-title">Schedule</div>
      <div class="card">
        <div class="kv"><span class="k">Interval</span><span class="v">${p.interval_days} days</span></div>
        <div class="kv"><span class="k">Last done</span><span class="v">${fmtDate(p.last_done_at)}</span></div>
        <div class="kv"><span class="k">Next due</span><span class="v">${fmtDate(p.next_due_at)}</span></div>
        <div class="kv"><span class="k">Assigned to</span><span class="v">${esc(p.assigned_to_name || "Unassigned")}</span></div>
      </div>
      ${p.description ? `<div class="section-title">Description</div><div class="card"><div class="desc-box">${esc(p.description)}</div></div>` : ""}
      ${isTech() ? `<div class="section-title">Actions</div>
      <div class="card"><div class="action-panel">
        <button class="btn btn-primary-2 btn-sm" onclick="openPMComplete(${p.id})">✔ Mark completed</button>
        <button class="btn btn-ghost btn-sm" onclick="openPMEditor(true)">✏️ Edit</button>
      </div></div>` : ""}
      <div class="section-title">Service history (${logs.length})</div>
      <div class="card">
        ${logs.length ? logs.map((l) => `
          <div class="comment">
            <div class="c-avatar">${esc(initials(l.performed_by_name))}</div>
            <div class="c-body">
              <div class="c-head"><span class="c-name">${esc(l.performed_by_name)}</span><span class="c-time">${fmtDate(l.performed_at)}</span></div>
              <div class="c-text">${esc(l.notes || "Completed.")}</div>
            </div>
          </div>`).join("") : '<p style="color:var(--ink-soft);font-size:13px">No service history yet.</p>'}
      </div>`;
  } catch (e) {
    v.innerHTML = `<div class="empty"><h3>Could not load</h3><p>${esc(e.message)}</p></div>`;
  }
}

async function openPMEditor(edit) {
  let customers = [], equipment = [], techs = [];
  if (isTech()) {
    try {
      [customers, equipment, techs] = await Promise.all([
        API.get("/api/customers"), API.get("/api/equipment"), API.get("/api/engineers"),
      ]);
    } catch (e) {}
  }
  const p = edit ? state.pm?.find((x) => x.id === state.viewParams.id) : null;
  const defaultNext = mytDatePlus(90);
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit schedule" : "New PM schedule"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Title *</span><input id="pmTitle" value="${esc(p ? p.title : "")}" placeholder="e.g. Centrifuge annual service"></label>
      <label class="field"><span>Description</span><textarea id="pmDesc">${esc(p ? p.description : "")}</textarea></label>
      ${isTech() ? `<label class="field"><span>Customer *</span>
        <select id="pmCustomer">
          ${customers.map((x) => `<option value="${x.id}" ${p && p.customer_id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>` : ""}
      <label class="field"><span>Equipment</span>
        <select id="pmEquipment">
          <option value="">— None / general —</option>
          ${equipment.map((x) => `<option value="${x.id}" ${p && p.equipment_id === x.id ? "selected" : ""}>${esc(x.name)} (${esc(x.serial_number || "n/a")})</option>`).join("")}
        </select></label>
      <label class="field"><span>Interval (days) *</span><input id="pmInterval" type="number" min="1" value="${p ? p.interval_days : 90}"></label>
      <label class="field"><span>Next due date</span><input id="pmNext" type="date" value="${p && p.next_due_at ? p.next_due_at.slice(0, 10) : defaultNext}"></label>
      ${isTech() ? `<label class="field"><span>Assign to</span>
        <select id="pmAssignee">
          <option value="">Unassigned</option>
          ${techs.map((t) => `<option value="${t.id}" ${p && p.assigned_to === t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}
        </select></label>` : ""}
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deletePM(${p.id})">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="savePM(${edit ? p.id : "null"})">${edit ? "Save" : "Create"}</button>
    </div>`);
}

async function savePM(id) {
  const body = {
    title: $("#pmTitle").value.trim(),
    description: $("#pmDesc").value.trim(),
    equipment_id: $("#pmEquipment").value || null,
    interval_days: parseInt($("#pmInterval").value || "90"),
    next_due_at: $("#pmNext").value || null,
  };
  if (isTech()) {
    body.customer_id = $("#pmCustomer").value;
    body.assigned_to = $("#pmAssignee").value || null;
  }
  if (!body.title) { toast("Title is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.patch("/api/pms/" + id, body);
    else await API.post("/api/pms", body);
    toast(id ? "Schedule updated" : "Schedule created", "success");
    state.pm = null;
    if (state.view === "pmDetail" && id) await viewPMDetail($("#view"));
    else await refreshPM();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function deletePM(id) {
  confirmDialog("Delete schedule?", "This removes the maintenance schedule (history is kept).", "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/pms/" + id);
      toast("Schedule deleted", "success");
      navigate("pm");
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

function openPMComplete(id) {
  openSheet(`
    <div class="sheet-head"><h3>Mark PM completed</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Performed on</span><input id="pmDoneAt" type="date" value="${mytToday()}"></label>
      <label class="field"><span>Notes</span><textarea id="pmNotes" placeholder="What was done, parts replaced…"></textarea></label>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="confirmPMComplete(${state.viewParams.id})">Complete service</button>
    </div>`);
}

async function confirmPMComplete(id) {
  const performed_at = $("#pmDoneAt").value + " 12:00:00";
  const notes = $("#pmNotes").value.trim();
  closeSheet();
  showLoading();
  try {
    await API.post("/api/pms/" + id + "/complete", { performed_at, notes });
    toast("Service logged — next due date updated", "success");
    state.pm = null;
    await viewPMDetail($("#view"));
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

// ---------------------------------------------------------------- Customer portal (QR)
async function viewPortals(v) {
  v.innerHTML = `
    <div class="btn-row" style="margin-bottom:12px">
      <button class="btn btn-primary" onclick="openPortalEditor(false)">＋ New QR link</button>
    </div>
    <div id="portalList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  await refreshPortals();
}

async function refreshPortals() {
  const box = $("#portalList");
  try {
    const list = await API.get("/api/portal-links");
    state.portals = list;
    box.innerHTML = list.length
      ? `<div class="list">${list.map((p) => `
        <div class="item">
          <div class="item-top">
            <div class="item-main">
              <div class="item-title">${esc(p.label || "QR link")}</div>
              <div class="item-sub">${esc(p.customer_name || "")}${p.equipment_name ? " · " + esc(p.equipment_name) : ""}</div>
              <div class="item-sub mono" style="margin-top:4px">${esc(p.token)}</div>
            </div>
            ${p.active ? '<span class="badge b-resolved">Active</span>' : '<span class="badge b-closed">Paused</span>'}
          </div>
          <div class="item-meta">
            <button class="btn btn-primary-2 btn-sm" onclick="showPortalQR(${p.id})">🔳 Show QR</button>
            <button class="btn btn-ghost btn-sm" onclick="copyPortalURL('${esc(p.token)}')">🔗 Copy link</button>
            <button class="btn btn-danger btn-sm" onclick="deletePortal(${p.id})">🗑</button>
          </div>
        </div>`).join("")}</div>`
      : emptyState("📱", "No QR links yet", "Create a QR code customers can scan to report issues directly.", "New QR link");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

function eqOpt(x, selectedId) {
  const sel = selectedId === x.id ? "selected" : "";
  return `<option value="${x.id}" ${sel}>${esc(x.name)} (${esc(x.customer_name || "")})</option>`;
}

// Filter the QR editor's equipment list to the selected customer.
function onPortalCust() {
  const cust = $("#plCustomer").value;
  const all = state._portalEquip || [];
  const list = cust ? all.filter((x) => String(x.customer_id) === String(cust)) : all;
  const sel = $("#plEquipment");
  sel.innerHTML = `<option value="">— All equipment for this customer —</option>` +
    list.map((x) => eqOpt(x)).join("");
}

async function openPortalEditor(edit) {
  // Load customers and equipment independently so one slow/failed call can't
  // empty both dropdowns (was: a single Promise.all in a silent catch).
  let customers = [], equipment = [], loadErr = "";
  const results = await Promise.allSettled([
    API.get("/api/customers"),
    API.get("/api/equipment"),
  ]);
  if (results[0].status === "fulfilled") customers = results[0].value || [];
  else loadErr = "Could not load customers — " + (results[0].reason?.message || "network error");
  if (results[1].status === "fulfilled") equipment = results[1].value || [];
  state._portalEquip = equipment; // keep for onPortalCust() filtering
  const p = edit ? state.portals?.find((x) => x.id === state.viewParams.id) : null;
  const selCust = edit && p ? p.customer_id : (customers[0] && customers[0].id);
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit QR link" : "New QR link"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      ${loadErr ? `<div class="form-error">⚠️ ${esc(loadErr)}</div>` : ""}
      <label class="field"><span>Label</span><input id="plLabel" value="${esc(p ? p.label : "")}" placeholder="e.g. Freezer QR — Lab A"></label>
      <label class="field"><span>Customer *</span>
        <select id="plCustomer" onchange="onPortalCust()">
          ${customers.length
            ? customers.map((x) => `<option value="${x.id}" ${String(x.id) === String(selCust) ? "selected" : ""}>${esc(x.name)}</option>`).join("")
            : `<option value="">— No customers available —</option>`}
        </select></label>
      <label class="field"><span>Specific equipment (optional)</span>
        <select id="plEquipment">
          <option value="">— All equipment for this customer —</option>
          ${equipment.filter((x) => !selCust || String(x.customer_id) === String(selCust))
            .map((x) => eqOpt(x, p && p.equipment_id)).join("")}
        </select></label>
      <p class="hint" style="font-size:11.5px;color:var(--ink-soft)">Anyone scanning the QR opens a self-service portal to report issues on this equipment — no login needed.</p>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" onclick="savePortal(${edit ? p.id : "null"})">${edit ? "Save" : "Create QR link"}</button>
    </div>`);
}

async function savePortal(id) {
  const body = {
    label: $("#plLabel").value.trim(),
    customer_id: $("#plCustomer").value,
    equipment_id: $("#plEquipment").value || null,
  };
  if (!body.customer_id) { toast("Customer is required", "error"); return; }
  closeSheet();
  showLoading();
  try {
    if (id) await API.patch("/api/portal-links/" + id, body);
    else await API.post("/api/portal-links", body);
    toast(id ? "Link updated" : "QR link created", "success");
    await refreshPortals();
    // if created new, show the QR immediately
    if (!id) {
      const list = await API.get("/api/portal-links");
      const latest = list[0];
      if (latest) showPortalQR(latest.id);
    }
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

function showPortalQR(id) {
  openSheet(`
    <div class="sheet-head"><h3>Scan to report</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body" style="text-align:center;padding-top:20px">
      <img id="qrImg" style="width:240px;height:240px;border-radius:16px;border:1px solid var(--line)" alt="QR code">
      <p style="margin-top:14px;font-size:13px;color:var(--ink-soft)">Print this QR and stick it on the equipment.<br>Customers scan it to log a complaint instantly.</p>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-primary-2" onclick="downloadQR(${id})">⬇ Download PNG</button>
    </div>`);
  authedImage($("#qrImg"), `/api/portal-links/${id}/qr`);
}

function copyPortalURL(token) {
  const url = location.origin + "/portal.html?t=" + token;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => toast("Link copied", "success")).catch(() => fallbackCopy(url));
  } else {
    fallbackCopy(url);
  }
}

function fallbackCopy(url) {
  const ta = document.createElement("textarea");
  ta.value = url;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); toast("Link copied", "success"); } catch (e) { toast("Copy failed: " + url, "error"); }
  ta.remove();
}

function downloadQR(id) {
  // fetch the PNG with auth (header + cookie + query token) and download
  fetch(withToken(`/api/portal-links/${id}/qr`), { headers: { "Authorization": "Bearer " + (API.token || "") }, credentials: "same-origin" })
    .then((r) => r.blob())
    .then((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "labcare-qr.png";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    })
    .catch(() => toast("Could not download QR", "error"));
}

async function deletePortal(id) {
  confirmDialog("Delete QR link?", "The QR code will stop working immediately.", "Delete", async () => {
    showLoading();
    try {
      await API.del("/api/portal-links/" + id);
      toast("QR link deleted", "success");
      await refreshPortals();
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

// ---------------------------------------------------------------- History (audit)
async function loadHistory(entity, id) {
  const box = $("#historyBox");
  if (!box) return;
  try {
    const rows = await API.get("/api/audit?entity_type=" + entity + "&entity_id=" + id);
    box.innerHTML = rows.length
      ? `<ul class="timeline" style="margin:4px 0">${rows.map(auditHtml).join("")}</ul>`
      : '<p style="color:var(--ink-soft);font-size:13px">No activity recorded yet.</p>';
  } catch (e) {
    box.innerHTML = `<p style="color:var(--ink-soft);font-size:13px">History unavailable.</p>`;
  }
}

function auditHtml(a) {
  const label = AUDIT_META[a.action] || { t: a.action, ico: "•" };
  return `
    <li class="tl-item">
      <div class="tl-title">${label.ico} <b>${esc(a.user_name || "System")}</b> · ${esc(label.t)}</div>
      ${a.detail ? `<div class="tl-body">${esc(a.detail)}</div>` : ""}
      <div class="tl-time">${timeAgo(a.created_at)}</div>
    </li>`;
}

// ---------------------------------------------------------------- Photos & files
async function loadPhotos(entity, id) {
  const box = $("#photosBox");
  if (!box) return;
  try {
    const list = await API.get(`/api/attachments?entity_type=${entity}&entity_id=${id}`);
    if (!list.length) {
      box.innerHTML = `<p style="color:var(--ink-soft);font-size:13px">No photos attached.</p>`;
    } else {
      box.innerHTML = `
        <div class="photo-grid">
          ${list.map((a) => {
            const isImg = (a.mime || "").startsWith("image/");
            return isImg
              ? `<img class="photo-thumb" data-aid="${a.id}" onclick="viewPhoto(${a.id})" alt="${esc(a.filename)}">`
              : `<div class="photo-file" onclick="downloadReport('/api/attachments/${a.id}/file')">📎<br>${esc(a.filename)}${isTech() ? `<br><span style="color:var(--danger)" onclick="event.stopPropagation();deleteAttachment(${a.id},'${entity}',${id})">remove</span>` : ""}</div>`;
          }).join("")}
        </div>`;
      // load thumbnails with auth headers (img tags can't send them)
      box.querySelectorAll("img[data-aid]").forEach((img) => authedImage(img, `/api/attachments/${img.dataset.aid}/file`));
    }
  } catch (e) {
    box.innerHTML = `<p style="color:var(--ink-soft);font-size:13px">Couldn't load photos.</p>`;
  }
  // upload button
  const actions = document.createElement("div");
  actions.className = "photo-actions";
  actions.innerHTML = `
    <span style="font-size:12px;color:var(--ink-soft)">Photo · PDF · Word · Excel (max 8 MB)</span>
    <label class="btn btn-ghost btn-sm" style="cursor:pointer">
      📎 Attach file
      <input type="file" accept="image/*,application/pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.txt" multiple="multiple" style="display:none"
             onchange="uploadPhotos(event,'${entity}',${id})">
    </label>`;
  box.appendChild(actions);
}

async function authedImage(img, url) {
  try {
    const res = await fetch(withToken(url), { headers: { "Authorization": "Bearer " + (API.token || "") }, credentials: "same-origin" });
    if (!res.ok) throw new Error("unauthorized");
    const blob = await res.blob();
    img.src = URL.createObjectURL(blob);
  } catch (e) {
    img.remove();
  }
}

async function uploadPhotos(ev, entity, id) {
  const files = Array.from(ev.target.files || []);
  for (const file of files) {
    const fd = new FormData();
    fd.append("entity_type", entity);
    fd.append("entity_id", id);
    fd.append("file", file);
    showLoading();
    try {
      const res = await fetch(withToken("/api/attachments"), {
        method: "POST",
        headers: { "Authorization": "Bearer " + (API.token || "") },
        credentials: "same-origin",
        body: fd,
      });
      if (!res.ok) {
        let err = {};
        try { err = await res.json(); } catch (_) {}
        throw new Error(err.error || "Upload failed");
      }
      await loadPhotos(entity, id);
      toast("Attachment added", "success");
    } catch (e) {
      toast(e.message || "Upload failed", "error");
    }
    hideLoading();
  }
}

function viewPhoto(aid) {
  openSheet(`
    <div class="sheet-head"><h3>Photo</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body" style="text-align:center;background:#000;padding:12px">
      <img id="bigPhoto" style="max-width:100%;max-height:70dvh;border-radius:10px">
    </div>`);
  authedImage($("#bigPhoto"), `/api/attachments/${aid}/file`);
}

async function deleteAttachment(aid, entity, id) {
  showLoading();
  try {
    await API.del("/api/attachments/" + aid);
    await loadPhotos(entity, id);
    toast("Attachment removed", "success");
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

function downloadReport(url) {
  // fetch with auth (header + cookie + query token), then trigger a download from a blob
  fetch(withToken(url), { headers: { "Authorization": "Bearer " + (API.token || "") }, credentials: "same-origin" })
    .then((res) => {
      if (!res.ok) throw new Error("Failed to generate");
      // try to keep the server-provided filename
      const cd = res.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^";]+)"?/);
      const name = m ? m[1] : blobName(res.headers.get("Content-Type"));
      return res.blob().then((blob) => ({ blob, name }));
    })
    .then(({ blob, name }) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name || "download";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    })
    .catch((e) => toast("Could not download file", "error"));
}

function blobName(contentType) {
  const ct = (contentType || "").split(";")[0].trim();
  const ext = { "application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "text/csv": "csv", "text/plain": "txt",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx" }[ct];
  if (ext) return "labcare-report." + ext;
  if (ct.startsWith("image/")) return "labcare-image." + (ct.split("/")[1] || "png");
  return "labcare-download";
}

// ---------------------------------------------------------------- Notifications
const NOTIF_VIEWS = ["dashboard", "complaints", "breakdowns", "equipment", "more"];

// Audible alert for new tickets/notifications.
// The sound is USER-CONFIGURABLE: a preset shipped with the app, or the user's
// own audio file (uploaded → stored as a data URL in localStorage, so nothing
// is sent to the server and each user hears their own choice). Falls back to a
// synthesized two-tone chime when no file can be played, so alerts never go
// silent in restricted/sandboxed contexts.
const SOUND_PRESETS = [
  { id: "chime", label: "Chime (default)" },
  { id: "bell", label: "Bell" },
  { id: "beep", label: "Triple beep" },
  { id: "alarm", label: "Siren" },
];
let _audioCtx = null;
let _lastNotifId = null;
let _notifSynced = false;
let _alertAudio = null;      // cached <audio> for the custom file
let _alertAudioUrl = "";     // the src it was created from

function _customSoundSrc() {
  const custom = store.get("labcare_alert_custom");
  if (custom && custom.startsWith("data:audio/")) return custom;
  return "";
}

function playAlertSound() {
  const customSrc = _customSoundSrc();
  if (customSrc) {
    // user's own external sound file (data URL from localStorage)
    try {
      if (!_alertAudio || _alertAudioUrl !== customSrc) {
        _alertAudio = new Audio(customSrc);
        _alertAudioUrl = customSrc;
      }
      const p = _alertAudio.play();
      if (p && p.catch) p.catch(() => {});
      return;
    } catch (e) { /* fall through to the preset */ }
  }
  // preset sound — load once, replay each alert
  const preset = (store.get("labcare_alert_preset") || "chime");
  const url = "sounds/" + preset + ".wav";
  try {
    if (!_alertAudio || _alertAudioUrl !== url) {
      _alertAudio = new Audio(url);
      _alertAudioUrl = url;
      // if the file can't load (offline / sandboxed preview), fall back to
      // the synthesized chime so alerts never go silent
      _alertAudio.onerror = () => { _synthChime(); };
    }
    _alertAudio.currentTime = 0;
    const p = _alertAudio.play();
    if (p && p.catch) p.catch(() => {});
  } catch (e) {
    // offline / sandboxed preview → synthesised fallback so alerts still sound
    _synthChime();
  }
}

function _synthChime() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!_audioCtx) _audioCtx = new AC();
    if (_audioCtx.state === "suspended") _audioCtx.resume();
    const t0 = _audioCtx.currentTime;
    [[880, 0], [587.33, 0.16]].forEach(([freq, at]) => {
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0 + at);
      gain.gain.exponentialRampToValueAtTime(0.35, t0 + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.5);
      osc.connect(gain).connect(_audioCtx.destination);
      osc.start(t0 + at);
      osc.stop(t0 + at + 0.55);
    });
  } catch (e) { /* ignore */ }
}

// Called on a real user gesture (sign-in click) so the AudioContext isn't
// blocked by the browser's autoplay policy when an alert arrives later.
function primeAudio() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!_audioCtx) _audioCtx = new AC();
    if (_audioCtx.state === "suspended") _audioCtx.resume();
  } catch (e) { /* ignore */ }
}

// ---- Desktop push alerts (ring even when the app / tab is closed) ----
// LabCare's bell can also ring as a real system notification via Web Push:
// a service worker receives pushes from the backend and the OS/browser plays
// its alert sound — even if the user has closed the tab, as long as the
// browser is running and "Desktop alerts" is ON.
//
// The backend sends one push for every bell notification, so enabling this
// here is all that's needed. Per-device: the subscription is stored against
// the signed-in user and can be toggled independently in each browser.
const PUSH_PREF = "labcare_push_alerts";

function pushSupported() {
  // Only secure origins can register service workers / push. Sandboxed
  // previews and plain http:// hosts can't, so fail quietly there.
  if (!("serviceWorker" in navigator)) return false;
  if (!("PushManager" in window)) return false;
  if (!("Notification" in window)) return false;
  if (location.protocol !== "https:" && location.hostname !== "localhost") return false;
  // An app sandboxed in an opaque-origin iframe can't register a worker.
  try { if (window.frameElement) return false; } catch (e) { return false; }
  return true;
}

let _swReg = null;
function swReady() {
  if (_swReg && _swReg.installing === null) return Promise.resolve(_swReg);
  if (!("serviceWorker" in navigator)) return Promise.resolve(null);
  return navigator.serviceWorker.register("/sw.js")
    .then((r) => { _swReg = r; return r; })
    .catch(() => null);
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function _pushSubPayload(sub) {
  const j = sub.toJSON();
  return {
    endpoint: j.endpoint,
    keys: { p256dh: j.keys.p256dh, auth: j.keys.auth },
    alert_on: true,
    user_agent: navigator.userAgent || "",
  };
}

async function enablePushAlerts() {
  if (!pushSupported()) return "unsupported";
  try {
    // Ask for permission FIRST — it must run synchronously inside the user's
    // click gesture or Chrome will silently auto-deny it.
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return perm;
    const reg = await swReady();
    if (!reg) return "unsupported";
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const k = await API.get("/api/push/vapid-key");
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(k.public_key),
      });
    }
    store.set(PUSH_PREF, "on");
    await API.post("/api/push/subscribe", _pushSubPayload(sub));
    return "granted";
  } catch (e) {
    return "unsupported";
  }
}

async function disablePushAlerts() {
  store.set(PUSH_PREF, "off");
  try {
    const reg = await swReady();
    if (!reg) return true;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await API.post("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {});
      await sub.unsubscribe();
    }
  } catch (e) { /* ignore */ }
  return true;
}

async function togglePushAlerts() {
  const on = store.get(PUSH_PREF) === "on";
  if (on) {
    await disablePushAlerts();
    toast("Desktop alerts off — you'll only hear the in-app sound", "success");
  } else {
    const res = await enablePushAlerts();
    if (res === "granted") toast("Desktop alerts on — your phone/PC will ring even when the app is closed", "success");
    else if (res === "denied") toast("Notifications are blocked for this site in your browser settings", "error");
    else if (res === "default") toast("You've been asked — allow notifications in the permission prompt to enable alerts", "error");
    else toast("Desktop alerts aren't supported on this browser/device", "error");
  }
  if (typeof openAlertSheet === "function") openAlertSheet();
}

// Quietly re-register an existing subscription after login / reload so an
// expired one can't silently stop ringing. Never prompts for permission.
async function syncPushAlerts() {
  if (store.get(PUSH_PREF) !== "on") return;
  if (!pushSupported()) return;
  const perm = "Notification" in window ? Notification.permission : "denied";
  if (perm !== "granted") return;
  try {
    const reg = await swReady();
    if (!reg) return;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    await API.post("/api/push/subscribe", _pushSubPayload(sub));
  } catch (e) { /* ignore */ }
}

function pushStateLabel() {
  if (!pushSupported()) return "Unavailable on this browser/device";
  const on = store.get(PUSH_PREF) === "on";
  if (!on) return "Off — only rings while the app is open";
  const perm = "Notification" in window ? Notification.permission : "denied";
  if (perm !== "granted") return "On, but blocked — allow notifications in browser settings";
  return "On — rings even when the app is closed";
}

async function refreshBell(silent) {
  if (!state.user || !booted) return;
  try {
    const p = await API.get("/api/notifications/ping");
    const dot = $("#bellDot");
    state.unread = p.unread;
    dot.classList.toggle("hidden", !p.unread);
    // A *new* unread notification (e.g. a freshly logged complaint/breakdown)
    // triggers the audible alarm — but only after the first sync, so users
    // don't get a burst of sounds for pre-existing notifications on login.
    if (p.latest && p.latest.id !== _lastNotifId) {
      if (_notifSynced && !silent && p.unread > 0 && store.get("labcare_alert_sound") !== "off") {
        // When desktop push alerts are active, the OS notification already
        // rings (this very push) — don't double-ring with the in-app sound.
        const pushOn = store.get(PUSH_PREF) === "on"
          && ("Notification" in window) && Notification.permission === "granted";
        if (!pushOn) playAlertSound();
      }
      _lastNotifId = p.latest.id;
    }
    _notifSynced = true;
  } catch (e) { /* ignore */ }
}

function toggleAlertSound() {
  const on = store.get("labcare_alert_sound") !== "off";
  const next = !on;
  store.set("labcare_alert_sound", next ? "on" : "off");
  const el = $("#alertSoundToggle");
  if (el) el.innerHTML = `<span class="mi-ico">${next ? "🔊" : "🔇"}</span> ${next ? "Alerts on" : "Alerts off"} <span class="mi-arrow">›</span>`;
  toast(next ? "Alert sound on" : "Alert sound off", "success");
}

// ---- Alert sound settings ----
// Each user picks their own alarm sound: a bundled preset, or their OWN audio
// file (uploaded and stored locally in this browser as a data URL — the server
// never sees it).
function openAlertSheet() {
  const on = store.get("labcare_alert_sound") !== "off";
  const preset = store.get("labcare_alert_preset") || "chime";
  const custom = _customSoundSrc();
  const customName = store.get("labcare_alert_custom_name") || "";
  const pushOn = store.get(PUSH_PREF) === "on";
  const pushState = pushStateLabel();

  openSheet(`
    <div class="sheet-head"><h3>Alerts &amp; sound</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field">
        <span>Alerts</span>
        <button class="btn ${on ? "btn-primary" : "btn-ghost"} btn-sm" onclick="toggleAlertSound();openAlertSheet()">
          ${on ? "🔊 On" : "🔇 Off"}
        </button>
      </label>

      <div class="section-label" style="margin-top:10px">Desktop alerts — ring even when the app is closed</div>
      <label class="field">
        <span style="display:flex;flex-direction:column;align-items:flex-start;gap:2px">
          <b style="font-size:13.5px">Push notifications</b>
          <span style="font-size:12px;color:var(--ink-soft);font-weight:400">${esc(pushState)}</span>
        </span>
        <button class="btn ${pushOn ? "btn-primary" : "btn-ghost"} btn-sm" onclick="togglePushAlerts()">
          ${pushOn ? "🔔 On" : "🔕 Off"}
        </button>
      </label>
      <p style="font-size:12.5px;color:var(--ink-soft);margin:0 0 6px">
        When on, your browser will play its system notification sound for every
        new alert — even with the tab or the whole app closed. When off, alerts
        only sound while the app is open.
      </p>

      <div class="section-label" style="margin-top:6px">In-app preset sounds (built in)</div>
      <div style="display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center">
        ${SOUND_PRESETS.map((p) => `
          <button class="btn btn-sm ${!custom && preset === p.id ? "btn-primary" : "btn-ghost"}"
                  onclick="pickAlertPreset('${p.id}')">▶</button>
          <span style="font-size:14px">${esc(p.label)}</span>
          <span>${!custom && preset === p.id ? "✓" : ""}</span>`).join("")}
      </div>

      <div class="section-label" style="margin-top:14px">Use your own sound file</div>
      <p style="font-size:12.5px;color:var(--ink-soft);margin:0 0 8px">
        Upload an audio file (mp3, wav, ogg, m4a — up to 2&nbsp;MB). It is kept in
        this browser and played when a new alert arrives.
      </p>
      <label class="field">
        <input id="alertCustomFile" type="file" accept="audio/*,.mp3,.wav,.ogg,.m4a,.aac"
               onchange="onAlertCustomPicked(this)" style="padding:8px;font-size:13px">
      </label>
      ${custom ? `
        <div style="display:flex;gap:8px;align-items:center">
          <span style="font-size:13px;color:var(--ink)" class="ellipsis">🎧 ${esc(customName || "Custom sound")} <span style="color:var(--ink-soft)">(yours)</span></span>
          <button class="btn btn-ghost btn-sm" onclick="alertPreviewCustom()">▶ Preview</button>
          <button class="btn btn-ghost btn-sm" onclick="clearAlertCustom();openAlertSheet()">Remove</button>
        </div>` : ""}
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Done</button>
      <button class="btn btn-primary-2" onclick="playAlertSound()">▶ Test sound</button>
    </div>`);
}

function pickAlertPreset(id) {
  store.remove("labcare_alert_custom");
  store.remove("labcare_alert_custom_name");
  store.set("labcare_alert_preset", id);
  _alertAudio = null; _alertAudioUrl = "";
  try { playAlertSound(); } catch (e) {}
  openAlertSheet();
}

function onAlertCustomPicked(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (!/^audio\//.test(file.type) && !/\.(mp3|wav|ogg|m4a|aac|flac|opus|aiff)$/i.test(file.name)) {
    toast("Please choose an audio file (mp3, wav, ogg, m4a…)", "error");
    return;
  }
  if (file.size > 2 * 1024 * 1024) {
    toast("File too large — keep it under 2 MB", "error");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    try {
      store.set("labcare_alert_custom", reader.result);
      store.set("labcare_alert_custom_name", file.name);
      _alertAudio = null; _alertAudioUrl = "";
      toast("Your sound file is set", "success");
      try { playAlertSound(); } catch (e) {}
      openAlertSheet();
    } catch (e) {
      // (data URLs can exceed localStorage quota for huge files)
      toast("Couldn't save this file — try a smaller one", "error");
    }
  };
  reader.onerror = () => toast("Couldn't read that file", "error");
  reader.readAsDataURL(file);
}

function clearAlertCustom() {
  store.remove("labcare_alert_custom");
  store.remove("labcare_alert_custom_name");
  _alertAudio = null; _alertAudioUrl = "";
  toast("Custom sound removed", "success");
}

function alertPreviewCustom() {
  const src = _customSoundSrc();
  if (!src) return;
  try {
    const a = new Audio(src);
    a.play().catch(() => {});
  } catch (e) {}
}

async function openNotifications() {
  openSheet(`
    <div class="sheet-head"><h3>Notifications</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <div id="notifList"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="markAllRead()">Mark all read</button>
    </div>`);
  const list = $("#notifList");
  try {
    const n = await API.get("/api/notifications");
    list.innerHTML = n.length
      ? n.map((x) => `
        <div class="notif ${x.read ? "" : "unread"}" onclick="openNotif(${x.id}, '${esc(x.entity_type)}', ${x.entity_id || "null"})">
          <div class="c-avatar">🔔</div>
          <div class="n-body">
            <div class="n-text">${esc(x.text)}</div>
            <div class="n-time">${timeAgo(x.created_at)}</div>
            ${x.accepted ? `<div class="n-accepted">✔ Accepted</div>` : ""}
          </div>
        </div>`).join("")
      : `<div class="empty"><p>You're all caught up 🎉</p></div>`;
  } catch (e) {
    list.innerHTML = `<div class="empty"><p>Couldn't load notifications</p></div>`;
  }
}

async function openNotif(nid, entityType, entityId) {
  await API.post("/api/notifications/read", { id: nid });
  closeSheet();
  refreshBell();
  if (entityType === "complaint" && entityId) navigate("complaintDetail", { id: entityId });
  else if (entityType === "breakdown" && entityId) navigate("breakdownDetail", { id: entityId });
}

async function markAllRead() {
  await API.post("/api/notifications/read", {});
  refreshBell();
  openNotifications();
}

// Poll for new notifications (and ring the audible alert) every 15s.
setInterval(() => refreshBell(false), 15000);

// ---------------------------------------------------------------- FAB + empty state
function emptyState(ico, title, sub, ctaLabel) {
  return `
    <div class="empty">
      <div class="e-ico">${ico}</div>
      <h3>${esc(title)}</h3>
      <p>${esc(sub)}</p>
    </div>`;
}

function onFab() {
  switch (state.view) {
    case "complaints": openComplaintEditor(false); break;
    case "breakdowns": openBreakdownEditor(false); break;
    case "equipment": openEquipmentEditor(false); break;
    case "pm": openPMEditor(false); break;
    default: openComplaintEditor(false);
  }
}

// ---------------------------------------------------------------- Join request
async function loadJoinOptions() {
  try {
    const opts = await API.get("/api/lookup/options");
    const custSel = $("#jnCustomer");
    custSel.innerHTML = `<option value="">— Select your organisation —</option>` +
      opts.customers.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    state.signup = { customers: opts.customers, locations: opts.locations, departments: opts.departments };
  } catch (e) { /* ignore */ }
}

function onJoinCust() {
  const cust = $("#jnCustomer").value;
  const locs = (state.signup?.locations || []).filter((l) => String(l.customer_id) === String(cust));
  $("#jnLocation").innerHTML = `<option value="">— Select location/department —</option>` + locs.map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("");
  if ($("#jnDepartment")) $("#jnDepartment").innerHTML = `<option value="">— Select department —</option>`;
}

function onJoinLoc() {
  const loc = $("#jnLocation").value;
  const depts = (state.signup?.departments || []).filter((d) => String(d.location_id) === String(loc));
  const dId = depts[0]?.id || loc;
  if ($("#jnDepartment")) {
    $("#jnDepartment").innerHTML = `<option value="${dId}" selected>— Select department —</option>`;
    $("#jnDepartment").value = String(dId);
  }
}

function onJoinRoleChange() {
  const isCust = $("#jnRole").value === "customer";
  $("#jnCustomerBlock").classList.toggle("hidden", !isCust);
}

$("#showJoinBtn").addEventListener("click", () => {
  const panel = $("#joinPanel");
  panel.classList.toggle("hidden");
  const open = !panel.classList.contains("hidden");
  $("#loginScreen").classList.toggle("align-top", open);
  if (open) loadJoinOptions();
});

$("#jnRole").addEventListener("change", onJoinRoleChange);

$("#joinForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#joinError").classList.add("hidden");
  $("#joinOk").classList.add("hidden");
  const role = $("#jnRole").value;
  const body = {
    name: $("#jnName").value.trim(),
    email: $("#jnEmail").value.trim(),
    phone: $("#jnPhone").value.trim(),
    password: $("#jnPassword").value,
    role,
  };
  if (role === "customer") {
    body.customer_id = $("#jnCustomer").value;
    body.location_id = $("#jnLocation").value;
    const depts = (state.signup?.departments || []).filter((d) => String(d.location_id) === String(body.location_id));
    body.department_id = $("#jnDepartment")?.value || depts[0]?.id || body.location_id;
  }
  try {
    const res = await API.post("/api/signup", body);
    $("#joinOk").textContent = res.message || "Submitted for approval ✓";
    $("#joinOk").classList.remove("hidden");
    $("#joinForm").reset();
    $("#jnCustomerBlock").classList.remove("hidden");
  } catch (err) {
    const el = $("#joinError");
    el.textContent = err.message;
    el.classList.remove("hidden");
  }
});

// ---------------------------------------------------------------- Event wiring
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginError").classList.add("hidden");
  const note = $("#joinNote");
  note.textContent = "";
  const btn = $("#loginBtn");
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    await login($("#loginEmail").value.trim(), $("#loginPassword").value);
    $("#loginEmail").value = "";
    $("#loginPassword").value = "";
    primeAudio();
    syncPushAlerts(); // re-register existing desktop-alert subscription quietly
    render();
  } catch (err) {
    const el = $("#loginError");
    el.textContent = err.message;
    el.classList.remove("hidden");
    if (/awaiting approval|approval/i.test(err.message)) {
      note.textContent = "Your account has been submitted but an administrator must approve it first.";
      note.style.cssText = "font-size:12.5px;color:var(--ink-soft);margin-top:10px;text-align:center";
    }
  }
  btn.disabled = false;
  btn.textContent = "Sign in";
});

$$(".bn-item").forEach((b) =>
  b.addEventListener("click", () => {
    // switching main tabs clears the drill-down history
    state.history = [];
    navigate(b.dataset.view);
  })
);
$("#backBtn").addEventListener("click", goBack);
$("#profileBtn").addEventListener("click", () => navigate("profile"));
$("#fab").addEventListener("click", onFab);
$("#bellBtn").addEventListener("click", openNotifications);

// expose functions used by inline handlers
Object.assign(window, {
  navigate, goBack, closeSheet, setComplaintFilter, setBreakdownFilter,
  goComplaints, goBreakdowns, goEquipment,
  setComplaintStatus, setBreakdownStatus, deleteTicket, openAssignSheet, assignTech, linkBreakdown,
  openAcceptSheet, submitAccept, openBreakdownAcceptSheet, submitBreakdownAccept,
  addComment, addBrokComment, openResolveSheet, confirmResolve,
  openComplaintEditor, saveComplaint, openBreakdownEditor, saveBreakdown,
  openEquipmentEditor, saveEquipment, deleteEquipment, openCustomerEditor, saveCustomer, deleteCustomer,
  openUserEditor, saveUser, deleteUser, toggleCustomerSelect, logout,
  renderCareList, addCareCustomer, removeCareCustomer,
  viewOnboarding, reviewJoin, toggleAlertSound, playAlertSound,
  openAlertSheet, pickAlertPreset, onAlertCustomPicked, clearAlertCustom, alertPreviewCustom,
  uploadPhotos, viewPhoto, deleteAttachment, downloadReport, openExportSheet,
  openNotifications, openNotif, markAllRead,
  setPMFilter, openPMEditor, savePM, deletePM, openPMComplete, confirmPMComplete,
  openPortalEditor, savePortal, showPortalQR, copyPortalURL, downloadQR, deletePortal, onPortalCust,
  viewOrg, setOrgTab,
  openLocationEditor, saveLocation, deleteLocation,
  openDepartmentEditor, saveDepartment, deleteDepartment,
  viewCategories, openCategoryEditor, saveCategory, deleteCategory, eqCategoryPick,
  onCustPick, onLocPick, locOpts, deptOpts,
  onCustPickComplaint, onLocPickComplaint,
  onCustPickBreakdown, onLocPickBreakdown,
  onUserCustPick, onUserLocPick,
  onJoinCust, onJoinLoc, onJoinRoleChange, loadJoinOptions, primeAudio,
  togglePushAlerts, syncPushAlerts, pushStateLabel,
});

async function boot() {
  // Try to restore a session. Even without a stored token, the HttpOnly auth
  // cookie may still be valid, so always ask the server who we are.
  try {
    state.user = await API.get("/api/me");
  } catch (e) {
    API.token = null;
    store.remove("labcare_token");
  }
  render();
  booted = true;
  if (state.user) {
    refreshBell(true); // baseline sync — no sound on login
    syncPushAlerts();  // re-register any existing desktop-alert subscription
  }

  // Detect a Netlify-style split deployment where the frontend is live but the
  // /api proxy target is missing or down — show a clear banner instead of a
  // mysterious "cannot sign in". Only probe when not signed in.
  if (!state.user && location.hostname.includes("netlify.app")) {
    try {
      const r = await fetch("/api/ping", { cache: "no-store" });
      if (!r.ok) showBackendBanner();
    } catch (e) {
      showBackendBanner();
    }
  }
}

function showBackendBanner() {
  const ls = $("#loginScreen");
  if (!ls || ls.querySelector(".backend-warn")) return;
  const bar = document.createElement("div");
  bar.className = "backend-warn";
  bar.innerHTML = `<b>⚠️ Backend not connected.</b> This site is serving the frontend only — the API server is unreachable (login will fail). Check that <code>netlify.toml</code>'s <code>/api/*</code> proxy points to a running HTTPS backend.`;
  ls.prepend(bar);
}

let booted = false;
boot();
