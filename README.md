# HTBMCP/1.0 — Hyper Text Beer Mug Control Protocol

> *"There is beer all around the world. Increasingly, in a world in which computation is ubiquitous, the consumption of beer in proximity to networked devices creates a strong operational requirement."*
>
> — RFC 1516, Abstract

---

## What is this?

This repository is a full implementation of **RFC 1516** — the Hyper Text Beer Mug Control Protocol (HTBMCP/1.0), published April 1st, 2026.

HTBMCP is the spiritual successor to [RFC 2324](https://tools.ietf.org/html/rfc2324) (HTCPCP/1.0, the coffee pot protocol). Where HTCPCP controls coffee pots, HTBMCP controls networked beer taps. The protocol extends HTTP with custom methods, headers, and error codes designed specifically for the distributed dispensing of fermented malt beverages.

The default port is **1414** — a memorial to the earliest known written attestation of the word *"piwo"* (beer, in Polish) in the municipal archives of Gdansk, 1414. That document no longer exists. We pour one out.

---

## What it teaches

Implementing an April Fools' RFC is a serious exercise in disguise. This one specifically teaches:

**How custom HTTP methods actually work** — or don't. `TAP`, `POUR`, and `WHEN` are valid RFC 7230 tokens, but uvicorn rejects them at the socket layer before any application code runs. The fix requires a raw asyncio TCP server. You now understand the HTTP request pipeline better than most developers who've shipped production APIs for years.

**How to read an RFC properly** — distinguishing MUST, SHOULD, and MAY. RFC 1516 uses all three with precision. A server MUST NOT serve a Stout at 3°C. That is not a SHOULD NOT. The authors feel strongly about this.

**Domain modeling** — the tap registry, vessel types, foam levels, concurrent session management with `brew_version` tokens. This is real entity design forced by an absurd but rigorous spec.

**State machines** — `idle → tapped → pouring → tapped → idle`. WHEN is a client-driven state transition. You'll see this pattern in every production system you build.

**Integration testing for edge cases** — the 419 wine glass, the Stout temperature MUST NOT, the goblet reserved for Trappist, the `piwo://` URI scheme. Absurd constraints make excellent test cases.

---

## Architecture

```
htbmcp/
├── registry.py       # Tap registry, domain models, temperature ranges
├── main.py           # FastAPI app — test suite only (TestClient bypass)
├── server.py         # Raw asyncio TCP server — real HTBMCP transport
├── test_htbmcp.py    # Full test suite (35 tests)
└── htbmcp-simulator.html  # Interactive browser simulator
```

### Why two server files?

**`server.py`** is the real implementation. It listens on port 1414 over raw TCP, parses HTTP/1.1 manually, and accepts any RFC 7230 token as a method name. This is the only way to actually receive `TAP`, `POUR`, and `WHEN` requests.

**`main.py`** is the FastAPI application used exclusively by the test suite. FastAPI's `TestClient` bypasses the HTTP transport layer entirely — custom methods work fine there. You get all the validation, schema enforcement, and structured code organisation of FastAPI, without the socket-level rejection.

This is the same architecture used in the [HTCPCP implementation](https://github.com/pcescato/htcpcp) that preceded this one. That project discovered the uvicorn limitation the hard way. This one documents it upfront.

---

## Protocol reference

### Methods

| Method | RFC | Description |
|--------|-----|-------------|
| `TAP` | §3.1.1 | Initiates or closes a dispensing session. MUST precede POUR. POST is accepted but STRONGLY DISCOURAGED. |
| `POUR` | §3.1.2 | Dispenses beer. Body MUST be `start` or `stop`. MUST NOT execute without an open TAP session. |
| `WHEN` | §3.1.5 | Stops foam dispensing. Inherited from HTCPCP RFC 2324. There is no WHEN-WHEN method. Once is sufficient. |
| `GET` | §3.1.3 | Returns tap status. Contains no beer. This is an important distinction. |
| `PROPFIND` | §3.1.4 | Discovers available beer styles and parameters. Inherited from WebDAV RFC 4918. |

### Request headers

| Header | Values | Default |
|--------|--------|---------|
| `Accept-Style` | `Lager`, `Pilsner`, `Ale`, `IPA`, `Stout`, `Porter`, `Wheat`, `Sour`, `Lambic`, `Trappist`, `*` | `*` |
| `Accept-Foam` | `none`, `light`, `normal`, `heavy`, `belgian` | `normal` |
| `Accept-Temperature` | Integer (°C) | — |
| `Vessel-Size` | `pint`, `half-pint`, `stein`, `tulip`, `goblet` | `pint` |
| `Content-Type` | `message/mugpot` | required for TAP/POUR |
| `X-Brew-Version` | Integer | — |

### Return codes

| Code | Meaning |
|------|---------|
| `200 OK` | Beer is being poured. Or request acknowledged. |
| `400 Bad Request` | Body is not a valid `message/mugpot` command. |
| `404 Not Found` | Tap does not exist. Check the menu. |
| `406 Not Acceptable` | Style unavailable, temperature out of range, vessel/foam conflict. |
| `409 Conflict` | Session already open, or `brew_version` mismatch on concurrent POUR. |
| `419 I'm a Wine Glass` | Attempt to pour beer into a wine glass. A wine glass has no handle. |
| `503 Service Unavailable` | Keg is empty. The server SHOULD return this rather than dispensing air. |

### Temperature ranges (RFC 1516 §3.2.3)

| Style | Range |
|-------|-------|
| Lager, Pilsner | 3–7°C |
| Wheat | 4–7°C |
| IPA | 7–10°C |
| Ale, Porter | 8–12°C |
| Stout | 10–13°C — **MUST NOT serve at 3°C** |
| Lambic, Sour | 10–14°C |
| Trappist | 12–16°C |

### URI scheme (RFC 1516 §4)

HTBMCP is international. The canonical scheme is `beer://`, but servers SHOULD also respond to regional variants:

```
beer://      English
bier://      German, Dutch, Afrikaans
bière://     French
birra://     Italian
cerveza://   Spanish
piwo://      Polish — Gdansk, 1414 (the port's namesake)
pivo://      Czech, Slovak, Croatian
```

This implementation includes `piwo://` on `tap-gdansk` as a memorial.

---

## Installation

```bash
pip install fastapi httpx pytest structlog
```

---

## Usage

### Run the TCP server

```bash
python server.py
# 🍺  HTBMCP/1.0 listening on 0.0.0.0:1414
```

### Full workflow with curl

```bash
# 1. PROPFIND — discover what's on tap
curl -X PROPFIND http://localhost:1414/tap/tap-1/styles

# 2. TAP — open a session
curl -X TAP http://localhost:1414/tap/tap-1 \
     -H "Content-Type: message/mugpot" \
     -H "Accept-Style: IPA" \
     -H "Accept-Temperature: 8" \
     -H "Accept-Foam: normal" \
     -H "Vessel-Size: pint" \
     -d "open"

# 3. GET — check status (contains no beer)
curl http://localhost:1414/tap/tap-1

# 4. POUR — start dispensing
curl -X POUR http://localhost:1414/tap/tap-1/pour \
     -H "Content-Type: message/mugpot" \
     -H "Accept-Foam: normal" \
     -d "start"

# 5. WHEN — enough foam
curl -X WHEN http://localhost:1414/tap/tap-1/when

# 6. TAP close — end session
curl -X TAP http://localhost:1414/tap/tap-1 \
     -H "Content-Type: message/mugpot" \
     -d "close"
```

### Provoke interesting errors

```bash
# 406 — MUST NOT serve Stout at 3°C (tap-2 is Stout)
curl -X TAP http://localhost:1414/tap/tap-2 \
     -H "Content-Type: message/mugpot" \
     -H "Accept-Style: Stout" \
     -H "Accept-Temperature: 3" \
     -d "open"

# 406 — goblet is Trappist-only (tap-3 is Trappist)
curl -X TAP http://localhost:1414/tap/tap-1 \
     -H "Content-Type: message/mugpot" \
     -H "Vessel-Size: goblet" \
     -d "open"

# 419 — I'm a Wine Glass
curl -X POUR http://localhost:1414/wine-glass/tap-1

# 409 — session already open
curl -X TAP http://localhost:1414/tap/tap-1 \
     -H "Content-Type: message/mugpot" -d "open"
curl -X TAP http://localhost:1414/tap/tap-1 \
     -H "Content-Type: message/mugpot" -d "open"

# piwo:// — Gdansk memorial tap
curl -X TAP http://localhost:1414/tap/tap-gdansk \
     -H "Content-Type: message/mugpot" \
     -H "Accept-Style: Lager" \
     -H "Accept-Temperature: 4" \
     -d "open"
```

### Run the test suite

```bash
pytest test_htbmcp.py -v
```

35 tests covering the full protocol: TAP/POUR/WHEN/GET/PROPFIND workflows, all 406/409/419/503 conditions, concurrent session detection, `brew_version` conflict, the Gdansk `piwo://` tap, Trappist in a goblet at 14°C with belgian foam, and the WHEN-once-is-sufficient invariant.

### Open the browser simulator

Open `htbmcp-simulator.html` directly in your browser. No server required — the simulator is a complete HTBMCP implementation in JavaScript, running entirely client-side. It was built before the server, for the same reason you build a UI mockup before writing the backend: to feel the protocol before committing to a stack.

---

## The uvicorn problem

The natural instinct when building a Python HTTP server is `uvicorn main:app`. Don't.

uvicorn delegates HTTP parsing to [h11](https://github.com/python-hyper/h11), which validates method names against the IANA HTTP Method Registry before any application code runs. `TAP`, `POUR`, and `WHEN` are not in that registry. uvicorn rejects them with `Invalid HTTP request received` — unconditionally, before FastAPI sees anything.

The fix is `server.py`: a minimal HTTP/1.1 parser over raw `asyncio.start_server`. It accepts any string that is a valid RFC 7230 method token — which `TAP`, `POUR`, and `WHEN` are. This is actually the more correct approach: HTBMCP is its own protocol that extends HTTP, and taking ownership of the transport layer is the honest implementation.

FastAPI and `main.py` remain valuable for testing. `TestClient` from Starlette injects requests directly into the ASGI application, bypassing the transport layer entirely. Custom methods work fine. You get all the structured routing, validation, and schema benefits of FastAPI at test time, without the socket-level rejection at runtime.

---

## The taps

| Tap | Scheme | Style | Temp | Level |
|-----|--------|-------|------|-------|
| `tap-1` | `beer://` | IPA | 8°C | 75% |
| `tap-2` | `beer://` | Stout | 11°C | 42% |
| `tap-3` | `bière://` | Trappist | 14°C | 90% |
| `tap-gdansk` | `piwo://` | Lager | 4°C | 88% |

`tap-gdansk` uses the `piwo://` URI scheme in honor of the 1414 Gdansk municipal archive entry that gave HTBMCP its port number. The document no longer exists. Port 1414 is therefore a memorial as much as a transport binding.

---

## References

- [RFC 1516](https://dev.to/pascal_cescato_692b7a8a20) — HTBMCP/1.0 (this protocol)
- [RFC 2324](https://tools.ietf.org/html/rfc2324) — HTCPCP/1.0, the coffee pot protocol. Required reading. The spiritual predecessor.
- [RFC 7168](https://tools.ietf.org/html/rfc7168) — HTCPCP-TEA, for tea appliances.
- [RFC 4918](https://tools.ietf.org/html/rfc4918) — WebDAV. Source of the PROPFIND method.
- [RFC 7230](https://tools.ietf.org/html/rfc7230) — HTTP/1.1 Message Syntax. Defines valid method tokens.
- [The Reinheitsgebot](https://en.wikipedia.org/wiki/Reinheitsgebot), 1516 — the first formal beer specification. Predates the IETF by approximately 460 years. No errata have been filed.
- [HTCPCP implementation](https://github.com/pcescato/htcpcp) — the predecessor project that discovered the uvicorn problem and documented it so this one didn't have to.

---

## Author

Pascal Cescato — [dev.to/pascal_cescato_692b7a8a20](https://dev.to/pascal_cescato_692b7a8a20)

*Built for the DEV Community "Build for Your Community" hackathon, February 2026.*

*The Reinheitsgebot requires no acknowledgement. It has been enforcing standards compliance since 1516 without asking for credit.*
