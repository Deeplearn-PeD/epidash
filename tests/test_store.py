"""Store layer: dashboards, charts, keys, audit, tweaks, view counts."""

import pytest

from epidash.store import Store


class TestDashboards:
    """Dashboard CRUD and status transitions."""

    def test_create_returns_slug_and_draft_status(self, store):
        d = store.create_dashboard("Dengue SP", description="cases")
        assert d["id"] > 0
        assert d["slug"]
        assert d["status"] == "draft"
        assert d["title"] == "Dengue SP"
        assert d["layout"] == {}

    def test_slug_conflict_raises_value_error(self, store):
        store.create_dashboard("A", slug="fixed-slug")
        with pytest.raises(ValueError, match="already exists"):
            store.create_dashboard("B", slug="fixed-slug")

    def test_get_by_slug_and_id(self, store):
        d = store.create_dashboard("T", slug="abc")
        assert store.get_dashboard(d["id"])["slug"] == "abc"
        assert store.get_dashboard_by_slug("abc")["id"] == d["id"]
        assert store.get_dashboard_by_slug("nope") is None

    def test_update_partial_fields(self, store):
        d = store.create_dashboard("T", slug="u1")
        out = store.update_dashboard(d["id"], title="T2")
        assert out["title"] == "T2"
        assert out["description"] == ""
        assert store.update_dashboard(999, title="X") is None

    def test_language_round_trip(self, store):
        d = store.create_dashboard("Casos de dengue", language="pt-BR")
        assert d["language"] == "pt-BR"
        # dashboards created without one default to unknown
        assert store.create_dashboard("Weekly cases")["language"] == ""
        out = store.update_dashboard(d["id"], language="en")
        assert out["language"] == "en"
        # updating unrelated fields leaves the language untouched
        assert store.update_dashboard(d["id"], title="T2")["language"] == "en"

    def test_status_filter(self, store):
        pub = store.create_dashboard("Pub", slug="pub")
        store.create_dashboard("Draft", slug="draft")
        store.set_dashboard_status(pub["id"], "published")
        slugs = {d["slug"] for d in store.list_dashboards(include_unpublished=False)}
        assert slugs == {"pub"}
        assert len(store.list_dashboards(include_unpublished=True)) == 2

    def test_publish_then_unpublish_keeps_published_at_history(self, store):
        d = store.create_dashboard("T", slug="pu")
        published = store.set_dashboard_status(d["id"], "published")
        assert published["status"] == "published"
        assert published["published_at"]
        unpublished = store.set_dashboard_status(d["id"], "draft")
        assert unpublished["status"] == "draft"
        assert unpublished["published_at"] == published["published_at"]


class TestCharts:
    """Chart CRUD inside dashboards."""

    def test_add_and_get_chart(self, store):
        d = store.create_dashboard("D")
        c = store.add_chart(
            d["id"], "line", title="cases",
            data={"columns": ["m", "v"], "rows": [["Jan", 1]]},
            spec={"color": "#fff"}, position={"order": 1},
        )
        assert c["kind"] == "line"
        assert c["data"] == {"columns": ["m", "v"], "rows": [["Jan", 1]]}
        assert store.get_chart(c["id"])["id"] == c["id"]

    def test_add_chart_invalid_kind(self, store):
        d = store.create_dashboard("D")
        with pytest.raises(ValueError, match="invalid chart kind"):
            store.add_chart(d["id"], "hologram")

    def test_add_chart_missing_dashboard(self, store):
        with pytest.raises(ValueError, match="does not exist"):
            store.add_chart(999, "line")

    def test_update_chart(self, store):
        d = store.create_dashboard("D")
        c = store.add_chart(d["id"], "bar", data={"columns": ["a"], "rows": [[1]]})
        out = store.update_chart(c["id"], title="renamed", data={"columns": ["b"], "rows": [[2]]})
        assert out["title"] == "renamed"
        assert out["data"]["columns"] == ["b"]
        assert out["kind"] == "bar"
        assert store.update_chart(999, title="X") is None

    def test_remove_chart(self, store):
        d = store.create_dashboard("D")
        c = store.add_chart(d["id"], "kpi", data={"value": 1})
        removed = store.remove_chart(c["id"])
        assert removed["id"] == c["id"]
        assert store.get_chart(c["id"]) is None
        assert store.remove_chart(c["id"]) is None

    def test_list_charts_orders_by_id(self, store):
        d = store.create_dashboard("D")
        c1 = store.add_chart(d["id"], "kpi", data={"value": 1})
        c2 = store.add_chart(d["id"], "kpi", data={"value": 2})
        assert [c["id"] for c in store.list_charts(d["id"])] == [c1["id"], c2["id"]]


class TestControlKeys:
    """dk_ key hashing, verification and revocation."""

    def test_add_and_verify_key(self, store):
        meta = store.add_key("dk_abcdef1234567890", name="test")
        assert meta["key_prefix"] == "dk_abcde"
        assert meta["name"] == "test"
        verified = store.verify_key("dk_abcdef1234567890")
        assert verified is not None
        assert verified["key_id"] == meta["id"]

    def test_verify_rejects_wrong_or_malformed_keys(self, store):
        store.add_key("dk_goodkey", name="k")
        assert store.verify_key("dk_badkey") is None
        assert store.verify_key("ek_not-a-dashboard-key") is None
        assert store.verify_key("") is None

    def test_revoked_key_fails_verification(self, store):
        meta = store.add_key("dk_revokeme", name="k")
        assert store.revoke_key(meta["id"]) is True
        assert store.verify_key("dk_revokeme") is None
        assert store.revoke_key(meta["id"]) is True
        assert store.revoke_key(999) is False

    def test_list_keys_never_contains_hashes_or_plaintext(self, store):
        store.add_key("dk_secret", name="k")
        keys = store.list_keys()
        assert "key_hash" not in keys[0]
        assert "dk_secret" not in str(keys)


class TestAudit:
    """Audit rows are best-effort but must land for valid stores."""

    def test_log_and_list(self, store):
        store.log_audit(7, "create_dashboard", target_type="dashboard",
                        target_id=3, params={"title": "T"})
        rows = store.list_audit()
        assert rows[0]["action"] == "create_dashboard"
        assert rows[0]["key_id"] == 7
        assert rows[0]["params"] == {"title": "T"}

    def test_log_never_raises(self, store):
        store.log_audit(None, "weird", params={"x": object()})  # json may fail → still no raise


class TestTweaks:
    """Tweak lifecycle rows."""

    def test_create_update_get(self, store):
        d = store.create_dashboard("D")
        t = store.create_tweak(d["id"], "make it red")
        assert t["status"] == "pending"
        out = store.update_tweak(t["id"], status="completed", response="done")
        assert out["status"] == "completed"
        assert out["response"] == "done"
        assert store.get_tweak(t["id"])["response"] == "done"
        assert store.get_tweak("missing") is None

    def test_update_ignores_unknown_fields_and_none(self, store):
        d = store.create_dashboard("D")
        t = store.create_tweak(d["id"], "req")
        out = store.update_tweak(t["id"], status=None, hacker="x", response="r")
        assert out["status"] == "pending"
        assert out["response"] == "r"

    def test_count_recent(self, store):
        d = store.create_dashboard("D")
        store.create_tweak(d["id"], "a")
        store.create_tweak(d["id"], "b")
        assert store.count_recent_tweaks(d["id"], "2000-01-01") == 2


class TestViewCounts:
    """View counters upsert correctly."""

    def test_record_and_read(self, store):
        d = store.create_dashboard("D")
        store.record_view(d["id"])
        store.record_view(d["id"])
        assert store.get_view_count(d["id"]) == 2
        assert store.get_view_count(999) == 0


class TestLikes:
    """Like counters upsert, floor at zero, and cascade cleanly."""

    def test_record_and_undo(self, store):
        d = store.create_dashboard("D")
        assert store.record_like(d["id"]) == 1
        assert store.record_like(d["id"]) == 2
        assert store.remove_like(d["id"]) == 1
        assert store.get_likes(d["id"]) == 1

    def test_remove_never_goes_negative(self, store):
        d = store.create_dashboard("D")
        assert store.remove_like(d["id"]) == 0
        assert store.remove_like(d["id"]) == 0

    def test_new_dashboard_has_zero_likes(self, store):
        d = store.create_dashboard("D")
        assert store.get_likes(d["id"]) == 0


class TestTweakKinds:
    """Tweaks and viewer questions share the table but carry a kind."""

    def test_default_kind_is_tweak(self, store):
        d = store.create_dashboard("D")
        t = store.create_tweak(d["id"], "make it red")
        assert t["kind"] == "tweak"

    def test_question_kind_persisted(self, store):
        d = store.create_dashboard("D")
        t = store.create_tweak(d["id"], "what is this?", kind="question")
        assert t["kind"] == "question"
        assert store.get_tweak(t["id"])["kind"] == "question"


class TestDataSources:
    """Per-chart auto-refresh sources: upsert, status, due query."""

    def _chart(self, store):
        d = store.create_dashboard("D")
        return store.add_chart(d["id"], "line", title="t")

    def test_attach_and_get(self, store):
        c = self._chart(store)
        s = store.set_data_source(
            c["id"], "url_json", {"url": "https://x.test/d.json"}, 120
        )
        assert s["chart_id"] == c["id"]
        assert s["kind"] == "url_json"
        assert s["refresh_interval_s"] == 120
        assert s["last_status"] == "pending"
        assert store.get_data_source(c["id"])["id"] == s["id"]

    def test_upsert_resets_status(self, store):
        c = self._chart(store)
        store.set_data_source(c["id"], "url_json", {"url": "https://x.test/a"})
        store.record_data_source_refresh(c["id"], "ok")
        assert store.get_data_source(c["id"])["last_status"] == "ok"
        store.set_data_source(c["id"], "url_csv", {"url": "https://x.test/b"})
        s = store.get_data_source(c["id"])
        assert s["kind"] == "url_csv"
        assert s["last_status"] == "pending" and s["last_refreshed_at"] is None

    def test_validation(self, store):
        c = self._chart(store)
        with pytest.raises(ValueError, match="invalid data source kind"):
            store.set_data_source(c["id"], "rss", {"url": "u"})
        with pytest.raises(ValueError, match="url"):
            store.set_data_source(c["id"], "url_json", {})
        with pytest.raises(ValueError, match="at least 30"):
            store.set_data_source(c["id"], "url_json", {"url": "u"}, 10)
        with pytest.raises(ValueError, match="does not exist"):
            store.set_data_source(999, "url_json", {"url": "u"})

    def test_due_sources_and_refresh_bookkeeping(self, store):
        c = self._chart(store)
        store.set_data_source(c["id"], "url_json", {"url": "u"}, 60)
        assert [s["chart_id"] for s in store.due_data_sources()] == [c["id"]]
        store.record_data_source_refresh(c["id"], "ok")
        assert store.due_data_sources() == []
        assert store.get_data_source(c["id"])["last_status"] == "ok"
        assert store.get_data_source(c["id"])["last_refreshed_at"]

    def test_error_status_keeps_error_text(self, store):
        c = self._chart(store)
        store.set_data_source(c["id"], "url_json", {"url": "u"})
        store.record_data_source_refresh(c["id"], "error", error="HTTP 500")
        s = store.get_data_source(c["id"])
        assert s["last_status"] == "error" and s["last_error"] == "HTTP 500"

    def test_delete_and_cascade(self, store):
        c = self._chart(store)
        store.set_data_source(c["id"], "url_json", {"url": "u"})
        assert store.delete_data_source(c["id"])
        assert store.get_data_source(c["id"]) is None
        assert not store.delete_data_source(c["id"])
        # removing the chart cascades the source away
        store.set_data_source(c["id"], "url_json", {"url": "u"})
        store.remove_chart(c["id"])
        assert store.get_data_source(c["id"]) is None

    def test_update_chart_data_bumps_dashboard(self, store):
        d = store.create_dashboard("D")
        c = store.add_chart(d["id"], "line", title="t")
        before = store.get_dashboard(d["id"])["updated_at"]
        out = store.update_chart_data(c["id"], {"columns": ["x"], "rows": [[1]]})
        assert out["data"]["rows"] == [[1]]
        assert store.get_dashboard(d["id"])["updated_at"] >= before


class TestDuplicateDashboard:
    def test_copies_charts_and_sources(self, store):
        d = store.create_dashboard("Dengue", created_by="EpidBot")
        c1 = store.add_chart(d["id"], "line", title="cases", spec={"color": "#fff"},
                             data={"columns": ["m", "v"], "rows": [["Jan", 1]]})
        store.add_chart(d["id"], "kpi", title="total", data={"value": 5})
        store.set_data_source(c1["id"], "url_json", {"url": "https://x.test/d"}, 90)
        store.set_dashboard_status(d["id"], "published")

        copy = store.duplicate_dashboard(d["id"])
        assert copy["status"] == "draft"
        assert copy["title"] == "Dengue (copy)"
        assert copy["slug"] != d["slug"]
        charts = store.list_charts(copy["id"])
        assert [c["kind"] for c in charts] == ["line", "kpi"]
        assert charts[0]["spec"] == {"color": "#fff"}
        src = store.get_data_source(charts[0]["id"])
        assert src["spec"]["url"] == "https://x.test/d" and src["refresh_interval_s"] == 90

    def test_overrides(self, store):
        d = store.create_dashboard("D", created_by="EpidBot")
        copy = store.duplicate_dashboard(
            d["id"], new_title="Variant", new_slug="variant", created_by="fccoelho"
        )
        assert copy["title"] == "Variant"
        assert copy["slug"] == "variant"
        assert copy["created_by"] == "fccoelho"

    def test_missing_returns_none(self, store):
        assert store.duplicate_dashboard(999) is None


class TestStyleCharts:
    def test_merges_fragment_into_all_charts(self, store):
        d = store.create_dashboard("D")
        c1 = store.add_chart(d["id"], "line", title="a", spec={"y_label": "casos"})
        c2 = store.add_chart(d["id"], "bar", title="b")
        n = store.style_charts(d["id"], {"color": "#4ade80"})
        assert n == 2
        assert store.get_chart(c1["id"])["spec"] == {"y_label": "casos", "color": "#4ade80"}
        assert store.get_chart(c2["id"])["spec"] == {"color": "#4ade80"}

    def test_empty_spec_rejected(self, store):
        d = store.create_dashboard("D")
        with pytest.raises(ValueError):
            store.style_charts(d["id"], {})


class TestAuditFilters:
    def test_filter_by_action_and_dashboard(self, store):
        d1 = store.create_dashboard("A", slug="a")
        d2 = store.create_dashboard("B", slug="b")
        store.log_audit(1, "create_dashboard", target_type="dashboard", target_id=d1["id"])
        store.log_audit(1, "publish_dashboard", target_type="dashboard", target_id=d1["id"])
        store.log_audit(1, "create_dashboard", target_type="dashboard", target_id=d2["id"])

        rows = store.list_audit(action="create_dashboard")
        assert len(rows) == 2
        rows = store.list_audit(dashboard_id=d1["id"])
        assert {r["action"] for r in rows} == {"create_dashboard", "publish_dashboard"}
        rows = store.list_audit(action="publish_dashboard", dashboard_id=d2["id"])
        assert rows == []


class TestCreatorAttribution:
    """created_by is stored at creation and survives re-reads."""

    def test_create_records_creator(self, store):
        d = store.create_dashboard("D", created_by="EpidBot")
        assert d["created_by"] == "EpidBot"
        assert store.get_dashboard(d["id"])["created_by"] == "EpidBot"

    def test_creator_defaults_to_empty(self, store):
        assert store.create_dashboard("D")["created_by"] == ""


class TestMarkdownCharts:
    """The markdown narrative kind."""

    def test_add_and_get(self, store):
        d = store.create_dashboard("D")
        c = store.add_chart(d["id"], "markdown", data={"text": "## Notas\ncasos **sobem**"})
        assert c["kind"] == "markdown"
        assert "sobem" in store.get_chart(c["id"])["data"]["text"]

    def test_text_required(self, store):
        d = store.create_dashboard("D")
        with pytest.raises(ValueError, match="'text'"):
            store.add_chart(d["id"], "markdown", data={})
        with pytest.raises(ValueError, match="'text'"):
            store.add_chart(d["id"], "markdown", data={"text": "   "})

    def test_text_length_cap(self, store):
        d = store.create_dashboard("D")
        with pytest.raises(ValueError, match="too long"):
            store.add_chart(
                d["id"], "markdown", data={"text": "x" * 10_001}
            )

    def test_update_chart_to_markdown(self, store):
        d = store.create_dashboard("D")
        c = store.add_chart(
            d["id"], "line", title="t",
            data={"columns": ["m", "v"], "rows": [["Jan", 1]]},
        )
        out = store.update_chart(c["id"], kind="markdown", data={"text": "now narrative"})
        assert out["kind"] == "markdown"

    def test_legacy_kind_check_relaxed(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "legacy-charts.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE dashboards ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " slug TEXT NOT NULL UNIQUE,"
            " title TEXT NOT NULL,"
            " description TEXT NOT NULL DEFAULT '',"
            " layout TEXT NOT NULL DEFAULT '{}',"
            " status TEXT NOT NULL DEFAULT 'draft',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '',"
            " published_at TEXT)"
        )
        con.execute(
            "CREATE TABLE charts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " dashboard_id INTEGER NOT NULL,"
            " kind TEXT NOT NULL CHECK (kind IN ('line', 'bar', 'pie', 'kpi', 'table', 'choropleth')),"
            " title TEXT NOT NULL DEFAULT '',"
            " data TEXT NOT NULL DEFAULT '{}',"
            " spec TEXT NOT NULL DEFAULT '{}',"
            " position TEXT NOT NULL DEFAULT '{}',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '')"
        )
        con.execute("INSERT INTO dashboards (slug, title) VALUES ('old', 'Old')")
        con.execute(
            "INSERT INTO charts (dashboard_id, kind, title) VALUES (1, 'line', 'keep me')"
        )
        con.commit()
        con.close()

        s = Store(path)
        assert s.get_chart(1)["title"] == "keep me"
        md = s.add_chart(1, "markdown", data={"text": "post-migration"})
        assert md["kind"] == "markdown"


class TestMigrations:
    """Pre-created_by databases are migrated in place on startup."""

    def test_legacy_database_gains_created_by(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "legacy.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE dashboards ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " slug TEXT NOT NULL UNIQUE,"
            " title TEXT NOT NULL,"
            " description TEXT NOT NULL DEFAULT '',"
            " layout TEXT NOT NULL DEFAULT '{}',"
            " status TEXT NOT NULL DEFAULT 'draft',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '',"
            " published_at TEXT)"
        )
        con.execute(
            "INSERT INTO dashboards (slug, title) VALUES ('old', 'Old Dash')"
        )
        con.commit()
        con.close()

        s = Store(path)
        legacy = s.get_dashboard_by_slug("old")
        assert legacy["created_by"] == ""
        d = s.create_dashboard("New", created_by="epidbot")
        assert d["created_by"] == "epidbot"

    def test_legacy_database_tweaks_gain_kind(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "legacy-tweaks.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE dashboards ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " slug TEXT NOT NULL UNIQUE,"
            " title TEXT NOT NULL,"
            " description TEXT NOT NULL DEFAULT '',"
            " layout TEXT NOT NULL DEFAULT '{}',"
            " status TEXT NOT NULL DEFAULT 'draft',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '',"
            " published_at TEXT)"
        )
        con.execute(
            "CREATE TABLE tweaks ("
            " id TEXT PRIMARY KEY,"
            " dashboard_id INTEGER NOT NULL,"
            " request TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " response TEXT,"
            " error TEXT,"
            " epidbot_job_id TEXT,"
            " created_at TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '')"
        )
        con.execute("INSERT INTO dashboards (slug, title) VALUES ('old', 'Old')")
        con.execute(
            "INSERT INTO tweaks (id, dashboard_id, request) VALUES ('t1', 1, 'fix')"
        )
        con.commit()
        con.close()

        s = Store(path)
        t = s.get_tweak("t1")
        assert t["kind"] == "tweak"
        q = s.create_tweak(1, "why?", kind="question")
        assert q["kind"] == "question"


class TestFreshStore:
    """A brand-new Store instance applies the schema idempotently."""

    def test_two_instances_share_data(self, tmp_path):
        path = str(tmp_path / "shared.db")
        s1 = Store(path)
        s1.create_dashboard("shared", slug="s")
        s2 = Store(path)
        assert s2.get_dashboard_by_slug("s")["title"] == "shared"
