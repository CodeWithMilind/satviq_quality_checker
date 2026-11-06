/* SATVIQ — Resume Intelligence: production UI.
 *
 * Vanilla JS, hash-routed pages:
 *   #/process            upload + process + result cards
 *   #/history            searchable, filterable, paginated history
 *   #/resume/<id>        full-page inspector (texts, AI output,
 *                        quality report, token usage, attempts)
 *   #/settings           LLM system-prompt configuration
 *
 * All data comes from the backend through js/api.js (window.Api).
 * Nothing is hardcoded: engine names, quality scores, token counts
 * and statuses are rendered from API responses only. Where the
 * backend has no value, the UI shows "N/A".
 */
"use strict";

const $ = (id) => document.getElementById(id);

const ENGINE_LABELS = {
  tika: "Apache Tika",
  pymupdf: "PyMuPDF",
  docling: "Docling",
  ocr: "OCR",
  muse_ai: "Muse AI",
  muse: "Muse AI",
};

const state = {
  health: null,
  selectedFiles: [], // { file, error }
  processing: false,
  history: { page: 1, pageSize: 10, q: "", status: "", data: null },
  analytics: { days: "" },
  recordCache: {}, // resume_id -> full record
  settings: null,
};

function engineLabel(name) {
  if (!name) return "N/A";
  return ENGINE_LABELS[String(name).toLowerCase()] || String(name);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return "N/A";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(2) + " MB";
}

function fmtMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "N/A";
  const seconds = Number(ms) / 1000;
  return seconds >= 60
    ? (seconds / 60).toFixed(2) + " min"
    : seconds.toFixed(2) + " sec";
}

function fmtNum(value) {
  if (value === null || value === undefined) return "N/A";
  return Number(value).toLocaleString("en-US");
}

function fmtDate(iso) {
  if (!iso) return "N/A";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

function toast(message, kind) {
  const root = $("toastRoot");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

/* ------------------------------------------------------------------ */
/* Quality helpers — presentation only. The Quality Checker itself    */
/* deliberately reports component scores; the headline "Quality       */
/* Score" is the mean of the component scores the backend returned,   */
/* shown alongside the components so nothing is hidden.               */
/* ------------------------------------------------------------------ */

function qualityComponents(quality) {
  if (!quality) return [];
  const defs = [
    ["Text Health", quality.text_health],
    ["Completeness", quality.completeness],
    ["Reading Order", quality.reading_order],
  ];
  return defs
    .filter(([, f]) => f && typeof f.score === "number")
    .map(([label, f]) => ({ label, filter: f, score: f.score }));
}

function overallScore(record) {
  const components = qualityComponents(record && record.quality);
  if (!components.length) return null;
  const mean = components.reduce((sum, c) => sum + c.score, 0) / components.length;
  return Math.round(mean);
}

function scoreClass(score) {
  return score >= 75 ? "high" : score >= 50 ? "mid" : "low";
}

function statusChip(record) {
  if (!record.success) return '<span class="chip chip-bad">Failed</span>';
  const q = record.final_quality_status || record.quality_status;
  const cls = q === "PASS" ? "chip-ok" : q === "WARNING" ? "chip-warn" : q === "FAIL" ? "chip-bad" : "";
  return '<span class="chip chip-ok">Processed</span>' +
    (q ? ` <span class="chip ${cls}">Quality ${escapeHtml(q)}</span>` : "");
}

/* ------------------------------------------------------------------ */
/* Three-dot menu                                                     */
/* ------------------------------------------------------------------ */

function menuHtml(record) {
  const id = escapeHtml(record.resume_id);
  return `
  <div class="menu-wrap">
    <button class="menu-btn" data-menu-btn="${id}" aria-haspopup="true"
            aria-label="Actions for ${escapeHtml(record.filename || "resume")}">⋮</button>
    <div class="menu" data-menu="${id}" hidden>
      <button data-menu-action="open" data-id="${id}">Open Details</button>
      <button data-menu-action="tab" data-tab="text" data-id="${id}">View Extracted Text</button>
      <button data-menu-action="tab" data-tab="normalized" data-id="${id}">View Normalized Text</button>
      <button data-menu-action="tab" data-tab="ai" data-id="${id}">View AI Output</button>
      <button data-menu-action="section" data-section="quality" data-id="${id}">View Quality Report</button>
      <button data-menu-action="section" data-section="attempts" data-id="${id}">View Extraction Attempts</button>
      <hr />
      <button data-menu-action="download-json" data-id="${id}">Download JSON</button>
      ${record.has_original_file ? `<a href="${Api.downloadOriginalUrl(record.resume_id)}" download>Download Original File</a>` : ""}
      ${(record.extracted_text || record.extracted_text_chars) ? `<a href="${Api.downloadTextUrl(record.resume_id)}" download>Download Extracted Text</a>` : ""}
      <hr />
      <button class="menu-danger" data-menu-action="delete" data-id="${id}">Delete Resume</button>
    </div>
  </div>`;
}

document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-menu-btn]");
  document.querySelectorAll(".menu").forEach((menu) => {
    const wrap = menu.closest(".menu-wrap");
    const owner = wrap ? wrap.querySelector("[data-menu-btn]") : null;
    if (!btn || owner !== btn) menu.hidden = true;
  });
  if (btn) {
    const menu = btn.closest(".menu-wrap").querySelector(".menu");
    menu.hidden = !menu.hidden;
    event.stopPropagation();
    return;
  }
  const action = event.target.closest("[data-menu-action]");
  if (action) {
    event.stopPropagation();
    handleMenuAction(action.dataset.menuAction, action.dataset.id, action.dataset);
  }
});

async function handleMenuAction(action, id, dataset) {
  if (action === "open") location.hash = "#/resume/" + id;
  else if (action === "tab") location.hash = "#/resume/" + id + "?tab=" + dataset.tab;
  else if (action === "section") location.hash = "#/resume/" + id + "?section=" + dataset.section;
  else if (action === "download-json") {
    try {
      const record = state.recordCache[id] || (await Api.getResume(id));
      const blob = new Blob([JSON.stringify(record, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = (record.filename || "resume").replace(/\.[^.]+$/, "") + "_record.json";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) { toast(err.message, "error"); }
  } else if (action === "delete") {
    if (!confirm("Delete this resume and its stored result?")) return;
    try {
      await Api.deleteResume(id);
      delete state.recordCache[id];
      toast("Resume deleted.", "success");
      if (location.hash.startsWith("#/resume/")) location.hash = "#/history";
      else if (location.hash.startsWith("#/history")) renderHistory();
      else renderProcessResults([]);
    } catch (err) { toast(err.message, "error"); }
  }
}

/* ------------------------------------------------------------------ */
/* Result card (shared by Process results and History)                */
/* ------------------------------------------------------------------ */

function resultCardHtml(record) {
  const score = overallScore(record);
  const tokens = record.token_usage || {};
  const hasTokens = tokens.usage_available && tokens.total_tokens !== null && tokens.total_tokens !== undefined;
  const attempts = record.attempt_count ??
    (Array.isArray(record.attempts) ? record.attempts.length : null);
  return `
  <article class="card result-card" data-card-id="${escapeHtml(record.resume_id)}">
    <div class="result-head">
      <div>
        <div class="result-title">${escapeHtml(record.filename || "Untitled")}</div>
        <div class="result-meta">
          ${escapeHtml(fmtBytes(record.file_size_bytes))} · Uploaded ${escapeHtml(fmtDate(record.uploaded_at))}
          · Primary engine: ${escapeHtml(engineLabel(record.primary_extractor || record.extractor_used))}
        </div>
        <div style="margin-top:6px">${statusChip(record)}</div>
      </div>
      ${menuHtml(record)}
    </div>
    ${record.error ? `<div class="error-box">${escapeHtml(record.error)}</div>` : ""}
    <div class="metric-grid">
      <div class="metric"><div class="metric-label">Quality Score</div>
        <div class="metric-value ${score === null ? "na" : ""}">${score === null ? "N/A" : score + "/100"}</div></div>
      <div class="metric"><div class="metric-label">Input Tokens</div>
        <div class="metric-value ${hasTokens ? "" : "na"}">${hasTokens ? fmtNum(tokens.input_tokens) : "N/A"}</div></div>
      <div class="metric"><div class="metric-label">Output Tokens</div>
        <div class="metric-value ${hasTokens ? "" : "na"}">${hasTokens ? fmtNum(tokens.output_tokens) : "N/A"}</div></div>
      <div class="metric"><div class="metric-label">Total Tokens</div>
        <div class="metric-value ${hasTokens ? "" : "na"}">${hasTokens ? fmtNum(tokens.total_tokens) : "N/A"}</div></div>
      <div class="metric"><div class="metric-label">Duration</div>
        <div class="metric-value ${record.duration_ms == null ? "na" : ""}">${fmtMs(record.duration_ms)}</div></div>
      <div class="metric"><div class="metric-label">Attempts</div>
        <div class="metric-value ${attempts === null ? "na" : ""}">${attempts === null ? "N/A" : attempts}</div></div>
    </div>
  </article>`;
}

/* ------------------------------------------------------------------ */
/* Process page                                                       */
/* ------------------------------------------------------------------ */

function maxUploadBytes() {
  return (state.health && state.health.max_upload_mb ? state.health.max_upload_mb : 5) * 1024 * 1024;
}

function validateFile(file) {
  const max = maxUploadBytes();
  const maxMb = state.health && state.health.max_upload_mb ? state.health.max_upload_mb : 5;
  if (file.size > max) {
    return `File exceeds the ${maxMb} MB limit. Please upload a smaller resume.`;
  }
  if (file.size === 0) return "File is empty.";
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  const supported = (state.health && state.health.supported_extensions) || [];
  if (supported.length && !supported.includes(ext)) {
    return `Unsupported file type ".${ext}". Supported types: ${supported.join(", ")}.`;
  }
  return null;
}

function renderProcess() {
  const view = $("view-process");
  const supported = state.health ? (state.health.supported_extensions || []).join(", ") : "—";
  const maxMb = state.health && state.health.max_upload_mb ? state.health.max_upload_mb : 5;
  view.innerHTML = `
    <h2>Process Resume</h2>
    <p class="page-subtitle">Upload a resume and run it through the extraction pipeline.
      Primary engine: <strong>${escapeHtml(engineLabel(state.health && state.health.extractor_order && state.health.extractor_order[0]))}</strong>
      ${state.health && state.health.extractor_order ? `· Pipeline order: ${state.health.extractor_order.map(engineLabel).join(" → ")}` : ""}</p>
    <div class="card">
      <div class="dropzone" id="dropzone" tabindex="0" role="button"
           aria-label="Upload resumes: drag and drop files or activate to browse">
        <p class="dz-title">Drag &amp; drop resume files here</p>
        <p class="hint">or</p>
        <button class="btn btn-primary" id="browseBtn" type="button">Choose Files</button>
        <p class="hint">Supported types: ${escapeHtml(supported)} · Maximum ${maxMb} MB per file</p>
        <input type="file" id="fileInput" multiple hidden
               accept="${state.health ? (state.health.supported_extensions || []).map((e) => "." + e).join(",") : ""}" />
      </div>
      <div class="selected-files" id="selectedFiles"></div>
      <div class="inline-row mt">
        <div style="flex:1"></div>
        <button class="btn btn-primary" id="processBtn" type="button" disabled>Process Resume</button>
      </div>
      <div class="progress-wrap" id="progressWrap" hidden>
        <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
        <div class="progress-label" id="progressLabel">Uploading…</div>
      </div>
    </div>
    <div id="processResults"></div>`;

  const dropzone = $("dropzone");
  const input = $("fileInput");
  $("browseBtn").addEventListener("click", (e) => { e.stopPropagation(); input.click(); });
  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => { addFiles(Array.from(input.files)); input.value = ""; });
  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (e) => { e.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (e) => { e.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", (e) => addFiles(Array.from(e.dataTransfer.files)));
  $("processBtn").addEventListener("click", processSelected);
  renderSelectedFiles();
}

function addFiles(files) {
  files.forEach((file) => {
    state.selectedFiles.push({ file, error: validateFile(file) });
  });
  renderSelectedFiles();
}

function renderSelectedFiles() {
  const wrap = $("selectedFiles");
  if (!wrap) return;
  if (!state.selectedFiles.length) { wrap.innerHTML = ""; }
  else {
    wrap.innerHTML = state.selectedFiles.map((entry, index) => `
      <div class="file-row ${entry.error ? "invalid" : ""}">
        <div>
          <div class="file-name">${escapeHtml(entry.file.name)}</div>
          ${entry.error ? `<div class="file-error">${escapeHtml(entry.error)}</div>` : ""}
        </div>
        <div class="spacer"></div>
        <span class="file-meta">${escapeHtml(fmtBytes(entry.file.size))} · ${escapeHtml(entry.file.type || entry.file.name.split(".").pop() || "file")}</span>
        <button class="btn btn-small" data-remove-file="${index}" type="button">Remove</button>
      </div>`).join("");
    wrap.querySelectorAll("[data-remove-file]").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.selectedFiles.splice(Number(btn.dataset.removeFile), 1);
        renderSelectedFiles();
      }));
  }
  const btn = $("processBtn");
  if (btn) {
    btn.disabled = state.processing ||
      !state.selectedFiles.length ||
      state.selectedFiles.some((entry) => entry.error);
  }
}

async function processSelected() {
  const valid = state.selectedFiles.filter((entry) => !entry.error);
  if (!valid.length) return;
  state.processing = true;
  renderSelectedFiles();
  const wrap = $("progressWrap");
  const fill = $("progressFill");
  const label = $("progressLabel");
  wrap.hidden = false;
  fill.style.width = "0%";
  label.textContent = "Uploading…";
  try {
    const body = await Api.processResumes(
      valid.map((entry) => entry.file),
      (percent) => {
        fill.style.width = percent + "%";
        label.textContent = percent >= 100
          ? "Upload complete — processing on the server…"
          : "Uploading… " + percent + "%";
      });
    const results = body.results || [];
    results.forEach((r) => { state.recordCache[r.resume_id] = r; });
    renderProcessResults(results);
    const failed = results.filter((r) => !r.success).length;
    if (failed) toast(failed + " file(s) could not be processed. See the result cards for details.", "error");
    else toast(results.length + " resume(s) processed.", "success");
    state.selectedFiles = [];
    renderSelectedFiles();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    state.processing = false;
    wrap.hidden = true;
    renderSelectedFiles();
  }
}

function attachCardNav(container) {
  container.querySelectorAll("[data-card-id]").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest(".menu-wrap")) return;
      location.hash = "#/resume/" + card.dataset.cardId;
    });
  });
}

function renderProcessResults(results) {
  const wrap = $("processResults");
  if (!wrap) return;
  if (!results.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = "<h3 style='margin:18px 0 10px'>Results</h3>" +
    results.map(resultCardHtml).join("");
  attachCardNav(wrap);
}

/* ------------------------------------------------------------------ */
/* History page (server-side search, filter, pagination)              */
/* ------------------------------------------------------------------ */

function renderHistory() {
  const view = $("view-history");
  view.innerHTML = `
    <h2>Resume History</h2>
    <p class="page-subtitle">Previously processed resumes, loaded page by page from the backend.</p>
    <div class="history-toolbar">
      <input type="search" class="text-input" id="historySearch" placeholder="Search by filename…"
             value="${escapeHtml(state.history.q)}" aria-label="Search by filename" />
      <select class="select-input" id="historyStatus" aria-label="Filter by status">
        <option value="">All statuses</option>
        <option value="PASS" ${state.history.status === "PASS" ? "selected" : ""}>Quality PASS</option>
        <option value="WARNING" ${state.history.status === "WARNING" ? "selected" : ""}>Quality WARNING</option>
        <option value="FAIL" ${state.history.status === "FAIL" ? "selected" : ""}>Quality FAIL</option>
        <option value="failed" ${state.history.status === "failed" ? "selected" : ""}>Processing failed</option>
      </select>
      <button class="btn" id="historyRefresh" type="button">Refresh</button>
      <button class="btn btn-danger" id="historyDeleteAll" type="button">Delete All Records</button>
    </div>
    <div id="historyList"><div class="loading">Loading history…</div></div>
    <div class="pagination" id="historyPagination"></div>`;

  let debounce = null;
  $("historySearch").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.history.q = e.target.value.trim();
      state.history.page = 1;
      loadHistory();
    }, 300);
  });
  $("historyStatus").addEventListener("change", (e) => {
    state.history.status = e.target.value;
    state.history.page = 1;
    loadHistory();
  });
  $("historyRefresh").addEventListener("click", loadHistory);
  $("historyDeleteAll").addEventListener("click", async () => {
    if (!confirm("Delete ALL resume records and their stored results? This cannot be undone.")) return;
    try {
      const body = await Api.deleteAllResumes();
      state.recordCache = {};
      state.history.page = 1;
      toast("Deleted " + (body.deleted_count ?? 0) + " record(s).", "success");
      renderHistory();
    } catch (err) { toast(err.message, "error"); }
  });
  loadHistory();
}

async function loadHistory() {
  const list = $("historyList");
  const pager = $("historyPagination");
  if (!list) return;
  list.innerHTML = '<div class="loading">Loading history…</div>';
  try {
    const data = await Api.listResumes({
      q: state.history.q,
      status: state.history.status,
      page: state.history.page,
      page_size: state.history.pageSize,
    });
    state.history.data = data;
    const resumes = data.resumes || [];
    if (!resumes.length) {
      list.innerHTML = '<div class="empty-state">No resumes found. Process a resume to see it here.</div>';
    } else {
      list.innerHTML = '<div class="history-list">' + resumes.map(resultCardHtml).join("") + "</div>";
      attachCardNav(list);
    }
    const pages = data.pages || 0;
    pager.innerHTML = pages > 0 ? `
      <button class="btn btn-small" id="pagePrev" ${data.page <= 1 ? "disabled" : ""}>Previous</button>
      <span>Page ${data.page} of ${pages} · ${fmtNum(data.total)} resume(s)</span>
      <button class="btn btn-small" id="pageNext" ${data.page >= pages ? "disabled" : ""}>Next</button>` : "";
    const prev = $("pagePrev");
    const next = $("pageNext");
    if (prev) prev.addEventListener("click", () => { state.history.page -= 1; loadHistory(); });
    if (next) next.addEventListener("click", () => { state.history.page += 1; loadHistory(); });
  } catch (err) {
    list.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
    pager.innerHTML = "";
  }
}

/* ------------------------------------------------------------------ */
/* Resume inspector                                                   */
/* ------------------------------------------------------------------ */

function attemptStatus(attempt) {
  if (attempt.error_message || attempt.error) return { label: "Failed", cls: "chip-bad" };
  if (attempt.decision === "accept" || attempt.is_final_selected || attempt.selected) {
    return { label: "Accepted", cls: "chip-ok" };
  }
  if (attempt.extraction_status) {
    return { label: String(attempt.extraction_status), cls: "chip-info" };
  }
  if (attempt.success) return { label: "Completed", cls: "chip-info" };
  return { label: "Rejected", cls: "chip-warn" };
}

function attemptText(attempt) {
  return attempt.extracted_text ?? attempt.text ?? "";
}

function attemptCardHtml(attempt, index) {
  const status = attemptStatus(attempt);
  const text = attemptText(attempt);
  const chars = attempt.extracted_text_length ?? text.length;
  const shortError = attempt.error_message || attempt.error || "";
  const quality = attempt.quality_checker_report || attempt.quality || null;
  const qComponents = qualityComponents(quality);
  const ocr = attempt.ocr && typeof attempt.ocr === "object" ? attempt.ocr : null;
  const id = "attempt-detail-" + index;
  return `
  <div class="attempt-card">
    <div class="attempt-head">
      <span class="engine">${escapeHtml(engineLabel(attempt.extractor_name))}</span>
      ${attempt.extractor_version ? `<span class="chip">v${escapeHtml(attempt.extractor_version)}</span>` : ""}
      <span class="chip ${status.cls}">${escapeHtml(status.label)}</span>
      ${quality && quality.status ? `<span class="chip">QC ${escapeHtml(quality.status)}</span>` : ""}
      <span class="meta">${fmtNum(chars)} chars · ${fmtMs(attempt.duration_ms)}</span>
      <span style="flex:1"></span>
      <button class="btn btn-small" data-toggle-attempt="${id}" type="button">Details</button>
      ${attempt.extractor_name !== "muse_ai" ? `<button class="btn btn-small" data-view-output="${escapeHtml(attempt.extractor_name)}" type="button">View Output</button>` : ""}
    </div>
    ${shortError ? `<div class="meta" style="color:var(--bad);margin-top:4px">${escapeHtml(String(shortError).slice(0, 160))}</div>` : ""}
    <div class="attempt-detail" id="${id}" hidden>
      ${attempt.decision_reason || attempt.fallback_reason ? `<p class="hint">${escapeHtml(attempt.decision_reason || attempt.fallback_reason)}</p>` : ""}
      <table class="kv-table">
        <tr><td>Actual duration</td><td>${fmtMs(attempt.duration_ms)}</td></tr>
        <tr><td>OCR</td><td>${ocr ? escapeHtml(Object.entries(ocr).map(([k, v]) => k + ": " + (typeof v === "object" ? JSON.stringify(v) : v)).join(" · ")) : "N/A"}</td></tr>
        ${attempt.provider || attempt.model ? `<tr><td>LLM</td><td>${escapeHtml(attempt.provider || "")} ${escapeHtml(attempt.model || "")}</td></tr>` : ""}
        ${attempt.json_validation ? `<tr><td>JSON validation</td><td>${escapeHtml(attempt.json_validation)}</td></tr>` : ""}
        ${attempt.input_type ? `<tr><td>Input type</td><td>${escapeHtml(attempt.input_type)}</td></tr>` : ""}
      </table>
      ${qComponents.length ? `<p class="hint">Quality: ${qComponents.map((c) => escapeHtml(c.label) + " " + c.score).join(" · ")}</p>` : ""}
      ${shortError ? `<div class="error-box">${escapeHtml(shortError)}${attempt.error_traceback ? "\n" + escapeHtml(attempt.error_traceback) : ""}</div>` : ""}
      ${attempt.metadata ? `<details><summary class="hint">Engine metadata</summary><pre class="json-view" style="max-height:240px">${escapeHtml(JSON.stringify(attempt.metadata, null, 2))}</pre></details>` : ""}
      <h4 style="margin-top:10px">Extracted text</h4>
      ${text ? `<div class="text-panel">${escapeHtml(text)}</div>` : '<p class="hint">This attempt produced no text.</p>'}
    </div>
  </div>`;
}

function structuredValueHtml(value) {
  if (value === null || value === undefined || value === "") return '<span class="hint">—</span>';
  if (Array.isArray(value)) {
    if (!value.length) return '<span class="hint">—</span>';
    if (value.every((v) => typeof v !== "object")) {
      return escapeHtml(value.join(", "));
    }
    return value.map((item) => `
      <div style="border-left:3px solid var(--border);padding:2px 0 2px 10px;margin:4px 0">
        ${Object.entries(item || {}).filter(([, v]) => v !== "" && v !== null && !(Array.isArray(v) && !v.length))
          .map(([k, v]) => `<div><strong>${escapeHtml(k)}:</strong> ${structuredValueHtml(v)}</div>`).join("")}
      </div>`).join("");
  }
  if (typeof value === "object") {
    return Object.entries(value)
      .map(([k, v]) => `<div><strong>${escapeHtml(k)}:</strong> ${structuredValueHtml(v)}</div>`).join("");
  }
  return escapeHtml(value);
}

function qualityReportHtml(record) {
  const quality = record.quality;
  const score = overallScore(record);
  if (!quality) return '<p class="hint">No quality report is available for this resume.</p>';
  const components = qualityComponents(quality);
  const cards = components.map(({ label, filter }) => {
    const flags = filter.flags || [];
    const reasons = filter.reasons || [];
    let extra = "";
    if (label === "Completeness") {
      const detected = (filter.detected_sections || []).map((s) => `<span class="chip chip-ok">${escapeHtml(s)}</span>`).join("");
      const missing = (filter.missing_sections || []).map((s) => `<span class="chip chip-bad">${escapeHtml(s)}</span>`).join("");
      extra = `${detected ? `<div class="hint" style="margin-top:8px">Detected sections</div><div class="section-chips">${detected}</div>` : ""}
               ${missing ? `<div class="hint" style="margin-top:8px">Missing sections</div><div class="section-chips">${missing}</div>` : ""}`;
    }
    return `
    <div class="qcard">
      <h4><span>${escapeHtml(label)}</span><span>${filter.score}/100</span></h4>
      <div class="score-bar"><div class="score-fill ${scoreClass(filter.score)}" style="width:${Math.max(0, Math.min(100, filter.score))}%"></div></div>
      ${flags.length ? `<div class="hint">Checks flagged: ${flags.map(escapeHtml).join(", ")}</div>` : '<div class="hint">All checks passed for this component.</div>'}
      ${reasons.length ? `<ul class="check-list">${reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : ""}
      ${extra}
    </div>`;
  }).join("");
  return `
    <div class="inline-row mb">
      <strong>Overall Quality Score: ${score === null ? "N/A" : score + "/100"}</strong>
      <span class="chip ${quality.status === "PASS" ? "chip-ok" : quality.status === "WARNING" ? "chip-warn" : "chip-bad"}">${escapeHtml(quality.status || "")}</span>
      <span class="hint">Overall = mean of the component scores below, as returned by the Quality Checker.</span>
    </div>
    ${record.selection_reason ? `<p class="hint">Selection: ${escapeHtml(record.selection_reason)}</p>` : ""}
    ${(record.warnings || []).length ? `<p class="hint">Warnings: ${(record.warnings || []).map(escapeHtml).join(" · ")}</p>` : ""}
    <div class="quality-components">${cards}</div>`;
}

function tokenUsageHtml(record) {
  const tokens = record.token_usage || {};
  const meta = record.resume_json_meta || {};
  const requests = record.muse_requests || [];
  if (!tokens.usage_available && !requests.length) {
    return '<p class="hint">Token usage is not available for this resume (no LLM usage was reported).</p>';
  }
  const rows = requests.map((r) => `
    <tr><td>${escapeHtml(r.stage || "")}</td><td>${escapeHtml(r.model || "")}</td>
    <td>${r.input_tokens != null ? fmtNum(r.input_tokens) : "N/A"}</td>
    <td>${r.output_tokens != null ? fmtNum(r.output_tokens) : "N/A"}</td>
    <td>${r.total_tokens != null ? fmtNum(r.total_tokens) : "N/A"}</td>
    <td>${fmtMs(r.duration_ms)}</td><td>${escapeHtml(r.status || "")}</td></tr>`).join("");
  return `
    <div class="metric-grid" style="margin-top:0">
      <div class="metric"><div class="metric-label">Input Tokens</div><div class="metric-value">${tokens.input_tokens != null ? fmtNum(tokens.input_tokens) : "N/A"}</div></div>
      <div class="metric"><div class="metric-label">Output Tokens</div><div class="metric-value">${tokens.output_tokens != null ? fmtNum(tokens.output_tokens) : "N/A"}</div></div>
      <div class="metric"><div class="metric-label">Total Tokens</div><div class="metric-value">${tokens.total_tokens != null ? fmtNum(tokens.total_tokens) : "N/A"}</div></div>
      <div class="metric"><div class="metric-label">Provider</div><div class="metric-value">${escapeHtml(meta.provider || "N/A")}</div></div>
      <div class="metric"><div class="metric-label">Model</div><div class="metric-value">${escapeHtml(meta.model || (tokens.models || []).join(", ") || "N/A")}</div></div>
      <div class="metric"><div class="metric-label">LLM Calls</div><div class="metric-value">${tokens.api_calls != null ? tokens.api_calls : requests.length}</div></div>
    </div>
    ${rows ? `<table class="kv-table mt"><thead><tr>
      <td>Stage</td><td>Model</td><td>Input</td><td>Output</td><td>Total</td><td>Duration</td><td>Status</td></tr></thead>
      <tbody>${rows}</tbody></table>` : ""}`;
}

async function renderResume(id, params) {
  const view = $("view-resume");
  view.innerHTML = '<div class="loading">Loading resume…</div>';
  let record = state.recordCache[id];
  try {
    record = await Api.getResume(id);
    state.recordCache[id] = record;
  } catch (err) {
    if (!record) {
      view.innerHTML = `<p><a href="#/history">← Back to History</a></p><div class="error-box">${escapeHtml(err.message)}</div>`;
      return;
    }
  }
  const norm = record.text_normalization || {};
  const extractedText = record.extracted_text || "";
  const normalizedText = norm.normalized_text || "";
  const activeTab = ["text", "normalized", "selected", "ai"].includes(params.tab) ? params.tab : "text";

  // --- Shared extractor viewer state -------------------------------------
  // ONE extractor selector above the tabs drives BOTH the Extracted and
  // Normalized tabs. Per extractor, the viewer shows its manual
  // comparison run (record.extractor_results) when one exists — the
  // fresher, user-requested output for that tool, badged "Manual run" —
  // otherwise its pipeline attempt. Pipeline attempts, the selected
  // extractor and the AI output are never changed by manual runs.
  const MANUAL_TOOLS = ["tika", "pymupdf", "docling"];
  const manualResults = record.extractor_results || {};
  const toolAttempts = (record.attempts || []).filter(
    (a) => a.extractor_name && a.extractor_name !== "muse_ai");
  const viewerTools = [];
  ["tika", "pymupdf", "docling", "ocr"].forEach((name) => {
    if (toolAttempts.some((a) => a.extractor_name === name) || manualResults[name]) viewerTools.push(name);
  });
  toolAttempts.forEach((a) => { if (!viewerTools.includes(a.extractor_name)) viewerTools.push(a.extractor_name); });
  Object.keys(manualResults).forEach((name) => { if (!viewerTools.includes(name)) viewerTools.push(name); });
  const finalAttempt = toolAttempts.find((a) => a.is_final_selected || a.selected)
    || toolAttempts.find((a) => a.extractor_name === record.extractor_used)
    || toolAttempts.find((a) => (a.extracted_text || a.text))
    || toolAttempts[0] || null;
  // Source extractor of the record's selected normalized text (the
  // normalized text produced from the pipeline-selected extractor and
  // sent to the AI stage) — shown in its own "Selected Normalized
  // Text" tab, NOT in the extractor selector.
  const selectedSource = norm.source || record.extractor_used
    || (finalAttempt ? finalAttempt.extractor_name : null);
  let currentExtractor = (params.extractor && viewerTools.includes(params.extractor))
    ? params.extractor
    : (finalAttempt ? finalAttempt.extractor_name : (viewerTools[0] || null));
  let currentTab = activeTab;
  const runningManual = new Set();

  function viewerData(name) {
    const manual = manualResults[name];
    if (manual) {
      return { raw: manual.extracted_text || "",
        normalized: manual.normalized_text != null ? manual.normalized_text : null,
        source: "manual", status: manual.status,
        qualityStatus: manual.quality_status, qualityScore: manual.quality_score,
        error: manual.error, durationMs: manual.duration_ms,
        pageCount: manual.page_count, isFinal: false };
    }
    const a = toolAttempts.find((x) => x.extractor_name === name);
    if (!a) return null;
    return { raw: a.extracted_text || a.text || "",
      normalized: a.normalized_text != null ? a.normalized_text
        : (name === (norm.source || record.extractor_used) ? normalizedText : null),
      source: "pipeline", status: a.success ? "success" : "failed",
      qualityStatus: a.quality_status, qualityScore: null, error: a.error,
      durationMs: a.duration_ms, pageCount: null,
      isFinal: !!(a.is_final_selected || a.selected) };
  }

  function extractorMetaText(name) {
    const d = viewerData(name);
    if (!d) return "";
    return [d.source === "manual" ? "Manual run" : "Pipeline attempt",
      d.qualityStatus ? "Quality " + d.qualityStatus : null,
      d.qualityScore != null ? "Score " + d.qualityScore + "/100" : null,
      d.isFinal ? "Final selected" : null,
      d.durationMs != null ? fmtMs(d.durationMs) : null].filter(Boolean).join(" · ");
  }

  function manualCardHtml(name) {
    const m = manualResults[name];
    const label = engineLabel(name);
    if (!m) {
      return `<div class="attempt-card"><div class="attempt-head">
        <span class="engine">${escapeHtml(label)}</span>
        <span class="chip chip-info">Not run manually</span>
        <span style="flex:1"></span>
        <button class="btn btn-small" data-run-extractor="${name}" type="button">Run ${escapeHtml(label)}</button>
      </div></div>`;
    }
    const ok = m.status === "success";
    return `<div class="attempt-card"><div class="attempt-head">
      <span class="engine">${escapeHtml(label)}</span>
      <span class="chip ${ok ? "chip-ok" : "chip-bad"}">${ok ? "Completed" : "Failed"}</span>
      ${m.quality_score != null ? `<span class="chip">Quality Score: ${m.quality_score}/100</span>` : ""}
      <span class="meta">${fmtNum(m.char_count ?? (m.extracted_text || "").length)} chars · ${fmtMs(m.duration_ms)}</span>
      <span style="flex:1"></span>
      <button class="btn btn-small" data-view-output="${name}" type="button">View Output</button>
      <button class="btn btn-small" data-run-extractor="${name}" type="button">Re-run</button>
    </div>
    ${m.error ? `<div class="meta" style="color:var(--bad);margin-top:4px">${escapeHtml(String(m.error).slice(0, 200))}</div>` : ""}
    </div>`;
  }

  view.innerHTML = `
    <p><a href="#/history">← Back to History</a></p>
    <div class="card">
      <div class="inspector-head">
        <div>
          <div class="result-title">${escapeHtml(record.filename || "Untitled")}</div>
          <div class="result-meta">${escapeHtml(fmtBytes(record.file_size_bytes))} · ${escapeHtml(record.file_type || "")} ·
            Uploaded ${escapeHtml(fmtDate(record.uploaded_at))} ·
            Extractor used: ${escapeHtml(engineLabel(record.extractor_used))}</div>
          <div style="margin-top:6px">${statusChip(record)}</div>
        </div>
        ${menuHtml(record)}
      </div>
      ${record.error ? `<div class="error-box">${escapeHtml(record.error)}</div>` : ""}
      <div class="metric-grid">
        <div class="metric"><div class="metric-label">Quality Score</div><div class="metric-value">${overallScore(record) === null ? "N/A" : overallScore(record) + "/100"}</div></div>
        <div class="metric"><div class="metric-label">Total Tokens</div><div class="metric-value">${record.token_usage && record.token_usage.usage_available ? fmtNum(record.token_usage.total_tokens) : "N/A"}</div></div>
        <div class="metric"><div class="metric-label">Duration</div><div class="metric-value">${fmtMs(record.duration_ms)}</div></div>
        <div class="metric"><div class="metric-label">Attempts</div><div class="metric-value">${(record.attempts || []).length}</div></div>
        <div class="metric"><div class="metric-label">Pages</div><div class="metric-value">${record.page_count ?? "N/A"}</div></div>
      </div>
    </div>

    <div class="card" id="textViewerCard">
      <div class="panel-toolbar">
        <label class="hint">Extractor
          <select id="extractorSelect" class="select-input" style="width:auto">
            ${viewerTools.map((name) => {
              const d = viewerData(name);
              const noText = d && d.status !== "success" && !(d.raw || "");
              return `<option value="${escapeHtml(name)}" ${name === currentExtractor ? "selected" : ""}>${escapeHtml(engineLabel(name))}${d && d.isFinal ? " (Final)" : ""}${manualResults[name] ? " (Manual run)" : ""}${noText ? " — no text" : ""}</option>`;
            }).join("")}
          </select>
        </label>
        <span class="hint" id="extractorMeta"></span>
      </div>
      <div class="tabs" role="tablist">
        <button data-tab="text" class="${activeTab === "text" ? "active" : ""}">Extracted Text</button>
        <button data-tab="normalized" class="${activeTab === "normalized" ? "active" : ""}">Normalized Text</button>
        <button data-tab="selected" class="${activeTab === "selected" ? "active" : ""}">Selected Normalized Text</button>
        <button data-tab="ai" class="${activeTab === "ai" ? "active" : ""}">AI Output</button>
      </div>
      <div id="tabPanel"></div>
    </div>

    <div class="card" id="qualitySection">
      <h3>Quality Report</h3>
      ${qualityReportHtml(record)}
    </div>

    <div class="card" id="tokenSection">
      <h3>LLM Token Usage</h3>
      ${tokenUsageHtml(record)}
    </div>

    ${(record.hyperlinks || []).length ? `
    <div class="card" id="hyperlinksSection">
      <h3>PDF Hyperlinks</h3>
      <p class="hint">URLs captured from the document's link annotations — authoritative for links in the AI output.</p>
      <table class="kv-table"><thead><tr><td>Page</td><td>URL</td><td>Visible text</td><td>Type</td></tr></thead>
      <tbody>${(record.hyperlinks || []).map((h) => `
        <tr><td>${h.page ?? "N/A"}</td><td style="word-break:break-all">${escapeHtml(h.url || "")}</td>
        <td>${escapeHtml(h.visible_text || "")}</td><td>${escapeHtml(h.type || "")}</td></tr>`).join("")}
      </tbody></table>
    </div>` : ""}

    <div class="card" id="attemptsSection">
      <h3>Extraction Attempts</h3>
      ${(record.attempts || []).length
        ? (record.attempts || []).map(attemptCardHtml).join("")
        : '<p class="hint">No extraction attempts were recorded for this resume.</p>'}
      <div class="manual-compare" style="margin-top:16px">
        <div class="panel-toolbar">
          <strong>Manual Comparison</strong>
          <span class="hint">Run an extractor independently on the stored original file. Manual runs never change the pipeline result or the AI output, and never call the LLM.</span>
          <span class="spacer"></span>
          <button class="btn btn-small btn-primary" data-run-all type="button">Run All Extractors</button>
        </div>
        <div id="manualResults">${MANUAL_TOOLS.map(manualCardHtml).join("")}</div>
      </div>
    </div>`;

  const panel = $("tabPanel");
  function showTab(tab) {
    currentTab = tab;
    view.querySelectorAll("[data-tab]").forEach((b) =>
      b.classList.toggle("active", b.dataset.tab === tab));
    if (tab === "text") {
      panel.innerHTML = `
        <div class="panel-toolbar">
          <span class="hint" id="extractedMeta"></span>
          <span class="spacer"></span>
          <label class="hint"><input type="checkbox" id="compareToggle" /> Compare with normalized text</label>
          <button class="btn btn-small" id="copyTextBtn" type="button">Copy text</button>
        </div>
        <div id="compareArea"></div>`;
      function drawText() {
        const data = viewerData(currentExtractor);
        const area = $("compareArea");
        if (!data) {
          $("extractedMeta").textContent = "";
          area.innerHTML = '<p class="hint">No extractor output is stored for this resume. If the Resume JSON exists, it was produced by the AI stage directly from the document.</p>';
          return;
        }
        const raw = data.raw || "";
        const pages = data.pageCount || record.page_count;
        $("extractedMeta").textContent = raw.length.toLocaleString() + " characters" + (pages ? " · " + pages + " page(s)" : "");
        if ($("compareToggle").checked) {
          area.innerHTML = `
            <div class="compare-grid">
              <div><h4>Original extracted text — ${escapeHtml(engineLabel(currentExtractor))} (${raw.length.toLocaleString()} characters)</h4>
                ${raw ? `<div class="text-panel">${escapeHtml(raw)}</div>` : '<p class="hint">This extractor produced no text.</p>'}</div>
              <div><h4>Normalized text — ${escapeHtml(engineLabel(currentExtractor))} (${(data.normalized || "").length.toLocaleString()} characters)</h4>
                ${data.normalized ? `<div class="text-panel">${escapeHtml(data.normalized)}</div>` : '<p class="hint">Normalized text is not available for this extractor.</p>'}</div>
            </div>`;
          return;
        }
        area.innerHTML = raw
          ? `<div class="text-panel">${escapeHtml(raw)}</div>`
          : '<p class="hint">This extractor produced no text.' + (data.error ? " Error: " + escapeHtml(String(data.error).slice(0, 200)) : "") + "</p>";
      }
      $("copyTextBtn").addEventListener("click", () => copyText((viewerData(currentExtractor) || {}).raw || ""));
      $("compareToggle").addEventListener("change", drawText);
      drawText();
    } else if (tab === "normalized") {
      const d = viewerData(currentExtractor);
      const normText = d ? (d.normalized || "") : "";
      const isPipelineSelected = !!d && d.source === "pipeline"
        && currentExtractor === (norm.source || record.extractor_used);
      panel.innerHTML = `
        <div class="panel-toolbar">
          <span class="hint">${escapeHtml(engineLabel(currentExtractor))} · ${d ? (d.source === "manual" ? "Manual run" : "Pipeline attempt") : "N/A"} · ${normText.length.toLocaleString()} characters${isPipelineSelected && norm.original_chars != null ? " · Original: " + Number(norm.original_chars).toLocaleString() + " characters" : ""}${isPipelineSelected && norm.estimated_reduction_percent != null ? " · Reduction: " + norm.estimated_reduction_percent + "%" : ""}</span>
          <span class="spacer"></span>
          <button class="btn btn-small" id="copyNormBtn" type="button">Copy text</button>
        </div>
        ${isPipelineSelected && (norm.normalization_steps || []).length ? `<p class="hint">Steps applied: ${norm.normalization_steps.map(escapeHtml).join(" · ")}</p>` : ""}
        ${isPipelineSelected && norm.error ? `<div class="error-box">${escapeHtml(norm.error)}</div>` : ""}
        ${normText ? `<div class="text-panel">${escapeHtml(normText)}</div>`
          : '<p class="hint">No normalized text is available for this extractor.</p>'}`;
      $("copyNormBtn").addEventListener("click", () => copyText(normText));
    } else if (tab === "selected") {
      panel.innerHTML = `
        <div class="panel-toolbar">
          <span class="hint">Selected normalized text · from ${escapeHtml(engineLabel(selectedSource))} (the pipeline-selected extractor) · ${normalizedText.length.toLocaleString()} characters${norm.original_chars != null ? " · Original: " + Number(norm.original_chars).toLocaleString() + " characters" : ""}${norm.estimated_reduction_percent != null ? " · Reduction: " + norm.estimated_reduction_percent + "%" : ""}</span>
          <span class="spacer"></span>
          <button class="btn btn-small" id="copySelectedBtn" type="button">Copy text</button>
        </div>
        ${(norm.normalization_steps || []).length ? `<p class="hint">Steps applied: ${norm.normalization_steps.map(escapeHtml).join(" · ")}</p>` : ""}
        ${norm.error ? `<div class="error-box">${escapeHtml(norm.error)}</div>` : ""}
        ${normalizedText ? `<div class="text-panel">${escapeHtml(normalizedText)}</div>`
          : '<p class="hint">No selected normalized text is stored for this resume.</p>'}`;
      $("copySelectedBtn").addEventListener("click", () => copyText(normalizedText));
    } else {
      const resumeJson = record.resume_json;
      panel.innerHTML = `
        <div class="panel-toolbar">
          <span class="hint">${record.resume_json_source ? "Source: " + escapeHtml(record.resume_json_source) : ""}${record.resume_json_meta && record.resume_json_meta.model ? " · Model: " + escapeHtml(record.resume_json_meta.model) : ""}${record.resume_json_meta && record.resume_json_meta.validation ? " · Validation: " + escapeHtml(record.resume_json_meta.validation) : ""}</span>
          <span class="spacer"></span>
          <label class="hint"><input type="checkbox" id="jsonToggle" /> JSON view</label>
          <button class="btn btn-small" id="copyJsonBtn" type="button">Copy JSON</button>
        </div>
        <div id="aiOutputArea"></div>`;
      const area = $("aiOutputArea");
      function drawAi() {
        if (!resumeJson) {
          area.innerHTML = '<p class="hint">No AI output is stored for this resume.</p>';
          return;
        }
        if ($("jsonToggle").checked) {
          area.innerHTML = `<pre class="json-view">${escapeHtml(JSON.stringify(resumeJson, null, 2))}</pre>`;
        } else {
          area.innerHTML = Object.entries(resumeJson).map(([key, value]) => `
            <div class="structured-field"><div class="sf-label">${escapeHtml(key)}</div>
            <div class="sf-value">${structuredValueHtml(value)}</div></div>`).join("");
        }
      }
      $("jsonToggle").addEventListener("change", drawAi);
      $("copyJsonBtn").addEventListener("click", () =>
        copyText(resumeJson ? JSON.stringify(resumeJson, null, 2) : ""));
      drawAi();
    }
  }
  const extractorSelect = $("extractorSelect");
  function drawExtractorMeta() {
    const m = $("extractorMeta");
    if (m) m.textContent = extractorMetaText(currentExtractor);
  }
  if (extractorSelect) {
    extractorSelect.addEventListener("change", (e) => {
      currentExtractor = e.target.value;
      drawExtractorMeta();
      if (currentTab !== "ai" && currentTab !== "selected") showTab(currentTab);
    });
  }
  function selectExtractorInViewer(name) {
    if (!viewerTools.includes(name)) return;
    currentExtractor = name;
    if (extractorSelect) extractorSelect.value = name;
    drawExtractorMeta();
    showTab("text");
    const card = $("textViewerCard");
    if (card) card.scrollIntoView();
  }
  view.querySelectorAll("[data-view-output]").forEach((btn) =>
    btn.addEventListener("click", () => selectExtractorInViewer(btn.dataset.viewOutput)));

  async function runManual(names, triggerBtn) {
    const fresh = names.filter((n) => !runningManual.has(n));
    if (!fresh.length) return;
    fresh.forEach((n) => runningManual.add(n));
    const allRunBtns = view.querySelectorAll("[data-run-extractor], [data-run-all]");
    allRunBtns.forEach((b) => { b.disabled = true; });
    const originalLabel = triggerBtn ? triggerBtn.textContent : "";
    if (triggerBtn) triggerBtn.textContent = "Running…";
    try {
      const body = await Api.runExtractors(id, fresh);
      if (body && body.record) state.recordCache[id] = body.record;
      toast(fresh.length === 1
        ? engineLabel(fresh[0]) + " manual run completed."
        : "Manual extractor runs completed.", "success");
      renderResume(id, { tab: currentTab, extractor: currentExtractor });
    } catch (err) {
      toast(err.message, "error");
      fresh.forEach((n) => runningManual.delete(n));
      allRunBtns.forEach((b) => { b.disabled = false; });
      if (triggerBtn) triggerBtn.textContent = originalLabel;
    }
  }
  view.querySelectorAll("[data-run-extractor]").forEach((btn) =>
    btn.addEventListener("click", () => runManual([btn.dataset.runExtractor], btn)));
  view.querySelectorAll("[data-run-all]").forEach((btn) =>
    btn.addEventListener("click", () => runManual(MANUAL_TOOLS.slice(), btn)));

  view.querySelectorAll("[data-tab]").forEach((btn) =>
    btn.addEventListener("click", () => showTab(btn.dataset.tab)));
  view.querySelectorAll("[data-toggle-attempt]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const detail = $(btn.dataset.toggleAttempt);
      detail.hidden = !detail.hidden;
      btn.textContent = detail.hidden ? "Details" : "Hide";
    }));
  showTab(activeTab);
  drawExtractorMeta();
  if (params.section === "quality") $("qualitySection").scrollIntoView();
  if (params.section === "attempts") $("attemptsSection").scrollIntoView();
  if (params.section === "tokens") $("tokenSection").scrollIntoView();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || "");
    toast("Copied to clipboard.", "success");
  } catch {
    toast("Copy failed — your browser blocked clipboard access.", "error");
  }
}

/* ------------------------------------------------------------------ */
/* Settings page                                                      */
/* ------------------------------------------------------------------ */

async function renderSettings() {
  const view = $("view-settings");
  view.innerHTML = '<div class="loading">Loading settings…</div>';
  try {
    const data = await Api.getSystemPrompt();
    state.settings = data;
    const ai = data.ai || {};
    view.innerHTML = `
      <h2>Settings</h2>
      <p class="page-subtitle">LLM configuration. The provider and model are configured on the
        backend (environment); the API key is never shown here.</p>
      <div class="card">
        <h3>LLM Provider</h3>
        <table class="kv-table">
          <tr><td>Provider</td><td>${escapeHtml(ai.provider || "N/A")}</td></tr>
          <tr><td>Model</td><td>${escapeHtml(ai.model || "N/A")}</td></tr>
          <tr><td>Base URL</td><td>${escapeHtml(ai.base_url || "N/A")}</td></tr>
          <tr><td>Vision enabled</td><td>${ai.vision_enabled === undefined ? "N/A" : ai.vision_enabled ? "Yes" : "No"}</td></tr>
          <tr><td>Status</td><td>${ai.configured ? '<span class="chip chip-ok">Configured</span>' : '<span class="chip chip-bad">Not configured</span>'}</td></tr>
        </table>
        ${ai.error ? `<div class="error-box">${escapeHtml(ai.error)}</div>` : ""}
      </div>
      <div class="card">
        <h3>System Prompt</h3>
        <div class="inline-row mb">
          ${data.is_custom
            ? '<span class="chip chip-info">Custom prompt</span>'
            : '<span class="chip">Default prompt</span>'}
          ${data.updated_at ? `<span class="hint">Last saved ${escapeHtml(fmtDate(data.updated_at))}</span>` : ""}
          <span class="hint" id="promptChars"></span>
        </div>
        <div class="field">
          <textarea id="promptEditor" class="textarea-input" rows="16"
                    maxlength="${data.max_chars || 50000}">${escapeHtml(data.system_prompt || "")}</textarea>
        </div>
        <p class="hint">This prompt is sent to the model as its instructions for resume
          extraction. Saving applies it to new processing requests; already-processed
          resumes are unchanged.</p>
        <div class="inline-row mt">
          <button class="btn btn-primary" id="promptSave" type="button">Save Configuration</button>
          <button class="btn" id="promptTest" type="button">Test Prompt</button>
          <button class="btn" id="promptReset" type="button">Reset to Default</button>
        </div>
        <div id="promptTestResult" class="mt"></div>
      </div>`;

    const editor = $("promptEditor");
    const chars = $("promptChars");
    const updateChars = () => {
      chars.textContent = editor.value.length.toLocaleString() + " / " +
        Number(data.max_chars || 50000).toLocaleString() + " characters";
    };
    editor.addEventListener("input", updateChars);
    updateChars();

    $("promptSave").addEventListener("click", async () => {
      try {
        const saved = await Api.saveSystemPrompt(editor.value);
        state.settings = saved;
        toast("System prompt saved.", "success");
        renderSettings();
      } catch (err) { toast(err.message, "error"); }
    });
    $("promptReset").addEventListener("click", async () => {
      if (!confirm("Reset the system prompt to the built-in default?")) return;
      try {
        await Api.resetSystemPrompt();
        toast("System prompt reset to default.", "success");
        renderSettings();
      } catch (err) { toast(err.message, "error"); }
    });
    $("promptTest").addEventListener("click", async () => {
      const box = $("promptTestResult");
      box.innerHTML = '<span class="hint">Running one real request with this prompt…</span>';
      try {
        const result = await Api.testSystemPrompt(editor.value);
        if (!result.configured) {
          box.innerHTML = `<div class="error-box">The LLM is not configured on the backend, so the prompt cannot be tested. ${escapeHtml(result.error || "")}</div>`;
        } else if (result.ok) {
          const usage = result.usage || {};
          box.innerHTML = `<div class="hint" style="color:var(--ok)">Test passed — the model returned valid resume JSON.
            Model: ${escapeHtml(result.model || "")} · Duration: ${fmtMs(result.duration_ms)} ·
            Tokens: ${usage.total_tokens != null ? fmtNum(usage.total_tokens) : "N/A"}</div>`;
        } else {
          box.innerHTML = `<div class="error-box">Test failed: ${escapeHtml(result.error || "The model did not return valid JSON.")}</div>`;
        }
      } catch (err) {
        box.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
      }
    });
  } catch (err) {
    view.innerHTML = `<h2>Settings</h2><div class="error-box">${escapeHtml(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ */
/* Analytics (Phase 1) — aggregates from GET /api/analytics/summary.  */
/* Hand-rolled inline SVG charts; no external libraries.              */
/* ------------------------------------------------------------------ */

const CHART_COLORS = ["#1d4ed8", "#15803d", "#b45309", "#b91c1c", "#7c3aed", "#0891b2", "#64748b", "#db2777"];

function fmtPct(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(1) + "%";
}

function fmtScore(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(1);
}

function hBarChart(items, color) {
  // items: [{label, value, display}] — horizontal bars scaled to the max.
  const max = Math.max(1, ...items.map((i) => i.value || 0));
  const rowH = 22, labelW = 130, barW = 300, valW = 70;
  const height = Math.max(1, items.length) * rowH + 4;
  const rows = items.map((item, idx) => {
    const y = idx * rowH + 4;
    const w = Math.max(0, (item.value || 0) / max * barW);
    return `<text x="0" y="${y + 13}" font-size="12" fill="var(--muted)">${escapeHtml(item.label)}</text>
      <rect x="${labelW}" y="${y + 3}" width="${w.toFixed(1)}" height="12" rx="2" fill="${color || CHART_COLORS[0]}"></rect>
      <text x="${labelW + barW + 8}" y="${y + 13}" font-size="12" fill="var(--text)">${escapeHtml(item.display)}</text>`;
  }).join("");
  return `<svg class="chart-box" viewBox="0 0 ${labelW + barW + valW} ${height}" role="img">${rows}</svg>`;
}

function vBarChart(items, color) {
  // items: [{label, value, display}] — vertical bars (histogram).
  const max = Math.max(1, ...items.map((i) => i.value || 0));
  const chartH = 110, barW = 54, gap = 18, base = chartH + 4;
  const width = items.length * (barW + gap) + gap;
  const bars = items.map((item, idx) => {
    const h = Math.max(0, (item.value || 0) / max * chartH);
    const x = gap + idx * (barW + gap);
    return `<rect x="${x}" y="${(base - h).toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" rx="2" fill="${color || CHART_COLORS[0]}"></rect>
      <text x="${x + barW / 2}" y="${(base - h - 5).toFixed(1)}" font-size="12" text-anchor="middle" fill="var(--text)">${escapeHtml(item.display)}</text>
      <text x="${x + barW / 2}" y="${base + 16}" font-size="11" text-anchor="middle" fill="var(--muted)">${escapeHtml(item.label)}</text>`;
  }).join("");
  return `<svg class="chart-box" viewBox="0 0 ${width} ${base + 22}" role="img">${bars}</svg>`;
}

function donutChart(parts) {
  // parts: [{label, value, color}] — ring segments; legend rendered by caller.
  const total = parts.reduce((sum, p) => sum + (p.value || 0), 0);
  const r = 52, c = 2 * Math.PI * r;
  if (!total) {
    return `<svg class="chart-box" viewBox="0 0 140 140" role="img"><circle cx="70" cy="70" r="${r}" fill="none" stroke="var(--border)" stroke-width="16"></circle><text x="70" y="74" font-size="12" text-anchor="middle" fill="var(--muted)">No data</text></svg>`;
  }
  let offset = 0;
  const segments = parts.map((p) => {
    const frac = (p.value || 0) / total;
    const seg = `<circle cx="70" cy="70" r="${r}" fill="none" stroke="${p.color}" stroke-width="16"
      stroke-dasharray="${(frac * c).toFixed(1)} ${c.toFixed(1)}" stroke-dashoffset="${(-offset * c).toFixed(1)}"
      transform="rotate(-90 70 70)"></circle>`;
    offset += frac;
    return seg;
  }).join("");
  return `<svg class="chart-box" viewBox="0 0 140 140" role="img">${segments}<text x="70" y="74" font-size="15" font-weight="700" text-anchor="middle" fill="var(--text)">${fmtNum(total)}</text></svg>`;
}

function chartLegend(parts) {
  return `<div class="chart-legend">${parts.map((p) =>
    `<span><span class="swatch" style="background:${p.color}"></span>${escapeHtml(p.label)}: ${escapeHtml(p.display)}</span>`).join("")}</div>`;
}

function lineChart(points) {
  // points: [{label, value}] — tokens per day.
  const width = 640, height = 150, padL = 8, padR = 8, padT = 14, padB = 22;
  if (!points.length) return `<p class="hint">No dated records in this range.</p>`;
  const max = Math.max(1, ...points.map((p) => p.value || 0));
  const stepX = points.length > 1 ? (width - padL - padR) / (points.length - 1) : 0;
  const coords = points.map((p, idx) => {
    const x = padL + idx * stepX;
    const y = padT + (1 - (p.value || 0) / max) * (height - padT - padB);
    return [x, y];
  });
  const poly = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const dots = coords.map(([x, y]) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5" fill="${CHART_COLORS[0]}"></circle>`).join("");
  const first = points[0], last = points[points.length - 1];
  return `<svg class="chart-box" viewBox="0 0 ${width} ${height}" role="img">
    <polyline points="${poly}" fill="none" stroke="${CHART_COLORS[0]}" stroke-width="2"></polyline>${dots}
    <text x="${padL}" y="${height - 6}" font-size="11" fill="var(--muted)">${escapeHtml(first.label)}</text>
    <text x="${width - padR}" y="${height - 6}" font-size="11" text-anchor="end" fill="var(--muted)">${escapeHtml(last.label)}</text>
    <text x="${padL}" y="11" font-size="11" fill="var(--muted)">peak ${fmtNum(max)} / day</text></svg>`;
}

function renderAnalytics() {
  const view = $("view-analytics");
  const days = state.analytics.days;
  view.innerHTML = `
    <h2>Analytics</h2>
    <p class="page-subtitle">Pipeline-wide insights from stored records — extractors, LLM tokens, and quality.</p>
    <div class="analytics-toolbar">
      <label for="analyticsRange">Range</label>
      <select class="select-input" id="analyticsRange" aria-label="Analytics range">
        <option value="" ${days === "" ? "selected" : ""}>All time</option>
        <option value="7" ${days === "7" ? "selected" : ""}>Last 7 days</option>
        <option value="30" ${days === "30" ? "selected" : ""}>Last 30 days</option>
        <option value="90" ${days === "90" ? "selected" : ""}>Last 90 days</option>
      </select>
      <span class="hint" id="analyticsMeta"></span>
    </div>
    <div id="analyticsBody"><div class="loading">Computing analytics…</div></div>`;
  $("analyticsRange").addEventListener("change", (e) => {
    state.analytics.days = e.target.value;
    loadAnalytics();
  });
  loadAnalytics();
}

async function loadAnalytics() {
  const body = $("analyticsBody");
  if (!body) return;
  body.innerHTML = '<div class="loading">Computing analytics…</div>';
  try {
    const params = state.analytics.days ? { days: Number(state.analytics.days) } : {};
    const data = await Api.getAnalytics(params);
    const meta = $("analyticsMeta");
    if (meta) {
      const cov = data.meta.token_usage_coverage || {};
      meta.textContent = `Based on ${fmtNum(data.meta.records_in_range)} of ${fmtNum(data.meta.records_total)} records` +
        (cov.calls_total > cov.calls_with_usage
          ? ` · Token totals cover ${fmtNum(cov.calls_with_usage)} of ${fmtNum(cov.calls_total)} LLM calls — the rest returned no usage`
          : "");
    }
    body.innerHTML = analyticsHtml(data);
  } catch (err) {
    body.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
  }
}

function analyticsHtml(data) {
  if (!data.meta.records_in_range) {
    return `<div class="card"><p>No records in this range — process a resume or widen the range.</p></div>`;
  }
  const k = data.kpis, t = data.tokens, q = data.quality, n = data.normalization;

  const kpiCards = [
    { value: fmtNum(k.total_processed), label: "Resumes processed" },
    { value: fmtPct(k.success_rate_percent), label: "Extraction success rate" },
    { value: fmtScore(k.avg_quality_score), label: "Avg quality score" },
    { value: k.avg_duration_ms === null ? "—" : fmtMs(k.avg_duration_ms), label: "Avg processing time" },
    { value: fmtNum(k.total_tokens), label: "Total LLM tokens" },
    { value: fmtPct(k.fallback_rate_percent), label: "Fallback rate",
      sub: k.fallback_base ? `of ${fmtNum(k.fallback_base)} records with fallback data` : "" },
  ].map((c) => `<div class="kpi-card"><div class="kpi-value">${c.value}</div><div class="kpi-label">${c.label}</div>${c.sub ? `<div class="kpi-sub">${escapeHtml(c.sub)}</div>` : ""}</div>`).join("");

  const extractorRows = data.extractors.map((row) => `<tr>
      <td>${escapeHtml(engineLabel(row.name))}</td><td>${fmtNum(row.attempts)}</td>
      <td>${fmtNum(row.succeeded)}</td><td>${fmtNum(row.failed)}</td><td>${fmtNum(row.selected)}</td>
      <td>${fmtPct(row.win_rate_percent)}</td><td>${fmtScore(row.avg_quality)}</td>
      <td>${row.avg_duration_ms === null ? "—" : fmtMs(row.avg_duration_ms)}</td>
      <td>${row.avg_chars === null ? "—" : fmtNum(row.avg_chars)}</td>
      <td>${fmtNum(row.wasted_attempts)} · ${fmtMs(row.wasted_ms)}</td></tr>`).join("");
  const winBars = hBarChart(data.extractors.map((row) => ({
    label: engineLabel(row.name), value: row.win_rate_percent || 0,
    display: fmtPct(row.win_rate_percent),
  })));
  const shareParts = data.extractors.filter((row) => row.selected > 0).map((row, idx) => ({
    label: engineLabel(row.name), value: row.selected, display: fmtNum(row.selected),
    color: CHART_COLORS[idx % CHART_COLORS.length],
  }));
  const extractorCard = data.extractors.length ? `
    <div class="card">
      <h3>Extractor performance</h3>
      <div style="overflow-x:auto"><table class="data-table">
        <thead><tr><th>Tool</th><th>Attempts</th><th>OK</th><th>Failed</th><th>Selected</th><th>Win %</th><th>Avg quality</th><th>Avg time</th><th>Avg chars</th><th>Wasted (n · time)</th></tr></thead>
        <tbody>${extractorRows}</tbody></table></div>
      <div class="analytics-split" style="margin-top:14px">
        <div><h4>Win rate %</h4>${winBars}</div>
        <div><h4>Selected share</h4>${donutChart(shareParts)}${chartLegend(shareParts)}</div>
      </div>
    </div>` : "";

  const tokenCard = `
    <div class="card">
      <h3>LLM &amp; tokens</h3>
      <table class="kv-table">
        <tr><td>Input tokens</td><td>${fmtNum(t.input)}</td></tr>
        <tr><td>Output tokens</td><td>${fmtNum(t.output)}</td></tr>
        <tr><td>Cached input tokens</td><td>${fmtNum(t.cached_input)}</td></tr>
        <tr><td>Total tokens</td><td><strong>${fmtNum(t.total)}</strong></td></tr>
        <tr><td>API calls</td><td>${fmtNum(t.api_calls)} — ${fmtNum(t.successful_calls)} ok · ${fmtNum(t.failed_calls)} failed · ${fmtNum(t.timed_out_calls)} timed out</td></tr>
        <tr><td>Avg tokens / resume</td><td>${t.avg_per_resume === null ? "—" : fmtNum(Math.round(t.avg_per_resume))}</td></tr>
        <tr><td>p95 tokens / resume</td><td>${t.p95_per_resume === null ? "—" : fmtNum(Math.round(t.p95_per_resume))}</td></tr>
        <tr><td>Tokens / 1,000 extracted chars</td><td>${t.tokens_per_1000_chars === null ? "—" : fmtNum(Math.round(t.tokens_per_1000_chars))}</td></tr>
      </table>
      <h4 style="margin-top:14px">Tokens per day</h4>
      ${lineChart((t.by_day || []).map((d) => ({ label: d.date, value: d.tokens })))}
      ${t.normalization_tokens_saved ? `<p class="hint">Normalization saved ≈ ${fmtNum(t.normalization_tokens_saved)} estimated tokens of LLM input across these records.</p>` : ""}
    </div>`;

  const statusParts = [
    { label: "PASS", value: q.pass, display: fmtNum(q.pass), color: "#15803d" },
    { label: "WARNING", value: q.warning, display: fmtNum(q.warning), color: "#b45309" },
    { label: "FAIL", value: q.fail, display: fmtNum(q.fail), color: "#b91c1c" },
  ];
  const filterBars = hBarChart([
    { label: "Text Health", value: q.filters_avg.text_health || 0, display: fmtScore(q.filters_avg.text_health) },
    { label: "Completeness", value: q.filters_avg.completeness || 0, display: fmtScore(q.filters_avg.completeness) },
    { label: "Reading Order", value: q.filters_avg.reading_order || 0, display: fmtScore(q.filters_avg.reading_order) },
  ], "#15803d");
  const hist = vBarChart((q.histogram || []).map((b) => ({
    label: b.bucket, value: b.count, display: fmtNum(b.count),
  })), "#1d4ed8");
  const missedRows = (q.missed_sections || []).map((m) => `<tr>
      <td>${escapeHtml(m.section)}</td><td>${fmtNum(m.missing)}</td><td>${fmtNum(m.of)}</td><td>${fmtPct(m.percent)}</td></tr>`).join("");
  const flagsList = (q.top_flags || []).length
    ? `<table class="kv-table">${q.top_flags.map((f) => `<tr><td>${escapeHtml(f.flag)}</td><td>${fmtNum(f.count)}</td></tr>`).join("")}</table>`
    : `<p class="hint">No quality flags recorded.</p>`;
  const qualityCard = `
    <div class="card">
      <h3>Quality</h3>
      <div class="analytics-split">
        <div><h4>Final quality status</h4>${donutChart(statusParts)}${chartLegend(statusParts)}
          <h4 style="margin-top:14px">Average filter scores</h4>${filterBars}</div>
        <div><h4>Score distribution</h4>${hist}</div>
      </div>
      <h4 style="margin-top:14px">Most-missed sections</h4>
      <div style="overflow-x:auto"><table class="data-table">
        <thead><tr><th>Section</th><th>Missing in</th><th>Of</th><th>%</th></tr></thead>
        <tbody>${missedRows}</tbody></table></div>
      <h4 style="margin-top:14px">Top quality flags</h4>${flagsList}
    </div>`;

  const normCard = `
    <div class="card">
      <h3>Normalization</h3>
      <table class="kv-table">
        <tr><td>Records normalized</td><td>${fmtNum(n.applied)}</td></tr>
        <tr><td>Avg text reduction</td><td>${fmtPct(n.avg_reduction_percent)}</td></tr>
        <tr><td>Total characters removed</td><td>${fmtNum(n.total_chars_removed)}</td></tr>
        <tr><td>Estimated tokens saved</td><td>${fmtNum(n.estimated_tokens_saved)}</td></tr>
      </table>
    </div>`;

  return `<div class="kpi-grid">${kpiCards}</div>${extractorCard}${tokenCard}${qualityCard}${normCard}`;
}

/* ------------------------------------------------------------------ */
/* Router + boot                                                      */
/* ------------------------------------------------------------------ */

const VIEWS = ["process", "history", "resume", "analytics", "settings"];

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const params = Object.fromEntries(new URLSearchParams(queryPart || ""));
  const segments = pathPart.split("/").filter(Boolean);
  if (segments[0] === "resume" && segments[1]) {
    return { view: "resume", id: decodeURIComponent(segments[1]), params };
  }
  if (["history", "settings", "process", "analytics"].includes(segments[0])) {
    return { view: segments[0], params };
  }
  return { view: "process", params };
}

function route() {
  const { view, id, params } = parseHash();
  VIEWS.forEach((name) => { $("view-" + name).hidden = name !== view; });
  document.querySelectorAll("[data-nav]").forEach((link) => {
    link.classList.toggle("active",
      link.dataset.nav === view || (view === "resume" && link.dataset.nav === "history"));
  });
  if (view === "process" && !$("view-process").dataset.rendered) {
    $("view-process").dataset.rendered = "1";
    renderProcess();
  }
  if (view === "history") renderHistory();
  if (view === "analytics") renderAnalytics();
  if (view === "settings") renderSettings();
  if (view === "resume") renderResume(id, params);
  window.scrollTo(0, 0);
}

async function boot() {
  try {
    state.health = await Api.health();
    const dot = $("backendStatus");
    dot.classList.add("ok");
    $("backendStatusText").textContent = "Backend connected";
    const order = state.health.extractor_order || [];
    $("engineInfo").textContent = order.length
      ? "Primary extraction engine: " + engineLabel(order[0]) +
        " · Pipeline: " + order.map(engineLabel).join(" → ")
      : "Primary extraction engine: N/A";
    const ai = state.health.ai || {};
    $("aiInfo").textContent = "LLM: " + (ai.provider || "N/A") +
      (ai.model ? " · " + ai.model : "") +
      (ai.configured ? " · Configured" : " · Not configured");
  } catch (err) {
    const dot = $("backendStatus");
    dot.classList.add("error");
    $("backendStatusText").textContent = "Backend unavailable";
  }
  if (!location.hash) location.hash = "#/process";
  route();
}

window.addEventListener("hashchange", route);
boot();
