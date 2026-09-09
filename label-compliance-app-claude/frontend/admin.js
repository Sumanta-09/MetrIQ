// ---------------------------------------------------------------------
// Check archive (admin) — reads past scans from backend/sessions via the
// /api/admin/* endpoints. The passcode is remembered per browser tab.
// ---------------------------------------------------------------------

const KEY_STORE = "metriq_admin_key";
const VERDICT_LABEL = {
  compliant: "Compliant",
  needs_review: "Needs review",
  non_compliant: "Non-compliant",
  no_verdict: "No verdict recorded",
};
const STATUS_ICON = { pass: "✓", fail: "✕", warn: "!", info: "i" };
const STATUS_LABEL = { pass: "Passed", fail: "Missing / invalid", warn: "Incomplete", info: "Conditional" };

const els = {
  gate: document.getElementById("gate"),
  gateForm: document.getElementById("gateForm"),
  passcodeInput: document.getElementById("passcodeInput"),
  gateError: document.getElementById("gateError"),
  logoutBtn: document.getElementById("logoutBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  statsRow: document.getElementById("statsRow"),
  statTotal: document.getElementById("statTotal"),
  statCompliant: document.getElementById("statCompliant"),
  statReview: document.getElementById("statReview"),
  statNonCompliant: document.getElementById("statNonCompliant"),
  toolbar: document.getElementById("toolbar"),
  searchInput: document.getElementById("searchInput"),
  sessionList: document.getElementById("sessionList"),
  listEmpty: document.getElementById("listEmpty"),
  listEmptyFilter: document.getElementById("listEmptyFilter"),
  detailPanel: document.getElementById("detailPanel"),
  detailPlaceholder: document.getElementById("detailPlaceholder"),
};

let adminKey = sessionStorage.getItem(KEY_STORE) || "";
let sessions = [];          // full list from the server
let filter = { status: "all", q: "" };
let currentDetailId = null;
let objectUrls = [];        // blob URLs created for thumbnails

// ---------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------
async function apiFetch(url) {
  const res = await fetch(url, { headers: adminKey ? { "X-Admin-Key": adminKey } : {} });
  if (res.status === 401) {
    const err = new Error("unauthorized");
    err.status = 401;
    throw err;
  }
  if (!res.ok) throw new Error(`Server returned ${res.status}`);
  return res.json();
}

function revokeObjectUrls() {
  objectUrls.forEach(u => URL.revokeObjectURL(u));
  objectUrls = [];
}

function setKey(key) {
  adminKey = key;
  if (key) sessionStorage.setItem(KEY_STORE, key);
  else sessionStorage.removeItem(KEY_STORE);
}

// ---------------------------------------------------------------------
// Gate (passcode)
// ---------------------------------------------------------------------
function showGate(show) {
  els.gate.hidden = !show;
  els.logoutBtn.hidden = show;
  if (show) els.passcodeInput.focus();
}

els.gateForm.addEventListener("submit", async e => {
  e.preventDefault();
  els.gateError.hidden = true;
  setKey(els.passcodeInput.value.trim());
  els.passcodeInput.value = "";
  try {
    await loadList();
    showGate(false);
  } catch (err) {
    if (err.status === 401) {
      setKey("");
      els.gateError.hidden = false;
    } else {
      showGate(false);
      renderListError(err.message);
    }
  }
});

els.logoutBtn.addEventListener("click", () => {
  setKey("");
  currentDetailId = null;
  clearList();
  showGate(true);
});

els.refreshBtn.addEventListener("click", async () => {
  els.refreshBtn.disabled = true;
  try {
    await loadList();
    if (currentDetailId) await openDetail(currentDetailId, true);
  } catch (err) {
    if (err.status === 401) showGate(true);
  } finally {
    els.refreshBtn.disabled = false;
  }
});

// ---------------------------------------------------------------------
// List
// ---------------------------------------------------------------------
async function loadList() {
  const data = await apiFetch("/api/admin/sessions");
  sessions = data.sessions || [];
  renderStats(data.stats || {});
  els.toolbar.hidden = sessions.length === 0;
  renderList();
}

function clearList() {
  sessions = [];
  els.statsRow.hidden = true;
  els.toolbar.hidden = true;
  renderList();
}

function renderStats(stats) {
  els.statTotal.textContent = stats.total || 0;
  els.statCompliant.textContent = stats.compliant || 0;
  els.statReview.textContent = stats.needs_review || 0;
  els.statNonCompliant.textContent = stats.non_compliant || 0;
  els.statsRow.hidden = false;
}

function renderListError(message) {
  els.statsRow.hidden = true;
  els.toolbar.hidden = true;
  els.sessionList.innerHTML = "";
  const box = document.createElement("div");
  box.className = "empty";
  const p = document.createElement("p");
  p.className = "empty__title";
  p.textContent = "Couldn't load the archive.";
  const sub = document.createElement("p");
  sub.className = "empty__text";
  sub.textContent = message;
  box.append(p, sub);
  els.sessionList.appendChild(box);
}

function visibleSessions() {
  const q = filter.q.trim().toLowerCase();
  return sessions.filter(s => {
    if (filter.status !== "all" && s.verdict !== filter.status) return false;
    if (!q) return true;
    return s.id.toLowerCase().includes(q)
      || (s.product_type_label || "").toLowerCase().includes(q);
  });
}

function renderList() {
  els.sessionList.querySelectorAll(".s-item").forEach(n => n.remove());
  const list = visibleSessions();
  els.listEmpty.hidden = sessions.length !== 0;
  els.listEmptyFilter.hidden = !(sessions.length > 0 && list.length === 0);

  const head = els.sessionList.previousElementSibling;
  if (head) head.hidden = sessions.length === 0;

  list.forEach(s => els.sessionList.appendChild(buildRow(s)));

  // Keep detail panel in sync if its session got filtered out of view.
  if (currentDetailId && !list.some(s => s.id === currentDetailId)) {
    closeDetail();
  }
}

function buildRow(s) {
  const row = document.createElement("div");
  row.className = "s-item" + (s.id === currentDetailId ? " is-active" : "");
  row.dataset.id = s.id;

  const time = document.createElement("span");
  time.className = "s-item__time";
  time.textContent = s.display;

  const id = document.createElement("span");
  id.className = "s-item__id";
  id.textContent = s.id;
  id.title = s.id;

  const product = document.createElement("span");
  product.className = "s-item__product";
  product.textContent = s.product_type_label || "—";

  const imgs = document.createElement("span");
  imgs.className = "s-item__imgs";
  imgs.textContent = s.image_count || 0;

  const req = document.createElement("span");
  req.className = "s-item__req";
  req.textContent = `${s.required_passed}/${s.required_total}`;

  const pill = document.createElement("span");
  pill.className = `verdict verdict--${s.verdict}`;
  pill.textContent = VERDICT_LABEL[s.verdict] || s.verdict;

  const chev = document.createElement("span");
  chev.className = "s-item__chev";
  chev.textContent = "›";
  chev.setAttribute("aria-hidden", "true");

  row.append(time, id, product, imgs, req, pill, chev);
  row.addEventListener("click", () => openDetail(s.id));
  return row;
}

// ---------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------
async function openDetail(id, silent) {
  if (!silent) {
    // optimistic highlight before the network round-trip
    els.sessionList.querySelectorAll(".s-item").forEach(r => {
      r.classList.toggle("is-active", r.dataset.id === id);
    });
  }
  try {
    const data = await apiFetch(`/api/admin/sessions/${id}`);
    currentDetailId = id;
    renderDetail(data);
    if (!silent) {
      els.detailPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  } catch (err) {
    if (err.status === 401) showGate(true);
  }
}

function renderDetail(d) {
  revokeObjectUrls();
  const compliance = d.compliance || {};

  const panel = document.createElement("div");
  panel.className = "detail";

  // Header
  const head = document.createElement("div");
  head.className = "detail__head";
  const back = document.createElement("button");
  back.className = "btn btn--ghost btn--xs";
  back.type = "button";
  back.textContent = "← All sessions";
  back.addEventListener("click", closeDetail);

  const dl = document.createElement("button");
  dl.className = "btn btn--ghost btn--xs";
  dl.type = "button";
  dl.textContent = "Download JSON";
  dl.addEventListener("click", () => downloadJson(d));

  const actions = document.createElement("div");
  actions.className = "detail__actions";
  actions.append(back, dl);
  head.append(actions);

  const pill = document.createElement("span");
  pill.className = `verdict verdict--lg verdict--${compliance.overall_status || "needs_review"}`;
  pill.textContent = VERDICT_LABEL[compliance.overall_status] || "Needs review";

  const title = document.createElement("h2");
  title.className = "detail__title";
  title.textContent = compliance.product_type_label || "General / Other Packaged Commodity";

  const sub = document.createElement("p");
  sub.className = "detail__sub";
  sub.textContent = `${d.display} · ${d.session_id}`;

  const meta = document.createElement("div");
  meta.className = "detail__meta";
  meta.append(
    metaChip("neutral", `${compliance.required_passed}/${compliance.required_total} required checks passed`),
    metaChip("neutral", `${(d.images || []).length} image(s) scanned`)
  );
  if (d.has_verdict) {
    meta.append(metaChip("neutral", compliance.is_imported ? "Imported product" : "Domestic product"));
  }
  const summary = compliance.summary || {};
  if (summary.total) {
    meta.append(
      metaChip("ok", `${summary.pass || 0} pass`),
      metaChip("warn", `${summary.warn || 0} incomplete`),
      metaChip("bad", `${summary.fail || 0} missing`),
      metaChip("info", `${summary.info || 0} conditional`)
    );
  }

  panel.append(head, pill, title, sub, meta);

  // Checks
  const checksBlock = document.createElement("div");
  checksBlock.className = "detail__block";
  const checksHead = document.createElement("h3");
  checksHead.className = "detail__block-title";
  checksHead.textContent = "Rule-by-rule checklist";
  checksBlock.appendChild(checksHead);

  const checksEl = document.createElement("div");
  checksEl.className = "checks-list";
  const severityOrder = { fail: 0, warn: 1, info: 2, pass: 3 };
  const checks = [...(compliance.checks || [])].sort((a, b) => {
    if (Boolean(a.required) !== Boolean(b.required)) return a.required ? -1 : 1;
    return severityOrder[a.status] - severityOrder[b.status];
  });
  if (checks.length === 0) {
    const p = document.createElement("p");
    p.className = "detail__none";
    p.textContent = d.has_verdict
      ? "No individual checks recorded for this session."
      : "This scan was archived before the rule-by-rule engine existed — only extracted fields and the original photos remain.";
    checksBlock.appendChild(p);
  } else {
    checks.forEach(c => checksEl.appendChild(buildCheckRow(c)));
    checksBlock.appendChild(checksEl);
  }
  panel.appendChild(checksBlock);

  // Extracted fields
  const fields = Object.entries(d.structured_fields || {})
    .filter(([k, v]) => !k.startsWith("_") && v && typeof v === "object" && "value" in v);
  if (fields.length) {
    const block = document.createElement("div");
    block.className = "detail__block";
    const h = document.createElement("h3");
    h.className = "detail__block-title";
    h.textContent = "Fields extracted from the label";
    block.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "field-grid";
    fields.forEach(([key, val]) => {
      const cell = document.createElement("div");
      cell.className = "field";
      const k = document.createElement("span");
      k.className = "field__key";
      k.textContent = prettyKey(key);
      const v = document.createElement("span");
      v.className = "field__value";
      v.textContent = String(val.value);
      cell.append(k, v);
      if (val.context || val.note || val.label_found) {
        const n = document.createElement("span");
        n.className = "field__note";
        n.textContent = val.note || val.context || val.label_found;
        cell.appendChild(n);
      }
      grid.appendChild(cell);
    });
    block.appendChild(grid);
    panel.appendChild(block);
  }

  // Label photos
  if ((d.images || []).length) {
    const block = document.createElement("div");
    block.className = "detail__block";
    const h = document.createElement("h3");
    h.className = "detail__block-title";
    h.textContent = "Original label photos";
    block.appendChild(h);
    const thumbs = document.createElement("div");
    thumbs.className = "img-thumbs";
    d.images.forEach(name => {
      const a = document.createElement("a");
      a.className = "img-thumb";
      a.target = "_blank";
      a.rel = "noopener";
      a.title = name;
      a.addEventListener("click", async e => {
        e.preventDefault();
        const url = await fetchImageUrl(d.session_id, name);
        if (url) window.open(url, "_blank", "noopener");
      });
      const img = document.createElement("img");
      img.alt = name;
      thumbs.appendChild(a).appendChild(img);
      if (name.startsWith("annotated_")) {
        const tag = document.createElement("span");
        tag.className = "img-thumb__tag";
        tag.textContent = "Flagged";
        a.appendChild(tag);
      }
      loadThumb(d.session_id, name, img);
    });
    block.appendChild(thumbs);
    panel.appendChild(block);
  }

  els.detailPanel.innerHTML = "";
  els.detailPanel.appendChild(panel);
}

async function loadThumb(sessionId, name, img) {
  try {
    const url = await fetchImageUrl(sessionId, name);
    if (url) img.src = url;
  } catch {
    img.alt = "(couldn't load)";
  }
}

async function fetchImageUrl(sessionId, name) {
  const res = await fetch(`/api/admin/sessions/${sessionId}/images/${name}`, {
    headers: adminKey ? { "X-Admin-Key": adminKey } : {},
  });
  if (!res.ok) throw new Error(res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  objectUrls.push(url);
  return url;
}

async function downloadJson(d) {
  try {
    const raw = await fetch(`/api/admin/sessions/${d.session_id}`, {
      headers: adminKey ? { "X-Admin-Key": adminKey } : {},
    });
    if (!raw.ok) throw new Error();
    const blob = await raw.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${d.session_id}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch {
    // ignore download failures silently
  }
}

function closeDetail() {
  currentDetailId = null;
  revokeObjectUrls();
  els.detailPanel.innerHTML = "";
  els.detailPanel.appendChild(els.detailPlaceholder);
  els.sessionList.querySelectorAll(".s-item.is-active").forEach(r => r.classList.remove("is-active"));
}

function metaChip(kind, text) {
  const span = document.createElement("span");
  span.className = `chip-badge chip-badge--${kind}`;
  span.textContent = text;
  return span;
}

function buildCheckRow(check) {
  const row = document.createElement("div");
  row.className = `check-row check-row--${check.status}`;

  const icon = document.createElement("span");
  icon.className = "check-row__icon";
  icon.textContent = STATUS_ICON[check.status] || "?";
  icon.setAttribute("aria-hidden", "true");

  const body = document.createElement("div");
  body.className = "check-row__body";

  const top = document.createElement("div");
  top.className = "check-row__top";
  const label = document.createElement("span");
  label.className = "check-row__label";
  label.textContent = check.label + (check.required ? "" : " (conditional)");
  const cite = document.createElement("span");
  cite.className = "check-row__citation";
  cite.textContent = check.citation || "";
  top.append(label, cite);

  const msg = document.createElement("div");
  msg.className = "check-row__message";
  const detail = check.message || "";
  const prefix = STATUS_LABEL[check.status] || "";
  msg.textContent = detail ? `${prefix} — ${detail}` : prefix;

  body.append(top, msg);
  row.append(icon, body);
  return row;
}

function prettyKey(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

// ---------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------
document.querySelectorAll(".seg__btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg__btn").forEach(b => b.classList.remove("is-active"));
    btn.classList.add("is-active");
    filter.status = btn.dataset.status;
    renderList();
  });
});

document.querySelectorAll(".stat-card").forEach(card => {
  card.addEventListener("click", () => {
    const status = card.dataset.filter;
    document.querySelectorAll(".seg__btn").forEach(b =>
      b.classList.toggle("is-active", b.dataset.status === status));
    filter.status = status;
    renderList();
  });
});

els.searchInput.addEventListener("input", () => {
  filter.q = els.searchInput.value;
  renderList();
});

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
(async function init() {
  if (!adminKey) {
    showGate(true);
    return;
  }
  try {
    await loadList();
    showGate(false);
  } catch (err) {
    if (err.status === 401) {
      setKey("");
      showGate(true);
    } else {
      showGate(false);
      renderListError(err.message);
    }
  }
})();