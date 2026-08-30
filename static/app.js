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

    // "running" going false is the authoritative completion signal - relying
    // on done >= total as well meant a run that stopped early (an error
    // partway through) would poll forever, since done would never catch up
    // to total.
    if (!p.running) {
      clearInterval(progressTimer);
      matchBtn.disabled = false;
      applyRow.hidden = false;
      await refreshTracks();
      showToast(p.error ? p.error : "Matching complete.", !!p.error);
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
    if (data.failed > 0) {
      showToast(`${data.succeeded} succeeded, ${data.failed} failed (of ${data.attempted} at or above ${thresholdInput.value}%). Check rows for errors.`, true);
    } else {
      showToast(`Applied to ${data.succeeded} file(s) at or above ${thresholdInput.value}% confidence.`);
    }
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
const tabTvBtn = el("tabTvBtn");
const musicView = el("musicView");
const moviesView = el("moviesView");
const tvView = el("tvView");
const counterLabel = el("counterLabel");

function switchTab(view) {
  musicView.hidden = view !== "music";
  moviesView.hidden = view !== "movies";
  tvView.hidden = view !== "tv";
  tabMusicBtn.classList.toggle("active", view === "music");
  tabMoviesBtn.classList.toggle("active", view === "movies");
  tabTvBtn.classList.toggle("active", view === "tv");

  const counts = { music: TRACKS.length, movies: MOVIES.length, tv: EPISODES.length };
  const labels = { music: "TRACKS", movies: "MOVIES", tv: "EPISODES" };
  counterLabel.textContent = labels[view];
  counterValue.textContent = String(counts[view]).padStart(3, "0");
  if (view !== "music") checkTmdbSettings();
}

tabMusicBtn.addEventListener("click", () => switchTab("music"));
tabMoviesBtn.addEventListener("click", () => switchTab("movies"));
tabTvBtn.addEventListener("click", () => switchTab("tv"));

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

const tvTmdbSettingsPanel = el("tvTmdbSettingsPanel");
const tvTmdbKeyInput = el("tvTmdbKeyInput");
const saveTvTmdbKeyBtn = el("saveTvTmdbKeyBtn");
const tvTmdbStatus = el("tvTmdbStatus");

async function checkTmdbSettings() {
  try {
    const resp = await fetch("/api/settings/tmdb");
    const data = await resp.json();
    tmdbConfigured = data.configured;
    // The TMDB key is shared by Movies and TV, so both panels reflect it.
    tmdbSettingsPanel.hidden = data.configured;
    tvTmdbSettingsPanel.hidden = data.configured;
    if (data.configured) {
      const label = `Using key ${data.masked_key}`;
      tmdbStatus.textContent = label;
      tvTmdbStatus.textContent = label;
    }
    if (!data.ffprobe_available) {
      movieScanStatus.textContent = "Warning: ffprobe wasn't found on this machine — duration and embedded tags won't be read, but filename-based matching will still work.";
    }
  } catch (e) {
    // settings check failing shouldn't block the rest of the UI
  }
}

async function saveTmdbKey(keyInput, statusBtn) {
  const key = keyInput.value.trim();
  if (!key) { showToast("Enter a TMDB API key.", true); return; }
  statusBtn.disabled = true;
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
    tvTmdbSettingsPanel.hidden = true;
    tmdbKeyInput.value = "";
    tvTmdbKeyInput.value = "";
    showToast("TMDB key saved.");
  } catch (e) {
    showToast(e.message, true);
  } finally {
    statusBtn.disabled = false;
  }
}

saveTmdbKeyBtn.addEventListener("click", () => saveTmdbKey(tmdbKeyInput, saveTmdbKeyBtn));
saveTvTmdbKeyBtn.addEventListener("click", () => saveTmdbKey(tvTmdbKeyInput, saveTvTmdbKeyBtn));

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

    if (!p.running) {
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
    if (data.warnings && data.warnings.length > 0) {
      data.warnings.forEach(w => showToast(w, true));
    }
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
    if (data.failed > 0) {
      showToast(`${data.succeeded} succeeded, ${data.failed} failed (of ${data.attempted} at or above ${movieThresholdInput.value}%). Check rows for errors.`, true);
    } else {
      showToast(`Applied to ${data.succeeded} file(s) at or above ${movieThresholdInput.value}% confidence.`);
    }
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
  const isExact = group.type === "exact";
  const rows = group.files.map((f, i) => `
    <label class="dupe-row">
      <input type="checkbox" class="movie-dupe-check" data-group="${groupIdx}" data-path="${escapeHtml(f.path)}" ${isExact && i !== 0 ? "checked" : ""}>
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
      const checked = Array.from(movieDedupResults.querySelectorAll(".movie-dupe-check:checked")).map(c => c.dataset.path);
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
  // Only pre-check "keep the first, flag the rest" for byte-identical
  // (exact) groups - "probable" is a heuristic match (same MusicBrainz
  // recording or same artist+title text), not proof of duplication, so
  // nothing is preselected and the person has to make an active choice.
  const isExact = group.type === "exact";
  const rows = group.files.map((f, i) => `
    <label class="dupe-row">
      <input type="checkbox" class="dupe-check" data-group="${groupIdx}" data-path="${escapeHtml(f.path)}" ${isExact && i !== 0 ? "checked" : ""}>
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
        <p class="status-line" style="margin:0;">Checked files are moved, never deleted. Exact (identical) duplicates come pre-checked except the first; probable duplicates are a heuristic, so nothing is pre-checked — review before selecting.</p>
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

/* ---------------------------- Crash recovery ---------------------------- */

async function checkRecoveryNotices() {
  try {
    const resp = await fetch("/api/recovery");
    const data = await resp.json();

    const banner = el("recoveryBanner");
    const title = el("recoveryBannerTitle");
    const detail = el("recoveryBannerDetail");

    const attention = data.needs_attention || [];
    const startup = [...(data.music || []), ...(data.movies || []), ...(data.tv || [])];

    if (attention.length > 0) {
      // Files that may be left inconsistent and couldn't be auto-restored -
      // the serious case. Persists across restarts until resolved.
      banner.classList.add("attention");
      title.textContent = `${attention.length} file${attention.length === 1 ? "" : "s"} may need a manual check after an interrupted operation`;
      const names = attention.slice(0, 5).map(n => (n.original_path || n.current_path || "").split("/").pop()).filter(Boolean).join(", ");
      const more = attention.length > 5 ? ` and ${attention.length - 5} more` : "";
      detail.textContent = `MetaMatch couldn't fully undo a failed change to: ${names}${more}. Check each file's tags, filename, and any .nfo/poster sidecars by hand.`;
      banner.hidden = false;
      return;
    }

    if (startup.length === 0) return;

    // Milder case: something was interrupted, but nothing was necessarily
    // left half-changed.
    banner.classList.remove("attention");
    title.textContent = `${startup.length} operation${startup.length === 1 ? "" : "s"} may have been interrupted by a crash or restart`;
    const fileList = startup.slice(0, 5).map(n => (n.original_path || "").split("/").pop()).filter(Boolean).join(", ");
    const more = startup.length > 5 ? ` and ${startup.length - 5} more` : "";
    detail.textContent = `MetaMatch was closed mid-operation on: ${fileList}${more}. Worth checking these files' tags/filenames by hand — their last apply may or may not have finished.`;
    banner.hidden = false;
  } catch (e) {
    // a failed recovery check shouldn't block the rest of the UI from loading
  }
}

el("recoveryBannerDismiss").addEventListener("click", () => {
  el("recoveryBanner").hidden = true;
});

checkRecoveryNotices();

/* --------------------------- Folder browser --------------------------- */

const browseModalOverlay = el("browseModalOverlay");
const browseModalClose = el("browseModalClose");
const browsePath = el("browsePath");
const browseList = el("browseList");
const browseStatus = el("browseStatus");
const browseSelectBtn = el("browseSelectBtn");

let browseTargetInput = null;  // which text input Select fills in
let browseCurrentPath = null;

function openBrowseModal(targetInput) {
  browseTargetInput = targetInput;
  browseModalOverlay.hidden = false;
  loadBrowsePath(targetInput.value.trim() || null);
}

function closeBrowseModal() {
  browseModalOverlay.hidden = true;
  browseTargetInput = null;
}

async function loadBrowsePath(path) {
  browseStatus.textContent = "";
  browseList.innerHTML = `<p class="browse-empty">Loading&hellip;</p>`;
  try {
    const params = path ? `?path=${encodeURIComponent(path)}` : "";
    const resp = await fetch(`/api/browse${params}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Could not browse that folder.");

    browseCurrentPath = data.is_drive_list ? null : data.path;
    browsePath.textContent = data.path;
    browseSelectBtn.disabled = data.is_drive_list;
    browseSelectBtn.title = data.is_drive_list ? "Pick a drive first" : "";
    renderBrowseList(data);
  } catch (e) {
    browseList.innerHTML = "";
    browseStatus.textContent = e.message;
  }
}

const BROWSE_DRIVES_SENTINEL = "__drives__";

function renderBrowseList(data) {
  let html = "";
  if (data.parent) {
    const upLabel = data.parent === BROWSE_DRIVES_SENTINEL ? ".. (This PC)" : ".. (up)";
    html += `<button class="browse-item parent-item" data-path="${escapeHtml(data.parent)}"><span class="browse-item-icon">&uarr;</span> ${upLabel}</button>`;
  }
  if (data.directories.length === 0 && !data.parent) {
    html += `<p class="browse-empty">No subfolders here.</p>`;
  } else {
    html += data.directories.map(name => {
      // A drive-list response's entries are already full paths ("D:\\"),
      // unlike a normal directory listing where they're just names that
      // need joining onto the current path.
      const fullPath = data.is_drive_list
        ? name
        : (data.path.endsWith("/") || data.path.endsWith("\\") ? data.path + name : data.path + "/" + name);
      const icon = data.is_drive_list ? "&#128189;" : "&#128193;";
      return `<button class="browse-item" data-path="${escapeHtml(fullPath)}"><span class="browse-item-icon">${icon}</span> ${escapeHtml(name)}</button>`;
    }).join("");
  }
  browseList.innerHTML = html;

  browseList.querySelectorAll(".browse-item").forEach(btn => {
    btn.addEventListener("click", () => loadBrowsePath(btn.dataset.path));
  });
}

browseModalClose.addEventListener("click", closeBrowseModal);
browseModalOverlay.addEventListener("click", (e) => {
  if (e.target === browseModalOverlay) closeBrowseModal();
});

browseSelectBtn.addEventListener("click", () => {
  if (browseTargetInput && browseCurrentPath) {
    browseTargetInput.value = browseCurrentPath;
  }
  closeBrowseModal();
});

el("browseBtn").addEventListener("click", () => openBrowseModal(folderInput));
el("movieBrowseBtn").addEventListener("click", () => openBrowseModal(movieFolderInput));
el("tvBrowseBtn").addEventListener("click", () => openBrowseModal(tvFolderInput));


/* ================================ TV ================================== */
/* The episode analogue of the Movies module above. Same shapes and flow,
   pointed at /api/tv/* and rendering series/season/episode instead of a
   movie title + year. */

let EPISODES = [];
let tvProgressTimer = null;

const tvFolderInput = el("tvFolderInput");
const tvRecursiveCheck = el("tvRecursiveCheck");
const tvScanBtn = el("tvScanBtn");
const tvScanStatus = el("tvScanStatus");

const tvActionPanel = el("tvActionPanel");
const tvMatchBtn = el("tvMatchBtn");
const tvProgressWrap = el("tvProgressWrap");
const tvProgressFill = el("tvProgressFill");
const tvProgressLabel = el("tvProgressLabel");
const tvApplyRow = el("tvApplyRow");
const tvThresholdInput = el("tvThresholdInput");
const tvThresholdValue = el("tvThresholdValue");
const tvRenameCheck = el("tvRenameCheck");
const tvNfoCheck = el("tvNfoCheck");
const tvThumbCheck = el("tvThumbCheck");
const tvTagCheck = el("tvTagCheck");
const tvApplyAllBtn = el("tvApplyAllBtn");
const tvSeriesMetaBtn = el("tvSeriesMetaBtn");
const tvUndoAllBtn = el("tvUndoAllBtn");
const tvExportBtn = el("tvExportBtn");

const tvDedupPanel = el("tvDedupPanel");
const tvScanDupesBtn = el("tvScanDupesBtn");
const tvDedupResults = el("tvDedupResults");

const tvTablePanel = el("tvTablePanel");
const tvTableBody = el("tvTableBody");

function seasonEpisodeLabel(ep) {
  if (ep.season === null || ep.season === undefined || ep.episode === null || ep.episode === undefined) return "";
  const pad = (n) => String(n).padStart(2, "0");
  const eps = ep.episodes && ep.episodes.length > 1
    ? `E${pad(ep.episodes[0])}-E${pad(ep.episodes[ep.episodes.length - 1])}`
    : `E${pad(ep.episode)}`;
  return `S${pad(ep.season)}${eps}`;
}

function tvParsedHtml(ep) {
  if (!ep.parsed) return `<span class="missing">couldn't parse an episode</span>`;
  const tag = seasonEpisodeLabel(ep);
  return `<div class="tag-line"><strong>${escapeHtml(ep.series_guess)}</strong> · ${escapeHtml(tag)}${ep.episode_title_guess ? " · " + escapeHtml(ep.episode_title_guess) : ""}</div>`;
}

function tvMatchHtml(ep) {
  if (!ep.match) return `<span class="no-match">not searched yet</span>`;
  const m = ep.match;
  const thumb = m.still_url
    ? `<img class="match-thumb poster-thumb" src="${m.still_url}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="match-thumb match-thumb-empty"></div>`;
  const epTag = seasonEpisodeLabel({ season: m.season, episode: m.episode, episodes: m.episodes });
  const title = m.episode_title || (m.episode_missing ? "episode not found on TMDB" : "Untitled episode");
  return `
    <div class="match-row">
      ${thumb}
      <div class="match-text">
        <div class="match-artist">${escapeHtml(m.series_name || "Unknown series")} · ${escapeHtml(epTag)}</div>
        <div class="match-title">${escapeHtml(title)}${m.vote_average ? " · ★ " + m.vote_average.toFixed(1) : ""}</div>
      </div>
    </div>
  `;
}

function renderTvTable() {
  tvTableBody.innerHTML = EPISODES.map((ep, idx) => {
    const conf = ep.match ? ep.match.confidence : null;
    return `
      <tr data-id="${escapeHtml(ep.id)}">
        <td class="col-file">
          <span class="file-name">${escapeHtml(ep.filename)}</span>
          <span class="file-path">${escapeHtml(ep.path)}</span>
        </td>
        <td class="col-current">${tvParsedHtml(ep)}</td>
        <td class="col-match">${tvMatchHtml(ep)}</td>
        <td class="col-confidence">${conf !== null ? movieVuMeterHtml(conf) : `<span class="no-match">&mdash;</span>`}</td>
        <td class="col-actions">
          <div class="row-actions">
            <button class="btn btn-ghost tv-apply-row-btn" data-idx="${idx}" ${ep.match ? "" : "disabled"}>Apply</button>
            <button class="btn btn-ghost tv-undo-row-btn" data-idx="${idx}" ${ep.can_undo ? "" : "disabled"}>Undo</button>
          </div>
          <div class="row-status" data-tv-status-for="${escapeHtml(ep.id)}"></div>
        </td>
      </tr>
    `;
  }).join("");

  document.querySelectorAll(".tv-apply-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyTvSingle(parseInt(btn.dataset.idx, 10)));
  });
  document.querySelectorAll(".tv-undo-row-btn").forEach((btn) => {
    btn.addEventListener("click", () => undoTvSingle(parseInt(btn.dataset.idx, 10)));
  });

  counterValue.textContent = String(EPISODES.length).padStart(3, "0");
}

tvScanBtn.addEventListener("click", async () => {
  const folder = tvFolderInput.value.trim();
  if (!folder) { showToast("Enter a folder path first.", true); return; }
  tvScanBtn.disabled = true;
  tvScanStatus.textContent = "Scanning...";
  try {
    const resp = await fetch("/api/tv/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, recursive: tvRecursiveCheck.checked }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Scan failed.");

    EPISODES = data.episodes;
    renderTvTable();
    const parsed = EPISODES.filter(e => e.parsed).length;
    tvScanStatus.textContent = `Found ${data.count} video file${data.count === 1 ? "" : "s"} in ${data.folder} — ${parsed} parsed as episodes.`;
    if (!data.ffprobe_available) {
      tvScanStatus.textContent += " (ffprobe not found — durations skipped, filenames still parsed.)";
    }
    tvActionPanel.hidden = false;
    tvDedupPanel.hidden = false;
    tvDedupResults.innerHTML = "";
    tvTablePanel.hidden = EPISODES.length === 0;
  } catch (e) {
    tvScanStatus.textContent = e.message;
    showToast(e.message, true);
  } finally {
    tvScanBtn.disabled = false;
  }
});

tvMatchBtn.addEventListener("click", async () => {
  if (!tmdbConfigured) {
    tvTmdbSettingsPanel.hidden = false;
    showToast("Add a TMDB API key first.", true);
    return;
  }
  tvMatchBtn.disabled = true;
  tvProgressWrap.hidden = false;
  try {
    const resp = await fetch("/api/tv/match/start", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Could not start matching.");
    pollTvProgress();
  } catch (e) {
    showToast(e.message, true);
    tvMatchBtn.disabled = false;
  }
});

function pollTvProgress() {
  clearInterval(tvProgressTimer);
  tvProgressTimer = setInterval(async () => {
    const resp = await fetch("/api/tv/match/progress");
    const p = await resp.json();
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    tvProgressFill.style.width = pct + "%";
    tvProgressLabel.textContent = `${p.done} / ${p.total}`;

    if (!p.running) {
      clearInterval(tvProgressTimer);
      tvMatchBtn.disabled = false;
      tvApplyRow.hidden = false;
      await refreshTv();
      showToast(p.error ? p.error : "Matching complete.", !!p.error);
    }
  }, 600);
}

async function refreshTv() {
  const resp = await fetch("/api/tv");
  const data = await resp.json();
  EPISODES = data.episodes;
  renderTvTable();
}

async function applyTvSingle(idx) {
  const ep = EPISODES[idx];
  try {
    const resp = await fetch("/api/tv/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: ep.id,
        tag: tvTagCheck.checked,
        rename: tvRenameCheck.checked,
        nfo: tvNfoCheck.checked,
        thumb: tvThumbCheck.checked,
      }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "Failed to apply.");
    showToast(data.renamed ? `Renamed → ${data.new_path.split("/").pop()}` : "Applied");
    if (data.recovery_required) showToast("A rollback couldn't fully restore this file — check it (see recovery notices).", true);
    await refreshTv();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function undoTvSingle(idx) {
  const ep = EPISODES[idx];
  try {
    const resp = await fetch("/api/tv/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: ep.id }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "Failed to undo.");
    showToast("Reverted " + (data.restored_path || "").split("/").pop());
    if (data.warnings && data.warnings.length > 0) data.warnings.forEach(w => showToast(w, true));
    await refreshTv();
  } catch (e) {
    showToast(e.message, true);
  }
}

tvThresholdInput.addEventListener("input", () => {
  tvThresholdValue.textContent = tvThresholdInput.value + "%";
});

tvApplyAllBtn.addEventListener("click", async () => {
  tvApplyAllBtn.disabled = true;
  try {
    const resp = await fetch("/api/tv/apply_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag: tvTagCheck.checked,
        rename: tvRenameCheck.checked,
        nfo: tvNfoCheck.checked,
        thumb: tvThumbCheck.checked,
        min_confidence: parseFloat(tvThresholdInput.value),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Bulk apply failed.");
    if (data.failed > 0) {
      showToast(`${data.succeeded} succeeded, ${data.failed} failed (of ${data.attempted} at or above ${tvThresholdInput.value}%). Check rows for errors.`, true);
    } else {
      showToast(`Applied to ${data.succeeded} file(s) at or above ${tvThresholdInput.value}% confidence.`);
    }
    await refreshTv();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    tvApplyAllBtn.disabled = false;
  }
});

tvUndoAllBtn.addEventListener("click", async () => {
  tvUndoAllBtn.disabled = true;
  try {
    const resp = await fetch("/api/tv/undo_all", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Undo all failed.");
    // Also revert any series-level metadata (tvshow.nfo / posters) written.
    const sResp = await fetch("/api/tv/series_metadata/undo", { method: "POST" });
    const sData = await sResp.json();
    const seriesReverted = sResp.ok ? (sData.reverted || 0) : 0;
    const parts = [`${data.restored} file(s)`];
    if (seriesReverted > 0) parts.push(`${seriesReverted} series' metadata`);
    showToast(`Reverted ${parts.join(" and ")}.`);
    await refreshTv();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    tvUndoAllBtn.disabled = false;
  }
});

tvExportBtn.addEventListener("click", () => {
  window.location.href = "/api/tv/export_csv";
});

tvSeriesMetaBtn.addEventListener("click", async () => {
  if (!tmdbConfigured) {
    tvTmdbSettingsPanel.hidden = false;
    showToast("Add a TMDB API key first.", true);
    return;
  }
  tvSeriesMetaBtn.disabled = true;
  try {
    const resp = await fetch("/api/tv/series_metadata", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        min_confidence: parseFloat(tvThresholdInput.value),
        poster: true,
        season_posters: true,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Series metadata failed.");
    if (data.series === 0) {
      showToast(`No series at or above ${tvThresholdInput.value}% confidence to write metadata for.`, true);
    } else if (data.failed > 0) {
      showToast(`Wrote metadata for ${data.succeeded} series, ${data.failed} failed — check for details.`, true);
    } else {
      showToast(`Wrote tvshow.nfo + posters for ${data.succeeded} series.`);
    }
    (data.results || []).forEach(r => (r.warnings || []).forEach(w => showToast(w, true)));
  } catch (e) {
    showToast(e.message, true);
  } finally {
    tvSeriesMetaBtn.disabled = false;
  }
});

tvScanDupesBtn.addEventListener("click", async () => {
  tvScanDupesBtn.disabled = true;
  tvDedupResults.innerHTML = `<p class="status-line">Hashing files and checking for repeats...</p>`;
  try {
    const resp = await fetch("/api/tv/duplicates/scan", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Duplicate scan failed.");

    const groups = [...data.exact, ...data.probable];
    if (groups.length === 0) {
      tvDedupResults.innerHTML = `<p class="status-line">No duplicates found.</p>`;
      return;
    }

    tvDedupResults.innerHTML = `
      <div class="dupe-groups">
        ${groups.map((g, i) => movieDedupGroupHtml(g, i)).join("")}
      </div>
      <div class="action-row" style="margin-top:16px;">
        <button id="tvQuarantineBtn" class="btn btn-primary">Move checked files to _metamatch_duplicates</button>
        <p class="status-line" style="margin:0;">Any .nfo/thumbnail sidecars move with their episode. Files are moved, never deleted.</p>
      </div>
    `;

    el("tvQuarantineBtn").addEventListener("click", async () => {
      const checked = Array.from(tvDedupResults.querySelectorAll(".movie-dupe-check:checked")).map(c => c.dataset.path);
      if (checked.length === 0) { showToast("No files checked.", true); return; }
      const qbtn = el("tvQuarantineBtn");
      qbtn.disabled = true;
      try {
        const r = await fetch("/api/tv/duplicates/quarantine", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paths: checked }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "Quarantine failed.");
        showToast(`Moved ${d.moved} file(s) to the duplicates folder.`);
        await refreshTv();
        tvScanDupesBtn.click();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        qbtn.disabled = false;
      }
    });
  } catch (e) {
    tvDedupResults.innerHTML = "";
    showToast(e.message, true);
  } finally {
    tvScanDupesBtn.disabled = false;
  }
});
