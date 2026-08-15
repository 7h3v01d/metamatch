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
const applyArtCheck = el("applyArtCheck");
const applyAllBtn = el("applyAllBtn");
const undoAllBtn = el("undoAllBtn");
const exportBtn = el("exportBtn");

const dedupPanel = el("dedupPanel");
const scanDupesBtn = el("scanDupesBtn");
const dedupResults = el("dedupResults");

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
  const art = m.release_id
    ? `<img class="match-thumb" src="/api/art/${encodeURIComponent(m.release_id)}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="match-thumb match-thumb-empty"></div>`;
  return `
    <div class="match-row">
      ${art}
      <div class="match-text">
        <div class="match-artist">${escapeHtml(m.artist || "Unknown artist")}</div>
        <div class="match-title">${escapeHtml(m.title || "Unknown title")}</div>
        <div class="match-album">${escapeHtml(m.album || "")}${m.date ? " · " + escapeHtml(m.date) : ""}</div>
      </div>
    </div>
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
            <button class="btn btn-ghost undo-row-btn" data-idx="${idx}" ${track.can_undo ? "" : "disabled"}>Undo</button>
          </div>
          <div class="row-status" data-status-for="${escapeHtml(track.id)}"></div>
        </td>
      </tr>
    `;
  }).join("");

  document.querySelectorAll(".apply-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => applySingle(parseInt(btn.dataset.idx, 10)));
  });
  document.querySelectorAll(".undo-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => undoSingle(parseInt(btn.dataset.idx, 10)));
  });

  counterValue.textContent = String(TRACKS.length).padStart(3, "0");
}

async function applySingle(idx) {
  const track = TRACKS[idx];
  try {
    const resp = await fetch("/api/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: track.id,
        tag: applyTagCheck.checked,
        rename: applyRenameCheck.checked,
        art: applyArtCheck.checked,
      }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "Failed to apply.");

    track.path = data.new_path;
    track.id = data.new_path;
    track.filename = data.new_path.split("/").pop();
    track.can_undo = true;
    showToast(data.renamed ? `Renamed → ${track.filename}` : "Tags updated");
    await refreshTracks();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function undoSingle(idx) {
  const track = TRACKS[idx];
  try {
    const resp = await fetch("/api/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: track.id }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "Failed to undo.");
    showToast("Reverted " + (data.restored_path || "").split("/").pop());
    await refreshTracks();
  } catch (e) {
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
    dedupPanel.hidden = false;
    dedupResults.innerHTML = "";
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
        art: applyArtCheck.checked,
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

undoAllBtn.addEventListener("click", async () => {
  undoAllBtn.disabled = true;
  try {
    const resp = await fetch("/api/undo_all", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Undo all failed.");
    showToast(`Reverted ${data.restored} file(s).`);
    await refreshTracks();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    undoAllBtn.disabled = false;
  }
});

exportBtn.addEventListener("click", () => {
  window.location.href = "/api/export_csv";
});

/* ------------------------------- Tabs -------------------------------- */

const tabMusicBtn = el("tabMusicBtn");
const tabMoviesBtn = el("tabMoviesBtn");
const musicView = el("musicView");
const moviesView = el("moviesView");
const counterLabel = el("counterLabel");

function switchTab(view) {
  const isMusic = view === "music";
  musicView.hidden = !isMusic;
  moviesView.hidden = isMusic;
  tabMusicBtn.classList.toggle("active", isMusic);
  tabMoviesBtn.classList.toggle("active", !isMusic);
  counterLabel.textContent = isMusic ? "TRACKS" : "MOVIES";
  counterValue.textContent = String(isMusic ? TRACKS.length : MOVIES.length).padStart(3, "0");
  if (!isMusic) checkTmdbSettings();
}

tabMusicBtn.addEventListener("click", () => switchTab("music"));
tabMoviesBtn.addEventListener("click", () => switchTab("movies"));

/* ------------------------------ Movies -------------------------------- */

const tmdbSettingsPanel = el("tmdbSettingsPanel");
const tmdbKeyInput = el("tmdbKeyInput");
const saveTmdbKeyBtn = el("saveTmdbKeyBtn");
const tmdbStatus = el("tmdbStatus");

const movieFolderInput = el("movieFolderInput");
const movieRecursiveCheck = el("movieRecursiveCheck");
const movieScanBtn = el("movieScanBtn");
const movieScanStatus = el("movieScanStatus");

const movieActionPanel = el("movieActionPanel");
const movieMatchBtn = el("movieMatchBtn");
const movieProgressWrap = el("movieProgressWrap");
const movieProgressFill = el("movieProgressFill");
const movieProgressLabel = el("movieProgressLabel");
const movieApplyRow = el("movieApplyRow");
const movieThresholdInput = el("movieThresholdInput");
const movieThresholdValue = el("movieThresholdValue");
const movieRenameCheck = el("movieRenameCheck");
const movieNfoCheck = el("movieNfoCheck");
const moviePosterCheck = el("moviePosterCheck");
const movieTagCheck = el("movieTagCheck");
const movieApplyAllBtn = el("movieApplyAllBtn");
const movieUndoAllBtn = el("movieUndoAllBtn");
const movieExportBtn = el("movieExportBtn");

const movieDedupPanel = el("movieDedupPanel");
const movieScanDupesBtn = el("movieScanDupesBtn");
const movieDedupResults = el("movieDedupResults");

const movieTablePanel = el("movieTablePanel");
const movieTableBody = el("movieTableBody");

let MOVIES = [];
let movieProgressTimer = null;
let tmdbConfigured = false;

async function checkTmdbSettings() {
  try {
    const resp = await fetch("/api/settings/tmdb");
    const data = await resp.json();
    tmdbConfigured = data.configured;
    tmdbSettingsPanel.hidden = data.configured;
    if (data.configured) {
      tmdbStatus.textContent = `Using key ${data.masked_key}`;
    }
    if (!data.ffprobe_available) {
      movieScanStatus.textContent = "Warning: ffprobe wasn't found on this machine — duration and embedded tags won't be read, but filename-based matching will still work.";
    }
  } catch (e) {
    // settings check failing shouldn't block the rest of the UI
  }
}

saveTmdbKeyBtn.addEventListener("click", async () => {
  const key = tmdbKeyInput.value.trim();
  if (!key) { showToast("Enter a TMDB API key.", true); return; }
  saveTmdbKeyBtn.disabled = true;
  try {
    const resp = await fetch("/api/settings/tmdb", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Couldn't save key.");
    tmdbConfigured = true;
    tmdbSettingsPanel.hidden = true;
    tmdbKeyInput.value = "";
    showToast("TMDB key saved.");
  } catch (e) {
    showToast(e.message, true);
  } finally {
    saveTmdbKeyBtn.disabled = false;
  }
});

function movieConfidenceClass(score) {
  if (score >= 80) return "high";
  if (score >= 55) return "mid";
  return "low";
}

function movieVuMeterHtml(score) {
  const segs = 10;
  const filled = Math.round((score / 100) * segs);
  const cls = movieConfidenceClass(score);
  let bars = "";
  for (let i = 0; i < segs; i++) {
    bars += `<div class="vu-seg ${i < filled ? "filled " + cls : ""}"></div>`;
  }
  return `<div class="vu"><div class="vu-track">${bars}</div><span class="vu-value">${score.toFixed(0)}%</span></div>`;
}

function moviePresetHtml(video) {
  const title = video.tag_title || video.guess_title;
  const year = video.tag_year || video.guess_year;
  if (!title) return `<span class="missing">couldn't parse a title</span>`;
  return `<div class="tag-line"><strong>${escapeHtml(title)}</strong>${year ? " (" + escapeHtml(year) + ")" : ""}</div>`;
}

function movieMatchHtml(video) {
  if (!video.match) return `<span class="no-match">not searched yet</span>`;
  const m = video.match;
  const poster = m.poster_url
    ? `<img class="match-thumb poster-thumb" src="${m.poster_url}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="match-thumb match-thumb-empty"></div>`;
  return `
    <div class="match-row">
      ${poster}
      <div class="match-text">
        <div class="match-artist">${escapeHtml(m.title || "Unknown title")}</div>
        <div class="match-title">${m.year ? escapeHtml(m.year) : ""}${m.vote_average ? " · ★ " + m.vote_average.toFixed(1) : ""}</div>
      </div>
    </div>
  `;
}

function renderMovieTable() {
  movieTableBody.innerHTML = MOVIES.map((video, idx) => {
    const conf = video.match ? video.match.confidence : null;
    return `
      <tr data-id="${escapeHtml(video.id)}">
        <td class="col-file">
          <span class="file-name">${escapeHtml(video.filename)}</span>
          <span class="file-path">${escapeHtml(video.path)}</span>
        </td>
        <td class="col-current">${moviePresetHtml(video)}</td>
        <td class="col-match">${movieMatchHtml(video)}</td>
        <td class="col-confidence">${conf !== null ? movieVuMeterHtml(conf) : `<span class="no-match">&mdash;</span>`}</td>
        <td class="col-actions">
          <div class="row-actions">
            <button class="btn btn-ghost movie-apply-row-btn" data-idx="${idx}" ${video.match ? "" : "disabled"}>Apply</button>
            <button class="btn btn-ghost movie-undo-row-btn" data-idx="${idx}" ${video.can_undo ? "" : "disabled"}>Undo</button>
          </div>
          <div class="row-status" data-movie-status-for="${escapeHtml(video.id)}"></div>
        </td>
      </tr>
    `;
  }).join("");

  document.querySelectorAll(".movie-apply-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyMovieSingle(parseInt(btn.dataset.idx, 10)));
  });
  document.querySelectorAll(".movie-undo-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => undoMovieSingle(parseInt(btn.dataset.idx, 10)));
  });

  counterValue.textContent = String(MOVIES.length).padStart(3, "0");
}

movieScanBtn.addEventListener("click", async () => {
  const folder = movieFolderInput.value.trim();
  if (!folder) { showToast("Enter a folder path first.", true); return; }

  movieScanBtn.disabled = true;
  movieScanStatus.textContent = "Scanning...";
  try {
    const resp = await fetch("/api/movies/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, recursive: movieRecursiveCheck.checked }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Scan failed.");

    MOVIES = data.videos;
    renderMovieTable();
    movieScanStatus.textContent = `Found ${data.count} video file${data.count === 1 ? "" : "s"} in ${data.folder}.`;
    if (!data.ffprobe_available) {
      movieScanStatus.textContent += " (ffprobe not found — durations/embedded tags skipped, filenames still parsed.)";
    }
    movieActionPanel.hidden = false;
    movieDedupPanel.hidden = false;
    movieDedupResults.innerHTML = "";
    movieTablePanel.hidden = MOVIES.length === 0;
  } catch (e) {
    movieScanStatus.textContent = e.message;
    showToast(e.message, true);
  } finally {
    movieScanBtn.disabled = false;
  }
});

movieMatchBtn.addEventListener("click", async () => {
  if (!tmdbConfigured) {
    tmdbSettingsPanel.hidden = false;
    showToast("Add a TMDB API key first.", true);
    return;
  }
  movieMatchBtn.disabled = true;
  movieProgressWrap.hidden = false;
  try {
    const resp = await fetch("/api/movies/match/start", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Could not start matching.");
    pollMovieProgress();
  } catch (e) {
    showToast(e.message, true);
    movieMatchBtn.disabled = false;
  }
});

function pollMovieProgress() {
  clearInterval(movieProgressTimer);
  movieProgressTimer = setInterval(async () => {
    const resp = await fetch("/api/movies/match/progress");
    const p = await resp.json();
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    movieProgressFill.style.width = pct + "%";
    movieProgressLabel.textContent = `${p.done} / ${p.total}`;

    if (!p.running && p.total > 0 && p.done >= p.total) {
      clearInterval(movieProgressTimer);
      movieMatchBtn.disabled = false;
      movieApplyRow.hidden = false;
      await refreshMovies();
      if (p.error) {
        showToast(p.error, true);
      } else {
        showToast("Matching complete.");
      }
    }
  }, 600);
}

async function refreshMovies() {
  const resp = await fetch("/api/movies");
  const data = await resp.json();
  MOVIES = data.videos;
  renderMovieTable();
}

async function applyMovieSingle(idx) {
  const video = MOVIES[idx];
  try {
    const resp = await fetch("/api/movies/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: video.id,
        tag: movieTagCheck.checked,
        rename: movieRenameCheck.checked,
        nfo: movieNfoCheck.checked,
        poster: moviePosterCheck.checked,
      }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "Failed to apply.");
    showToast(data.renamed ? `Renamed → ${data.new_path.split("/").pop()}` : "Applied");
    await refreshMovies();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function undoMovieSingle(idx) {
  const video = MOVIES[idx];
  try {
    const resp = await fetch("/api/movies/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: video.id }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "Failed to undo.");
    showToast("Reverted " + (data.restored_path || "").split("/").pop());
    await refreshMovies();
  } catch (e) {
    showToast(e.message, true);
  }
}

movieThresholdInput.addEventListener("input", () => {
  movieThresholdValue.textContent = movieThresholdInput.value + "%";
});

movieApplyAllBtn.addEventListener("click", async () => {
  movieApplyAllBtn.disabled = true;
  try {
    const resp = await fetch("/api/movies/apply_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag: movieTagCheck.checked,
        rename: movieRenameCheck.checked,
        nfo: movieNfoCheck.checked,
        poster: moviePosterCheck.checked,
        min_confidence: parseFloat(movieThresholdInput.value),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Bulk apply failed.");
    showToast(`Applied to ${data.applied} file(s) at or above ${movieThresholdInput.value}% confidence.`);
    await refreshMovies();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    movieApplyAllBtn.disabled = false;
  }
});

movieExportBtn.addEventListener("click", () => {
  window.location.href = "/api/movies/export_csv";
});

movieUndoAllBtn.addEventListener("click", async () => {
  movieUndoAllBtn.disabled = true;
  try {
    const resp = await fetch("/api/movies/undo_all", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Undo all failed.");
    showToast(`Reverted ${data.restored} file(s).`);
    await refreshMovies();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    movieUndoAllBtn.disabled = false;
  }
});

/* ------------------------- Movie duplicates --------------------------- */

function movieDedupGroupHtml(group, groupIdx) {
  const rows = group.files.map((f, i) => `
    <label class="dupe-row">
      <input type="checkbox" class="movie-dupe-check" data-group="${groupIdx}" data-path="${escapeHtml(f.path)}" ${i === 0 ? "" : "checked"}>
      <span class="dupe-name">${escapeHtml(f.filename)}</span>
      <span class="dupe-meta">${(f.size_bytes / 1024 / 1024).toFixed(0)} MB${f.duration_seconds ? " · " + Math.round(f.duration_seconds / 60) + " min" : ""}${f.confidence !== null && f.confidence !== undefined ? " · " + f.confidence.toFixed(0) + "% match" : ""}</span>
    </label>
  `).join("");

  return `
    <div class="dupe-group">
      <div class="dupe-group-label">${escapeHtml(group.label)} <span class="dupe-count">(${group.files.length} files)</span></div>
      ${rows}
    </div>
  `;
}

movieScanDupesBtn.addEventListener("click", async () => {
  movieScanDupesBtn.disabled = true;
  movieDedupResults.innerHTML = `<p class="status-line">Hashing files and checking for repeats...</p>`;
  try {
    const resp = await fetch("/api/movies/duplicates/scan", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Duplicate scan failed.");

    const groups = [...data.exact, ...data.probable];
    if (groups.length === 0) {
      movieDedupResults.innerHTML = `<p class="status-line">No duplicates found.</p>`;
      return;
    }

    movieDedupResults.innerHTML = `
      <div class="dupe-groups">
        ${groups.map((g, i) => movieDedupGroupHtml(g, i)).join("")}
      </div>
      <div class="action-row" style="margin-top:16px;">
        <button id="movieQuarantineBtn" class="btn btn-primary">Move checked files to _metamatch_duplicates</button>
        <p class="status-line" style="margin:0;">Any .nfo/poster sidecars move with their video. Files are moved, never deleted.</p>
      </div>
    `;

    el("movieQuarantineBtn").addEventListener("click", async () => {
      const checked = Array.from(document.querySelectorAll(".movie-dupe-check:checked")).map(c => c.dataset.path);
      if (checked.length === 0) { showToast("No files checked.", true); return; }
      const qbtn = el("movieQuarantineBtn");
      qbtn.disabled = true;
      try {
        const r = await fetch("/api/movies/duplicates/quarantine", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paths: checked }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "Quarantine failed.");
        showToast(`Moved ${d.moved} file(s) to the duplicates folder.`);
        await refreshMovies();
        movieScanDupesBtn.click();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        qbtn.disabled = false;
      }
    });
  } catch (e) {
    movieDedupResults.innerHTML = "";
    showToast(e.message, true);
  } finally {
    movieScanDupesBtn.disabled = false;
  }
});

/* ---------------------------- Duplicates ---------------------------- */

function dedupGroupHtml(group, groupIdx) {
  const rows = group.files.map((f, i) => `
    <label class="dupe-row">
      <input type="checkbox" class="dupe-check" data-group="${groupIdx}" data-path="${escapeHtml(f.path)}" ${i === 0 ? "" : "checked"}>
      <span class="dupe-name">${escapeHtml(f.filename)}</span>
      <span class="dupe-meta">${(f.size_bytes / 1024 / 1024).toFixed(1)} MB${f.duration_seconds ? " · " + Math.round(f.duration_seconds) + "s" : ""}${f.confidence !== null && f.confidence !== undefined ? " · " + f.confidence.toFixed(0) + "% match" : ""}</span>
    </label>
  `).join("");

  return `
    <div class="dupe-group">
      <div class="dupe-group-label">${escapeHtml(group.label)} <span class="dupe-count">(${group.files.length} files)</span></div>
      ${rows}
    </div>
  `;
}

scanDupesBtn.addEventListener("click", async () => {
  scanDupesBtn.disabled = true;
  dedupResults.innerHTML = `<p class="status-line">Hashing files and checking for repeats...</p>`;
  try {
    const resp = await fetch("/api/duplicates/scan", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Duplicate scan failed.");

    const groups = [...data.exact, ...data.probable];
    if (groups.length === 0) {
      dedupResults.innerHTML = `<p class="status-line">No duplicates found.</p>`;
      return;
    }

    dedupResults.innerHTML = `
      <div class="dupe-groups">
        ${groups.map((g, i) => dedupGroupHtml(g, i)).join("")}
      </div>
      <div class="action-row" style="margin-top:16px;">
        <button id="quarantineBtn" class="btn btn-primary">Move checked files to _metamatch_duplicates</button>
        <p class="status-line" style="margin:0;">Checked files are moved, never deleted — the first file in each group is unchecked by default.</p>
      </div>
    `;

    el("quarantineBtn").addEventListener("click", async () => {
      const checked = Array.from(document.querySelectorAll(".dupe-check:checked")).map(c => c.dataset.path);
      if (checked.length === 0) { showToast("No files checked.", true); return; }
      const qbtn = el("quarantineBtn");
      qbtn.disabled = true;
      try {
        const r = await fetch("/api/duplicates/quarantine", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paths: checked }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "Quarantine failed.");
        showToast(`Moved ${d.moved} file(s) to the duplicates folder.`);
        await refreshTracks();
        scanDupesBtn.click();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        qbtn.disabled = false;
      }
    });
  } catch (e) {
    dedupResults.innerHTML = "";
    showToast(e.message, true);
  } finally {
    scanDupesBtn.disabled = false;
  }
});
