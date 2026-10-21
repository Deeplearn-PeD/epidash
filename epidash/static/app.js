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

let currentSlug = null;
let pollTimer = null;
let tweaksEnabled = false;

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
  tweaksEnabled = false;
  tweakPanel.hidden = true;
  document.getElementById("dash-status").hidden = true;
  app.innerHTML = '<div class="home"><h1>Published dashboards</h1><div id="list" class="dash-grid"></div></div>';
  try {
    const dashboards = await api("/api/dashboards");
    const list = document.getElementById("list");
    if (!dashboards.length) {
      list.innerHTML = '<p class="empty">No published dashboards yet.</p>';
      return;
    }
    for (const d of dashboards) {
      const a = document.createElement("a");
      a.className = "dash-card";
      a.href = `#/d/${d.slug}`;
      a.innerHTML = `<h3>${esc(d.title)}</h3><p>${esc(d.description || "")}</p>`;
      list.appendChild(a);
    }
  } catch (e) {
    app.innerHTML = `<p class="empty">Failed to load dashboards: ${esc(String(e))}</p>`;
  }
}

// ───────────────────────────── dashboard ───────────────────────────

async function showDashboard(slug) {
  currentSlug = slug;
  app.innerHTML = '<p class="empty">Loading…</p>';
  let data;
  try {
    data = await api(`/api/dashboards/${encodeURIComponent(slug)}`);
  } catch (e) {
    app.innerHTML = '<p class="empty">Dashboard not found or not published.</p>';
    tweakPanel.hidden = true;
    return;
  }

  tweaksEnabled = !!data.tweaks_enabled;
  tweakLog.innerHTML = "";
  tweakPanel.hidden = !tweaksEnabled;
  tweakBody.hidden = true;

  const status = document.getElementById("dash-status");
  status.hidden = false;
  status.classList.add("live");
  status.textContent = "live";

  app.innerHTML = `
    <div class="dash-head">
      <h1>${esc(data.title)}</h1>
      <p>${esc(data.description || "")}</p>
    </div>
    <div class="chart-grid" id="charts"></div>`;
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

function renderKpi(el, data, spec) {
  const delta = data.delta != null && data.delta !== ""
    ? `<span class="kpi-delta ${String(data.delta).trim().startsWith("-") ? "down" : "up"}">${esc(String(data.delta))}</span>`
    : "";
  el.innerHTML = `
    <div class="kpi-row">
      <span class="kpi-value">${esc(String(data.value))}</span>
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
  return { responsive: true, displayModeBar: false };
}

// ───────────────────────────── tweak panel ─────────────────────────

tweakToggle.addEventListener("click", () => { tweakBody.hidden = !tweakBody.hidden; });

tweakSend.addEventListener("click", sendTweak);
tweakInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendTweak(); }
});

async function sendTweak() {
  const request = tweakInput.value.trim();
  if (!request || !currentSlug) return;
  tweakSend.disabled = true;
  addTweakMsg("user", request);
  tweakInput.value = "";
  try {
    const res = await api(`/api/dashboards/${encodeURIComponent(currentSlug)}/tweak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request }),
    });
    await pollTweak(res.tweak_id);
  } catch (e) {
    addTweakMsg("error", String(e.message || e));
    tweakSend.disabled = false;
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
        tweakSend.disabled = false;
        return;
      }
      if (t.status === "failed") {
        clearPendingMsg();
        addTweakMsg("error", t.error || "The request failed.");
        tweakSend.disabled = false;
        return;
      }
      if (Date.now() - started > 6 * 60 * 1000) {
        clearPendingMsg();
        addTweakMsg("error", "Timed out waiting for EpidBot.");
        tweakSend.disabled = false;
        return;
      }
      setTimeout(tick, 3000);
    } catch (e) {
      clearPendingMsg();
      addTweakMsg("error", String(e.message || e));
      tweakSend.disabled = false;
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
