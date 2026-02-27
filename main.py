"""
HTBMCP/1.0 — FastAPI application
RFC 1516 — Hyper Text Beer Mug Control Protocol

NOTE: This file is used for the test suite only (FastAPI TestClient
bypasses HTTP transport, so custom methods work fine here).

For real HTBMCP traffic, use server.py (raw asyncio TCP).
uvicorn rejects non-IANA method names at the socket layer —
same issue as HTCPCP. See server.py for the correct approach.
"""

import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from registry import (
    TAP_REGISTRY, get_tap, list_taps,
    TapStatus, FoamLevel, TEMP_RANGES,
    VesselSize,
)

app = FastAPI(
    title="HTBMCP/1.0",
    version="1.0",
    description="Hyper Text Beer Mug Control Protocol — RFC 1516",
)


# ── Middleware ────────────────────────────────────────────────────────────────

class HTBMCPMiddleware(BaseHTTPMiddleware):
    """Inject protocol headers on every response."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Protocol"] = "HTBMCP/1.0"
        response.headers["X-RFC"] = "RFC-1516"
        response.headers["X-Port"] = "1414"
        return response

app.add_middleware(HTBMCPMiddleware)


# ── Helpers ───────────────────────────────────────────────────────────────────

def htbmcp_response(status: int, body: dict) -> JSONResponse:
    body.setdefault("X-Protocol", "HTBMCP/1.0")
    body.setdefault("X-RFC", "RFC-1516")
    return JSONResponse(status_code=status, content=body)


def validate_style(tap_id: str, requested_style: str) -> JSONResponse | None:
    """Return 406 if style not available on this tap."""
    tap = get_tap(tap_id)
    if not tap:
        return None
    if requested_style == "*":
        return None
    if requested_style not in tap.compatible_styles and requested_style != tap.style:
        return htbmcp_response(406, {
            "status": 406,
            "error": "Not Acceptable",
            "requested_style": requested_style,
            "available_styles": tap.compatible_styles,
            "rfc": "RFC 1516 §3.3.3",
        })
    return None


def validate_temperature(tap_id: str, temp: int | None) -> JSONResponse | None:
    """Return 406 on temperature violations — RFC 1516 §3.2.3."""
    tap = get_tap(tap_id)
    if not tap or temp is None:
        return None

    # RFC 1516 §3.2.3: "A server MUST NOT serve a Stout at 3°C.
    # This is not a SHOULD NOT. This is a MUST NOT."
    if tap.style == "Stout" and temp <= 5:
        return htbmcp_response(406, {
            "status": 406,
            "error": "Temperature violation — MUST NOT",
            "detail": (
                "A server MUST NOT serve a Stout at 3°C. "
                "This is not a SHOULD NOT. This is a MUST NOT. "
                "The authors feel strongly about this."
            ),
            "temp_requested": temp,
            "allowed_range": "10–13°C",
            "rfc": "RFC 1516 §3.2.3",
        })

    lo, hi = tap.temp_range
    # Tolerance: ±2°C beyond published range before hard rejection
    if temp < lo - 2 or temp > hi + 2:
        return htbmcp_response(406, {
            "status": 406,
            "error": "Not Acceptable — temperature out of range",
            "requested_temp": f"{temp}°C",
            "allowed_range": f"{lo}–{hi}°C",
            "style": tap.style,
            "rfc": "RFC 1516 §3.2.3",
        })
    return None


def validate_vessel(style: str, vessel: str) -> JSONResponse | None:
    """Goblet is Trappist-only — RFC 1516 §4."""
    if vessel == VesselSize.GOBLET and style not in ("Trappist", "*"):
        return htbmcp_response(406, {
            "status": 406,
            "error": "Not Acceptable — vessel conflict",
            "detail": "The goblet vessel is reserved for Trappist ales.",
            "vessel": vessel,
            "rfc": "RFC 1516 §4",
        })
    return None


def validate_foam(style: str, foam: str) -> JSONResponse | None:
    """Belgian foam on a non-Belgian style is philosophically inconsistent."""
    if foam == FoamLevel.BELGIAN and style not in ("Trappist", "Lambic", "*"):
        return htbmcp_response(406, {
            "status": 406,
            "error": "Not Acceptable — foam conflict",
            "detail": (
                "belgian foam level SHOULD only be applied to Trappist or Lambic styles. "
                "Certain beer traditions consider foam structurally and philosophically "
                "integral to the beverage. Other traditions do not. Respect the difference."
            ),
            "rfc": "RFC 1516 §3.2.2",
        })
    return None


def parse_headers(request: Request) -> dict:
    return {
        "accept_style":       request.headers.get("accept-style", "*"),
        "accept_foam":        request.headers.get("accept-foam", FoamLevel.NORMAL),
        "accept_temperature": request.headers.get("accept-temperature"),
        "vessel":             request.headers.get("vessel-size", VesselSize.PINT),
        "brew_version":       request.headers.get("x-brew-version"),
        "content_type":       request.headers.get("content-type", ""),
    }


# ── TAP ───────────────────────────────────────────────────────────────────────

@app.api_route("/tap/{tap_id}", methods=["TAP", "POST"])
async def tap(tap_id: str, request: Request):
    """
    RFC 1516 §3.1.1 — The TAP Method.
    Initiates or closes a dispensing session.
    POST is accepted but STRONGLY DISCOURAGED.
    """
    tap = get_tap(tap_id)
    if not tap:
        return htbmcp_response(404, {
            "status": 404,
            "error": "Not Found",
            "detail": f"Tap '{tap_id}' does not exist. Check the menu.",
            "available_taps": list(TAP_REGISTRY.keys()),
            "rfc": "RFC 1516 §3.3.2",
        })

    if tap.is_empty:
        return htbmcp_response(503, {
            "status": 503,
            "error": "Service Unavailable",
            "detail": "Keg empty. The server SHOULD return this code rather than dispensing air.",
            "keg_level": "0%",
            "rfc": "RFC 1516 §3.3.6",
        })

    body_bytes = await request.body()
    body = body_bytes.decode().strip().lower()

    # ── TAP close ──
    if body == "close":
        if not tap.status != TapStatus.IDLE:
            pass  # closing a non-open tap is a no-op, not an error
        tap.status = TapStatus.IDLE
        tap.session_id = None
        return htbmcp_response(200, {
            "status": 200,
            "method": "TAP",
            "body": "close",
            "Content-Type": "message/mugpot",
            "tap": tap_id,
            "session": "closed",
            "note": "Any in-progress POUR requests SHOULD be allowed to complete. It would be rude otherwise.",
            "rfc": "RFC 1516 §3.1.1",
        })

    # ── TAP open ──
    if body != "open":
        return htbmcp_response(400, {
            "status": 400,
            "error": "Bad Request",
            "detail": "TAP body MUST be 'open' or 'close' (message/mugpot)",
            "rfc": "RFC 1516 §3.1.1",
        })

    # 409 if session already open — RFC 1516 §3.3.4
    if tap.status != TapStatus.IDLE:
        return htbmcp_response(409, {
            "status": 409,
            "error": "Conflict",
            "detail": (
                "A TAP open request was received when a session is already active. "
                "Servers MUST NOT silently allow two simultaneous sessions. "
                "This is how keg lines get contaminated."
            ),
            "current_brew_version": tap.brew_version,
            "rfc": "RFC 1516 §3.3.4",
        })

    h = parse_headers(request)

    # Validate
    for check in [
        validate_style(tap_id, h["accept_style"]),
        validate_temperature(tap_id, int(h["accept_temperature"]) if h["accept_temperature"] else None),
        validate_vessel(h["accept_style"], h["vessel"]),
        validate_foam(h["accept_style"], h["accept_foam"]),
    ]:
        if check:
            return check

    if request.method == "POST":
        post_warning = " (POST accepted but STRONGLY DISCOURAGED — RFC 1516 §3.1.1)"
    else:
        post_warning = ""

    tap.status = TapStatus.TAPPED
    tap.session_id = str(uuid.uuid4())[:8]
    tap.brew_version += 1

    return htbmcp_response(200, {
        "status": 200,
        "method": "TAP" + post_warning,
        "body": "open",
        "Content-Type": "message/mugpot",
        "tap": tap_id,
        "uri": f"{tap.uri}/{h['vessel']}",
        "style": tap.style,
        "temperature": f"{tap.temp}°C",
        "pressure": f"{tap.pressure} BAR",
        "accept_foam": h["accept_foam"],
        "session_id": tap.session_id,
        "X-Brew-Version": tap.brew_version,
        "note": "Session opened. POUR requests now accepted.",
        "rfc": "RFC 1516 §3.1.1",
    })


# ── POUR ──────────────────────────────────────────────────────────────────────

@app.api_route("/tap/{tap_id}/pour", methods=["POUR"])
async def pour(tap_id: str, request: Request):
    """
    RFC 1516 §3.1.2 — The POUR Method.
    The server MUST NOT execute POUR if no TAP session is open.
    """
    tap = get_tap(tap_id)
    if not tap:
        return htbmcp_response(404, {
            "status": 404, "error": "Not Found",
            "available_taps": list(TAP_REGISTRY.keys()),
        })

    if tap.status == TapStatus.IDLE:
        return htbmcp_response(409, {
            "status": 409,
            "error": "Conflict — no session",
            "detail": "The server MUST NOT execute a POUR request if no TAP session is currently open.",
            "hint": f"Send TAP /tap/{tap_id} with body 'open' first.",
            "rfc": "RFC 1516 §3.1.2",
        })

    if tap.is_empty:
        tap.status = TapStatus.EMPTY
        return htbmcp_response(503, {
            "status": 503,
            "error": "Service Unavailable",
            "detail": "The keg is empty. The server SHOULD return this rather than attempting to dispense air.",
            "rfc": "RFC 1516 §3.3.6",
        })

    # Concurrent POUR check — RFC 1516 §3.3.4
    h = parse_headers(request)
    if h["brew_version"] and int(h["brew_version"]) != tap.brew_version:
        return htbmcp_response(409, {
            "status": 409,
            "error": "Conflict — brew_version mismatch",
            "detail": "Concurrent POUR detected. The keg has a brew_version token.",
            "current_brew_version": tap.brew_version,
            "your_brew_version": h["brew_version"],
            "rfc": "RFC 1516 §3.3.4",
        })

    body_bytes = await request.body()
    body = body_bytes.decode().strip().lower()

    if body == "stop":
        # Equivalent to WHEN — RFC 1516 §3.1.2
        tap.status = TapStatus.TAPPED
        return htbmcp_response(200, {
            "status": 200, "method": "POUR", "body": "stop",
            "note": "POUR stop is equivalent to WHEN. Pour stopped.",
            "rfc": "RFC 1516 §3.1.2",
        })

    if body != "start":
        return htbmcp_response(400, {
            "status": 400, "error": "Bad Request",
            "detail": "POUR body MUST be 'start' or 'stop' (message/mugpot)",
        })

    tap.status = TapStatus.POURING
    tap.brew_version += 1
    tap.level = max(0, tap.level - 12)

    foam = h["accept_foam"]
    foam_note = {
        FoamLevel.NONE:    "No foam requested. Server complies, reluctantly.",
        FoamLevel.LIGHT:   "Light foam (0.5–1cm). Appropriate.",
        FoamLevel.NORMAL:  "Normal foam (1–2cm). The default. Good choice.",
        FoamLevel.HEAVY:   "Heavy foam (2–4cm). Bold. Acceptable.",
        FoamLevel.BELGIAN: "Belgian foam. Implementation-defined, but significant. Send WHEN when ready.",
    }.get(foam, "Normal foam applied.")

    pour_record = {
        "pour_id": len(tap.pour_history) + 1,
        "style": tap.style,
        "foam": foam,
        "keg_after": tap.level,
    }
    tap.pour_history.append(pour_record)

    return htbmcp_response(200, {
        "status": 200,
        "method": "POUR",
        "body": "start",
        "Content-Type": "message/mugpot",
        "pour_status": "dispensing",
        "tap": tap_id,
        "pour_id": pour_record["pour_id"],
        "foam_level": foam,
        "foam_note": foam_note,
        "keg_remaining": f"{tap.level}%",
        "X-Brew-Version": tap.brew_version,
        "note": "Beer is flowing. Say WHEN when foam is sufficient.",
        "rfc": "RFC 1516 §3.1.2",
    })


# ── WHEN ──────────────────────────────────────────────────────────────────────

@app.api_route("/tap/{tap_id}/when", methods=["WHEN"])
async def when(tap_id: str, request: Request):
    """
    RFC 1516 §3.1.5 — The WHEN Method.
    Inherited from HTCPCP RFC 2324.
    Enough? Say WHEN.
    There is no WHEN-WHEN method. Once is sufficient.
    """
    tap = get_tap(tap_id)
    if not tap:
        return htbmcp_response(404, {"status": 404, "error": "Not Found"})

    if tap.status != TapStatus.POURING:
        return htbmcp_response(200, {
            "status": 200,
            "method": "WHEN",
            "acknowledged": True,
            "note": "No pour was in progress, but the client's enthusiasm is appreciated.",
            "addendum": "There is no WHEN-WHEN method. Once is sufficient.",
            "rfc": "RFC 1516 §3.1.5",
        })

    tap.status = TapStatus.TAPPED
    return htbmcp_response(200, {
        "status": 200,
        "method": "WHEN",
        "foam_stopped": True,
        "message": "The server has acknowledged WHEN and ceased foam dispensing immediately.",
        "note": "There is no WHEN-WHEN method. Once is sufficient.",
        "rfc": "RFC 1516 §3.1.5",
    })


# ── GET ───────────────────────────────────────────────────────────────────────

@app.get("/tap/{tap_id}")
async def get_tap_status(tap_id: str):
    """
    RFC 1516 §3.1.3 — The GET Method.
    Returns tap state. Contains no beer.
    """
    tap = get_tap(tap_id)
    if not tap:
        return htbmcp_response(404, {
            "status": 404, "error": "Not Found",
            "available_taps": list(TAP_REGISTRY.keys()),
            "rfc": "RFC 1516 §3.3.2",
        })

    resp = tap.to_status_dict()
    resp["pour_count"] = len(tap.pour_history)
    resp["note"] = "The data returned by GET contains no beer. This is an important distinction."
    resp["rfc"] = "RFC 1516 §3.1.3"
    return htbmcp_response(200, resp)


@app.get("/taps")
async def get_all_taps():
    """List all available taps."""
    return htbmcp_response(200, {
        "taps": list_taps(),
        "count": len(TAP_REGISTRY),
        "port": 1414,
        "rfc": "RFC 1516",
    })


# ── PROPFIND ──────────────────────────────────────────────────────────────────

@app.api_route("/tap/{tap_id}/styles", methods=["PROPFIND"])
async def propfind(tap_id: str):
    """
    RFC 1516 §3.1.4 — The PROPFIND Method.
    Discover available beer styles and their parameters.
    Inherited from WebDAV RFC 4918, previously adopted by HTCPCP RFC 2324.
    """
    tap = get_tap(tap_id)
    if not tap:
        return htbmcp_response(404, {"status": 404, "error": "Not Found"})

    styles_detail = []
    for style, (lo, hi) in TEMP_RANGES.items():
        styles_detail.append({
            "style": style,
            "available_on_tap": style in tap.compatible_styles,
            "temperature_range": f"{lo}–{hi}°C",
        })

    return htbmcp_response(200, {
        "status": 200,
        "method": "PROPFIND",
        "tap": tap_id,
        "currently_connected": tap.style,
        "compatible_styles": tap.compatible_styles,
        "all_styles": styles_detail,
        "foam_capable": True,
        "foam_levels": ["none", "light", "normal", "heavy", "belgian"],
        "temperature_range": f"{tap.temp_range[0]}–{tap.temp_range[1]}°C",
        "vessel_sizes": ["pint", "half-pint", "stein", "tulip", "goblet (Trappist only)"],
        "non_alcoholic": (
            "NOT_ACCEPTABLE — No provision is made for non-alcoholic beer. "
            "What would be the point? RFC 1516 §3.2.4"
        ),
        "rfc": "RFC 1516 §3.1.4",
    })


# ── 419 Wine Glass ────────────────────────────────────────────────────────────

@app.api_route("/wine-glass/{anything:path}", methods=["TAP", "POUR", "GET", "POST"])
async def wine_glass(anything: str):
    """
    RFC 1516 §3.3.5 — 419 I'm a Wine Glass.
    Any attempt to pour beer into a wine glass SHOULD result in this error.
    The resulting entity body MAY be stemmed and fragile.
    """
    return htbmcp_response(419, {
        "status": 419,
        "error": "I'm a Wine Glass",
        "body": "The resulting entity body MAY be stemmed and fragile.",
        "detail": (
            "A wine glass is not a mug. A wine glass has no handle. "
            "Beer poured into a wine glass loses carbonation 23% faster, "
            "a figure the authors have not verified but feel is directionally correct."
        ),
        "suggestion": "Please provide a proper mug. Steins also acceptable.",
        "rfc": "RFC 1516 §3.3.5",
    })
