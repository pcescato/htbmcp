"""
HTBMCP/1.0 — Raw asyncio TCP server
RFC 1516 — Port 1414 (memorial: Gdansk, 1414)

WHY THIS FILE EXISTS:
  uvicorn validates HTTP method names at the socket level (h11 layer),
  before any request parsing. TAP, POUR, and WHEN are not registered
  IANA methods, so uvicorn rejects them with "Invalid HTTP request received"
  — regardless of any FastAPI configuration.

  The fix: a minimal HTTP/1.1 parser over raw asyncio TCP that accepts
  any valid RFC 7230 token as a method name. TAP, POUR, WHEN, PROPFIND
  are all valid RFC 7230 tokens. This is the correct approach.

  FastAPI (main.py) remains useful for the test suite: TestClient bypasses
  the HTTP transport layer entirely, so custom methods work fine there.

ARCHITECTURE:
  asyncio.start_server → HTBMCPProtocol.handle_connection
    → parse_request()
    → dispatch() → handler functions
    → build_response()

USAGE:
  python server.py
  # 🍺 HTBMCP/1.0 listening on 0.0.0.0:1414

  curl -X TAP http://localhost:1414/tap/tap-1 \\
       -H "Content-Type: message/mugpot" \\
       -H "Accept-Style: IPA" \\
       -H "Accept-Temperature: 8" \\
       -d "open"

  curl -X POUR http://localhost:1414/tap/tap-1/pour \\
       -H "Content-Type: message/mugpot" \\
       -H "Accept-Foam: normal" \\
       -d "start"

  curl -X WHEN http://localhost:1414/tap/tap-1/when

  curl http://localhost:1414/tap/tap-1

  curl -X PROPFIND http://localhost:1414/tap/tap-1/styles
"""

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime

import structlog

from registry import (
    TAP_REGISTRY, get_tap, list_taps,
    TapStatus, FoamLevel, TEMP_RANGES,
    VesselSize,
)

# ── Logging ───────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
)
log = structlog.get_logger()

HOST = "0.0.0.0"
PORT = 1414   # RFC 1516 §3 — memorial port


# ── HTTP/1.1 Parser ───────────────────────────────────────────────────────────

class HTTPRequest:
    __slots__ = ("method", "path", "version", "headers", "body", "raw")

    def __init__(self):
        self.method = ""
        self.path = ""
        self.version = "HTTP/1.1"
        self.headers: dict[str, str] = {}
        self.body = b""
        self.raw = b""

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


def parse_request(data: bytes) -> HTTPRequest | None:
    """
    Minimal RFC 7230-compliant HTTP/1.1 parser.
    Accepts any token as a method name — that's the point.
    """
    req = HTTPRequest()
    req.raw = data

    try:
        header_end = data.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        header_part = data[:header_end].decode("utf-8", errors="replace")
        req.body = data[header_end + 4:]

        lines = header_part.split("\r\n")
        request_line = lines[0].split(" ", 2)
        if len(request_line) < 2:
            return None

        req.method = request_line[0].upper()
        req.path = request_line[1]
        req.version = request_line[2] if len(request_line) > 2 else "HTTP/1.1"

        for line in lines[1:]:
            if ": " in line:
                key, _, value = line.partition(": ")
                req.headers[key.lower()] = value.strip()

        # Read body up to Content-Length
        content_length = int(req.headers.get("content-length", 0))
        if content_length and len(req.body) < content_length:
            req.body = req.body[:content_length]

        return req

    except Exception as e:
        log.warning("parse_error", error=str(e))
        return None


# ── Response builder ──────────────────────────────────────────────────────────

def build_response(status: int, body: dict) -> bytes:
    body["X-Protocol"] = "HTBMCP/1.0"
    body["X-RFC"] = "RFC-1516"
    body["X-Port"] = "1414"

    status_text = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        406: "Not Acceptable",
        409: "Conflict",
        419: "I'm a Wine Glass",
        503: "Service Unavailable",
    }.get(status, "Unknown")

    payload = json.dumps(body, indent=2).encode()
    headers = (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"X-Protocol: HTBMCP/1.0\r\n"
        f"X-RFC: RFC-1516\r\n"
        f"X-Port: 1414\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()

    return headers + payload


def err(status: int, error: str, detail: str = "", rfc: str = "") -> bytes:
    body: dict = {"status": status, "error": error}
    if detail:
        body["detail"] = detail
    if rfc:
        body["rfc"] = rfc
    return build_response(status, body)


# ── Validators ────────────────────────────────────────────────────────────────

def check_style(tap_id: str, style: str) -> bytes | None:
    tap = get_tap(tap_id)
    if not tap or style == "*":
        return None
    if style not in tap.compatible_styles and style != tap.style:
        return build_response(406, {
            "status": 406, "error": "Not Acceptable",
            "requested_style": style,
            "available_styles": tap.compatible_styles,
            "rfc": "RFC 1516 §3.3.3",
        })
    return None


def check_temperature(tap_id: str, raw_temp: str | None) -> bytes | None:
    tap = get_tap(tap_id)
    if not tap or not raw_temp:
        return None
    try:
        temp = int(raw_temp)
    except ValueError:
        return None

    if tap.style == "Stout" and temp <= 5:
        return build_response(406, {
            "status": 406,
            "error": "Temperature violation — MUST NOT",
            "detail": (
                "A server MUST NOT serve a Stout at 3°C. "
                "This is not a SHOULD NOT. This is a MUST NOT."
            ),
            "temp_requested": temp,
            "allowed_range": "10–13°C",
            "rfc": "RFC 1516 §3.2.3",
        })

    lo, hi = tap.temp_range
    if temp < lo - 2 or temp > hi + 2:
        return build_response(406, {
            "status": 406, "error": "Temperature out of range",
            "requested": f"{temp}°C",
            "allowed_range": f"{lo}–{hi}°C",
            "style": tap.style,
        })
    return None


def check_vessel(style: str, vessel: str) -> bytes | None:
    if vessel == "goblet" and style not in ("Trappist", "*"):
        return build_response(406, {
            "status": 406, "error": "Vessel conflict",
            "detail": "goblet is reserved for Trappist ales.",
            "rfc": "RFC 1516 §4",
        })
    return None


def check_foam(style: str, foam: str) -> bytes | None:
    if foam == "belgian" and style not in ("Trappist", "Lambic", "*"):
        return build_response(406, {
            "status": 406, "error": "Foam conflict",
            "detail": "belgian foam SHOULD only be applied to Trappist or Lambic styles.",
            "rfc": "RFC 1516 §3.2.2",
        })
    return None


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def dispatch(req: HTTPRequest) -> bytes:
    path = req.path.rstrip("/")
    method = req.method

    # ── wine-glass → 419 ──
    if path.startswith("/wine-glass"):
        log.warning("htbmcp.wine_glass", path=path, status=419)
        return build_response(419, {
            "status": 419, "error": "I'm a Wine Glass",
            "body": "The resulting entity body MAY be stemmed and fragile.",
            "detail": (
                "A wine glass is not a mug. A wine glass has no handle. "
                "Beer poured into a wine glass loses carbonation 23% faster."
            ),
            "rfc": "RFC 1516 §3.3.5",
        })

    # ── /taps — list all ──
    if path == "/taps" and method == "GET":
        return build_response(200, {"taps": list_taps(), "port": PORT, "rfc": "RFC 1516"})

    # ── /tap/{id}/styles — PROPFIND ──
    if path.endswith("/styles") and method == "PROPFIND":
        tap_id = path.split("/")[2]
        tap = get_tap(tap_id)
        if not tap:
            return err(404, "Not Found", f"Tap '{tap_id}' unknown.", "RFC 1516 §3.3.2")

        styles = [
            {"style": s, "available": s in tap.compatible_styles,
             "temp_range": f"{lo}–{hi}°C"}
            for s, (lo, hi) in TEMP_RANGES.items()
        ]
        log.info("htbmcp.propfind", tap=tap_id, status=200)
        return build_response(200, {
            "status": 200, "method": "PROPFIND", "tap": tap_id,
            "connected_style": tap.style,
            "compatible_styles": tap.compatible_styles,
            "all_styles": styles,
            "foam_levels": ["none", "light", "normal", "heavy", "belgian"],
            "vessel_sizes": ["pint", "half-pint", "stein", "tulip", "goblet (Trappist only)"],
            "non_alcoholic": "NOT_ACCEPTABLE — What would be the point? (RFC 1516 §3.2.4)",
            "rfc": "RFC 1516 §3.1.4",
        })

    # ── /tap/{id}/when — WHEN ──
    if path.endswith("/when") and method == "WHEN":
        tap_id = path.split("/")[2]
        tap = get_tap(tap_id)
        if not tap:
            return err(404, "Not Found")

        if tap.status != TapStatus.POURING:
            log.info("htbmcp.when", tap=tap_id, note="no_pour_in_progress")
            return build_response(200, {
                "status": 200, "method": "WHEN", "acknowledged": True,
                "note": "No pour in progress, but the client's enthusiasm is appreciated.",
                "addendum": "There is no WHEN-WHEN method. Once is sufficient.",
                "rfc": "RFC 1516 §3.1.5",
            })

        tap.status = TapStatus.TAPPED
        log.info("htbmcp.when", tap=tap_id, foam_stopped=True)
        return build_response(200, {
            "status": 200, "method": "WHEN", "foam_stopped": True,
            "message": "Server has acknowledged WHEN. Foam dispensing ceased.",
            "note": "There is no WHEN-WHEN method. Once is sufficient.",
            "rfc": "RFC 1516 §3.1.5",
        })

    # ── /tap/{id}/pour — POUR ──
    if path.endswith("/pour") and method == "POUR":
        tap_id = path.split("/")[2]
        tap = get_tap(tap_id)
        if not tap:
            return err(404, "Not Found")

        if tap.status == TapStatus.IDLE:
            return build_response(409, {
                "status": 409, "error": "Conflict — no session",
                "detail": "The server MUST NOT execute POUR if no TAP session is open.",
                "hint": f"Send TAP /tap/{tap_id} with body 'open' first.",
                "rfc": "RFC 1516 §3.1.2",
            })

        if tap.is_empty:
            return err(503, "Service Unavailable", "Keg empty — not dispensing air.", "RFC 1516 §3.3.6")

        body_str = req.body.decode().strip().lower()

        if body_str == "stop":
            tap.status = TapStatus.TAPPED
            log.info("htbmcp.pour", tap=tap_id, body="stop", status=200)
            return build_response(200, {
                "status": 200, "method": "POUR", "body": "stop",
                "note": "POUR stop is equivalent to WHEN.",
            })

        if body_str != "start":
            return err(400, "Bad Request", "POUR body MUST be 'start' or 'stop'.")

        # Concurrent POUR check
        client_version = req.header("x-brew-version")
        if client_version and int(client_version) != tap.brew_version:
            return build_response(409, {
                "status": 409, "error": "Conflict — brew_version mismatch",
                "current": tap.brew_version, "yours": client_version,
                "rfc": "RFC 1516 §3.3.4",
            })

        async with tap.lock:
            tap.status = TapStatus.POURING
            tap.brew_version += 1
            tap.level = max(0, tap.level - 12)
            pour_id = len(tap.pour_history) + 1
            foam = req.header("accept-foam", "normal")
            tap.pour_history.append({"pour_id": pour_id, "foam": foam})

        log.info("htbmcp.pour", tap=tap_id, pour_id=pour_id, foam=foam,
                 keg_remaining=tap.level, status=200)
        return build_response(200, {
            "status": 200, "method": "POUR", "body": "start",
            "Content-Type": "message/mugpot",
            "pour_id": pour_id,
            "tap": tap_id,
            "foam_level": foam,
            "keg_remaining": f"{tap.level}%",
            "X-Brew-Version": tap.brew_version,
            "note": "Beer is flowing. Say WHEN when foam is sufficient.",
            "rfc": "RFC 1516 §3.1.2",
        })

    # ── /tap/{id} — TAP or GET ──
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "tap":
        tap_id = parts[2]
        tap = get_tap(tap_id)

        if method == "GET":
            if not tap:
                return err(404, "Not Found", f"Tap '{tap_id}' unknown.")
            resp = tap.to_status_dict()
            resp["note"] = "GET contains no beer. This is an important distinction."
            log.info("htbmcp.get", tap=tap_id, status=200)
            return build_response(200, resp)

        if method in ("TAP", "POST"):
            if method == "POST":
                post_warn = " (POST accepted but STRONGLY DISCOURAGED)"
            else:
                post_warn = ""

            if not tap:
                return err(404, "Not Found", f"Tap '{tap_id}' unknown.",
                           "RFC 1516 §3.3.2")

            if tap.is_empty:
                return err(503, "Service Unavailable", "Keg empty.", "RFC 1516 §3.3.6")

            body_str = req.body.decode().strip().lower()

            if body_str == "close":
                tap.status = TapStatus.IDLE
                tap.session_id = None
                log.info("htbmcp.tap", tap=tap_id, body="close", status=200)
                return build_response(200, {
                    "status": 200, "method": "TAP", "body": "close",
                    "session": "closed",
                    "note": "Any in-progress POUR requests SHOULD complete. It would be rude otherwise.",
                    "rfc": "RFC 1516 §3.1.1",
                })

            if body_str != "open":
                return err(400, "Bad Request", "TAP body MUST be 'open' or 'close'.")

            if tap.status != TapStatus.IDLE:
                return build_response(409, {
                    "status": 409, "error": "Conflict — session already open",
                    "detail": "MUST NOT silently allow two simultaneous sessions. This is how keg lines get contaminated.",
                    "brew_version": tap.brew_version,
                    "rfc": "RFC 1516 §3.3.4",
                })

            # Validate headers
            style  = req.header("accept-style", "*")
            temp   = req.header("accept-temperature") or None
            vessel = req.header("vessel-size", "pint")
            foam   = req.header("accept-foam", "normal")

            for chk in [
                check_style(tap_id, style),
                check_temperature(tap_id, temp),
                check_vessel(style, vessel),
                check_foam(style, foam),
            ]:
                if chk:
                    return chk

            tap.status = TapStatus.TAPPED
            tap.session_id = str(uuid.uuid4())[:8]
            tap.brew_version += 1

            log.info("htbmcp.tap", tap=tap_id, body="open",
                     style=style, foam=foam, session=tap.session_id, status=200)
            return build_response(200, {
                "status": 200,
                "method": "TAP" + post_warn,
                "body": "open",
                "Content-Type": "message/mugpot",
                "tap": tap_id,
                "uri": f"{tap.uri}/{vessel}",
                "style": tap.style,
                "temperature": f"{tap.temp}°C",
                "pressure": f"{tap.pressure} BAR",
                "accept_foam": foam,
                "session_id": tap.session_id,
                "X-Brew-Version": tap.brew_version,
                "note": "Session opened. POUR requests now accepted.",
                "rfc": "RFC 1516 §3.1.1",
            })

    return err(404, "Not Found", f"No HTBMCP route matches '{method} {path}'.")


# ── Connection handler ────────────────────────────────────────────────────────

async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    try:
        data = await asyncio.wait_for(reader.read(8192), timeout=10.0)
        if not data:
            return

        req = parse_request(data)
        if not req:
            response = err(400, "Bad Request", "Could not parse HTTP/1.1 request.")
        else:
            log.debug("htbmcp.request", method=req.method, path=req.path, peer=addr)
            response = await dispatch(req)

        writer.write(response)
        await writer.drain()

    except asyncio.TimeoutError:
        log.warning("htbmcp.timeout", peer=addr)
    except Exception as e:
        log.error("htbmcp.error", error=str(e), peer=addr)
        try:
            writer.write(err(500, "Internal Server Error", str(e)))
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(handle_connection, HOST, PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)

    print(f"\n🍺  HTBMCP/1.0 — RFC 1516")
    print(f"    Listening on {addrs}")
    print(f"    Port 1414 — memorial: Gdansk municipal archives, 1414")
    print(f"    (the document no longer exists. we pour one out.)\n")
    print(f"    Try:")
    print(f"    curl -X TAP http://localhost:1414/tap/tap-1 \\")
    print(f"         -H 'Content-Type: message/mugpot' \\")
    print(f"         -H 'Accept-Style: IPA' \\")
    print(f"         -H 'Accept-Temperature: 8' \\")
    print(f"         -d 'open'\n")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n🍺  Server stopped. Last round called.")
        sys.exit(0)
