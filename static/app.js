const el = (id) => document.getElementById(id);

const folderInput = el("folderInput");
const recursiveCheck = el("recursiveCheck");
const scanBtn = el("scanBtn");
const scanStatus = el("scanStatus");
const counterValue = el("counterValue");

const actionPanel = el("actionPanel");
const matchBtn = el("matchBtn");
const progressWrap = el("progressWrap");
const progressFill = el("progressFill");
const progressLabel = el("progressLabel");
const applyRow = el("applyRow");
const thresholdInput = el("thresholdInput");
const thresholdValue = el("thresholdValue");
const applyTagCheck = el("applyTagCheck");
const applyRenameCheck = el("applyRenameCheck");
const applyAllBtn = el("applyAllBtn");
const exportBtn = el("exportBtn");

const tablePanel = el("tablePanel");
const trackTableBody = el("trackTableBody");
const toast = el("toast");

let TRACKS = [];
let progressTimer = null;

function showToast(msg, isError = false) {
  toast.textContent = msg;
  toast.className = "toast show" + (isError ? " error" : "");
  setTimeout(() => { toast.className = "toast"; }, 3200);
}

function confidenceClass(score) {
  if (score >= 80) return "high";
  if (score >= 55) return "mid";
  return "low";
}

function vuMeterHtml(score) {
  const segs = 10;
  const filled = Math.round((score / 100) * segs);
  const cls = confidenceClass(score);
  let bars = "";
  for (let i = 0; i < segs; i++) {
    bars += `<div class="vu-seg ${i < filled ? "filled " + cls : ""}"></div>`;
  }
  return `<div class="vu"><div class="vu-track">${bars}</div><span class="vu-value">${score.toFixed(0)}%</span></div>`;
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function tagLineHtml(track) {
  const artist = track.tag_artist ? `<strong>${escapeHtml(track.tag_artist)}</strong>` : `<span class="missing">no artist tag</span>`;
  const title = track.tag_title ? `<strong>${escapeHtml(track.tag_title)}</strong>` : `<span class="missing">no title tag</span>`;
  const album = track.tag_album ? escapeHtml(track.tag_album) : `<span class="missing">no album tag</span>`;
  return `<div class="tag-line">${artist} &mdash; ${title}<br>${album}</div>`;
}

function matchHtml(track) {
  if (!track.match) {
    return `<span class="no-match">not searched yet</span>`;
  }
  const m = track.match;
  return `
    <div class="match-artist">${escapeHtml(m.artist || "Unknown artist")}</div>
    <div class="match-title">${escapeHtml(m.title || "Unknown title")}</div>
    <div class="match-album">${escapeHtml(m.album || "")}${m.date ? " · " + escapeHtml(m.date) : ""}</div>
  `;
}

function renderTable() {
  trackTableBody.innerHTML = TRACKS.map((track, idx) => {
    const conf = track.match ? track.match.confidence : null;
    return `
      <tr data-id="${escapeHtml(track.id)}">
        <td class="col-file">
          <span class="file-name">${escapeHtml(track.filename)}</span>
          <span class="file-path">${escapeHtml(track.path)}</span>
        </td>
        <td class="col-current">${tagLineHtml(track)}</td>
        <td class="col-match">${matchHtml(track)}</td>
        <td class="col-confidence">${conf !== null ? vuMeterHtml(conf) : `<span class="no-match">&mdash;</span>`}</td>
        <td class="col-actions">
          <div class="row-actions">
            <button class="btn btn-ghost apply-row-btn" data-idx="${idx}" ${track.match ? "" : "disabled"}>Apply</button>
          </div>
          <div class="row-status" data-status-for="${escapeHtml(track.id)}"></div>
        </td>
      </tr>
    `;
  }).join("");

  document.querySelectorAll(".apply-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => applySingle(parseInt(btn.dataset.idx, 10)));
  });

  counterValue.textContent = String(TRACKS.length).padStart(3, "0");
}

async function applySingle(idx) {
  const track = TRACKS[idx];
  const statusEl = document.querySelector(`[data-status-for="${CSS.escape(track.id)}"]`);
  try {
    const resp = await fetch("/api/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: track.id,
        tag: applyTagCheck.checked,
        rename: applyRenameCheck.checked,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Failed to apply.");
    if (statusEl) {
      statusEl.textContent = data.renamed ? `Renamed → ${data.new_path.split("/").pop()}` : "Tags updated";
      statusEl.className = "row-status";
    }
    track.path = data.new_path;
    track.id = data.new_path;
    showToast("Applied match to " + track.filename);
  } catch (e) {
    if (statusEl) { statusEl.textContent = e.message; statusEl.className = "row-status error"; }
    showToast(e.message, true);
  }
}

scanBtn.addEventListener("click", async () => {
  const folder = folderInput.value.trim();
  if (!folder) { showToast("Enter a folder path first.", true); return; }

  scanBtn.disabled = true;
  scanStatus.textContent = "Scanning...";
  try {
    const resp = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, recursive: recursiveCheck.checked }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Scan failed.");

    TRACKS = data.tracks;
    renderTable();
    scanStatus.textContent = `Found ${data.count} audio file${data.count === 1 ? "" : "s"} in ${data.folder}.`;
    actionPanel.hidden = false;
    tablePanel.hidden = TRACKS.length === 0;
  } catch (e) {
    scanStatus.textContent = e.message;
    showToast(e.message, true);
  } finally {
    scanBtn.disabled = false;
  }
});

matchBtn.addEventListener("click", async () => {
  matchBtn.disabled = true;
  progressWrap.hidden = false;
  try {
    const resp = await fetch("/api/match/start", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Could not start matching.");
    pollProgress();
  } catch (e) {
    showToast(e.message, true);
    matchBtn.disabled = false;
  }
});

function pollProgress() {
  clearInterval(progressTimer);
  progressTimer = setInterval(async () => {
    const resp = await fetch("/api/match/progress");
    const p = await resp.json();
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    progressFill.style.width = pct + "%";
    progressLabel.textContent = `${p.done} / ${p.total}`;

    if (!p.running && p.total > 0 && p.done >= p.total) {
      clearInterval(progressTimer);
      matchBtn.disabled = false;
      applyRow.hidden = false;
      await refreshTracks();
      showToast("Matching complete.");
    }
  }, 600);
}

async function refreshTracks() {
  const resp = await fetch("/api/tracks");
  const data = await resp.json();
  TRACKS = data.tracks;
  renderTable();
}

thresholdInput.addEventListener("input", () => {
  thresholdValue.textContent = thresholdInput.value + "%";
});

applyAllBtn.addEventListener("click", async () => {
  applyAllBtn.disabled = true;
  try {
    const resp = await fetch("/api/apply_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag: applyTagCheck.checked,
        rename: applyRenameCheck.checked,
        min_confidence: parseFloat(thresholdInput.value),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Bulk apply failed.");
    showToast(`Applied to ${data.applied} file(s) at or above ${thresholdInput.value}% confidence.`);
    await refreshTracks();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    applyAllBtn.disabled = false;
  }
});

exportBtn.addEventListener("click", () => {
  window.location.href = "/api/export_csv";
});
