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
let VIEWER = null; // {name, email} when logged in
// share token from ?t= — appended to API calls so restricted dashboards
// work from share links
const SHARE_TOKEN = new URLSearchParams(location.search).get("t") || "";

async function initViewer() {
  try {
    const me = await api("/auth/me");
    VIEWER = me.viewer;
    const chip = document.getElementById("auth-chip");
    const link = document.getElementById("auth-link");
    if (VIEWER) {
      link.textContent = VIEWER.name || VIEWER.email || "…";
      link.href = "/auth/logout";
      chip.dataset.i18nTitle = "sign_out";
      link.title = t("sign_out");
    } else if (me.login_enabled) {
      link.textContent = t("sign_in");
      link.href = "/auth/login";
    } else {
      chip.hidden = true;
    }
  } catch (_) { /* auth probe is best effort */ }
}

function withToken(path) {
  return SHARE_TOKEN
    ? path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(SHARE_TOKEN)
    : path;
}

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

// ───────────────────────── card metadata helpers ───────────────────

// Flag emoji for a stored dashboard language ("" when unknown).
function flagFor(lang) {
  const l = String(lang || "").toLowerCase();
  if (l.startsWith("pt")) return "🇧🇷";
  if (l.startsWith("en")) return "🇬🇧";
  return "";
}

// Unambiguous function/content words per language, for guessing the
// language of dashboards created before `language` was tracked.
const LANG_HINTS = {
  "pt-BR": new Set([
    "de", "da", "do", "das", "dos", "em", "no", "na", "nas", "nos",
    "com", "por", "para", "uma", "um", "que", "casos", "óbitos",
    "mortalidade", "semana", "saúde", "notificações", "vigilância",
    "febre", "brasileira", "brasileiro", "brasil",
  ]),
  en: new Set([
    "of", "the", "and", "for", "with", "cases", "deaths", "mortality",
    "week", "weekly", "health", "surveillance", "report", "fever",
    "brazil",
  ]),
};

function guessLanguage(text) {
  const words = String(text).toLowerCase().split(/[^\p{L}]+/u).filter(Boolean);
  let pt = 0, en = 0;
  for (const w of words) {
    if (LANG_HINTS["pt-BR"].has(w)) pt += 1;
    if (LANG_HINTS.en.has(w)) en += 1;
  }
  if (pt > en) return "pt-BR";
  if (en > pt) return "en";
  return "";
}

function formatDateTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(CURRENT_LANG, {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

// ───────────────────────────── home ────────────────────────────────

async function showHome() {
  currentSlug = null;
  currentDashboard = null;
  tweaksEnabled = false;
  tweakPanel.hidden = true;
  document.getElementById("dash-status").hidden = true;
  app.innerHTML =
    '<div class="home">' +
    `<div class="home-head"><h1>${t("home_title")}</h1>` +
    `<input id="dash-search" type="search" placeholder="${t("search_placeholder")}" aria-label="${t("search_placeholder")}"></div>` +
    '<div id="list" class="dash-grid"></div></div>';
  let dashboards;
  try {
    dashboards = await api("/api/dashboards");
  } catch (e) {
    app.innerHTML = `<p class="empty">${t("load_failed")}${esc(String(e))}</p>`;
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
      list.innerHTML = `<p class="empty">${dashboards.length ? t("no_match") : t("no_dashboards")}</p>`;
      return;
    }
    for (const d of matches) {
      const a = document.createElement("a");
      a.className = "dash-card";
      a.href = `#/d/${d.slug}`;
      const lang = d.language || guessLanguage(`${d.title} ${d.description || ""}`);
      const flag = flagFor(lang);
      const flagHtml = flag
        ? `<span class="dash-card-flag" title="${esc(lang)}" lang="${esc(lang)}">${flag}</span>`
        : "";
      const by = d.created_by
        ? `<span class="dash-card-by">${t("by")} ${esc(d.created_by)}</span>` : "";
      const when = d.created_at
        ? `<time class="dash-card-date" datetime="${esc(d.created_at)}" title="${t("created_at")}">${esc(formatDateTime(d.created_at))}</time>` : "";
      const left = [by, when].filter(Boolean).join('<span class="dash-card-sep">·</span>');
      a.innerHTML =
        `<h3>${flagHtml}${esc(d.title)}</h3><p>${esc(d.description || "")}</p>` +
        `<p class="dash-card-meta"><span class="dash-card-left">${left}</span><span>♥ ${d.likes ?? 0}</span></p>`;
      list.appendChild(a);
    }
  };
  search.addEventListener("input", paint);
  paint();
}

// ───────────────────────────── dashboard ───────────────────────────

async function showDashboard(slug) {
  currentSlug = slug;
  app.innerHTML = `<p class="empty">${t("loading")}</p>`;
  let data;
  try {
    data = await api(withToken(`/api/dashboards/${encodeURIComponent(slug)}`));
  } catch (e) {
    currentDashboard = null;
    if (e.status === 403) {
      app.innerHTML =
        `<p class="empty">${t("login_required")}</p>` +
        '<p class="empty"><a class="action-btn" href="/auth/login">' + t("login_cta") + '</a></p>';
    } else {
      app.innerHTML = `<p class="empty">${t("not_found")}</p>`;
    }
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
  status.textContent = t("live");

  const byLine = data.created_by ? `${t("by")} ${esc(data.created_by)}` : "";
  app.innerHTML = `
    <div class="dash-head">
      <a class="back-link" href="#/">${t("back")}</a>
      <div class="dash-title-row">
        <h1>${esc(data.title)}</h1>
        <div class="dash-actions">
          <button id="like-btn" class="action-btn like-btn" title="Like this dashboard">
            ${HEART_SVG}<span id="like-count">${data.likes ?? 0}</span>
          </button>
          <div class="export-wrap">
            <button id="export-btn" class="action-btn" title="${t("export_title")}">${t("export")}</button>
            <div id="export-menu" class="export-menu" hidden>
              <button data-export="png">${t("export_png")}</button>
              <button data-export="pdf">${t("export_pdf")}</button>
              <button data-export="json">${t("export_json")}</button>
            </div>
          </div>
          <button id="share-btn" class="action-btn" title="${t("share_title")}">${t("permalink")}</button>
        </div>
      </div>
      <p class="dash-byline">${byLine}</p>
      <p>${esc(data.description || "")}</p>
    </div>
    <div class="chart-grid" id="charts"></div>`;

  wireDashActions(slug, data.likes ?? 0, !!data.viewer_liked);
  const grid = document.getElementById("charts");

  const charts = [...data.charts].sort(
    (a, b) => (a.position.order ?? a.id) - (b.position.order ?? b.id)
  );
  for (const chart of charts) {
    const card = document.createElement("div");
    card.className = "chart-card" + (chart.position.width === "half" ? "" : " full");
    const srcBadge = chart.source
      ? `<span class="src-badge src-${esc(chart.source.status)}" title="Auto-updates every ${esc(String(chart.source.refresh_interval_s))}s · last refresh: ${esc(String(chart.source.last_refreshed_at || "never"))} (${esc(chart.source.status)})">⇣ auto</span>`
      : "";
    card.innerHTML = `<h2>${esc(chart.title || chart.kind)}${srcBadge}</h2><div class="plot"></div>`;
    grid.appendChild(card);
    try {
      renderChart(card.querySelector(".plot"), chart);
    } catch (e) {
      card.querySelector(".plot").innerHTML =
        `<p class="empty">${t("render_failed")}${esc(String(e))}</p>`;
    }
  }

  if (tweaksEnabled) pollForUpdates(data.updated_at);
}

function wireDashActions(slug, serverLikes, viewerLiked = false) {
  const likeBtn = document.getElementById("like-btn");
  const likeCount = document.getElementById("like-count");
  const shareBtn = document.getElementById("share-btn");
  let likes = serverLikes;
  let busy = false;

  const paint = (liked) => {
    likeCount.textContent = likes;
    likeBtn.classList.toggle("liked", liked);
    likeBtn.title = liked ? t("unlike") : t("like");
  };

  // logged-in viewers get server-side like state; anonymous viewers fall
  // back to a localStorage marker
  let liked = VIEWER ? viewerLiked : likedSet().has(slug);
  // reconcile: a stored like that the server no longer counts was reset
  if (liked && likes === 0) { setLiked(slug, false); liked = false; }
  paint(liked);

  likeBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    const goingToLike = !liked;
    likeCount.textContent = likes + (goingToLike ? 1 : -1); // optimistic
    try {
      const res = await api(withToken(`/api/dashboards/${encodeURIComponent(slug)}/like`), {
        method: goingToLike ? "POST" : "DELETE",
      });
      likes = res.likes;
      liked = goingToLike;
      if (!VIEWER) setLiked(slug, liked);
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
      shareBtn.textContent = t("copied");
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

const EXPORT_FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
const EXPORT_MARGIN = 40;
const EXPORT_HEADER_H = 118;
const EXPORT_MAX_H = 16000; // stay under browser canvas limits

async function exportPng() {
  const data = currentDashboard;
  const grid = document.getElementById("charts");
  if (!data || !grid) return;

  const gridRect = grid.getBoundingClientRect();
  const W = Math.round(gridRect.width) + EXPORT_MARGIN * 2;
  const H = Math.min(
    Math.round(gridRect.height) + EXPORT_HEADER_H + EXPORT_MARGIN,
    EXPORT_MAX_H
  );
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.textBaseline = "top";
  ctx.fillStyle = "#0d1117";
  ctx.fillRect(0, 0, W, H);

  // header
  let y = EXPORT_MARGIN;
  ctx.fillStyle = "#e6edf3";
  ctx.font = `700 26px ${EXPORT_FONT}`;
  ctx.fillText(data.title, EXPORT_MARGIN, y);
  y += 36;
  ctx.fillStyle = "#8b949e";
  ctx.font = `14px ${EXPORT_FONT}`;
  const by = data.created_by ? `by ${data.created_by} · ` : "";
  ctx.fillText(`${by}${new Date().toLocaleDateString()}`, EXPORT_MARGIN, y);
  if (data.description) {
    y += 24;
    ctx.fillText(String(data.description).slice(0, 200), EXPORT_MARGIN, y);
  }

  // DOM-position helpers: everything is painted 1:1 where it sits on the page
  const gridTop = EXPORT_MARGIN + EXPORT_HEADER_H;
  const rel = (el) => {
    const r = el.getBoundingClientRect();
    return {
      x: r.left - gridRect.left + EXPORT_MARGIN,
      y: r.top - gridRect.top + gridTop,
      w: r.width,
      h: r.height,
    };
  };

  for (const card of grid.querySelectorAll(".chart-card")) {
    const c = rel(card);
    if (c.y + c.h > H) break; // beyond canvas cap
    ctx.fillStyle = "#161b22";
    ctx.fillRect(c.x, c.y, c.w, c.h);
    ctx.strokeStyle = "#30363d";
    ctx.strokeRect(c.x + 0.5, c.y + 0.5, c.w - 1, c.h - 1);
    ctx.fillStyle = "#8b949e";
    ctx.font = `600 15px ${EXPORT_FONT}`;
    ctx.fillText(card.querySelector("h2").textContent, c.x + 18, c.y + 18);

    const plotEl = card.querySelector(".plot");
    if (plotEl && plotEl.data) {
      // rasterize the chart at its on-screen size (scale 2 for crispness)
      const p = rel(plotEl);
      const img = await loadImage(
        await Plotly.toImage(plotEl, {
          format: "png",
          width: Math.max(1, Math.round(p.w)),
          height: Math.max(1, Math.round(p.h)),
          scale: 2,
        })
      );
      ctx.drawImage(img, p.x, p.y, p.w, p.h);
      continue;
    }
    const kpi = card.querySelector(".kpi-row");
    const table = card.querySelector(".data-table");
    const md = card.querySelector(".markdown-body");
    if (kpi) drawKpiCard(ctx, kpi, rel);
    else if (table) drawTableCard(ctx, table, rel, H);
    else if (md) drawMarkdownCard(ctx, md, rel, H);
  }

  const a = document.createElement("a");
  a.href = canvas.toDataURL("image/png");
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

function drawKpiCard(ctx, kpiEl, rel) {
  const value = kpiEl.querySelector(".kpi-value");
  const unit = kpiEl.querySelector(".kpi-unit");
  const delta = kpiEl.querySelector(".kpi-delta");
  const label = kpiEl.parentElement.querySelector(".kpi-label");

  if (value) {
    const r = rel(value);
    ctx.fillStyle = "#4ade80";
    ctx.font = `700 40px ${EXPORT_FONT}`;
    ctx.fillText(value.textContent, r.x, r.y);
    let cx = r.x + ctx.measureText(value.textContent).width + 10;
    if (unit) {
      ctx.fillStyle = "#8b949e";
      ctx.font = `16px ${EXPORT_FONT}`;
      ctx.fillText(unit.textContent, cx, r.y + 14);
      cx += ctx.measureText(unit.textContent).width + 10;
    }
    if (delta) {
      ctx.fillStyle = delta.textContent.trim().startsWith("-") ? "#f85149" : "#4ade80";
      ctx.font = `600 14px ${EXPORT_FONT}`;
      ctx.fillText(delta.textContent, cx, r.y + 16);
    }
  }
  if (label) {
    const r = rel(label);
    ctx.fillStyle = "#8b949e";
    ctx.font = `13px ${EXPORT_FONT}`;
    ctx.fillText(label.textContent, r.x, r.y);
  }
}

function drawTableCard(ctx, tableEl, rel, canvasH) {
  const headCells = [...tableEl.querySelectorAll("thead th")];
  const rows = [...tableEl.querySelectorAll("tbody tr")];
  if (!headCells.length || !rows.length) return;

  // column geometry from the real header cells
  const cols = headCells.map((th) => {
    const r = rel(th);
    return { x: r.x, y: r.y, w: r.w, label: th.textContent };
  });
  const rowH = rel(rows[0]).h || 34;

  ctx.font = `600 13px ${EXPORT_FONT}`;
  cols.forEach((col) => {
    ctx.fillStyle = "#8b949e";
    drawClipped(ctx, col.label, col.x + 10, cols[0].y + 9, col.w);
  });

  ctx.font = `13px ${EXPORT_FONT}`;
  for (const tr of rows) {
    const ry = rel(tr).y;
    if (ry > canvasH - 40) break;
    [...tr.querySelectorAll("td")].forEach((td, i) => {
      if (i >= cols.length) return;
      ctx.fillStyle = "#e6edf3";
      drawClipped(ctx, td.textContent, cols[i].x + 10, ry + 9, cols[i].w);
    });
  }
}

function drawClipped(ctx, text, x, y, maxW) {
  let t = String(text);
  if (ctx.measureText(t).width > maxW - 12) {
    while (t.length > 1 && ctx.measureText(t + "…").width > maxW - 12) t = t.slice(0, -1);
    t += "…";
  }
  ctx.fillText(t, x, y);
}

function wrapText(ctx, text, maxW) {
  const out = [];
  let cur = "";
  for (const w of String(text).split(/\s+/).filter(Boolean)) {
    const test = cur ? cur + " " + w : w;
    if (ctx.measureText(test).width > maxW && cur) { out.push(cur); cur = w; }
    else cur = test;
  }
  if (cur) out.push(cur);
  return out;
}

function drawMarkdownCard(ctx, mdEl, rel, canvasH) {
  // markdown is exported as wrapped plain text (documented limitation)
  const r = rel(mdEl);
  ctx.font = `14px ${EXPORT_FONT}`;
  let y = r.y;
  for (const raw of String(mdEl.innerText || "").split("\n")) {
    const line = raw.trimEnd();
    if (!line) { y += 10; continue; }
    for (const piece of wrapText(ctx, line, r.w)) {
      if (y > canvasH - 30) return;
      ctx.fillStyle = "#e6edf3";
      ctx.fillText(piece, r.x, y);
      y += 20;
    }
  }
}

async function pollForUpdates(since) {
  stopTweakPolling();
  const slug = currentSlug;
  const tick = async () => {
    if (currentSlug !== slug) return;
    try {
      const head = await api(withToken(`/api/dashboards/${encodeURIComponent(slug)}`));
      if (head.updated_at !== since && currentSlug === slug) {
        await showDashboard(slug);
        addTweakMsg("agent", t("updated"));
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

// Plotly (4.8 MB) is loaded on demand: home/gallery pages never fetch it.
let plotlyPromise = null;
function ensurePlotly() {
  if (window.Plotly) return Promise.resolve();
  if (!plotlyPromise) {
    plotlyPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/vendor/plotly.min.js";
      s.onload = () => resolve();
      s.onerror = () => {
        plotlyPromise = null;
        reject(new Error("Failed to load the charting library"));
      };
      document.head.appendChild(s);
    });
  }
  return plotlyPromise;
}

function renderChart(el, chart) {
  const { kind, data, spec = {} } = chart;
  if (kind === "kpi") return renderKpi(el, data, spec);
  if (kind === "table") return renderTable(el, data, spec);
  if (kind === "choropleth") {
    el.classList.add("plot-aspect", "plot-aspect-geo");
    return renderChoropleth(el, data, spec);
  }
  if (kind === "markdown") return renderMarkdown(el, data);
  // line/bar/pie go through Plotly: keep a pleasant, predictable
  // aspect ratio instead of stretching to the card width
  el.classList.add("plot-aspect");
  el.innerHTML = `<p class="empty">${t("loading_chart")}</p>`;
  ensurePlotly()
    .then(() => renderPlotlyChart(el, kind, data, spec))
    .catch((e) => {
      el.innerHTML = `<p class="empty">${t("render_failed")}${esc(String(e))}</p>`;
    });
}

function renderMarkdownText(text) {
  // All markdown passes DOMPurify before touching the DOM.
  return DOMPurify.sanitize(marked.parse(String(text || "")));
}

function renderMarkdown(el, data) {
  el.innerHTML = `<div class="markdown-body">${renderMarkdownText(data.text)}</div>`;
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
  Promise.all([
    fetch("/geo/br-states.json").then(r => r.json()),
    ensurePlotly(),
  ]).then(([geojson]) => {
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
  addTweakMsg("agent", t("working"), true);
  const started = Date.now();
  const tick = async () => {
    try {
      const t = await api(`/api/tweaks/${tweakId}`);
      if (t.status === "completed") {
        clearPendingMsg();
        addTweakMsg("agent", t.response || t("done"));
        setSendBusy(false);
        return;
      }
      if (t.status === "failed") {
        clearPendingMsg();
        addTweakMsg("error", t.error || t("request_failed"));
        setSendBusy(false);
        return;
      }
      if (Date.now() - started > 6 * 60 * 1000) {
        clearPendingMsg();
        addTweakMsg("error", t("timeout"));
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
  const who = { user: t("who_user"), agent: t("who_agent"), error: t("who_error") }[kind] || kind;
  // EpidBot replies are markdown; user input and errors stay plain text.
  const body = kind === "agent" ? renderMarkdownText(text) : esc(text);
  div.innerHTML = `<span class="who">${who}</span>${body}`;
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

initViewer();
route();
