/* LabSynch — mobile web app (complaints & breakdowns for lab equipment) */
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
  async req(method, path, body, options = {}) {
    const headers = { "Content-Type": "application/json" };
    if (this.token) headers["Authorization"] = "Bearer " + this.token;
    const attempts = method === "GET" ? (options.attempts || 3) : 1;
    for (let i = 0; i < attempts; i++) {
      const controller = new AbortController();
      // Bound both the response headers AND body. A stalled API must never
      // leave startup (or a sign-in button) waiting indefinitely.
      const timer = setTimeout(() => controller.abort(), options.timeoutMs || 15000);
      let failure;
      try {
        const res = await fetch(withToken(path), {
          method, headers, credentials: "same-origin",
          body: body ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });
        if ([502, 503, 504].includes(res.status)) {
          throw Object.assign(new Error("The LabSynch server is temporarily unavailable. Please try again shortly."), { status: res.status });
        }
        let data;
        if ((res.headers.get("content-type") || "").includes("application/json")) {
          data = await res.json();
        } else {
          throw Object.assign(new Error("Unexpected response from the LabSynch server (" + res.status + "). Please try again."), { status: res.status });
        }
        if (!res.ok) {
          throw Object.assign(new Error((data && typeof data.error === "string" && data.error) || "Request failed (" + res.status + ")"), { status: res.status });
        }
        return data;
      } catch (e) {
        if (controller.signal.aborted) {
          failure = new Error("The LabSynch server took too long to respond. Please try again shortly.");
        } else if (e instanceof SyntaxError) {
          failure = new Error("Invalid response from the LabSynch server. Please try again shortly.");
        } else if (e.status) {
          failure = e;
        } else {
          failure = new Error("Cannot reach the LabSynch server. Check your connection and try again.");
        }
        const retryable = !e.status || [502, 503, 504].includes(e.status);
        // Never repeat writes, and don't retry authentication/validation errors.
        if (!retryable || i === attempts - 1) throw failure;
      } finally {
        clearTimeout(timer);
      }
      await new Promise((r) => setTimeout(r, 600 * (i + 1)));
    }
  },
  get(p, options) { return this.req("GET", p, undefined, options); },
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
const waChatTextHref = (s, msg) => { const n = waNumber(s); if (!n) return ""; const base = "https://wa.me/" + n; return msg ? base + "?text=" + encodeURIComponent(msg) : base; };
const phoneContactHtml = (phone, opts = {}) => {
  if (!phone) return "";
  const wa = waChatHref(phone);
  const tel = telHref(phone);
  const label = opts.label || phone;
  // tappable WhatsApp link + tel icon; whole number links to WhatsApp as requested
  return `<span class="phone-actions"><a class="wa-link" href="${wa}" target="_blank" rel="noopener" title="Chat on WhatsApp">💬 ${esc(label)}</a><a class="tel-link" href="${tel}" title="Call">📞</a></span>`;
};
const personWithPhoneHtml = (name, phone) => {
  if (!name && !phone) return "—";
  const n = esc(name || "—");
  if (!phone) return n;
  return `${n} ${phoneContactHtml(phone)}`;
};

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
  rating: { t: "Rated the service", ico: "⭐" },
  feedback: { t: "Left feedback", ico: "📝" },
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
  $("#startupStatus").textContent = "";
  $("#startupRetry").classList.add("hidden");
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
    customers: "Organization, Customer & Department", customerDetail: "Organization", users: "Add team member",
    complaintDetail: "Complaint", breakdownDetail: "Breakdown", profile: "My Account",
    pm: "Maintenance", pmDetail: "Maintenance", portals: "QR Portal",
    locations: "Organization, Customer & Department", departments: "Organization, Customer & Department", org: "Organization, Customer & Department",
    onboarding: "Join requests", categories: "Categories",
  };
  $("#tbTitle").textContent = titles[state.view] || "LabSynch";

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
    const fb = d.customer_feedback || { average_rating: null, rating_count: 0 };
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
      <div class="section-title">Organization base &amp; maintenance</div>
      <div class="stats-grid">
        <div class="stat tone-brand"><span class="stat-num">${c.total_customers}</span><span class="stat-label">Organizations</span></div>
        <div class="stat tone-green"><span class="stat-num">${c.resolved_complaints}</span><span class="stat-label">Resolved complaints</span></div>
        <div class="stat ${c.pm_due ? "tone-amber" : "tone-green"}" onclick="navigate('pm')" style="cursor:pointer"><span class="stat-num">${c.pm_due}</span><span class="stat-label">PM due</span></div>
        <div class="stat tone-blue" onclick="navigate('pm')" style="cursor:pointer"><span class="stat-num">${c.pm_total}</span><span class="stat-label">PM schedules</span></div>
      </div>` : ""}

      ${fb.rating_count ? `
      <div class="section-title">Customer satisfaction</div>
      <div class="card" style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
        <div style="font-size:34px;font-weight:800;line-height:1">${fb.average_rating}</div>
        <div>
          ${starsHtml(fb.average_rating, 18)}
          <div style="font-size:12px;color:var(--ink-soft);margin-top:4px">
            average of ${fb.rating_count} customer rating${fb.rating_count === 1 ? "" : "s"} on settled tickets
          </div>
        </div>
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
  const sn = c.equipment_serial ? ` · S/N: ${esc(c.equipment_serial)}` : "";
  return `
    <div class="item" onclick="navigate('complaintDetail',{id:${c.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title">${esc(c.subject)}</div>
          <div class="item-sub">${isCust() ? "" : "<b>" + esc(c.customer_name || "") + "</b> · "}${esc(c.equipment_name || "General")}${sn}</div>
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
    // No loadPhotos() here: attachments are gone from complaints too — the
    // ticket offers a Service report (PDF) instead, rendered by the template.
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
          ${esc(c.customer_name || "")} · ${esc(c.equipment_name || "General equipment")}${c.equipment_serial ? " · S/N: " + esc(c.equipment_serial) : ""}
        </div>
      </div>
    </div>

    <div class="section-title">Details</div>
    <div class="card">
      <div class="kv"><span class="k">Category</span><span class="v">${esc(c.category || "General")}</span></div>
      <div class="kv"><span class="k">Location/Department</span><span class="v">${esc(locDept || "—")}</span></div>
      ${c.equipment_name ? `<div class="kv"><span class="k">Equipment</span><span class="v">${esc(c.equipment_name)}</span></div>` : ""}
      ${c.equipment_serial ? `<div class="kv"><span class="k">Serial number</span><span class="v mono">${esc(c.equipment_serial)}</span></div>` : ""}
      <div class="kv"><span class="k">Opened by</span><span class="v">${personWithPhoneHtml(c.created_by_name, c.created_by_phone)}</span></div>
      ${c.reporter_name ? `<div class="kv"><span class="k">Reporter (portal)</span><span class="v">${personWithPhoneHtml(c.reporter_name, c.reporter_phone)}</span></div>` : (c.reporter_phone ? `<div class="kv"><span class="k">Reporter contact</span><span class="v">${phoneContactHtml(c.reporter_phone)}</span></div>` : "")}
      ${c.created_by_phone && c.reporter_phone && c.created_by_phone !== c.reporter_phone ? "" : ""}
      <div class="kv"><span class="k">Assigned to</span><span class="v">${c.assigned_to_name ? personWithPhoneHtml(c.assigned_to_name, c.assigned_to_phone) : "Unassigned"}</span></div>
      ${c.accepted_by_name ? `<div class="kv"><span class="k">Accepted by</span><span class="v">${personWithPhoneHtml(c.accepted_by_name, c.accepted_by_phone)}${c.accepted_at ? " · " + fmtDate(c.accepted_at) : ""}</span></div>` : ""}
      ${c.accept_reply ? `<div class="kv"><span class="k">Reply to sender</span><span class="v">“${esc(c.accept_reply)}”</span></div>` : ""}
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

    ${feedbackSectionHtml("complaint", c)}

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

// ------------------------------------------------------- Customer feedback
// A settled ticket can be rated 1-5 and commented on by the customer side.
// Mirrors the server's FEEDBACK_STATUSES: a complaint when resolved or closed,
// a breakdown when resolved (breakdowns have no closed status).
const FEEDBACK_STATUSES = { complaint: ["resolved", "closed"], breakdown: ["resolved"] };
const FEEDBACK_STARS = [1, 2, 3, 4, 5];

function starsHtml(n, size) {
  const filled = Math.max(0, Math.min(5, Math.round(n || 0)));
  return `<span style="font-size:${size || 16}px;letter-spacing:1px;white-space:nowrap">`
    + `<span style="color:#f59e0b">${"★".repeat(filled)}</span>`
    + `<span style="color:#cbd5e1">${"☆".repeat(5 - filled)}</span></span>`;
}

function feedbackSectionHtml(kind, rec) {
  const fb = rec.feedback || { rating: null, comments: [] };
  const comments = fb.comments || [];
  const rated = fb.rating ? fb.rating.rating : 0;
  const mine = isCust();
  // The server states it outright; fall back to the status for older payloads.
  const open = rec.feedback_open !== undefined
    ? !!rec.feedback_open
    : (FEEDBACK_STATUSES[kind] || []).indexOf(rec.status) >= 0;
  // Nothing given and nothing to give: leave the section out entirely rather
  // than showing an empty box on every open ticket.
  if (!open && !fb.rating && !comments.length) return "";

  const writeable = open && mine;
  const thread = comments.map((x) => commentHtml({
    user_name: x.author_name || "Customer", created_at: x.created_at, text: x.text,
  })).join("");

  return `
    <div class="section-title">Customer feedback</div>
    <div class="card">
      ${fb.rating ? `
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          ${starsHtml(rated, 20)}
          <b style="font-size:14px">${rated} / 5</b>
          <span style="font-size:12px;color:var(--ink-soft)">by ${esc(fb.rating.rated_by || "Customer")}</span>
        </div>`
      : `<p style="font-size:13px;color:var(--ink-soft);margin:0">${
          writeable ? "Not rated yet." : "The customer did not leave a rating."}</p>`}

      ${writeable ? `
        <div style="margin-top:12px">
          <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:6px">
            ${fb.rating ? "Change your rating:" : "How was the service? Tap a star — this is optional."}
          </div>
          <div id="fbStars" style="display:flex;gap:6px;font-size:30px;line-height:1">
            ${FEEDBACK_STARS.map((n) => `<span role="button" aria-label="${n} star${n > 1 ? "s" : ""}"
              onclick="rateTicket('${kind}', ${rec.id}, ${n})"
              style="cursor:pointer;color:${n <= rated ? "#f59e0b" : "#cbd5e1"}">★</span>`).join("")}
          </div>
        </div>` : ""}

      <div style="margin-top:${fb.rating || writeable ? 14 : 0}">
        ${thread || (writeable ? "" : `<p style="color:var(--ink-soft);font-size:13px;margin:0">No customer comments.</p>`)}
        ${writeable ? `
          <div style="display:flex;gap:8px;margin-top:${thread ? 12 : 0}">
            <input id="fbInput" placeholder="Add a comment… (optional)" onkeydown="if(event.key==='Enter')addFeedback('${kind}', ${rec.id})">
            <button class="btn btn-primary btn-sm" onclick="addFeedback('${kind}', ${rec.id})">Send</button>
          </div>` : ""}
      </div>

      ${open && !mine ? `<p style="font-size:11.5px;color:var(--ink-soft);margin:12px 0 0">Feedback comes from the customer side — you can read it but not write it.</p>` : ""}
    </div>`;
}

async function reloadTicketDetail() {
  if (state.view === "complaintDetail") await viewComplaintDetail($("#view"));
  else if (state.view === "breakdownDetail") await viewBreakdownDetail($("#view"));
}

async function rateTicket(kind, id, stars) {
  showLoading();
  try {
    const r = await API.post(`/api/tickets/${kind}/${id}/rating`, { rating: stars });
    toast(r && r.created === false ? "Rating updated — thank you" : "Thanks for your rating", "success");
    await reloadTicketDetail();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function addFeedback(kind, id) {
  const input = $("#fbInput");
  const text = ((input && input.value) || "").trim();
  if (!text) return;
  showLoading();
  try {
    await API.post(`/api/tickets/${kind}/${id}/feedback`, { text });
    if (input) input.value = "";
    toast("Feedback added", "success");
    await reloadTicketDetail();
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
    b.unshift(`<div class="accept-banner">✔ Accepted by <b>${esc(rec.accepted_by_name)}</b>${rec.accepted_by_phone ? " " + phoneContactHtml(rec.accepted_by_phone) : ""}${rec.accepted_at ? ` · ${fmtDate(rec.accepted_at)}` : ""}</div>`);
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
  const sn = b.equipment_serial ? `<span class="badge" style="font-family:ui-monospace,monospace;font-size:11px;font-weight:600;background:var(--brand-soft,#fee2e2);color:var(--brand,#b91c1c)">S/N: ${esc(b.equipment_serial)}</span>` : "";
  const scope = isCust() ? "" : (b.customer_name ? "<b>" + esc(b.customer_name) + "</b> · " : "");
  return `
    <div class="item" onclick="navigate('breakdownDetail',{id:${b.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <span>${esc(b.equipment_name || "Equipment")}</span>
            ${sn}
          </div>
          <div class="item-sub">${scope}${esc(truncate(b.fault_description, 90))}</div>
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
    // No loadPhotos() here: breakdown tickets no longer take attachments —
    // they offer a Service report (PDF) instead, rendered by the template.
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
        ${b.equipment_serial ? `<div style="margin-top:4px"><span class="badge" style="font-family:ui-monospace,monospace;font-size:11.5px;font-weight:600;background:var(--brand-soft,#fee2e2);color:var(--brand,#b91c1c)">S/N: ${esc(b.equipment_serial)}</span></div>` : ""}
        <div class="item-sub" style="margin-top:6px">${esc(b.customer_name || "")}</div>
      </div>
    </div>

    <div class="section-title">Fault description</div>
    <div class="card"><div class="desc-box">${esc(b.fault_description)}</div></div>

    <div class="section-title">Details</div>
    <div class="card">
      <div class="kv"><span class="k">Location/Department</span><span class="v">${esc(b.location_name || b.department_name || "—")}</span></div>
      ${b.equipment_serial ? `<div class="kv"><span class="k">Serial number</span><span class="v mono">${esc(b.equipment_serial)}</span></div>` : ""}
      ${b.complaint_id ? `<div class="kv"><span class="k">Source complaint</span><span class="v" style="color:var(--brand);text-decoration:underline" onclick="navigate('complaintDetail',{id:${b.complaint_id}})">${esc("View")}</span></div>` : ""}
      <div class="kv"><span class="k">Opened by</span><span class="v">${personWithPhoneHtml(b.reported_by_name, b.reported_by_phone)}</span></div>
      ${b.reporter_name ? `<div class="kv"><span class="k">Reporter (portal)</span><span class="v">${personWithPhoneHtml(b.reporter_name, b.reporter_phone)}</span></div>` : (b.reporter_phone && b.reporter_phone !== b.reported_by_phone ? `<div class="kv"><span class="k">Reporter contact</span><span class="v">${phoneContactHtml(b.reporter_phone)}</span></div>` : "")}
      <div class="kv"><span class="k">Assigned to</span><span class="v">${b.assigned_to_name ? personWithPhoneHtml(b.assigned_to_name, b.assigned_to_phone) : "Unassigned"}</span></div>
      ${b.accepted_by_name ? `<div class="kv"><span class="k">Accepted by</span><span class="v">${personWithPhoneHtml(b.accepted_by_name, b.accepted_by_phone)}${b.accepted_at ? " · " + fmtDate(b.accepted_at) : ""}</span></div>` : ""}
      ${b.accept_reply ? `<div class="kv"><span class="k">Reply to sender</span><span class="v">“${esc(b.accept_reply)}”</span></div>` : ""}
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

    <div class="section-title">History</div>
    <div class="card" id="historyBox"><div class="empty" style="padding:12px"><div class="spinner" style="margin:0 auto"></div></div></div>

    <div class="section-title">Work log</div>
    <div class="card" id="commentsBox">
      ${(b.comments || []).map(commentHtml).join("") || '<p style="color:var(--ink-soft);font-size:13px">No updates yet.</p>'}
      <div style="display:flex;gap:8px;margin-top:12px">
        <input id="commentInput" placeholder="Add an update…" onkeydown="if(event.key==='Enter')addBrokComment()">
        <button class="btn btn-primary btn-sm" onclick="addBrokComment()">Send</button>
      </div>
    </div>

    ${feedbackSectionHtml("breakdown", b)}

    <div class="section-title">Report</div>
    <div class="card">
      <div class="action-panel">
        <button class="btn btn-ghost btn-sm" onclick="downloadReport('/api/breakdowns/${b.id}/report.pdf')">📄 Service report (PDF)</button>
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

// Group equipment by Organization and Location/Department — equipment name can be shared,
// separated and distinguished by details (e.g. serial number, model).
function renderEquipmentGrouped(list) {
  const groups = new Map();
  for (const e of list) {
    const cust = e.customer_name || "Organization";
    const loc = e.location_name || e.department_name || "No location/department";
    const key = (e.customer_id || cust) + "||" + loc;
    if (!groups.has(key)) groups.set(key, { cust, loc, items: [] });
    groups.get(key).items.push(e);
  }
  let html = "";
  for (const g of groups.values()) {
    const title = isCust()
      ? `📍 ${esc(g.loc)}`
      : `🏢 ${esc(g.cust)} › 📍 ${esc(g.loc)}`;
    html += `
      <div class="section-title" style="margin-top:14px;display:flex;align-items:center;gap:6px">
        <span>${title}</span>
        <span class="badge" style="background:var(--bg);color:var(--ink-soft);margin-left:auto">${g.items.length} ${g.items.length === 1 ? "asset" : "assets"}</span>
      </div>
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
  const snBadge = e.serial_number
    ? `<span class="badge" style="font-family:ui-monospace,monospace;font-size:11.5px;font-weight:600;background:var(--brand-soft,#fee2e2);color:var(--brand,#b91c1c)">S/N: ${esc(e.serial_number)}</span>`
    : `<span class="badge" style="font-size:11.5px;background:var(--bg);color:var(--ink-soft)">No S/N</span>`;
  const details = [
    e.model ? `Model: ${esc(e.model)}` : null,
    e.serial_number ? `S/N: ${esc(e.serial_number)}` : null,
  ].filter(Boolean).join(" · ");
  return `
    <div class="item" onclick="navigate('equipmentDetail',{id:${e.id}})">
      <div class="item-top">
        <div class="item-main">
          <div class="item-title" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="font-weight:600">${esc(e.name)}</span>
            ${snBadge}
          </div>
          <div class="item-sub mono">${esc(details || "No details specified")}</div>
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
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <h2 style="font-size:18px;margin:0">${esc(e.name)}</h2>
            ${e.serial_number ? `<span class="badge" style="font-family:ui-monospace,monospace;font-size:12px;font-weight:600;background:var(--brand-soft,#fee2e2);color:var(--brand,#b91c1c)">S/N: ${esc(e.serial_number)}</span>` : `<span class="badge" style="font-size:12px;background:var(--bg);color:var(--ink-soft)">No S/N</span>`}
          </div>
          <div class="item-sub mono" style="margin-top:4px">${esc(e.model ? "Model: " + e.model : "No model specified")}</div>
          <div class="item-meta" style="margin-top:8px">
            ${e.category ? `<span class="badge" style="background:var(--bg);color:var(--ink-soft)">${esc(e.category)}</span>` : ""}
            ${e.status === "retired" ? '<span class="badge b-closed">Retired</span>' : '<span class="badge b-resolved">Active</span>'}
          </div>
        </div>
      </div>
      <div class="section-title">Asset details</div>
      <div class="card">
        <div class="kv"><span class="k">Organization</span><span class="v">${esc(e.customer_name || "—")}</span></div>
        <div class="kv"><span class="k">Location/Department</span><span class="v">${esc(e.location_name || e.department_name || "—")}</span></div>
        <div class="kv"><span class="k">Serial number</span><span class="v mono">${esc(e.serial_number || "—")}</span></div>
        <div class="kv"><span class="k">Model</span><span class="v">${esc(e.model || "—")}</span></div>
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
    ? [["customers", "🏢 Organizations"], ["locations", "📍 Location/Department"]]
    : [["customers", "🏢 My organization"]];
  const addBtn = t === "customers"
    ? (isAdmin() ? `<button class="btn btn-primary" onclick="openCustomerEditor(false)">＋ Add organization</button>` : "")
    : (isTech() ? `<button class="btn btn-primary" onclick="openLocationEditor(false)">＋ Add location/department</button>` : "");
  v.innerHTML = `
    <div class="hero" style="background:linear-gradient(135deg,#450a0a,#b91c1c)">
      <h2>Organization, Customer & Department</h2>
      <p>Manage organizations, customers and their locations/departments in one place.</p>
    </div>
    <div class="seg" style="margin:14px 0 12px">
      ${tabs.map(([k, label]) => `<button class="${t === k ? "active" : ""}" onclick="setOrgTab('${k}')">${label}</button>`).join("")}
    </div>
    ${addBtn ? `<div class="btn-row" style="margin-bottom:12px">${addBtn}</div>` : ""}
    <div id="locList" class="${t === "locations" ? "" : "hidden"}"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>
    <div id="pendingCare" class="${t === "customers" ? "" : "hidden"}"></div>
    <div id="custList" class="${t === "customers" ? "" : "hidden"}"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  if (t === "customers") { await refreshPendingCare(); await refreshCustomers(); }
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
      : emptyState("🏢", "No organizations yet", "Add your first organization to start tracking.", "Add organization");
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
    if (!cu) throw new Error("Organization not found");
    const [eq, cmp, brk, locs] = await Promise.all([
      API.get("/api/equipment?customer_id=" + id),
      API.get("/api/complaints?customer_id=" + id),
      API.get("/api/breakdowns?customer_id=" + id),
      API.get("/api/locations?customer_id=" + id),
    ]);
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
      <div class="section-title">Locations/Departments (${locs.length})</div>
      ${locs.length ? `<div class="list">${locs.map((l) => `
        <div class="item" onclick="${isTech() ? `openLocationEditor(true, ${l.id})` : ""}">
          <div class="item-top">
            <div class="c-avatar">📍</div>
            <div class="item-main">
              <div class="item-title">${esc(l.name)}</div>
              <div class="item-sub">${esc([l.city, l.address].filter(Boolean).join(" · ") || "No address specified")}</div>
            </div>
          </div>
          <div class="item-meta">
            <span class="badge" style="background:var(--bg);color:var(--ink-soft)">${l.equipment_count} equipment</span>
          </div>
        </div>`).join("")}</div>` : `<div class="card"><p style="color:var(--ink-soft);font-size:13px">No locations/departments registered.</p></div>`}
      <div class="section-title">Equipment (${eq.length})</div>
      ${eq.length ? `<div class="list">${eq.map(equipmentCard).join("")}</div>` : `<div class="card"><p style="color:var(--ink-soft);font-size:13px">No equipment registered.</p></div>`}
      <div class="section-title">Recent complaints (${cmp.length})</div>
      ${cmp.length ? `<div class="list">${cmp.slice(0, 3).map(complaintCard).join("")}</div>` : `<div class="card"><p style="color:var(--ink-soft);font-size:13px">None.</p></div>`}
      ${isAdmin() ? `<div class="action-panel" style="margin-top:14px"><button class="btn btn-ghost" onclick="openCustomerEditor(true)">✏️ Edit organization</button></div>` : ""}
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

// Group locations/departments by Organization/Customer — location/department name can be
// shared across different organizations/customers, separated by which organization they belong to.
function renderLocationsGrouped(list) {
  const groups = new Map();
  for (const l of list) {
    const cust = l.customer_name || "Organization";
    const key = String(l.customer_id || cust);
    if (!groups.has(key)) groups.set(key, { cust, items: [] });
    groups.get(key).items.push(l);
  }
  let html = "";
  for (const g of groups.values()) {
    html += `
      <div class="section-title" style="margin-top:16px;display:flex;align-items:center;gap:6px">
        <span>🏢</span>
        <span style="font-weight:700">${esc(g.cust)}</span>
        <span class="badge" style="background:var(--bg);color:var(--ink-soft);margin-left:auto">${g.items.length} ${g.items.length === 1 ? "location/department" : "locations/departments"}</span>
      </div>
      <div class="list">${g.items.map((l) => `
        <div class="item" onclick="${isTech() ? `openLocationEditor(true, ${l.id})` : ""}">
          <div class="item-top">
            <div class="c-avatar">📍</div>
            <div class="item-main">
              <div class="item-title">${esc(l.name)}</div>
              <div class="item-sub">${esc([l.city, l.address].filter(Boolean).join(" · ") || "No address specified")}</div>
            </div>
          </div>
          <div class="item-meta">
            <span class="badge" style="background:var(--bg);color:var(--ink-soft)">${l.equipment_count} equipment</span>
          </div>
        </div>`).join("")}</div>`;
  }
  return html;
}

async function refreshLocations() {
  const box = $("#locList");
  try {
    const list = await API.get("/api/locations");
    state.locations = list;
    box.innerHTML = list.length
      ? renderLocationsGrouped(list)
      : emptyState("📍", "No locations/departments", "Add your first location/department.", "Add location/department");
  } catch (e) {
    box.innerHTML = `<div class="empty"><h3>Load failed</h3><p>${esc(e.message)}</p></div>`;
  }
}

async function openLocationEditor(edit, id) {
  let customers = [];
  try { customers = await API.get("/api/customers"); } catch (e) {}
  const l = edit ? state.locations?.find((x) => x.id === id) : null;
  const hasCustomers = customers && customers.length > 0;
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit location/department" : "Add location/department"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Location/Department name *</span><input id="locName" value="${esc(l ? l.name : "")}" placeholder="e.g. Molecular Lab"></label>
      <label class="field"><span>Organization *</span>
        ${hasCustomers ? `<select id="locCustomer">
          ${customers.map((x) => `<option value="${x.id}" ${l && String(l.customer_id) === String(x.id) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select>` : `<div style="padding:12px;border:1.5px dashed var(--line);border-radius:12px;background:var(--bg);color:var(--ink-soft);font-size:13px">
          No organizations found. As Tenant Admin, please create an Organization first via <b>Organization, Customer & Department → ＋ Add organization</b>, then add its Location/Department here. The new organization will automatically be linked to your account.
        </div><input type="hidden" id="locCustomer" value="">`}
        </label>
      <label class="field"><span>City</span><input id="locCity" value="${esc(l ? l.city : "")}" placeholder="e.g. Kuala Lumpur"></label>
      <label class="field"><span>Address</span><input id="locAddress" value="${esc(l ? l.address : "")}"></label>
    </div>
    <div class="sheet-foot">
      ${edit ? `<button class="btn btn-danger" style="flex:0 0 auto;padding:11px 16px" onclick="deleteLocation(${l.id})">Delete</button>` : ""}
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary-2" ${!hasCustomers && !edit ? "disabled style='opacity:.5;pointer-events:none'" : ""} onclick="saveLocation(${edit ? l.id : "null"})">${edit ? "Save" : "Add location/department"}</button>
    </div>`);
}

async function saveLocation(id) {
  const rawCid = $("#locCustomer")?.value;
  const body = {
    name: $("#locName").value.trim(),
    customer_id: rawCid ? parseInt(rawCid, 10) : null,
    city: $("#locCity").value.trim(),
    address: $("#locAddress").value.trim(),
  };
  if (!body.name) { toast("Location/department name is required", "error"); return; }
  if (!body.customer_id) { toast("Organization is required — create an Organization first", "error"); return; }
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
          <div class="item-sub">${a.role === "customer" ? "Scope: " + scope : "LabSynch engineer"}</div>
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
      ${u.customer_name ? `<div class="kv"><span class="k">Organization</span><span class="v">${esc(u.customer_name)}</span></div>` : ""}
      ${(u.location_name || u.department_name) ? `<div class="kv"><span class="k">Location/Department</span><span class="v">${esc(u.location_name || u.department_name)}</span></div>` : ""}
    </div>
    ${isTenantAdmin ? `
    <div class="section-title">Organizations I care for</div>
    <div class="card">
      <p style="font-size:13px;color:var(--ink-soft);margin:0 0 10px">Pick which organizations you manage. You can add engineers and view tickets, equipment and reports for every organization on your list.</p>
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
    host.innerHTML = `<div class="empty"><p>Couldn't load your organizations: ${esc(e.message)}</p></div>`;
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
    : `<p style="font-size:13px;color:var(--ink-soft);margin:0">You already care for every organization.</p>`}`;
}

async function addCareCustomer(id) {
  if (!id) return;
  showLoading();
  try {
    await API.post("/api/my-customers/" + id);
    toast("Added to your organization list", "success");
    state.user = await API.get("/api/me");
    await renderCareList();
  } catch (e) { toast(e.message, "error"); }
  hideLoading();
}

async function removeCareCustomer(id) {
  confirmDialog("Remove organization", "Deselecting this organization stops you from managing its users, tickets and equipment.", "Remove", async () => {
    showLoading();
    try {
      await API.del("/api/my-customers/" + id);
      toast("Removed from your organization list", "success");
      state.user = await API.get("/api/me");
      await renderCareList();
    } catch (e) { toast(e.message, "error"); }
    hideLoading();
  });
}

// ------------------------------------------- Pending care (new join requests)
// A signup that creates a brand-new organization leaves it with no tenant
// admin. Every tenant admin is asked whether it is under their care; the first
// to claim it wins. The master may assign it to a chosen admin instead.
async function refreshPendingCare() {
  const host = $("#pendingCare");
  if (!host) return;
  if (!isAdmin()) { host.innerHTML = ""; return; }
  let data = null;
  try { data = await API.get("/api/customers/pending-care"); }
  catch (e) { host.innerHTML = ""; return; }
  state.pendingCare = data || { customers: [] };
  const list = state.pendingCare.customers || [];
  if (!list.length) { host.innerHTML = ""; return; }
  const master = isMaster();
  host.innerHTML = `
    <div class="action-panel" style="margin-bottom:12px">
      <div class="section-title">New organizations awaiting care</div>
      <p style="font-size:12.5px;color:var(--ink-soft);margin:2px 0 10px">
        ${master
          ? "A join request created these and no tenant admin has claimed them yet. Assign one, or let the tenant admins claim it themselves."
          : "A join request created these and nobody is looking after them yet. Take one into your care — the first admin to claim it wins."}
      </p>
      ${list.map((cu) => `
        <div class="item" style="margin-bottom:8px">
          <div class="item-top">
            <div class="c-avatar" style="width:40px;height:40px;font-size:14px">${esc(initials(cu.name))}</div>
            <div class="item-main">
              <div class="item-title">${esc(cu.name)}</div>
              <div class="item-sub">${esc(cu.requested_by || cu.contact_name || "—")}${cu.requested_by_email ? " · " + esc(cu.requested_by_email) : ""}${cu.phone ? " · " + esc(cu.phone) : ""}</div>
            </div>
          </div>
          <div class="btn-row" style="margin-top:8px">
            ${master
              ? `<button class="btn btn-primary btn-sm" onclick="openAssignCare(${cu.id})">Assign to an admin…</button>`
              : `<button class="btn btn-primary btn-sm" onclick="takeCare(${cu.id})">Take into my care</button>
                 <button class="btn btn-ghost btn-sm" onclick="declineCare(${cu.id})">Not mine</button>`}
          </div>
          ${cu.declines ? `<div style="font-size:11px;color:var(--ink-soft);margin-top:6px">${cu.declines} tenant admin${cu.declines > 1 ? "s" : ""} already said not theirs</div>` : ""}
        </div>`).join("")}
    </div>`;
}

async function takeCare(id) {
  showLoading();
  try {
    await API.post("/api/customers/" + id + "/take-care");
    toast("Taken into your care", "success");
    state.user = await API.get("/api/me");
    await refreshPendingCare();
    await refreshCustomers();
  } catch (e) {
    toast(e.message, "error");
    await refreshPendingCare();
  }
  hideLoading();
}

async function declineCare(id, fromNotif) {
  confirmDialog("Not under your care",
    "It leaves your list, but the other tenant admins and the master can still claim or assign it.",
    "Not mine", async () => {
      showLoading();
      try {
        await API.post("/api/customers/" + id + "/decline-care");
        toast("Removed from your pending list", "success");
        if (fromNotif) { await openNotifications(); refreshBell(); }
        else await refreshPendingCare();
      } catch (e) {
        toast(e.message, "error");
        if (fromNotif) await openNotifications(); else await refreshPendingCare();
      }
      hideLoading();
    });
}

// Answering straight from the bell, without leaving the notification sheet.
async function careDecision(id, action) {
  if (action === "decline") { closeSheet(); return declineCare(id, true); }
  showLoading();
  try {
    await API.post("/api/customers/" + id + "/take-care");
    toast("Taken into your care", "success");
    state.user = await API.get("/api/me");
    await openNotifications();
    refreshBell();
  } catch (e) {
    toast(e.message, "error");
    await openNotifications();
  }
  hideLoading();
}

function openAssignCare(id) {
  const admins = (state.pendingCare && state.pendingCare.tenant_admins) || [];
  const cu = ((state.pendingCare && state.pendingCare.customers) || [])
    .find((x) => String(x.id) === String(id));
  if (!admins.length) { toast("There is no tenant admin to assign this to yet", "error"); return; }
  openSheet(`
    <div class="sheet-head"><h3>Assign care</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <p style="font-size:14px;color:var(--ink-soft);margin-top:0">Which tenant admin looks after <b>${esc(cu ? cu.name : "this organization")}</b>?</p>
      <label class="field"><span>Tenant admin</span>
        <select id="assignCareSel">
          ${admins.map((a) => `<option value="${a.id}">${esc(a.name)} — ${esc(a.email)}</option>`).join("")}
        </select>
      </label>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-ghost" onclick="closeSheet()">Cancel</button>
      <button class="btn btn-primary" onclick="assignCare(${id}, document.getElementById('assignCareSel').value)">Assign</button>
    </div>`);
}

async function assignCare(id, adminId) {
  closeSheet();
  if (!adminId) { toast("Choose a tenant admin", "error"); return; }
  showLoading();
  try {
    await API.post("/api/customers/" + id + "/assign-care", { admin_id: adminId });
    toast("Assigned", "success");
    await refreshPendingCare();
    await refreshCustomers();
  } catch (e) {
    toast(e.message, "error");
    await refreshPendingCare();
  }
  hideLoading();
}

function viewMore(v) {
  const items = [];
  items.push(`<button class="menu-item" onclick="navigate('profile')"><span class="mi-ico">👤</span> My account <span class="mi-arrow">›</span></button>`);
  if (isAdmin() || isTech()) items.push(`<button class="menu-item" onclick="navigate('org')"><span class="mi-ico">🏢</span> Organization, Customer &amp; Department <span class="mi-arrow">›</span></button>`);
  if (isMaster()) items.push(`<button class="menu-item" onclick="navigate('categories')"><span class="mi-ico">🏷️</span> Categories <span class="mi-arrow">›</span></button>`);
  if (isAdmin()) items.push(`<button class="menu-item" onclick="navigate('users')"><span class="mi-ico">👥</span> Add team member <span class="mi-arrow">›</span></button>`);
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
      <p style="font-size:12.5px;color:var(--ink-soft)">LabSynch v1.1 — complaints &amp; breakdowns, photos, reports &amp; notifications.</p>
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
  state.equipment = equipment;
  const c = edit ? state.complaintDetail : null;
  const defCust = c && c.customer_id ? c.customer_id : (customers.length ? customers[0].id : "");

  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit complaint" : "Log complaint"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Subject *</span><input id="fSubject" value="${esc(c ? c.subject : "")}" placeholder="What went wrong?"></label>
      <label class="field"><span>Description</span><textarea id="fDesc" placeholder="Details, symptoms, when it started…">${esc(c ? c.description : "")}</textarea></label>
      ${isTech() ? `
      <label class="field"><span>Organization *</span>
        <select id="fCustomer" onchange="onCustPickComplaint()">
          ${customers.map((x) => `<option value="${x.id}" ${c && c.customer_id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>
      <label class="field"><span>Location/Department</span>
        <select id="fLocation" onchange="onLocPickComplaint()">
          ${locOpts(state.locations || [], c && c.location_id, defCust)}
        </select></label>
      <select id="fDepartment" style="display:none">
        ${deptOpts(state.departments || [], c && c.department_id, c && c.location_id)}
      </select>` : ""}
      <label class="field"><span>Related equipment</span>
        <select id="fEquipment" onchange="onEquipmentPickComplaint()">
          ${eqOpts(equipment, c && c.equipment_id, isTech() ? defCust : null, "— None / general —")}
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
    body.customer_id = $("#fCustomer").value ? parseInt($("#fCustomer").value, 10) : null;
    body.location_id = $("#fLocation").value ? parseInt($("#fLocation").value, 10) : null;
    const deptMatch = (state.departments || []).find((d) => String(d.location_id) === String(body.location_id));
    body.department_id = $("#fDepartment")?.value ? parseInt($("#fDepartment").value, 10) : (deptMatch?.id ? parseInt(deptMatch.id, 10) : (body.location_id || null));
    body.assigned_to = ($("#fAssignee").value ? parseInt($("#fAssignee").value, 10) : null);
    // Responsible tenant admin auto-resolved by backend based on organization — no manual picker
  }
  if (!body.subject) { toast("Subject is required", "error"); return; }
  if (isTech() && !body.customer_id) { toast("Organization is required", "error"); return; }
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
  state.equipment = equipment;
  const b = edit && !prefill ? state.breakdownDetail : null;
  const p = prefill || {};
  const defCust = (b ? b.customer_id : p.customer_id) || (isTech() && customers.length ? customers[0].id : "");

  openSheet(`
    <div class="sheet-head"><h3>${edit && !prefill ? "Edit breakdown" : "Report breakdown"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Affected equipment</span>
        <select id="bEquipment" onchange="onEquipmentPickBreakdown()">
          ${eqOpts(equipment, (b ? b.equipment_id : p.equipment_id), isTech() ? defCust : null, "— Select —")}
        </select></label>
      ${isTech() ? `
      <label class="field"><span>Organization *</span>
        <select id="bCustomer" onchange="onCustPickBreakdown()">
          ${customers.map((x) => `<option value="${x.id}" ${(b ? b.customer_id === x.id : p.customer_id === x.id) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>
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
    body.customer_id = $("#bCustomer").value ? parseInt($("#bCustomer").value, 10) : null;
    body.location_id = $("#bLocation").value ? parseInt($("#bLocation").value, 10) : null;
    const deptMatch = (state.departments || []).find((d) => String(d.location_id) === String(body.location_id));
    body.department_id = $("#bDepartment")?.value ? parseInt($("#bDepartment").value, 10) : (deptMatch?.id ? parseInt(deptMatch.id, 10) : (body.location_id || null));
    body.complaint_id = $("#bComplaint").value ? parseInt($("#bComplaint").value, 10) : null;
    body.assigned_to = ($("#bAssignee").value ? parseInt($("#bAssignee").value, 10) : null);
    // Responsible tenant admin auto-resolved by backend
  }
  if (!body.fault_description) { toast("Fault description is required", "error"); return; }
  if (isTech() && !body.customer_id) { toast("Organization is required", "error"); return; }
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
      <div class="section-label">Organization</div>
      <label class="field"><span>Organization *</span>
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
      <label class="field"><span>Serial number</span><input id="eqSerial" value="${esc(e ? e.serial_number : "")}" placeholder="e.g. SN-00123 (identifies this asset)"></label>
      <label class="field"><span>Category</span>
        <select id="eqCategory" onchange="eqCategoryPick()">
          <option value="">— Select —</option>
          ${catNames.map((x) => `<option value="${esc(x)}" ${currentCat === x ? "selected" : ""}>${esc(x)}</option>`).join("")}
          ${isTech() ? `<option value="__custom__">＋ New category…</option>` : ""}
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
// Tenant admins who may be named as a user's "linked tenant admin". Calling
// /api/tenant-admins with NO customer_id returns exactly the actor's own tenant:
// every admin whose primary organization or care list overlaps theirs — and, for
// an admin with no organizations yet, just themselves.
let _peerAdminsCache = null;
async function tenantAdminPeers() {
  if (_peerAdminsCache) return _peerAdminsCache;
  try {
    _peerAdminsCache = await API.get("/api/tenant-admins");
    return _peerAdminsCache;
  } catch (e) { return []; }
}

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
    + admins.map((a) => `<option value="${a.id}" ${String(selectedId) === String(a.id) ? "selected" : ""}>${esc(a.name)}</option>`).join("");
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
  return `<option value="">— Select location/department —</option>` + list.map((l) => {
    const custPrefix = (!customerId && l.customer_name) ? `${esc(l.customer_name)} — ` : "";
    return `<option value="${l.id}" ${String(selectedId) === String(l.id) ? "selected" : ""}>${custPrefix}${esc(l.name)}</option>`;
  }).join("");
}

function deptOpts(departments, selectedId, locationId) {
  const list = locationId ? departments.filter((d) => String(d.location_id) === String(locationId)) : departments;
  return `<option value="">— Select department —</option>` + list.map((d) =>
    `<option value="${d.id}" ${String(selectedId) === String(d.id) ? "selected" : ""}>${esc(d.name)}</option>`).join("");
}

function eqOpts(equipment, selectedId, customerId, placeholder = "— Select —") {
  const list = customerId ? equipment.filter((x) => String(x.customer_id) === String(customerId)) : equipment;
  return (placeholder ? `<option value="">${placeholder}</option>` : "") + list.map((x) => {
    const custPrefix = (!customerId && x.customer_name) ? `${esc(x.customer_name)} — ` : "";
    const sn = x.serial_number ? `S/N: ${x.serial_number}` : "No S/N";
    const model = x.model ? ` · ${x.model}` : "";
    const loc = x.location_name || x.department_name ? ` (📍 ${x.location_name || x.department_name})` : "";
    const label = `${custPrefix}${esc(x.name)} [${esc(sn)}${esc(model)}]${esc(loc)}`;
    return `<option value="${x.id}" ${String(selectedId) === String(x.id) ? "selected" : ""}>${label}</option>`;
  }).join("");
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

function onEquipmentPickComplaint() {
  const eqId = $("#fEquipment")?.value;
  if (!eqId) return;
  const eq = (state.equipment || []).find((x) => String(x.id) === String(eqId));
  if (eq) {
    if (eq.location_id && $("#fLocation")) {
      $("#fLocation").value = String(eq.location_id);
      onLocPickComplaint();
    }
    if (eq.category && $("#fCategory")) {
      const match = Array.from($("#fCategory").options).find((o) => o.value.toLowerCase() === eq.category.toLowerCase());
      if (match) $("#fCategory").value = match.value;
    }
  }
}

function onEquipmentPickBreakdown() {
  const eqId = $("#bEquipment")?.value;
  if (!eqId) return;
  const eq = (state.equipment || []).find((x) => String(x.id) === String(eqId));
  if (eq && eq.location_id && $("#bLocation")) {
    $("#bLocation").value = String(eq.location_id);
    onLocPickBreakdown();
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
  if ($("#fEquipment")) {
    $("#fEquipment").innerHTML = eqOpts(state.equipment || [], null, cust, "— None / general —");
    $("#fEquipment").value = "";
  }
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
  if ($("#bEquipment")) {
    $("#bEquipment").innerHTML = eqOpts(state.equipment || [], null, cust, "— Select —");
    $("#bEquipment").value = "";
  }
}

function onLocPickBreakdown() {
  const loc = $("#bLocation").value;
  const depts = (state.departments || []).filter((d) => String(d.location_id) === String(loc));
  if ($("#bDepartment")) {
    $("#bDepartment").innerHTML = deptOpts(state.departments || [], depts[0]?.id || null, loc);
    $("#bDepartment").value = depts[0]?.id ? String(depts[0].id) : (loc || "");
  }
}

function onCustPickPM() {
  const cust = $("#pmCustomer")?.value;
  if ($("#pmEquipment")) {
    $("#pmEquipment").innerHTML = eqOpts(state.equipment || [], null, cust, "— None / general —");
    $("#pmEquipment").value = "";
  }
}

async function saveEquipment(id) {
  let category = $("#eqCategory").value;
  if (category === "__custom__") {
    category = ($("#eqCategoryCustom").value || "").trim();
    if (!category) { toast("Please name the new category", "error"); return; }
    // Register it so it joins the shared list and is offered from now on.
    // A duplicate just means somebody else already added that name. Anything
    // else is worth saying out loud — the equipment still saves with this
    // category, but the name would not appear in the list for next time.
    try {
      await API.post("/api/categories", { name: category });
    } catch (e) {
      if (!/already exists/i.test(e.message || "")) {
        toast("The equipment will save, but the new category could not be added to the shared list: " + e.message, "error");
      }
    }
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
  if (isTech() && !body.customer_id) { toast("Organization is required", "error"); return; }
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
    <div class="sheet-head"><h3>${edit ? "Edit organization" : "Add organization"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Organization name *</span><input id="cuName" value="${esc(cu ? cu.name : "")}" placeholder="e.g. Hospital/Clinic"></label>
      <label class="field"><span>Contact person</span><input id="cuContact" value="${esc(cu ? cu.contact_name : "")}"></label>
      <label class="field"><span>Email</span><input id="cuEmail" type="email" value="${esc(cu ? cu.email : "")}"></label>
      <label class="field"><span>Phone</span><input id="cuPhone" value="${esc(cu ? cu.phone : "")}"></label>
      <label class="field"><span>City</span><input id="cuCity" value="${esc(cu ? cu.city : "")}"></label>
      <label class="field"><span>Address</span><input id="cuAddress" value="${esc(cu ? cu.address : "")}"></label>
      ${!edit ? `
      <div class="section-title" style="margin-top:18px">Customer login (optional — creates a customer account for this organization)</div>
      <label class="field"><span>Customer full name</span><input id="cuLoginName" placeholder="e.g. Lab Manager"></label>
      <label class="field"><span>Customer login email</span><input id="cuLoginEmail" type="email" placeholder="customer@hospital.com"></label>
      <label class="field"><span>Customer login password</span><input id="cuLoginPassword" type="password" placeholder="Min 6 characters"></label>
      <label class="field"><span>Location/Department for customer (optional, defaults to Main Lab)</span><input id="cuLoginLocation" placeholder="e.g. Molecular Lab"></label>
      <small style="display:block;margin-top:4px;color:var(--ink-soft);font-size:12px">If you set a password, a customer account will be created and can log in immediately to see only this organization+location tickets.</small>
      ` : ""}
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
      toast("Organization deleted", "success");
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
  if (!body.name) { toast("Organization name is required", "error"); return; }
  const isNew = !id;
  const loginName = isNew ? $("#cuLoginName")?.value.trim() : "";
  const loginEmail = isNew ? $("#cuLoginEmail")?.value.trim() : "";
  const loginPassword = isNew ? $("#cuLoginPassword")?.value : "";
  const loginLocation = isNew ? $("#cuLoginLocation")?.value.trim() : "";
  if (isNew && loginPassword && loginPassword.length < 6) {
    toast("Customer password must be at least 6 characters", "error");
    return;
  }
  if (isNew && loginPassword && !loginEmail) {
    toast("Customer login email is required when setting a password", "error");
    return;
  }
  closeSheet();
  showLoading();
  try {
    let orgId = id;
    let orgRes;
    if (id) {
      await API.put("/api/customers/" + id, body);
    } else {
      orgRes = await API.post("/api/customers", body);
      orgId = orgRes.id;
    }
    // If password provided, create a customer user for this org
    if (isNew && loginPassword) {
      try {
        // Create or get location for this customer
        let locId = null, deptId = null;
        const locName = loginLocation || "Main Lab";
        // Try to find existing location with same name for this customer, or create
        try {
          const locs = await API.get(`/api/locations?customer_id=${orgId}`);
          const existing = locs.find((l) => l.name.toLowerCase() === locName.toLowerCase());
          if (existing) {
            locId = existing.id;
          } else {
            const newLoc = await API.post("/api/locations", { name: locName, customer_id: orgId });
            locId = newLoc.id;
          }
          // Get department for location
          try {
            const depts = await API.get(`/api/departments?location_id=${locId}`);
            if (depts.length) deptId = depts[0].id;
          } catch (e) {}
        } catch (e) {
          // If location creation fails, proceed without location (backend will still allow)
        }
        const userBody = {
          name: loginName || body.contact_name || body.name,
          email: loginEmail,
          password: loginPassword,
          role: "customer",
          customer_id: orgId,
          location_id: locId,
          department_id: deptId,
        };
        await API.post("/api/users", userBody);
        toast(`Organization added + customer login ${loginEmail} created`, "success");
      } catch (e) {
        // Org created but user failed — show warning
        toast(`Organization ${body.name} added, but customer login failed: ${e.message}`, "error");
      }
    } else {
      toast(id ? "Organization updated" : "Organization added", "success");
    }
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
  // bind engineers to a customer or leave them LabSynch-wide, and may create
  // tenant admins linked to a customer or entirely unlinked (they create their
  // own organizations after first login); non-master tenant admins always pick
  // which of their care-list customers the new account belongs to.
  const tenantAdmin = isAdmin() && !master;
  const showCustFields = master || startCust || tenantAdmin;
  const showLocDept = startCust;
  // Who may be named as the linked tenant admin:
  //  * master       -> the admins of whichever organization is selected
  //  * tenant admin -> their own peer admins, because the organization is now
  //                    optional and the account can sit under the tenant's care
  // Tenant staff below admin are still auto-assigned by the backend.
  const startCustId = u && u.customer_id ? u.customer_id : (state.user && !master ? state.user.customer_id : "");
  const respAdmins = tenantAdmin
    ? await tenantAdminPeers()
    : ((u && u.customer_id) || (state.user && !master && state.user.customer_id)
        ? await adminsForCustomer(startCustId || null)
        : []);
  // A tenant admin defaults to themselves; the master may leave it unassigned.
  const respSelected = (u && u.responsible_admin_id) || (tenantAdmin ? state.user.id : null);
  const respOptions = master
    ? respAdminOpts(respAdmins, respSelected, true)
    : (tenantAdmin ? respAdminOpts(respAdmins, respSelected, false) : "");
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit user" : "Add user"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Full name *</span><input id="uName" value="${esc(u ? u.name : "")}"></label>
      <label class="field"><span>Email *</span><input id="uEmail" type="email" value="${esc(u ? u.email : "")}"></label>
      <label class="field"><span>Phone</span><input id="uPhone" value="${esc(u ? u.phone : "")}"></label>
      <label class="field"><span>Role</span>
        <select id="uRole" onchange="toggleCustomerSelect()">
          ${(() => {
            const base = [["engineer", "Engineer"], ["application", "Application"]];
            // When editing, keep the existing role visible even if it's customer/admin
            if (u && !base.find(([r]) => r === u.role)) base.unshift([u.role, u.role === "admin" ? "Tenant admin" : (u.role === "customer" ? "Customer" : u.role)]);
            return base.map(([r, lbl]) => `<option value="${r}" ${startRole === r ? "selected" : ""}>${lbl}</option>`).join("");
          })()}
        </select></label>
      <div id="uCustomerFields" style="${showCustFields ? "" : "display:none"}">
        <label class="field" id="uCustomerField"><span id="uCustomerLabel">${master && !startCust ? "Linked organization (optional — leave empty for LabSynch-wide)" : (tenantAdmin && !startCust ? "Linked organization (optional — leave empty to place them under tenant admin care)" : "Linked organization")}</span>
          <select id="uCustomer" onchange="onUserCustPick()">
            ${master ? `<option value="" ${!u || !u.customer_id ? "selected" : ""}>— LabSynch-wide (no customer) —</option>` : ""}
            ${tenantAdmin ? `<option value="" ${!u || !u.customer_id ? "selected" : ""}>— Under tenant admin care (no single organization) —</option>` : ""}
            ${customers.map((x) => `<option value="${x.id}" ${String(x.id) === String(u && u.customer_id ? u.customer_id : (!master ? startCustId : null)) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
          </select></label>
        <label class="field" id="uLocationField" style="${showLocDept ? "" : "display:none"}"><span>Linked location/department</span>
          <select id="uLocation" onchange="onUserLocPick()">
            ${locOpts(state.locations || [], u && u.location_id, u && u.customer_id)}
          </select>
          <small style="display:block;margin-top:6px;color:var(--ink-soft);font-size:12px">If no location appears, create one via <a href="#" onclick="event.preventDefault(); const cust=$('#uCustomer').value; closeSheet(); setTimeout(()=>{openLocationEditor(false); setTimeout(()=>{const s=$('#locCustomer'); if(s&&cust) s.value=cust;},100);},150);" style="color:var(--brand);text-decoration:underline">Organizations → Add location</a> or select "Create new" above.</small>
        </label>
        <select id="uDepartment" style="display:none">
          ${deptOpts(state.departments || [], u && u.department_id, u && u.location_id)}
        </select>
        ${master ? `<label class="field" id="uRespAdminField" style="${startCustId ? "" : "display:none"}"><span>Responsible tenant admin</span>
          <select id="uRespAdmin">${respOptions}</select></label>` : ""}
        ${tenantAdmin ? `<label class="field" id="uRespAdminField" style="${startCust ? "display:none" : ""}"><span>Linked tenant admin</span>
          <select id="uRespAdmin">${respOptions}</select>
          <small style="display:block;margin-top:4px;color:var(--ink-soft);font-size:12px">With no organization selected, this account sees every organization that tenant admin cares for.</small></label>` : ""}
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
  const isStaff = !isCustRole && !isTenantAdminRole;
  if (isMaster()) {
    // Master: customer and tenant admin need organization picker.
    // Engineer/Application should NOT have organization — only linked tenant admin (optional).
    $("#uCustomerFields").style.display = "";
    const custField = $("#uCustomerField");
    if (custField) custField.style.display = isStaff ? "none" : "";
    const lbl = $("#uCustomerLabel");
    if (lbl) lbl.textContent = isTenantAdminRole
      ? "Linked organization (optional — they create their own after login)"
      : "Linked organization (optional — leave empty for LabSynch-wide)";
    const emptyOpt = document.querySelector("#uCustomer option[value='']");
    if (emptyOpt) emptyOpt.textContent = isTenantAdminRole
      ? "— Not linked yet (they create their own later) —"
      : "— LabSynch-wide (no customer) —";
    const respFieldM = $("#uRespAdminField");
    if (respFieldM) {
      if (isStaff) {
        respFieldM.style.display = "";
        if (!respFieldM.querySelector("select")?.innerHTML?.trim()) {
          tenantAdminPeers().then((admins) => {
            const sel = $("#uRespAdmin");
            if (sel) sel.innerHTML = respAdminOpts(admins, null, true);
          });
        }
      } else if (isCustRole) {
        // keep existing logic via onUserCustPick
      } else {
        respFieldM.style.display = isTenantAdminRole ? "none" : "";
      }
    }
  } else {
    // Tenant admin: customer accounts need organization + location.
    // Engineer/Application should NOT have organization selection — only linked tenant admin.
    $("#uCustomerFields").style.display = "";
    const custField = $("#uCustomerField");
    if (custField) custField.style.display = isStaff ? "none" : "";
    const sel = $("#uCustomer");
    if (isCustRole && sel && !sel.value && state.user && state.user.customer_id) {
      sel.value = String(state.user.customer_id);
    }
    const lbl = $("#uCustomerLabel");
    if (lbl && !isMaster()) lbl.textContent = isCustRole
      ? "Linked organization"
      : "Linked organization (optional — leave empty to place them under tenant admin care)";
    const respField = $("#uRespAdminField");
    if (respField && !isMaster()) respField.style.display = isCustRole ? "none" : "";
  }
  $("#uLocationField").style.display = (function(){
    const r = $("#uRole").value;
    return r === "customer" ? "" : "none";
  })();
  if ($("#uDepartmentField")) $("#uDepartmentField").style.display = "none";
  if ($("#uCustomerField") && $("#uCustomerField").style.display !== "none") {
    onUserCustPick();
  }
}

function onUserCustPick() {
  const cust = $("#uCustomer").value;
  // Build location options: existing + create new, so tenant admin can create location directly from user editor
  const baseOpts = locOpts(state.locations || [], null, cust);
  const hasCust = !!cust;
  let extra = "";
  if (hasCust) {
    // Check if there are any locations for this customer
    const list = (state.locations || []).filter((l) => String(l.customer_id) === String(cust));
    if (!list.length) {
      extra = `<option value="" disabled>— No locations yet —</option>`;
    }
    extra += `<option value="__new__">＋ Create new location/department…</option>`;
  }
  $("#uLocation").innerHTML = baseOpts + extra;
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
  if (loc === "__new__") {
    const cust = $("#uCustomer").value;
    if (!cust) {
      toast("Select an organization first", "error");
      $("#uLocation").value = "";
      return;
    }
    // Open location editor pre-filled with this customer, then refresh
    closeSheet();
    setTimeout(() => {
      openLocationEditor(false);
      // Pre-select the customer in the location editor after it opens
      setTimeout(() => {
        const sel = $("#locCustomer");
        if (sel) sel.value = cust;
      }, 100);
    }, 150);
    return;
  }
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
    if (!body.customer_id) { toast("Linked organization is required for customer accounts", "error"); return; }
    if (isMaster() && $("#uRespAdmin")) body.responsible_admin_id = $("#uRespAdmin").value || null;
  } else if (isMaster()) {
    // a tenant admin may be created WITHOUT a customer — they build their own
    // organization after first login; techs/customers may be global or bound
    body.customer_id = $("#uCustomer").value || null;
    body.location_id = null;
    body.department_id = null;
    if ($("#uRespAdmin")) body.responsible_admin_id = $("#uRespAdmin").value || null;
  } else {
    // Tenant staff creating an engineer/application account: the organization is
    // OPTIONAL. Left empty, the account sits under the linked tenant admin's care
    // and inherits that admin's care list instead of being tied to one
    // organization — so there is no fallback to the admin's primary any more.
    body.customer_id = ($("#uCustomer") && $("#uCustomer").value) || null;
    body.location_id = null;
    body.department_id = null;
    if ($("#uRespAdmin")) body.responsible_admin_id = $("#uRespAdmin").value || null;
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
          <div class="item-sub">${esc(p.equipment_name || "—")}${p.equipment_serial ? " (S/N: " + esc(p.equipment_serial) + ")" : ""}${isCust() ? "" : " · " + esc(p.customer_name || "")}</div>
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
          <div class="item-sub" style="margin-top:6px">${esc(p.equipment_name || "—")}${p.equipment_serial ? " · S/N: " + esc(p.equipment_serial) : ""} · ${esc(p.customer_name || "")}</div>
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
  } else if (isCust()) {
    try { equipment = await API.get("/api/equipment"); } catch (e) {}
  }
  state.equipment = equipment;
  const p = edit ? state.pm?.find((x) => x.id === state.viewParams.id) : null;
  const defCust = (p && p.customer_id) || (isTech() && customers.length ? customers[0].id : null);
  const defaultNext = mytDatePlus(90);
  openSheet(`
    <div class="sheet-head"><h3>${edit ? "Edit schedule" : "New PM schedule"}</h3><button class="close-x" onclick="closeSheet()">✕</button></div>
    <div class="sheet-body">
      <label class="field"><span>Title *</span><input id="pmTitle" value="${esc(p ? p.title : "")}" placeholder="e.g. Centrifuge annual service"></label>
      <label class="field"><span>Description</span><textarea id="pmDesc">${esc(p ? p.description : "")}</textarea></label>
      ${isTech() ? `<label class="field"><span>Organization *</span>
        <select id="pmCustomer" onchange="onCustPickPM()">
          ${customers.map((x) => `<option value="${x.id}" ${String(defCust) === String(x.id) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select></label>` : ""}
      <label class="field"><span>Equipment</span>
        <select id="pmEquipment">
          ${eqOpts(equipment, p && p.equipment_id, defCust, "— None / general —")}
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
              <div class="item-sub">${esc(p.customer_name || "")}${p.equipment_name ? " · " + esc(p.equipment_name) : ""}${p.equipment_serial ? " (S/N: " + esc(p.equipment_serial) + ")" : ""}</div>
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
  const sn = x.serial_number ? `S/N: ${x.serial_number}` : "No S/N";
  const model = x.model ? ` · ${x.model}` : "";
  const loc = x.location_name || x.department_name ? ` (📍 ${x.location_name || x.department_name})` : "";
  return `<option value="${x.id}" ${sel}>${esc(x.name)} [${esc(sn)}${esc(model)}]${esc(loc)}</option>`;
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
      <label class="field"><span>Organization *</span>
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
  if (!body.customer_id) { toast("Organization is required", "error"); return; }
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

// ---------------------------------------------------------------- Authed images
// Fetches a protected image with the bearer token and swaps in an object URL,
// because <img> tags cannot send auth headers. Still used by the portal QR code.
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
// LabSynch's bell can also ring as a real system notification via Web Push:
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
      ? n.map((x) => {
          // A pending-care notification is actionable: the tenant admin can
          // answer it right here instead of hunting for the organization.
          const isCare = x.entity_type === "pending_care";
          let careExtra = "";
          if (isCare && x.care_pending) {
            careExtra = isMaster()
              ? `<div class="btn-row" style="margin-top:8px">
                  <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();closeSheet();navigate('customers')">Review in Organizations</button>
                </div>`
              : `<div class="btn-row" style="margin-top:8px">
                  <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();careDecision(${x.entity_id}, 'take')">Take into my care</button>
                  <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();careDecision(${x.entity_id}, 'decline')">Not mine</button>
                </div>`;
          } else if (isCare) {
            careExtra = `<div class="n-accepted">✔ ${x.care_claimed_by ? `Under the care of ${esc(x.care_claimed_by)}` : "No longer awaiting a decision"}</div>`;
          }
          return `
        <div class="notif ${x.read ? "" : "unread"}" onclick="openNotif(${x.id}, '${esc(x.entity_type)}', ${x.entity_id || "null"})">
          <div class="c-avatar">🔔</div>
          <div class="n-body">
            <div class="n-text">${esc(x.text)}</div>
            <div class="n-time">${timeAgo(x.created_at)}</div>
            ${x.accepted ? `<div class="n-accepted">✔ Accepted</div>` : ""}
            ${careExtra}
          </div>
        </div>`;
        }).join("")
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
  else if (entityType === "pending_care") navigate("customers");
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
    custSel.innerHTML = `<option value="">— Select your organization —</option>` +
      opts.customers.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("") +
      `<option value="__new__">＋ Create new organization…</option>`;
    state.signup = { customers: opts.customers, locations: opts.locations, departments: opts.departments };
  } catch (e) { /* ignore */ }
}

function onJoinCust() {
  const cust = $("#jnCustomer").value;
  const isNewCust = cust === "__new__";
  if ($("#jnNewCustomerWrap")) {
    $("#jnNewCustomerWrap").classList.toggle("hidden", !isNewCust);
    if (isNewCust && $("#jnNewCustomer")) $("#jnNewCustomer").focus();
  }
  if (!isNewCust && $("#jnNewCustomer")) $("#jnNewCustomer").value = "";

  if (isNewCust) {
    // When creating a new organization, user can choose from existing location/department names in the system or create a new one
    const allNames = [
      ...(state.signup?.locations || []).map((l) => l.name),
      ...(state.signup?.departments || []).map((d) => d.name),
    ]
      .map((n) => (n || "").trim())
      .filter(Boolean);
    const distinctNames = Array.from(new Set(allNames)).sort((a, b) => a.localeCompare(b));

    let opts = `<option value="">— Select location/department —</option>`;
    if (distinctNames.length) {
      opts += distinctNames.map((name) => `<option value="name:${esc(name)}">${esc(name)}</option>`).join("");
    }
    opts += `<option value="__new__">＋ Create new location/department…</option>`;
    $("#jnLocation").innerHTML = opts;
    $("#jnLocation").value = "";
    if ($("#jnNewLocationWrap")) $("#jnNewLocationWrap").classList.add("hidden");
    if ($("#jnNewLocation")) $("#jnNewLocation").value = "";
    if ($("#jnDepartment")) $("#jnDepartment").innerHTML = `<option value="">— Select department —</option>`;
    return;
  }

  const locs = cust ? (state.signup?.locations || []).filter((l) => String(l.customer_id) === String(cust)) : [];
  let opts;
  if (!cust) {
    opts = `<option value="">— Select organization first —</option>`;
  } else {
    opts = `<option value="">— Select location/department —</option>`;
    if (locs.length) {
      opts += locs.map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("");
    } else {
      opts += `<option value="" disabled>— No locations yet —</option>`;
    }
    opts += `<option value="__new__">＋ Create new location/department…</option>`;
  }
  $("#jnLocation").innerHTML = opts;
  $("#jnLocation").value = "";
  if ($("#jnNewLocationWrap")) $("#jnNewLocationWrap").classList.add("hidden");
  if ($("#jnNewLocation")) $("#jnNewLocation").value = "";
  if ($("#jnDepartment")) $("#jnDepartment").innerHTML = `<option value="">— Select department —</option>`;
}

function onJoinLoc() {
  const loc = $("#jnLocation").value;
  const isNew = loc === "__new__";
  if ($("#jnNewLocationWrap")) {
    $("#jnNewLocationWrap").classList.toggle("hidden", !isNew);
    if (isNew && $("#jnNewLocation")) $("#jnNewLocation").focus();
  }
  if (!isNew && $("#jnNewLocation")) {
    $("#jnNewLocation").value = "";
  }
  if (!isNew && loc && !loc.startsWith("name:")) {
    const depts = (state.signup?.departments || []).filter((d) => String(d.location_id) === String(loc));
    const dId = depts[0]?.id || loc;
    if ($("#jnDepartment")) {
      $("#jnDepartment").innerHTML = `<option value="${dId}" selected>— Select department —</option>`;
      $("#jnDepartment").value = String(dId);
    }
  } else if ($("#jnDepartment")) {
    $("#jnDepartment").innerHTML = `<option value="">— Select department —</option>`;
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
  if (!body.name || !body.email || !body.password) {
    const el = $("#joinError");
    el.textContent = "Please fill in your full name, email and password";
    el.classList.remove("hidden");
    return;
  }
  if (!body.phone) {
    const el = $("#joinError");
    el.textContent = "Please enter your phone number";
    el.classList.remove("hidden");
    return;
  }
  if (role === "customer") {
    const custVal = $("#jnCustomer").value;
    if (custVal === "__new__") {
      const newCustName = ($("#jnNewCustomer")?.value || "").trim();
      if (!newCustName) {
        const el = $("#joinError");
        el.textContent = "Please enter the new organization name";
        el.classList.remove("hidden");
        return;
      }
      body.new_customer_name = newCustName;
    } else if (custVal) {
      body.customer_id = custVal;
    } else {
      const el = $("#joinError");
      el.textContent = "Please select or create an organization";
      el.classList.remove("hidden");
      return;
    }

    const locVal = $("#jnLocation").value;
    if (locVal === "__new__") {
      const newLocName = ($("#jnNewLocation")?.value || "").trim();
      if (!newLocName) {
        const el = $("#joinError");
        el.textContent = "Please enter the new location/department name";
        el.classList.remove("hidden");
        return;
      }
      body.new_location_name = newLocName;
    } else if (locVal.startsWith("name:")) {
      body.new_location_name = locVal.slice(5).trim();
    } else if (locVal) {
      body.location_id = locVal;
      const depts = (state.signup?.departments || []).filter((d) => String(d.location_id) === String(body.location_id));
      body.department_id = $("#jnDepartment")?.value || depts[0]?.id || body.location_id;
    } else {
      const el = $("#joinError");
      el.textContent = "Please select or create a location/department";
      el.classList.remove("hidden");
      return;
    }
  }
  try {
    const res = await API.post("/api/signup", body);
    $("#joinOk").textContent = res.message || "Submitted for approval ✓";
    $("#joinOk").classList.remove("hidden");
    $("#joinForm").reset();
    if ($("#jnNewCustomerWrap")) $("#jnNewCustomerWrap").classList.add("hidden");
    if ($("#jnNewCustomer")) $("#jnNewCustomer").value = "";
    if ($("#jnNewLocationWrap")) $("#jnNewLocationWrap").classList.add("hidden");
    if ($("#jnNewLocation")) $("#jnNewLocation").value = "";
    $("#jnCustomerBlock").classList.remove("hidden");
    onJoinCust();
    loadJoinOptions();
  } catch (err) {
    const el = $("#joinError");
    el.textContent = err.message || "Submission failed";
    el.classList.remove("hidden");
  }
});

// ---------------------------------------------------------------- Event wiring
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (bootInProgress) return;
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
  refreshPendingCare, takeCare, declineCare, careDecision, openAssignCare, assignCare,
  rateTicket, addFeedback, feedbackSectionHtml, starsHtml,
  viewOnboarding, reviewJoin, toggleAlertSound, playAlertSound,
  openAlertSheet, pickAlertPreset, onAlertCustomPicked, clearAlertCustom, alertPreviewCustom,
  downloadReport, openExportSheet,
  openNotifications, openNotif, markAllRead,
  setPMFilter, openPMEditor, savePM, deletePM, openPMComplete, confirmPMComplete,
  openPortalEditor, savePortal, showPortalQR, copyPortalURL, downloadQR, deletePortal, onPortalCust,
  viewOrg, setOrgTab,
  openLocationEditor, saveLocation, deleteLocation,
  openDepartmentEditor, saveDepartment, deleteDepartment,
  viewCategories, openCategoryEditor, saveCategory, deleteCategory, eqCategoryPick,
  onCustPick, onLocPick, locOpts, deptOpts, eqOpts,
  onEquipmentPickComplaint, onEquipmentPickBreakdown, onCustPickPM,
  onCustPickComplaint, onLocPickComplaint,
  onCustPickBreakdown, onLocPickBreakdown,
  onUserCustPick, onUserLocPick,
  onJoinCust, onJoinLoc, onJoinRoleChange, loadJoinOptions, primeAudio,
  togglePushAlerts, syncPushAlerts, pushStateLabel,
});

const BUILD_VERSION = "49";

async function checkVersion() {
  // Advisory only: a version endpoint outage must not block sign-in/session
  // restoration. Keep BUILD_VERSION aligned with the backend release number.
  try {
    const ver = await API.get("/api/version", { attempts: 1, timeoutMs: 8000 });
    if (ver && ver.version && ver.version !== BUILD_VERSION) {
      if (!sessionStorage.getItem("reloaded_for_version_" + ver.version)) {
        sessionStorage.setItem("reloaded_for_version_" + ver.version, "1");
        const url = new URL(location.href);
        url.searchParams.set("_t", Date.now());
        location.replace(url.href);
      }
    }
  } catch (e) { /* version checks are best effort */ }
}

async function boot() {
  if (bootInProgress) return;
  bootInProgress = true;
  const status = $("#startupStatus");
  const retry = $("#startupRetry");
  const loginBtn = $("#loginBtn");
  const joinBtn = $("#showJoinBtn");
  status.textContent = "Connecting to LabSynch…";
  retry.classList.add("hidden");
  loginBtn.disabled = joinBtn.disabled = true;
  render(); // paint before any network request, including for saved sessions
  let startupError = null;
  try {
    const user = await API.get("/api/me", { attempts: 1, timeoutMs: 8000 });
    if (!user || !user.id || !user.role) throw new Error("Invalid session response from the LabSynch server. Please try again.");
    state.user = user;
  } catch (e) {
    if (e.status === 401) {
      // Only an explicit expired/invalid session should discard the token.
      API.token = null;
      store.remove("labcare_token");
    } else {
      startupError = e;
    }
  } finally {
    status.textContent = startupError ? startupError.message : "";
    retry.classList.toggle("hidden", !startupError);
    loginBtn.disabled = joinBtn.disabled = false;
    bootInProgress = false;
    booted = true;
    render();
  }
  if (state.user) {
    refreshBell(true);
    syncPushAlerts();
  }
  if (!startupError) void checkVersion();
}

let booted = false;
let bootInProgress = false;
$("#startupRetry").addEventListener("click", boot);
boot();
