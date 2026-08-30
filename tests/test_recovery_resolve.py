"""
test_recovery_resolve.py
Covers resolving RECOVERY_REQUIRED items: the journal transition, the API
endpoints, and that a resolved item stops appearing in needs_attention (so
the persistent list can actually be cleared once a file has been fixed by
hand, rather than resurfacing on every restart forever).
"""

from __future__ import annotations

import pytest

from metamatch.journal import Journal, RECOVERY_REQUIRED, RESOLVED, COMMITTED


def _recovery_required(jr, kind="tv", path="/x/a.mkv"):
    tid = jr.begin(kind, path, path, {}, {})
    jr.mark_applying(tid)
    jr.mark_recovery_required(tid, {"note": "rollback couldn't restore"})
    return tid


class TestJournalResolve:
    def test_resolve_transitions_only_recovery_required(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        rr = _recovery_required(jr)
        ok = jr.begin("tv", "/x/ok.mkv", "/x/ok.mkv", {}, {})
        jr.mark_applying(ok)
        jr.commit(ok, "/x/ok.mkv")

        assert jr.mark_resolved(rr) is True
        assert jr.get(rr).status == RESOLVED
        assert jr.mark_resolved(ok) is False       # can't resolve a healthy row
        assert jr.mark_resolved(999999) is False    # missing row

    def test_resolved_item_leaves_outstanding_list(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        rr = _recovery_required(jr)
        assert len(jr.list_by_status("tv", RECOVERY_REQUIRED)) == 1
        jr.mark_resolved(rr)
        assert jr.list_by_status("tv", RECOVERY_REQUIRED) == []

    def test_resolve_preserves_history_note(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        rr = _recovery_required(jr)
        jr.mark_resolved(rr, note="fixed tags by hand")
        info = jr.get(rr).rollback_info
        assert info["note"] == "rollback couldn't restore"  # original kept
        assert info["resolved"] is True
        assert info["resolved_note"] == "fixed tags by hand"

    def test_resolve_is_idempotent_noop(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        rr = _recovery_required(jr)
        assert jr.mark_resolved(rr) is True
        assert jr.mark_resolved(rr) is False  # already resolved -> no-op


class TestResolveApi:
    def _seed(self, app_module, kind="tv"):
        jr = app_module.music_library.journal  # shared journal
        return _recovery_required(jr, kind=kind)

    def test_resolve_single_clears_from_needs_attention(self, app_client):
        import app as app_module
        tid = self._seed(app_module)

        before = app_client.get("/api/recovery").get_json()
        assert before["summary"]["recovery_required"] >= 1

        resp = app_client.post("/api/recovery/resolve", json={"txn_id": tid})
        assert resp.status_code == 200 and resp.get_json()["resolved"] is True

        after = app_client.get("/api/recovery").get_json()
        ids = {n["id"] for n in after["needs_attention"]}
        assert tid not in ids

    def test_resolve_missing_txn_id_is_400(self, app_client):
        assert app_client.post("/api/recovery/resolve", json={}).status_code == 400

    def test_resolve_non_recovery_row_is_409(self, app_client):
        import app as app_module
        jr = app_module.music_library.journal
        ok = jr.begin("tv", "/x/ok.mkv", "/x/ok.mkv", {}, {})
        jr.mark_applying(ok)
        jr.commit(ok, "/x/ok.mkv")
        assert app_client.post("/api/recovery/resolve", json={"txn_id": ok}).status_code == 409

    def test_resolve_all_across_kinds(self, app_client):
        import app as app_module
        jr = app_module.music_library.journal
        _recovery_required(jr, kind="music", path="/m/a.mp3")
        _recovery_required(jr, kind="movie", path="/v/a.mkv")
        _recovery_required(jr, kind="tv", path="/t/a.mkv")
        _recovery_required(jr, kind="tv_series", path="/t/tvshow.nfo")

        resp = app_client.post("/api/recovery/resolve_all")
        assert resp.status_code == 200
        assert resp.get_json()["resolved"] == 4

        after = app_client.get("/api/recovery").get_json()
        assert after["summary"]["needs_attention"] is False


class TestRecoveryPanelWiring:
    def test_index_has_recovery_panel(self, app_client):
        html = app_client.get("/").data.decode("utf-8")
        assert 'id="recoveryPanel"' in html
        assert 'id="recoveryBannerReview"' in html
        assert 'id="recoveryResolveAllBtn"' in html

    def test_app_js_references_resolve_endpoints(self, app_client):
        js = app_client.get("/static/app.js").data.decode("utf-8")
        assert "/api/recovery/resolve" in js
        assert "/api/recovery/resolve_all" in js
