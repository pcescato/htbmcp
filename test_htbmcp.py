"""
HTBMCP/1.0 — Test Suite
RFC 1516 — Hyper Text Beer Mug Control Protocol

Uses FastAPI TestClient (bypasses HTTP transport — custom methods work fine).
For real TCP server testing, use server.py directly with curl.

Run:
    pytest test_htbmcp.py -v
"""

import pytest
from fastapi.testclient import TestClient
from main import app
from registry import TAP_REGISTRY, TapStatus

client = TestClient(app, raise_server_exceptions=True)

MUG_POT = "message/mugpot"


def reset_taps():
    """Reset tap registry state between tests."""
    defaults = {
        "tap-1":     (TapStatus.IDLE, 75,  1),
        "tap-2":     (TapStatus.IDLE, 42,  1),
        "tap-3":     (TapStatus.IDLE, 90,  1),
        "tap-gdansk":(TapStatus.IDLE, 88,  1),
    }
    for tap_id, (status, level, bv) in defaults.items():
        t = TAP_REGISTRY[tap_id]
        t.status = status
        t.level = level
        t.brew_version = bv
        t.session_id = None
        t.pour_history.clear()


@pytest.fixture(autouse=True)
def clean_state():
    reset_taps()
    yield
    reset_taps()


# ─────────────────────────────────────────────────────────────────────────────
# GET — RFC 1516 §3.1.3
# ─────────────────────────────────────────────────────────────────────────────

class TestGet:
    def test_get_known_tap(self):
        r = client.get("/tap/tap-1")
        assert r.status_code == 200
        data = r.json()
        assert data["tap"] == "tap-1"
        assert data["style"] == "IPA"
        assert "keg_level" in data
        assert "temperature" in data
        assert "pressure" in data
        assert data["X-Protocol"] == "HTBMCP/1.0"

    def test_get_contains_no_beer(self):
        """RFC 1516 §3.1.3: GET contains no beer. This is an important distinction."""
        r = client.get("/tap/tap-1")
        data = r.json()
        assert "note" in data
        assert "no beer" in data["note"].lower()

    def test_get_unknown_tap(self):
        r = client.get("/tap/nonexistent")
        assert r.status_code == 404
        data = r.json()
        assert "available_taps" in data

    def test_get_all_taps(self):
        r = client.get("/taps")
        assert r.status_code == 200
        data = r.json()
        assert "taps" in data
        assert len(data["taps"]) == 4


# ─────────────────────────────────────────────────────────────────────────────
# PROPFIND — RFC 1516 §3.1.4
# ─────────────────────────────────────────────────────────────────────────────

class TestPropfind:
    def test_propfind_lists_styles(self):
        r = client.request("PROPFIND", "/tap/tap-1/styles")
        assert r.status_code == 200
        data = r.json()
        assert "all_styles" in data
        assert "compatible_styles" in data
        assert "IPA" in data["compatible_styles"]

    def test_propfind_includes_foam_levels(self):
        r = client.request("PROPFIND", "/tap/tap-1/styles")
        data = r.json()
        assert "belgian" in data["foam_levels"]

    def test_propfind_rejects_non_alcoholic(self):
        """RFC 1516 §3.2.4: No provision for non-alcoholic beer. What would be the point?"""
        r = client.request("PROPFIND", "/tap/tap-1/styles")
        data = r.json()
        assert "non_alcoholic" in data
        assert "NOT_ACCEPTABLE" in data["non_alcoholic"]

    def test_propfind_unknown_tap(self):
        r = client.request("PROPFIND", "/tap/ghost/styles")
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# TAP — RFC 1516 §3.1.1
# ─────────────────────────────────────────────────────────────────────────────

class TestTap:
    def _tap_open(self, tap_id="tap-1", style="IPA", temp=8, foam="normal", vessel="pint"):
        return client.request(
            "TAP", f"/tap/{tap_id}",
            headers={
                "Content-Type": MUG_POT,
                "Accept-Style": style,
                "Accept-Temperature": str(temp),
                "Accept-Foam": foam,
                "Vessel-Size": vessel,
            },
            content=b"open",
        )

    def test_tap_open_200(self):
        r = self._tap_open()
        assert r.status_code == 200
        data = r.json()
        assert data["body"] == "open"
        assert "session_id" in data
        assert "X-Brew-Version" in data

    def test_tap_opens_session(self):
        self._tap_open()
        tap = TAP_REGISTRY["tap-1"]
        assert tap.status == TapStatus.TAPPED

    def test_tap_close(self):
        self._tap_open()
        r = client.request(
            "TAP", "/tap/tap-1",
            headers={"Content-Type": MUG_POT},
            content=b"close",
        )
        assert r.status_code == 200
        assert r.json()["session"] == "closed"
        assert TAP_REGISTRY["tap-1"].status == TapStatus.IDLE

    def test_tap_409_session_already_open(self):
        """RFC 1516 §3.3.4: MUST NOT allow two simultaneous sessions."""
        self._tap_open()
        r = self._tap_open()
        assert r.status_code == 409
        data = r.json()
        assert "keg lines" in data["detail"].lower()

    def test_tap_404_unknown(self):
        r = self._tap_open(tap_id="tap-99")
        assert r.status_code == 404

    def test_tap_503_empty_keg(self):
        """RFC 1516 §3.3.6: Server SHOULD NOT dispense air."""
        TAP_REGISTRY["tap-1"].level = 0
        r = self._tap_open()
        assert r.status_code == 503
        assert "air" in r.json()["detail"].lower()

    def test_tap_post_accepted_but_discouraged(self):
        """RFC 1516 §3.1.1: POST MUST be accepted but STRONGLY DISCOURAGED."""
        r = client.post(
            "/tap/tap-1",
            headers={"Content-Type": MUG_POT},
            content=b"open",
        )
        assert r.status_code == 200
        assert "STRONGLY DISCOURAGED" in r.json()["method"]

    def test_tap_406_wrong_style(self):
        r = self._tap_open(style="Stout")   # tap-1 is IPA only
        assert r.status_code == 406
        data = r.json()
        assert "available_styles" in data

    def test_tap_406_stout_too_cold(self):
        """RFC 1516 §3.2.3: MUST NOT serve Stout at 3°C. This is not a SHOULD NOT."""
        r = self._tap_open(tap_id="tap-2", style="Stout", temp=3)
        assert r.status_code == 406
        data = r.json()
        assert "MUST NOT" in data["error"]

    def test_tap_406_temperature_out_of_range(self):
        r = self._tap_open(tap_id="tap-1", style="IPA", temp=2)
        assert r.status_code == 406

    def test_tap_406_goblet_not_trappist(self):
        """RFC 1516 §4: goblet is reserved for Trappist ales."""
        r = self._tap_open(style="IPA", vessel="goblet")
        assert r.status_code == 406
        assert "Trappist" in r.json()["detail"]

    def test_tap_406_belgian_foam_non_belgian_style(self):
        """RFC 1516 §3.2.2: belgian foam on non-Belgian style."""
        r = self._tap_open(style="IPA", foam="belgian")
        assert r.status_code == 406

    def test_tap_goblet_trappist_ok(self):
        """Goblet + Trappist is fine. RFC 1516 §4."""
        r = self._tap_open(tap_id="tap-3", style="Trappist", temp=14, vessel="goblet")
        assert r.status_code == 200

    def test_tap_piwo_scheme(self):
        """RFC 1516 §4: piwo URI scheme — Gdansk, 1414."""
        tap = TAP_REGISTRY["tap-gdansk"]
        assert tap.scheme == "piwo"
        r = self._tap_open(tap_id="tap-gdansk", style="Lager", temp=4)
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POUR — RFC 1516 §3.1.2
# ─────────────────────────────────────────────────────────────────────────────

class TestPour:
    def _tap_open(self, tap_id="tap-1"):
        client.request(
            "TAP", f"/tap/{tap_id}",
            headers={"Content-Type": MUG_POT, "Accept-Style": "*"},
            content=b"open",
        )

    def _pour(self, tap_id="tap-1", body="start", foam="normal"):
        return client.request(
            "POUR", f"/tap/{tap_id}/pour",
            headers={"Content-Type": MUG_POT, "Accept-Foam": foam},
            content=body.encode(),
        )

    def test_pour_200_after_tap(self):
        self._tap_open()
        r = self._pour()
        assert r.status_code == 200
        data = r.json()
        assert data["pour_status"] == "dispensing"
        assert "pour_id" in data
        assert "keg_remaining" in data

    def test_pour_409_without_tap(self):
        """RFC 1516 §3.1.2: MUST NOT execute POUR if no TAP session open."""
        r = self._pour()
        assert r.status_code == 409
        assert "TAP" in r.json()["hint"]

    def test_pour_503_empty_keg(self):
        self._tap_open()
        TAP_REGISTRY["tap-1"].level = 0
        r = self._pour()
        assert r.status_code == 503

    def test_pour_stop_equivalent_to_when(self):
        """RFC 1516 §3.1.2: POUR stop is equivalent to WHEN."""
        self._tap_open()
        self._pour()
        r = self._pour(body="stop")
        assert r.status_code == 200
        assert TAP_REGISTRY["tap-1"].status == TapStatus.TAPPED

    def test_pour_decrements_keg_level(self):
        self._tap_open()
        level_before = TAP_REGISTRY["tap-1"].level
        self._pour()
        assert TAP_REGISTRY["tap-1"].level < level_before

    def test_pour_brew_version_conflict(self):
        """RFC 1516 §3.3.4: concurrent POUR brew_version mismatch → 409."""
        self._tap_open()
        current = TAP_REGISTRY["tap-1"].brew_version
        r = client.request(
            "POUR", "/tap/tap-1/pour",
            headers={"Content-Type": MUG_POT, "X-Brew-Version": str(current - 1)},
            content=b"start",
        )
        assert r.status_code == 409
        assert "brew_version" in r.json()["error"].lower()

    def test_pour_bad_body(self):
        self._tap_open()
        r = self._pour(body="sip")
        assert r.status_code == 400

    def test_pour_records_history(self):
        self._tap_open()
        self._pour()
        assert len(TAP_REGISTRY["tap-1"].pour_history) == 1


# ─────────────────────────────────────────────────────────────────────────────
# WHEN — RFC 1516 §3.1.5
# ─────────────────────────────────────────────────────────────────────────────

class TestWhen:
    def test_when_stops_pour(self):
        """RFC 1516 §3.1.5: WHEN MUST cause immediate cessation of foam dispensing."""
        client.request("TAP", "/tap/tap-1",
                       headers={"Content-Type": MUG_POT}, content=b"open")
        client.request("POUR", "/tap/tap-1/pour",
                       headers={"Content-Type": MUG_POT}, content=b"start")
        r = client.request("WHEN", "/tap/tap-1/when")
        assert r.status_code == 200
        data = r.json()
        assert data["foam_stopped"] is True
        assert TAP_REGISTRY["tap-1"].status == TapStatus.TAPPED

    def test_when_no_pour_in_progress(self):
        """RFC 1516 §3.1.5: If no pour, acknowledge and appreciate the enthusiasm."""
        r = client.request("WHEN", "/tap/tap-1/when")
        assert r.status_code == 200
        data = r.json()
        assert data["acknowledged"] is True
        assert "enthusiasm" in data["note"].lower()

    def test_when_once_is_sufficient(self):
        """RFC 1516 §3.1.5: There is no WHEN-WHEN method. Once is sufficient."""
        r = client.request("WHEN", "/tap/tap-1/when")
        data = r.json()
        assert "Once is sufficient" in data["addendum"]


# ─────────────────────────────────────────────────────────────────────────────
# 419 — RFC 1516 §3.3.5
# ─────────────────────────────────────────────────────────────────────────────

class TestWineGlass:
    def test_419_wine_glass(self):
        """RFC 1516 §3.3.5: Any attempt to pour into a wine glass → 419."""
        r = client.request("POUR", "/wine-glass/tap-1")
        assert r.status_code == 419
        data = r.json()
        assert data["error"] == "I'm a Wine Glass"
        assert "handle" in data["detail"].lower()

    def test_419_tap_on_wine_glass(self):
        r = client.request("TAP", "/wine-glass/anything",
                           headers={"Content-Type": MUG_POT}, content=b"open")
        assert r.status_code == 419

    def test_419_not_for_empty_keg(self):
        """RFC 1516 §3.3.6: 419 MUST NOT be returned for empty keg. Different problems."""
        TAP_REGISTRY["tap-1"].level = 0
        client.request("TAP", "/tap/tap-1",
                       headers={"Content-Type": MUG_POT}, content=b"open")
        # Actually empty keg blocks at TAP level — test that it's 503, not 419
        r = client.request("TAP", "/tap/tap-1",
                           headers={"Content-Type": MUG_POT}, content=b"open")
        assert r.status_code in (409, 503)
        assert r.status_code != 419


# ─────────────────────────────────────────────────────────────────────────────
# Protocol headers — middleware
# ─────────────────────────────────────────────────────────────────────────────

class TestProtocolHeaders:
    def test_every_response_has_protocol_header(self):
        r = client.get("/tap/tap-1")
        assert r.headers.get("x-protocol") == "HTBMCP/1.0"
        assert r.headers.get("x-rfc") == "RFC-1516"

    def test_404_has_protocol_header(self):
        r = client.get("/tap/nobody")
        assert r.headers.get("x-protocol") == "HTBMCP/1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Full workflow — RFC 1516 §3 happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestFullWorkflow:
    def test_tap_pour_when_close(self):
        """The complete HTBMCP workflow: TAP open → POUR start → WHEN → TAP close."""
        # TAP open
        r = client.request("TAP", "/tap/tap-1",
                           headers={"Content-Type": MUG_POT,
                                    "Accept-Style": "IPA",
                                    "Accept-Temperature": "8",
                                    "Accept-Foam": "normal"},
                           content=b"open")
        assert r.status_code == 200
        brew_v = r.json()["X-Brew-Version"]

        # POUR start
        r = client.request("POUR", "/tap/tap-1/pour",
                           headers={"Content-Type": MUG_POT,
                                    "Accept-Foam": "normal",
                                    "X-Brew-Version": str(brew_v)},
                           content=b"start")
        assert r.status_code == 200

        # WHEN — foam is sufficient
        r = client.request("WHEN", "/tap/tap-1/when")
        assert r.status_code == 200
        assert r.json()["foam_stopped"] is True

        # TAP close
        r = client.request("TAP", "/tap/tap-1",
                           headers={"Content-Type": MUG_POT},
                           content=b"close")
        assert r.status_code == 200
        assert TAP_REGISTRY["tap-1"].status == TapStatus.IDLE

    def test_trappist_goblet_workflow(self):
        """Trappist ale in a goblet at 14°C. RFC 1516 §4."""
        r = client.request("TAP", "/tap/tap-3",
                           headers={"Content-Type": MUG_POT,
                                    "Accept-Style": "Trappist",
                                    "Accept-Temperature": "14",
                                    "Accept-Foam": "belgian",
                                    "Vessel-Size": "goblet"},
                           content=b"open")
        assert r.status_code == 200

        r = client.request("POUR", "/tap/tap-3/pour",
                           headers={"Content-Type": MUG_POT, "Accept-Foam": "belgian"},
                           content=b"start")
        assert r.status_code == 200

    def test_gdansk_piwo_tap(self):
        """piwo:// scheme — Gdansk 1414. We pour one out."""
        r = client.request("TAP", "/tap/tap-gdansk",
                           headers={"Content-Type": MUG_POT,
                                    "Accept-Style": "Lager",
                                    "Accept-Temperature": "4"},
                           content=b"open")
        assert r.status_code == 200
        assert TAP_REGISTRY["tap-gdansk"].scheme == "piwo"
