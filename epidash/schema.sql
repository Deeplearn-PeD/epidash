-- EpiDash SQLite schema (applied idempotently at startup).

CREATE TABLE IF NOT EXISTS dashboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    layout TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS charts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dashboard_id INTEGER NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('line', 'bar', 'pie', 'kpi', 'table', 'choropleth')),
    title TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    spec TEXT NOT NULL DEFAULT '{}',
    position TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_charts_dashboard ON charts(dashboard_id);

CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chart_id INTEGER NOT NULL UNIQUE REFERENCES charts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('url_json', 'url_csv')),
    spec TEXT NOT NULL DEFAULT '{}',
    refresh_interval_s INTEGER NOT NULL DEFAULT 300,
    last_refreshed_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'pending' CHECK (last_status IN ('pending', 'ok', 'error')),
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS control_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    key_id INTEGER,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    params TEXT,
    result TEXT NOT NULL DEFAULT 'ok',
    error TEXT
);

CREATE TABLE IF NOT EXISTS tweaks (
    id TEXT PRIMARY KEY,
    dashboard_id INTEGER NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    request TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'tweak' CHECK (kind IN ('tweak', 'question')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    response TEXT,
    error TEXT,
    epidbot_job_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_tweaks_dashboard ON tweaks(dashboard_id);

CREATE TABLE IF NOT EXISTS view_counts (
    dashboard_id INTEGER PRIMARY KEY REFERENCES dashboards(id) ON DELETE CASCADE,
    views INTEGER NOT NULL DEFAULT 0,
    last_viewed_at TEXT
);

CREATE TABLE IF NOT EXISTS dashboard_likes (
    dashboard_id INTEGER PRIMARY KEY REFERENCES dashboards(id) ON DELETE CASCADE,
    likes INTEGER NOT NULL DEFAULT 0,
    last_liked_at TEXT
);
