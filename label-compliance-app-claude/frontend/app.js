// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
const pendingFiles = []; // { file: Blob, name: string, url: string }
let cameraStream = null;
let mirrored = true;

// ---------------------------------------------------------------------
// Element refs
// ---------------------------------------------------------------------
const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".tab-panel");

const productType = document.getElementById("productType");
const isImported = document.getElementById("isImported");

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");

const cameraVideo = document.getElementById("cameraVideo");
const cameraCanvas = document.getElementById("cameraCanvas");
const cameraIdle = document.getElementById("cameraIdle");
const cameraControls = document.getElementById("cameraControls");
const startCameraBtn = document.getElementById("startCameraBtn");
const stopCameraBtn = document.getElementById("stopCameraBtn");
const captureBtn = document.getElementById("captureBtn");
const flipCameraBtn = document.getElementById("flipCameraBtn");

const queue = document.getElementById("queue");
const queueItems = document.getElementById("queueItems");
const queueCount = document.getElementById("queueCount");

const scanBtn = document.getElementById("scanBtn");
const stationStatus = document.getElementById("stationStatus");

const logEmpty = document.getElementById("logEmpty");
const logFeed = document.getElementById("logFeed");
const logCount = document.getElementById("logCount");
const ticketTemplate = document.getElementById("ticketTemplate");

// ---------------------------------------------------------------------
// Product type dropdown
// ---------------------------------------------------------------------
async function loadProductTypes() {
  try {
    const res = await fetch("/api/product-types");
    if (!res.ok) return;
    const types = await res.json();
    productType.innerHTML = "";
    types.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t.value;
      opt.textContent = t.label;
      productType.appendChild(opt);
    });
  } catch {
    // Keep the static "General" fallback option already in the HTML.
  }
}
loadProductTypes();

// ---------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------
tabs.forEach(tab => {
  tab.addEventListener("click", () => {
    tabs.forEach(t => { t.classList.remove("is-active"); t.setAttribute("aria-selected", "false"); });
    panels.forEach(p => p.classList.remove("is-active"));
    tab.classList.add("is-active");
    tab.setAttribute("aria-selected", "true");
    document.querySelector(`.tab-panel[data-panel="${tab.dataset.tab}"]`).classList.add("is-active");
    if (tab.dataset.tab !== "camera") stopCamera();
  });
});

// ---------------------------------------------------------------------
// Drag & drop / file picker
// ---------------------------------------------------------------------
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});

fileInput.addEventListener("change", () => {
  addFiles(Array.from(fileInput.files));
  fileInput.value = "";
});

["dragenter", "dragover"].forEach(evt =>
  dropzone.addEventListener(evt, e => {
    e.preventDefault();
    dropzone.classList.add("is-dragover");
  })
);
["dragleave", "drop"].forEach(evt =>
  dropzone.addEventListener(evt, e => {
    e.preventDefault();
    dropzone.classList.remove("is-dragover");
  })
);
dropzone.addEventListener("drop", e => {
  const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith("image/"));
  addFiles(files);
});

function addFiles(files) {
  files.forEach(file => {
    const url = URL.createObjectURL(file);
    pendingFiles.push({ file, name: file.name, url });
  });
  renderQueue();
}

// ---------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------
startCameraBtn.addEventListener("click", startCamera);
stopCameraBtn.addEventListener("click", stopCamera);
flipCameraBtn.addEventListener("click", () => {
  mirrored = !mirrored;
  cameraVideo.style.transform = mirrored ? "scaleX(-1)" : "none";
});
captureBtn.addEventListener("click", captureFrame);

async function startCamera() {
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 960 } }
    });
    cameraVideo.srcObject = cameraStream;
    cameraVideo.style.transform = mirrored ? "scaleX(-1)" : "none";
    cameraIdle.hidden = true;
    cameraControls.hidden = false;
  } catch (err) {
    setStatus("Couldn't access the camera: " + err.message, "error");
  }
}

function stopCamera() {
  if (cameraStream) {
    cameraStream.getTracks().forEach(t => t.stop());
    cameraStream = null;
  }
  cameraIdle.hidden = false;
  cameraControls.hidden = true;
}

function captureFrame() {
  const w = cameraVideo.videoWidth;
  const h = cameraVideo.videoHeight;
  if (!w || !h) return;

  cameraCanvas.width = w;
  cameraCanvas.height = h;
  const ctx = cameraCanvas.getContext("2d");

  if (mirrored) {
    ctx.translate(w, 0);
    ctx.scale(-1, 1);
  }
  ctx.drawImage(cameraVideo, 0, 0, w, h);

  cameraCanvas.toBlob(blob => {
    const name = `camera_${Date.now()}.jpg`;
    const url = URL.createObjectURL(blob);
    pendingFiles.push({ file: blob, name, url });
    renderQueue();
  }, "image/jpeg", 0.92);
}

// ---------------------------------------------------------------------
// Queue rendering
// ---------------------------------------------------------------------
function renderQueue() {
  queue.hidden = pendingFiles.length === 0;
  queueCount.textContent = pendingFiles.length;
  scanBtn.disabled = pendingFiles.length === 0;

  queueItems.innerHTML = "";
  pendingFiles.forEach((item, idx) => {
    const el = document.createElement("div");
    el.className = "queue__item";
    el.innerHTML = `
      <img src="${item.url}" alt="Pending label image ${idx + 1}">
      <button class="queue__remove" aria-label="Remove image" data-idx="${idx}">✕</button>
    `;
    queueItems.appendChild(el);
  });

  queueItems.querySelectorAll(".queue__remove").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      URL.revokeObjectURL(pendingFiles[idx].url);
      pendingFiles.splice(idx, 1);
      renderQueue();
    });
  });
}

// ---------------------------------------------------------------------
// Submit to backend
// ---------------------------------------------------------------------
scanBtn.addEventListener("click", async () => {
  if (pendingFiles.length === 0) return;

  scanBtn.disabled = true;
  setStatus(`Scanning ${pendingFiles.length} image(s)...`, "busy");

  const formData = new FormData();
  pendingFiles.forEach(item => formData.append("images", item.file, item.name));
  formData.append("product_type", productType.value || "other");
  formData.append("is_imported", isImported.checked ? "true" : "false");

  try {
    const res = await fetch("/api/process", { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }
    const result = await res.json();
    renderTicket(result);
    setStatus("Scan complete.", "");
    pendingFiles.forEach(item => URL.revokeObjectURL(item.url));
    pendingFiles.length = 0;
    renderQueue();
  } catch (err) {
    setStatus("Scan failed: " + err.message, "error");
  } finally {
    scanBtn.disabled = pendingFiles.length === 0;
  }
});

function setStatus(msg, kind) {
  stationStatus.textContent = msg;
  stationStatus.className = "station__status" + (kind === "error" ? " is-error" : kind === "busy" ? " is-busy" : "");
}

// ---------------------------------------------------------------------
// Ticket rendering — driven entirely by the backend's compliance
// verdict, so the log shows what's right / wrong / missing against
// the rules for the chosen product type, not raw OCR text.
// ---------------------------------------------------------------------
const STATUS_ICON = { pass: "✓", fail: "✕", warn: "!", info: "i" };

const VERDICT_COPY = {
  compliant:     "Compliant — all required declarations found",
  needs_review:  "Needs review — some declarations look incomplete",
  non_compliant: "Non-compliant — required declarations missing",
};

let ticketCounter = 0;

function renderTicket(result) {
  ticketCounter += 1;
  const node = ticketTemplate.content.cloneNode(true);
  const compliance = result.compliance || { checks: [], overall_status: "needs_review", required_total: 0, required_passed: 0 };

  // Photos flagged by the compliance/visual checks, keyed by filename.
  let currentAnnotated = { ...(result.annotated_images || {}) };

  node.querySelector(".ticket__id").textContent = `#${String(ticketCounter).padStart(3, "0")}`;
  node.querySelector(".ticket__time").textContent = new Date().toLocaleTimeString();

  const meta = node.querySelector(".ticket__meta");
  const imgCount = (result.per_image_results || []).length;
  meta.appendChild(badge(`${imgCount} image${imgCount === 1 ? "" : "s"} scanned`, "neutral"));
  meta.appendChild(badge(compliance.product_type_label || "General", "neutral"));
  const requiredBadge = badge("", "neutral");
  meta.appendChild(requiredBadge);

  const verdictEl = node.querySelector(".ticket__verdict");
  const photosEl = node.querySelector(".ticket__photos");
  const checksEl = node.querySelector(".ticket__checks");

  function applyCompliance(compliance) {
    requiredBadge.textContent = `${compliance.required_passed}/${compliance.required_total} required checks passed`;
    requiredBadge.className = `ticket__badge ticket__badge--${compliance.overall_status === "compliant" ? "ok" : "warn"}`;

    verdictEl.className = "ticket__verdict";
    verdictEl.classList.add(`ticket__verdict--${compliance.overall_status}`);
    verdictEl.textContent = VERDICT_COPY[compliance.overall_status] || "Review needed";

    checksEl.innerHTML = "";
    // Required checks first (so gaps that actually matter lead the list),
    // then optional/conditional ones; within each group, worst-first.
    const severityOrder = { fail: 0, warn: 1, info: 2, pass: 3 };
    const sortedChecks = [...compliance.checks].sort((a, b) => {
      if (a.required !== b.required) return a.required ? -1 : 1;
      return severityOrder[a.status] - severityOrder[b.status];
    });

    sortedChecks.forEach(check => {
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

      const citation = document.createElement("span");
      citation.className = "check-row__citation";
      citation.textContent = check.citation;

      top.append(label, citation);

      const message = document.createElement("div");
      message.className = "check-row__message";
      message.textContent = check.message;

      body.append(top, message);
      row.append(icon, body);
      checksEl.appendChild(row);
    });
  }

  function applyPhotos() {
    const names = Object.keys(currentAnnotated);
    photosEl.innerHTML = "";
    if (!names.length) {
      photosEl.hidden = true;
      return;
    }
    photosEl.hidden = false;
    const heading = document.createElement("p");
    heading.className = "ticket__photos-label";
    heading.textContent = "Flagged on the photo";
    photosEl.appendChild(heading);

    const strip = document.createElement("div");
    strip.className = "img-thumbs";
    names.forEach(name => {
      const a = document.createElement("a");
      a.className = "img-thumb";
      a.href = currentAnnotated[name];
      a.target = "_blank";
      a.rel = "noopener";
      a.title = "Open full size";
      const img = document.createElement("img");
      img.src = currentAnnotated[name];
      img.alt = `Flagged issues on ${name}`;
      a.appendChild(img);
      strip.appendChild(a);
    });
    photosEl.appendChild(strip);
  }

  applyCompliance(compliance);
  applyPhotos();

  logEmpty.hidden = true;
  logFeed.prepend(node);
  logCount.textContent = logFeed.children.length;
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `ticket__badge ticket__badge--${kind}`;
  span.textContent = text;
  return span;
}