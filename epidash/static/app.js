/* EpiDash viewer — vanilla JS, hash-routed SPA.
 *
 * Routes:
 *   #/           home: list of published dashboards
 *   #/d/{slug}   dashboard view
 *
 * When a dashboard advertises tweaks_enabled, the Ask-EpidBot panel lets
 * viewers request changes; the tweak turn is processed server-side via
 * EpidBot and the dashboard is refetched when it completes.
 */

const PALETTE = ["#4ade80", "#58a6ff", "#f78c6c", "#c084fc", "#facc15", "#2dd4bf", "#fb7185"];

const app = document.getElementById("app");
const tweakPanel = document.getElementById("tweak-panel");
const tweakToggle = document.getElementById("tweak-toggle");
const tweakBody = document.getElementById("tweak-body");
const tweakLog = document.getElementById("tweak-log");
const tweakInput = document.getElementById("tweak-input");
const tweakSend = document.getElementById("tweak-send");
const askSend = document.getElementById("ask-send");

let currentSlug = null;
let currentDashboard = null;
let pollTimer = null;
let tweaksEnabled = false;

const HEART_SVG =
  '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
  '<path class="heart-path" d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 ' +
  '2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 ' +
  '19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/>' +
  "</svg>";

const LIKED_STORAGE_KEY = "epidash-liked-dashboards";

function likedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(LIKED_STORAGE_KEY) || "[]")); }
  catch (_) { return new Set(); }
}

function setLiked(slug, liked) {
  const set = likedSet();
  if (liked) set.add(slug); else set.delete(slug);
  localStorage.setItem(LIKED_STORAGE_KEY, JSON.stringify([...set]));
}

// ───────────────────────────── routing ─────────────────────────────

async function route() {
  const hash = location.hash || "#/";
  const dashMatch = hash.match(/^#\/d\/([A-Za-z0-9_-]+)$/);
  stopTweakPolling();
  if (dashMatch) {
    await showDashboard(dashMatch[1]);
  } else {
    await showHome();
  }
}

window.addEventListener("hashchange", route);

// ───────────────────────────── home ────────────────────────────────

async function showHome() {
  currentSlug = null;
  currentDashboard = null;
  tweaksEnabled = false;
  tweakPanel.hidden = true;
  document.getElementById("dash-status").hidden = true;
  app.innerHTML =
    '<div class="home">' +
    '<div class="home-head"><h1>Published dashboards</h1>' +
    '<input id="dash-search" type="search" placeholder="Search dashboards…" aria-label="Search dashboards"></div>' +
    '<div id="list" class="dash-grid"></div></div>';
  let dashboards;
  try {
    dashboards = await api("/api/dashboards");
  } catch (e) {
    app.innerHTML = `<p class="empty">Failed to load dashboards: ${esc(String(e))}</p>`;
    return;
  }
  const list = document.getElementById("list");
  const search = document.getElementById("dash-search");
  const paint = () => {
    const q = (search.value || "").trim().toLowerCase();
    const matches = dashboards.filter((d) =>
      !q ||
      (d.title || "").toLowerCase().includes(q) ||
      (d.description || "").toLowerCase().includes(q) ||
      (d.created_by || "").toLowerCase().includes(q)
    );
    list.innerHTML = "";
    if (!matches.length) {
      list.innerHTML = `<p class="empty">${dashboards.length ? "No dashboards match your search." : "No published dashboards yet."}</p>`;
      return;
    }
    for (const d of matches) {
      const a = document.createElement("a");
      a.className = "dash-card";
      a.href = `#/d/${d.slug}`;
      const by = d.created_by ? `<span class="dash-card-by">by ${esc(d.created_by)}</span>` : "<span></span>";
      a.innerHTML =
        `<h3>${esc(d.title)}</h3><p>${esc(d.description || "")}</p>` +
        `<p class="dash-card-meta">${by}<span>♥ ${d.likes ?? 0}</span></p>`;
      list.appendChild(a);
    }
  };
  search.addEventListener("input", paint);
  paint();
}

// ───────────────────────────── dashboard ───────────────────────────

async function showDashboard(slug) {
  currentSlug = slug;
  app.innerHTML = '<p class="empty">Loading…</p>';
  let data;
  try {
    data = await api(`/api/dashboards/${encodeURIComponent(slug)}`);
  } catch (e) {
    currentDashboard = null;
    app.innerHTML = '<p class="empty">Dashboard not found or not published.</p>';
    tweakPanel.hidden = true;
    return;
  }
  currentDashboard = data;

  tweaksEnabled = !!data.tweaks_enabled;
  tweakLog.innerHTML = "";
  tweakPanel.hidden = !tweaksEnabled;
  tweakBody.hidden = true;

  const status = document.getElementById("dash-status");
  status.hidden = false;
  status.classList.add("live");
  status.textContent = "live";

  const byLine = data.created_by ? `by ${esc(data.created_by)}` : "";
  app.innerHTML = `
    <div class="dash-head">
      <a class="back-link" href="#/">← All dashboards</a>
      <div class="dash-title-row">
        <h1>${esc(data.title)}</h1>
        <div class="dash-actions">
          <button id="like-btn" class="action-btn like-btn" title="Like this dashboard">
            ${HEART_SVG}<span id="like-count">${data.likes ?? 0}</span>
          </button>
          <div class="export-wrap">
            <button id="export-btn" class="action-btn" title="Export this dashboard">⬇ Export</button>
            <div id="export-menu" class="export-menu" hidden>
              <button data-export="png">PNG image</button>
              <button data-export="pdf">PDF (print)</button>
              <button data-export="json">JSON data</button>
            </div>
          </div>
          <button id="share-btn" class="action-btn" title="Copy a shareable link to this dashboard">🔗 Permalink</button>
        </div>
      </div>
      <p class="dash-byline">${byLine}</p>
      <p>${esc(data.description || "")}</p>
    </div>
    <div class="chart-grid" id="charts"></div>`;

  wireDashActions(slug, data.likes ?? 0);
  const grid = document.getElementById("charts");

  const charts = [...data.charts].sort(
    (a, b) => (a.position.order ?? a.id) - (b.position.order ?? b.id)
  );
  for (const chart of charts) {
    const card = document.createElement("div");
    card.className = "chart-card" + (chart.position.width === "half" ? "" : " full");
    card.innerHTML = `<h2>${esc(chart.title || chart.kind)}</h2><div class="plot"></div>`;
    grid.appendChild(card);
    try {
      renderChart(card.querySelector(".plot"), chart);
    } catch (e) {
      card.querySelector(".plot").innerHTML =
        `<p class="empty">Failed to render chart: ${esc(String(e))}</p>`;
    }
  }

  if (tweaksEnabled) pollForUpdates(data.updated_at);
}

function wireDashActions(slug, serverLikes) {
  const likeBtn = document.getElementById("like-btn");
  const likeCount = document.getElementById("like-count");
  const shareBtn = document.getElementById("share-btn");
  let likes = serverLikes;
  let busy = false;

  const paint = (liked) => {
    likeCount.textContent = likes;
    likeBtn.classList.toggle("liked", liked);
    likeBtn.title = liked ? "Unlike this dashboard" : "Like this dashboard";
  };

  let liked = likedSet().has(slug);
  // reconcile: a stored like that the server no longer counts was reset
  if (liked && likes === 0) { setLiked(slug, false); liked = false; }
  paint(liked);

  likeBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    const goingToLike = !liked;
    likeCount.textContent = likes + (goingToLike ? 1 : -1); // optimistic
    try {
      const res = await api(`/api/dashboards/${encodeURIComponent(slug)}/like`, {
        method: goingToLike ? "POST" : "DELETE",
      });
      likes = res.likes;
      liked = goingToLike;
      setLiked(slug, liked);
    } catch (_) { /* keep server truth on failure */ }
    paint(liked);
    busy = false;
  });

  shareBtn.addEventListener("click", async () => {
    const url = `${location.origin}/#/d/${encodeURIComponent(slug)}`;
    const original = shareBtn.textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(url);
      } else {
        const ta = document.createElement("textarea");
        ta.value = url;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      shareBtn.textContent = "✓ Copied!";
    } catch (_) {
      shareBtn.textContent = url;
    }
    shareBtn.disabled = true;
    setTimeout(() => {
      shareBtn.textContent = original;
      shareBtn.disabled = false;
    }, 1600);
  });

  const exportBtn = document.getElementById("export-btn");
  const exportMenu = document.getElementById("export-menu");
  exportBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    exportMenu.hidden = !exportMenu.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!exportMenu.hidden && !exportMenu.contains(e.target)) exportMenu.hidden = true;
  });
  exportMenu.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", async () => {
      exportMenu.hidden = true;
      if (b.dataset.export === "png") {
        exportBtn.disabled = true;
        try { await exportPng(); } finally { exportBtn.disabled = false; }
      } else if (b.dataset.export === "pdf") {
        window.print();
      } else {
        location.href = `/api/dashboards/${encodeURIComponent(slug)}/export`;
      }
    });
  });
}

// ───────────────────────────── export ──────────────────────────────

const EXPORT_WIDTH = 1320;
const CARD_X = 48;
const CARD_PAD = 22;
const EXPORT_FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

async function exportPng() {
  const data = currentDashboard;
  if (!data) return;
  const canvas = document.createElement("canvas");
  canvas.width = EXPORT_WIDTH;
  canvas.height = 8000;
  const ctx = canvas.getContext("2d");
  ctx.textBaseline = "top";
  ctx.fillStyle = "#0d1117";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  let y = 52;
  ctx.fillStyle = "#e6edf3";
  ctx.font = `700 32px ${EXPORT_FONT}`;
  ctx.fillText(data.title, CARD_X, y);
  y += 46;
  ctx.fillStyle = "#8b949e";
  ctx.font = `16px ${EXPORT_FONT}`;
  const by = data.created_by ? `by ${data.created_by} · ` : "";
  ctx.fillText(`${by}${new Date().toLocaleDateString()}`, CARD_X, y);
  y += 44;

  for (const card of document.querySelectorAll(".chart-card")) {
    const title = card.querySelector("h2").textContent;
    const plotEl = card.querySelector(".plot");
    const contentW = EXPORT_WIDTH - 2 * CARD_X - 2 * CARD_PAD;
    let draw = null; // (ctx, x, y) -> void, given content-area coords
    let contentH = 0;

    if (plotEl && plotEl.data) {
      const imgH = Math.max(
        1, Math.round(plotEl.clientHeight * (contentW / Math.max(plotEl.clientWidth, 1)))
      );
      const url = await Plotly.toImage(plotEl, {
        format: "png", width: contentW, height: imgH, scale: 2,
      });
      const img = await loadImage(url);
      contentH = imgH;
      draw = (c, x, dy) => c.drawImage(img, x, dy, contentW, imgH);
    } else {
      const kpi = card.querySelector(".kpi-row");
      const table = card.querySelector(".data-table");
      if (kpi) { contentH = 96; draw = drawKpi(null, kpi); }
      else if (table) {
        const rows = table.querySelectorAll("tbody tr").length;
        contentH = 30 + Math.min(rows, 40) * 26 + (rows > 40 ? 22 : 0);
        draw = drawTable(table);
      } else continue;
    }

    const cardH = 2 * CARD_PAD + 30 + contentH;
    ctx.fillStyle = "#161b22";
    ctx.fillRect(CARD_X, y, EXPORT_WIDTH - 2 * CARD_X, cardH);
    ctx.strokeStyle = "#30363d";
    ctx.strokeRect(CARD_X + 0.5, y + 0.5, EXPORT_WIDTH - 2 * CARD_X - 1, cardH - 1);
    ctx.fillStyle = "#8b949e";
    ctx.font = `600 17px ${EXPORT_FONT}`;
    ctx.fillText(title, CARD_X + CARD_PAD, y + CARD_PAD);
    draw(ctx, CARD_X + CARD_PAD, y + CARD_PAD + 30);
    y += cardH + 24;
    if (y > canvas.height - 600) break; // safety cap
  }

  // trim to content
  const finalH = Math.ceil(y) + 52;
  const out = document.createElement("canvas");
  out.width = EXPORT_WIDTH;
  out.height = finalH;
  out.getContext("2d").drawImage(canvas, 0, 0);
  const a = document.createElement("a");
  a.href = out.toDataURL("image/png");
  a.download = `${currentSlug || "dashboard"}.png`;
  a.click();
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });
}

function drawKpi(_, kpiEl) {
  const value = kpiEl.querySelector(".kpi-value")?.textContent || "";
  const unit = kpiEl.querySelector(".kpi-unit")?.textContent || "";
  const delta = kpiEl.querySelector(".kpi-delta")?.textContent || "";
  const label = kpiEl.parentElement.querySelector(".kpi-label")?.textContent || "";
  return (ctx, x, y) => {
    ctx.textBaseline = "top";
    ctx.fillStyle = "#4ade80";
    ctx.font = `700 54px ${EXPORT_FONT}`;
    ctx.fillText(value, x, y);
    let cx = x + ctx.measureText(value).width + 12;
    if (unit) {
      ctx.fillStyle = "#8b949e";
      ctx.font = `20px ${EXPORT_FONT}`;
      ctx.fillText(unit, cx, y + 26);
      cx += ctx.measureText(unit).width + 12;
    }
    if (delta) {
      ctx.fillStyle = delta.trim().startsWith("-") ? "#f85149" : "#4ade80";
      ctx.font = `600 18px ${EXPORT_FONT}`;
      ctx.fillText(delta, cx, y + 26);
    }
    if (label) {
      ctx.fillStyle = "#8b949e";
      ctx.font = `15px ${EXPORT_FONT}`;
      ctx.fillText(label, x, y + 66);
    }
  };
}

function drawTable(tableEl) {
  const headers = [...tableEl.querySelectorAll("thead th")].map((th) => th.textContent);
  const rows = [...tableEl.querySelectorAll("tbody tr")].map((tr) =>
    [...tr.querySelectorAll("td")].map((td) => td.textContent)
  );
  const ncols = Math.max(headers.length, ...(rows.length ? rows.map((r) => r.length) : [1]));
  return (ctx, x, y) => {
    ctx.textBaseline = "top";
    const colW = (EXPORT_WIDTH - 2 * CARD_X - 2 * CARD_PAD) / ncols;
    const clip = (text, cx, w) => {
      let t = text;
      if (ctx.measureText(t).width > w - 12) {
        while (t.length > 1 && ctx.measureText(t + "…").width > w - 12) t = t.slice(0, -1);
        t += "…";
      }
      ctx.fillText(t, cx, y);
    };
    ctx.font = `600 13px ${EXPORT_FONT}`;
    headers.forEach((h, i) => {
      ctx.fillStyle = "#8b949e";
      clip(h, x + i * colW, colW);
    });
    ctx.font = `13px ${EXPORT_FONT}`;
    rows.slice(0, 40).forEach((row, r) => {
      const ry = y + 30 + r * 26;
      row.forEach((cell, i) => {
        ctx.fillStyle = "#e6edf3";
        clip(String(cell), x + i * colW, colW);
      });
    });
    if (rows.length > 40) {
      ctx.fillStyle = "#8b949e";
      ctx.font = `12px ${EXPORT_FONT}`;
      ctx.fillText(`+ ${rows.length - 40} more rows`, x, y + 30 + 40 * 26);
    }
  };
}

async function pollForUpdates(since) {
  stopTweakPolling();
  const slug = currentSlug;
  const tick = async () => {
    if (currentSlug !== slug) return;
    try {
      const head = await api(`/api/dashboards/${encodeURIComponent(slug)}`);
      if (head.updated_at !== since && currentSlug === slug) {
        await showDashboard(slug);
        addTweakMsg("agent", "Dashboard updated.");
        return;
      }
    } catch (_) { /* transient; keep polling */ }
    pollTimer = setTimeout(tick, 5000);
  };
  pollTimer = setTimeout(tick, 5000);
}

function stopTweakPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

// ───────────────────────────── renderers ───────────────────────────

function renderChart(el, chart) {
  const { kind, data, spec = {} } = chart;
  if (kind === "kpi") return renderKpi(el, data, spec);
  if (kind === "table") return renderTable(el, data, spec);
  if (kind === "choropleth") return renderChoropleth(el, data, spec);
  return renderPlotlyChart(el, kind, data, spec);
}

function formatKpiValue(value) {
  // Thousands separators for numbers; non-numeric strings stay as-is.
  if (value == null || String(value).trim() === "") return String(value ?? "");
  const n = typeof value === "number" ? value : Number(String(value).trim());
  if (Number.isFinite(n)) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return String(value);
}

function renderKpi(el, data, spec) {
  const delta = data.delta != null && data.delta !== ""
    ? `<span class="kpi-delta ${String(data.delta).trim().startsWith("-") ? "down" : "up"}">${esc(String(data.delta))}</span>`
    : "";
  el.innerHTML = `
    <div class="kpi-row">
      <span class="kpi-value">${esc(formatKpiValue(data.value))}</span>
      ${data.unit ? `<span class="kpi-unit">${esc(String(data.unit))}</span>` : ""}
      ${delta}
    </div>
    ${data.label ? `<div class="kpi-label">${esc(String(data.label))}</div>` : ""}`;
}

function renderTable(el, data, spec) {
  const columns = data.columns || [];
  const rows = data.rows || [];
  const table = document.createElement("table");
  table.className = "data-table";
  table.innerHTML =
    "<thead><tr>" +
    columns.map((c, i) => `<th data-col="${i}">${esc(c)}</th>`).join("") +
    "</tr></thead><tbody></tbody>";
  const tbody = table.querySelector("tbody");
  let view = rows.map(r => r.slice());
  const paint = () => {
    tbody.innerHTML = view.map(r => "<tr>" + r.map(v => `<td>${esc(String(v))}</td>`).join("") + "</tr>").join("");
  };
  table.querySelectorAll("th").forEach(th => {
    th.addEventListener("click", () => {
      const col = Number(th.dataset.col);
      view.sort((a, b) => {
        const x = a[col], y = b[col];
        if (typeof x === "number" && typeof y === "number") return x - y;
        return String(x).localeCompare(String(y));
      });
      paint();
    });
  });
  paint();
  el.innerHTML = "";
  el.appendChild(table);
}

function renderChoropleth(el, data, spec) {
  const locations = Object.keys(data.values || {});
  const z = locations.map(k => data.values[k]);
  fetch("/geo/br-states.json").then(r => r.json()).then(geojson => {
    const trace = {
      type: "choropleth",
      geojson,
      featureidkey: "properties.sigla",
      locations,
      z,
      colorscale: spec.colorscale || "Viridis",
      marker: { line: { color: "#0d1117", width: 0.6 } },
      showscale: true,
    };
    const layout = baseLayout(spec);
    layout.geo = {
      scope: "south america",
      fitbounds: "locations",
      bgcolor: "rgba(0,0,0,0)",
      showcountries: false,
      showcoastlines: false,
      showland: false,
    };
    layout.margin = { t: 10, b: 10, l: 0, r: 0 };
    Plotly.newPlot(el, [trace], layout, config());
  });
}

function renderPlotlyChart(el, kind, data, spec) {
  const columns = data.columns || [];
  const rows = data.rows || [];
  let traces;

  if (kind === "pie") {
    traces = [{
      type: "pie",
      labels: rows.map(r => r[0]),
      values: rows.map(r => Number(r[1])),
      hole: spec.hole ?? 0.45,
      textinfo: "label+percent",
    }];
  } else {
    const traceType = kind === "line" ? "scatter" : "bar";
    const mode = kind === "line" ? "lines+markers" : undefined;
    if (columns.length > 2 && rows.every(r => typeof r[0] === "string")) {
      // first column = x, every other column = a series
      traces = columns.slice(1).map((col, i) => ({
        type: traceType,
        mode,
        name: col,
        x: rows.map(r => r[0]),
        y: rows.map(r => Number(r[i + 1])),
        line: { color: PALETTE[i % PALETTE.length] },
        marker: { color: spec.color || PALETTE[i % PALETTE.length] },
      }));
    } else {
      traces = [{
        type: traceType,
        mode,
        x: rows.map(r => r[0]),
        y: rows.map(r => Number(r[1])),
        line: { color: spec.color || PALETTE[0], shape: spec.shape || "linear" },
        marker: { color: spec.color || PALETTE[0] },
      }];
    }
  }

  const layout = baseLayout(spec);
  layout.margin = { t: 10, b: 40, l: 50, r: 10 };
  if (spec.y_label) layout.yaxis.title = { text: spec.y_label };
  if (spec.x_label) layout.xaxis.title = { text: spec.x_label };
  if (traces.length > 1) layout.showlegend = true;
  Plotly.newPlot(el, traces, layout, config());
}

function baseLayout(spec) {
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#8b949e", family: "inherit" },
    xaxis: { gridcolor: "#21262d", zeroline: false },
    yaxis: { gridcolor: "#21262d", zeroline: false },
    showlegend: false,
    legend: { orientation: "h", y: -0.2 },
    ...spec.layout,
  };
}

function config() {
  return {
    responsive: true,
    // modebar appears on hover; gives zoom/pan plus "Reset axes"
    // (cartesian) and reset-view buttons for geo maps. Double-click
    // also resets the view.
    displayModeBar: true,
    displaylogo: false,
    scrollZoom: true,
    modeBarButtonsToRemove: [
      "select2d", "lasso2d",
      "hoverClosestCartesian", "hoverCompareCartesian",
      "toggleSpikelines",
    ],
  };
}

// ───────────────────────────── tweak panel ─────────────────────────

tweakToggle.addEventListener("click", () => { tweakBody.hidden = !tweakBody.hidden; });

tweakSend.addEventListener("click", () => sendTweak("tweak"));
askSend.addEventListener("click", () => sendTweak("question"));
tweakInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendTweak("question"); }
});

function setSendBusy(busy) {
  tweakSend.disabled = busy;
  askSend.disabled = busy;
}

async function sendTweak(kind) {
  const request = tweakInput.value.trim();
  if (!request || !currentSlug) return;
  setSendBusy(true);
  addTweakMsg("user", request);
  tweakInput.value = "";
  try {
    const res = await api(
      `/api/dashboards/${encodeURIComponent(currentSlug)}/${kind === "question" ? "ask" : "tweak"}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request }),
      }
    );
    await pollTweak(res.tweak_id);
  } catch (e) {
    addTweakMsg("error", String(e.message || e));
    setSendBusy(false);
  }
}

async function pollTweak(tweakId) {
  addTweakMsg("agent", "EpidBot is working on it…", true);
  const started = Date.now();
  const tick = async () => {
    try {
      const t = await api(`/api/tweaks/${tweakId}`);
      if (t.status === "completed") {
        clearPendingMsg();
        addTweakMsg("agent", t.response || "Done.");
        setSendBusy(false);
        return;
      }
      if (t.status === "failed") {
        clearPendingMsg();
        addTweakMsg("error", t.error || "The request failed.");
        setSendBusy(false);
        return;
      }
      if (Date.now() - started > 6 * 60 * 1000) {
        clearPendingMsg();
        addTweakMsg("error", "Timed out waiting for EpidBot.");
        setSendBusy(false);
        return;
      }
      setTimeout(tick, 3000);
    } catch (e) {
      clearPendingMsg();
      addTweakMsg("error", String(e.message || e));
      setSendBusy(false);
    }
  };
  setTimeout(tick, 3000);
}

function addTweakMsg(kind, text, pending = false) {
  const div = document.createElement("div");
  div.className = `tweak-msg ${kind}`;
  if (pending) div.dataset.pending = "1";
  const who = { user: "You", agent: "EpidBot", error: "Error" }[kind] || kind;
  div.innerHTML = `<span class="who">${who}</span>${esc(text)}`;
  tweakLog.appendChild(div);
  tweakLog.scrollTop = tweakLog.scrollHeight;
}

function clearPendingMsg() {
  tweakLog.querySelectorAll("[data-pending]").forEach((n) => n.remove());
}

// ───────────────────────────── helpers ─────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

route();
