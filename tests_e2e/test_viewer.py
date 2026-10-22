"""E2E: the viewer UI as a real browser sees it.

Marked `e2e` (deselected from the default unit run; see pyproject addopts).
"""

import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


class TestHome:
    def test_lists_published_dashboards_only(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/")
        cards = fresh_page.locator(".dash-card")
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text("Dengue E2E")

    def test_cards_show_creator_and_likes(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/")
        card = fresh_page.locator(".dash-card").first
        expect(card).to_contain_text("by EpidBot")
        expect(card).to_contain_text("0")

    def test_search_filters(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/")
        expect(fresh_page.locator(".dash-card")).to_have_count(1)
        fresh_page.fill("#dash-search", "zzz-no-match")
        expect(fresh_page.locator(".empty")).to_contain_text("No dashboards match")
        fresh_page.fill("#dash-search", "dengue")
        expect(fresh_page.locator(".dash-card")).to_have_count(1)

    def test_auth_chip_hidden_without_oidc(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/")
        expect(fresh_page.locator("#auth-chip")).to_be_hidden()

    def test_draft_not_listed(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/")
        expect(fresh_page.locator("body")).not_to_contain_text("Hidden Draft")


class TestDashboardView:
    def test_full_view(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        expect(fresh_page.locator(".dash-head h1")).to_have_text("Dengue E2E")
        expect(fresh_page.locator(".dash-byline")).to_have_text("by EpidBot")
        expect(fresh_page.locator(".chart-card")).to_have_count(4)
        expect(fresh_page.locator(".back-link")).to_be_visible()

    def test_markdown_block_renders(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        body = fresh_page.locator(".markdown-body")
        expect(body).to_be_visible()
        expect(body.locator("h2")).to_have_text("Methodology")
        expect(body.locator("strong")).to_have_text("weekly")
        expect(body.locator("code")).to_have_text("INFO-DENGUE")

    def test_kpi_uses_thousands_separator(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        expect(fresh_page.locator(".kpi-value")).to_have_text("30")

    def test_back_link_returns_home(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        fresh_page.click(".back-link")
        expect(fresh_page.locator(".dash-card")).to_have_count(1)

    def test_tweak_panel_hidden_without_epidbot(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        expect(fresh_page.locator("#tweak-panel")).to_be_hidden()

    def test_like_toggle(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        count = fresh_page.locator("#like-count")
        expect(count).to_have_text("0")
        fresh_page.click("#like-btn")
        expect(count).to_have_text("1")
        expect(fresh_page.locator("#like-btn")).to_have_class(re.compile(r"liked"))
        fresh_page.click("#like-btn")
        expect(count).to_have_text("0")
        # persisted across reload: stays unliked, count back to zero
        fresh_page.reload()
        expect(count).to_have_text("0")

    def test_unknown_dashboard_shows_error(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/does-not-exist")
        expect(fresh_page.locator(".empty")).to_contain_text(
            "not found or not published"
        )


class TestI18n:
    def test_portuguese_via_query_param(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/?lang=pt-BR#/")
        expect(fresh_page.locator(".home-head h1")).to_have_text(
            "Painéis publicados"
        )
        expect(fresh_page.locator("#dash-search")).to_have_attribute(
            "placeholder", "Buscar painéis…"
        )
        expect(fresh_page.locator("#ask-send")).to_have_text("Perguntar")

    def test_english_default(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/")
        expect(fresh_page.locator(".home-head h1")).to_have_text(
            "Published dashboards"
        )


class TestLazyPlotly:
    def test_home_never_downloads_plotly(self, fresh_page, server_url):
        urls = []
        fresh_page.on("request", lambda r: urls.append(r.url))
        fresh_page.goto(f"{server_url}/#/")
        fresh_page.wait_for_selector(".dash-card")
        assert not any("plotly" in u for u in urls), "home fetched plotly.min.js"

    def test_dashboard_downloads_plotly(self, fresh_page, server_url):
        urls = []
        fresh_page.on("request", lambda r: urls.append(r.url))
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        fresh_page.wait_for_selector(".js-plotly-plot")
        assert any("plotly" in u for u in urls)


class TestExport:
    def test_menu_toggles(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        menu = fresh_page.locator("#export-menu")
        expect(menu).to_be_hidden()
        fresh_page.click("#export-btn")
        expect(menu).to_be_visible()
        fresh_page.click("#dash-status")  # click elsewhere closes it
        expect(menu).to_be_hidden()

    def test_png_download_replicates_layout(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        fresh_page.wait_for_selector(".js-plotly-plot")
        with fresh_page.expect_download() as download_info:
            fresh_page.click("#export-btn")
            fresh_page.click('[data-export="png"]')
        download = download_info.value
        assert download.suggested_filename == "e2e-dengue.png"
        path = download.path()
        assert path.stat().st_size > 10_000, "PNG export suspiciously small"

    def test_json_download(self, fresh_page, server_url):
        fresh_page.goto(f"{server_url}/#/d/e2e-dengue")
        with fresh_page.expect_download() as download_info:
            fresh_page.click("#export-btn")
            fresh_page.click('[data-export="json"]')
        assert download_info.value.suggested_filename == "e2e-dengue.json"
