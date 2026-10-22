"""SQLite persistence layer.

Plain sqlite3 with per-call connections (WAL mode): cheap, safe across
threads (handlers run via anyio worker threads and asyncio tasks), and
good enough for a single-node, light dashboard service.
"""

import json
import logging
import os
import secrets
import sqlite3
import hashlib
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("epidash.store")

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

CHART_KINDS = ("line", "bar", "pie", "kpi", "table", "choropleth")


def hash_key(raw_key: str) -> str:
    """SHA-256 of the raw control key (the plaintext is never stored)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _loads(raw: Any) -> Any:
    if raw is None or raw == "":
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


class Store:
    """All database access for EpiDash."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_schema()

    @contextmanager
    def conn(self):
        """Short-lived connection with WAL and foreign keys enabled."""
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init_schema(self) -> None:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            schema = f.read()
        with self.conn() as con:
            con.executescript(schema)
            self._migrate(con)

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """Idempotent in-place migrations for databases created before a
        schema change (CREATE TABLE IF NOT EXISTS alone won't add columns)."""
        columns = {r["name"] for r in con.execute("PRAGMA table_info(dashboards)")}
        if "created_by" not in columns:
            con.execute(
                "ALTER TABLE dashboards ADD COLUMN created_by TEXT NOT NULL DEFAULT ''"
            )
        columns = {r["name"] for r in con.execute("PRAGMA table_info(tweaks)")}
        if "kind" not in columns:
            # CHECK constraints can't be added via ALTER TABLE; plain column.
            con.execute(
                "ALTER TABLE tweaks ADD COLUMN kind TEXT NOT NULL DEFAULT 'tweak'"
            )

    # ───────────────────────── dashboards ─────────────────────────

    def create_dashboard(
        self,
        title: str,
        description: str = "",
        slug: str | None = None,
        layout: dict | None = None,
        created_by: str = "",
    ) -> dict:
        slug = slug or secrets.token_urlsafe(9)
        with self.conn() as con:
            try:
                cur = con.execute(
                    "INSERT INTO dashboards (slug, title, description, layout, created_by)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (slug, title, description, json.dumps(layout or {}), created_by),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"slug '{slug}' already exists") from e
            row = con.execute(
                "SELECT * FROM dashboards WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self.dashboard_out(_row_to_dict(row))

    def get_dashboard(self, dashboard_id: int) -> dict | None:
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM dashboards WHERE id = ?", (dashboard_id,)
            ).fetchone()
        return self.dashboard_out(_row_to_dict(row)) if row else None

    def get_dashboard_by_slug(self, slug: str) -> dict | None:
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM dashboards WHERE slug = ?", (slug,)
            ).fetchone()
        return self.dashboard_out(_row_to_dict(row)) if row else None

    def list_dashboards(self, include_unpublished: bool = True) -> list[dict]:
        query = "SELECT * FROM dashboards"
        if not include_unpublished:
            query += " WHERE status = 'published'"
        query += " ORDER BY updated_at DESC, id DESC"
        with self.conn() as con:
            rows = con.execute(query).fetchall()
        return [self.dashboard_out(_row_to_dict(r)) for r in rows]

    def update_dashboard(
        self,
        dashboard_id: int,
        title: str | None = None,
        description: str | None = None,
        layout: dict | None = None,
        created_by: str | None = None,
    ) -> dict | None:
        sets, args = ["updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"], []
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if description is not None:
            sets.append("description = ?")
            args.append(description)
        if layout is not None:
            sets.append("layout = ?")
            args.append(json.dumps(layout))
        if created_by is not None:
            sets.append("created_by = ?")
            args.append(created_by)
        args.append(dashboard_id)
        with self.conn() as con:
            cur = con.execute(
                f"UPDATE dashboards SET {', '.join(sets)} WHERE id = ?", args
            )
            if cur.rowcount == 0:
                return None
            row = con.execute(
                "SELECT * FROM dashboards WHERE id = ?", (dashboard_id,)
            ).fetchone()
        return self.dashboard_out(_row_to_dict(row))

    def set_dashboard_status(self, dashboard_id: int, status: str) -> dict | None:
        published_at = (
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')" if status == "published" else "NULL"
        )
        with self.conn() as con:
            cur = con.execute(
                "UPDATE dashboards SET status = ?, published_at = "
                f"COALESCE({published_at}, published_at), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (status, dashboard_id),
            )
            if cur.rowcount == 0:
                return None
            row = con.execute(
                "SELECT * FROM dashboards WHERE id = ?", (dashboard_id,)
            ).fetchone()
        return self.dashboard_out(_row_to_dict(row))

    def delete_dashboard(self, dashboard_id: int) -> dict | None:
        """Permanently remove a dashboard and all of its charts.

        Returns the deleted dashboard document, or None when the id is
        unknown. Charts are removed explicitly (foreign keys are ON, but
        being explicit keeps the deletion self-evident).
        """
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM dashboards WHERE id = ?", (dashboard_id,)
            ).fetchone()
            if row is None:
                return None
            con.execute(
                "DELETE FROM charts WHERE dashboard_id = ?", (dashboard_id,)
            )
            con.execute("DELETE FROM dashboards WHERE id = ?", (dashboard_id,))
        return self.dashboard_out(_row_to_dict(row))

    # ───────────────────────── charts ─────────────────────────

    def add_chart(
        self,
        dashboard_id: int,
        kind: str,
        title: str = "",
        data: dict | None = None,
        spec: dict | None = None,
        position: dict | None = None,
    ) -> dict:
        if kind not in CHART_KINDS:
            raise ValueError(f"invalid chart kind '{kind}'")
        with self.conn() as con:
            exists = con.execute(
                "SELECT 1 FROM dashboards WHERE id = ?", (dashboard_id,)
            ).fetchone()
            if not exists:
                raise ValueError(f"dashboard {dashboard_id} does not exist")
            cur = con.execute(
                "INSERT INTO charts (dashboard_id, kind, title, data, spec, position)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dashboard_id,
                    kind,
                    title,
                    json.dumps(data or {}),
                    json.dumps(spec or {}),
                    json.dumps(position or {}),
                ),
            )
            row = con.execute(
                "SELECT * FROM charts WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self.chart_out(_row_to_dict(row))

    def get_chart(self, chart_id: int) -> dict | None:
        with self.conn() as con:
            row = con.execute("SELECT * FROM charts WHERE id = ?", (chart_id,)).fetchone()
        return self.chart_out(_row_to_dict(row)) if row else None

    def list_charts(self, dashboard_id: int) -> list[dict]:
        with self.conn() as con:
            rows = con.execute(
                "SELECT * FROM charts WHERE dashboard_id = ? ORDER BY id", (dashboard_id,)
            ).fetchall()
        return [self.chart_out(_row_to_dict(r)) for r in rows]

    def update_chart(
        self,
        chart_id: int,
        kind: str | None = None,
        title: str | None = None,
        data: dict | None = None,
        spec: dict | None = None,
        position: dict | None = None,
    ) -> dict | None:
        sets, args = ["updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"], []
        if kind is not None:
            if kind not in CHART_KINDS:
                raise ValueError(f"invalid chart kind '{kind}'")
            sets.append("kind = ?")
            args.append(kind)
        for column, value in (("title", title), ("data", data), ("spec", spec), ("position", position)):
            if value is not None:
                sets.append(f"{column} = ?")
                args.append(value if isinstance(value, str) else json.dumps(value))
        args.append(chart_id)
        with self.conn() as con:
            cur = con.execute(f"UPDATE charts SET {', '.join(sets)} WHERE id = ?", args)
            if cur.rowcount == 0:
                return None
            row = con.execute("SELECT * FROM charts WHERE id = ?", (chart_id,)).fetchone()
        return self.chart_out(_row_to_dict(row))

    def remove_chart(self, chart_id: int) -> dict | None:
        chart = self.get_chart(chart_id)
        if chart is None:
            return None
        with self.conn() as con:
            con.execute("DELETE FROM charts WHERE id = ?", (chart_id,))
            con.execute(
                "UPDATE dashboards SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE id = ?",
                (chart["dashboard_id"],),
            )
        return chart

    # ───────────────────────── view counts ─────────────────────────

    def record_view(self, dashboard_id: int) -> None:
        with self.conn() as con:
            con.execute(
                "INSERT INTO view_counts (dashboard_id, views, last_viewed_at)"
                " VALUES (?, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                " ON CONFLICT(dashboard_id) DO UPDATE SET"
                " views = views + 1,"
                " last_viewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (dashboard_id,),
            )

    def get_view_count(self, dashboard_id: int) -> int:
        with self.conn() as con:
            row = con.execute(
                "SELECT views FROM view_counts WHERE dashboard_id = ?", (dashboard_id,)
            ).fetchone()
        return row["views"] if row else 0

    # ───────────────────────── likes ───────────────────────────────

    def _adjust_likes(self, dashboard_id: int, delta: int) -> int:
        """Apply a like delta and return the new count (floored at 0)."""
        now = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        with self.conn() as con:
            cur = con.execute(
                f"UPDATE dashboard_likes SET likes = MAX(likes + ?, 0),"
                f" last_liked_at = {now} WHERE dashboard_id = ?",
                (delta, dashboard_id),
            )
            if cur.rowcount == 0:
                con.execute(
                    f"INSERT INTO dashboard_likes (dashboard_id, likes, last_liked_at)"
                    f" VALUES (?, MAX(?, 0), {now})",
                    (dashboard_id, delta),
                )
            row = con.execute(
                "SELECT likes FROM dashboard_likes WHERE dashboard_id = ?",
                (dashboard_id,),
            ).fetchone()
        return row["likes"]

    def record_like(self, dashboard_id: int) -> int:
        return self._adjust_likes(dashboard_id, +1)

    def remove_like(self, dashboard_id: int) -> int:
        return self._adjust_likes(dashboard_id, -1)

    def get_likes(self, dashboard_id: int) -> int:
        with self.conn() as con:
            row = con.execute(
                "SELECT likes FROM dashboard_likes WHERE dashboard_id = ?",
                (dashboard_id,),
            ).fetchone()
        return row["likes"] if row else 0

    # ───────────────────────── control keys ─────────────────────────

    def add_key(self, raw_key: str, name: str) -> dict:
        prefix = raw_key[:8]
        with self.conn() as con:
            cur = con.execute(
                "INSERT INTO control_keys (name, key_hash, key_prefix) VALUES (?, ?, ?)",
                (name, hash_key(raw_key), prefix),
            )
            row = con.execute(
                "SELECT id, name, key_prefix, created_at, revoked FROM control_keys"
                " WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return _row_to_dict(row)

    def verify_key(self, raw_key: str) -> dict | None:
        """Return key metadata ({id, name, key_prefix}) when valid and live."""
        if not raw_key.startswith("dk_"):
            return None
        with self.conn() as con:
            row = con.execute(
                "SELECT id, name, key_prefix, revoked FROM control_keys"
                " WHERE key_hash = ?",
                (hash_key(raw_key),),
            ).fetchone()
        if row is None or row["revoked"]:
            return None
        try:
            with self.conn() as con:
                con.execute(
                    "UPDATE control_keys SET last_used_at ="
                    " strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                    (row["id"],),
                )
        except Exception as e:  # noqa: BLE001 — last-used stamping is best effort
            logger.debug("Failed to stamp key last_used_at: %s", e)
        return {"key_id": row["id"], "name": row["name"], "key_prefix": row["key_prefix"]}

    def list_keys(self) -> list[dict]:
        with self.conn() as con:
            rows = con.execute(
                "SELECT id, name, key_prefix, created_at, last_used_at, revoked"
                " FROM control_keys ORDER BY id"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def revoke_key(self, key_id: int) -> bool:
        with self.conn() as con:
            cur = con.execute(
                "UPDATE control_keys SET revoked = 1 WHERE id = ?", (key_id,)
            )
        return cur.rowcount > 0

    # ───────────────────────── audit log ─────────────────────────

    def log_audit(
        self,
        key_id: int | None,
        action: str,
        target_type: str | None = None,
        target_id: Any = None,
        params: dict | None = None,
        result: str = "ok",
        error: str | None = None,
    ) -> None:
        try:
            with self.conn() as con:
                con.execute(
                    "INSERT INTO audit_log (key_id, action, target_type, target_id,"
                    " params, result, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key_id,
                        action,
                        target_type,
                        str(target_id) if target_id is not None else None,
                        json.dumps(params) if params else None,
                        result,
                        error,
                    ),
                )
        except Exception as e:  # noqa: BLE001 — audit must never break tools
            logger.error("Audit write failed for %s: %s", action, e)

    def list_audit(self, limit: int = 100) -> list[dict]:
        with self.conn() as con:
            rows = con.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["params"] = _loads(d.get("params"))
            out.append(d)
        return out

    # ───────────────────────── tweaks ─────────────────────────

    def create_tweak(self, dashboard_id: int, request: str, kind: str = "tweak") -> dict:
        tweak_id = secrets.token_urlsafe(12)
        with self.conn() as con:
            con.execute(
                "INSERT INTO tweaks (id, dashboard_id, request, kind) VALUES (?, ?, ?, ?)",
                (tweak_id, dashboard_id, request, kind),
            )
            row = con.execute(
                "SELECT * FROM tweaks WHERE id = ?", (tweak_id,)
            ).fetchone()
        return self.tweak_out(_row_to_dict(row))

    def get_tweak(self, tweak_id: str) -> dict | None:
        with self.conn() as con:
            row = con.execute("SELECT * FROM tweaks WHERE id = ?", (tweak_id,)).fetchone()
        return self.tweak_out(_row_to_dict(row)) if row else None

    def update_tweak(self, tweak_id: str, **fields: Any) -> dict | None:
        allowed = {"status", "response", "error", "epidbot_job_id"}
        sets, args = ["updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"], []
        for name, value in fields.items():
            if name not in allowed or value is None:
                continue
            sets.append(f"{name} = ?")
            args.append(value)
        args.append(tweak_id)
        with self.conn() as con:
            con.execute(f"UPDATE tweaks SET {', '.join(sets)} WHERE id = ?", args)
            row = con.execute("SELECT * FROM tweaks WHERE id = ?", (tweak_id,)).fetchone()
        return self.tweak_out(_row_to_dict(row)) if row else None

    def count_recent_tweaks(self, dashboard_id: int, since_iso: str) -> int:
        with self.conn() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM tweaks"
                " WHERE dashboard_id = ? AND created_at >= ?",
                (dashboard_id, since_iso),
            ).fetchone()
        return row["n"] if row else 0

    # ───────────────────────── output shaping ─────────────────────────

    @staticmethod
    def dashboard_out(row: dict) -> dict:
        row["layout"] = _loads(row.get("layout"))
        return row

    @staticmethod
    def chart_out(row: dict) -> dict:
        for key in ("data", "spec", "position"):
            row[key] = _loads(row.get(key))
        return row

    @staticmethod
    def tweak_out(row: dict) -> dict:
        return row
