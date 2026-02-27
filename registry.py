"""
HTBMCP/1.0 — Tap Registry
RFC 1516 §3 — Core domain models

The tap registry is the heart of the architecture.
A tap is identified by a beer:// URI (§4).
"""

from dataclasses import dataclass, field
from enum import Enum
import asyncio


class TapStatus(str, Enum):
    IDLE = "idle"
    TAPPED = "tapped"          # Session open, ready to POUR
    POURING = "pouring"        # POUR start received
    FOAM_OVERFLOW = "foam-overflow"
    EMPTY = "empty"


class VesselSize(str, Enum):
    PINT = "pint"
    HALF_PINT = "half-pint"
    STEIN = "stein"
    TULIP = "tulip"
    GOBLET = "goblet"          # Trappist only — RFC 1516 §4


class FoamLevel(str, Enum):
    NONE = "none"
    LIGHT = "light"
    NORMAL = "normal"          # DEFAULT — RFC 1516 §3.2.2
    HEAVY = "heavy"
    BELGIAN = "belgian"        # Implementation-defined, but significant


# Temperature ranges by style — RFC 1516 §3.2.3
TEMP_RANGES: dict[str, tuple[int, int]] = {
    "Lager":    (3, 7),
    "Pilsner":  (3, 7),
    "Wheat":    (4, 7),
    "IPA":      (7, 10),
    "Ale":      (8, 12),
    "Porter":   (8, 12),
    "Stout":    (10, 13),
    "Lambic":   (10, 14),
    "Sour":     (10, 14),
    "Trappist": (12, 16),
}


@dataclass
class Tap:
    id: str                          # e.g. "tap-1"
    scheme: str                      # beer / bier / bière / piwo / ...
    style: str                       # current beer style on tap
    temp: float                      # current temperature °C
    pressure: float                  # BAR
    level: int                       # keg level 0–100%
    compatible_styles: list[str]     # what this tap can serve

    status: TapStatus = TapStatus.IDLE
    session_id: str | None = None
    brew_version: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pour_history: list[dict] = field(default_factory=list)

    @property
    def uri(self) -> str:
        return f"{self.scheme}://{self.id}"

    @property
    def is_empty(self) -> bool:
        return self.level <= 0

    @property
    def temp_range(self) -> tuple[int, int]:
        return TEMP_RANGES.get(self.style, (2, 20))

    def to_status_dict(self) -> dict:
        return {
            "tap": self.id,
            "uri": self.uri,
            "style": self.style,
            "status": self.status,
            "keg_level": f"{self.level}%",
            "temperature": f"{self.temp}°C",
            "pressure": f"{self.pressure} BAR",
            "session_open": self.status != TapStatus.IDLE,
            "brew_version": self.brew_version,
            "X-Protocol": "HTBMCP/1.0",
            "X-RFC": "RFC-1516",
        }


# ── The Registry ──────────────────────────────────────────────────────────────
# RFC 1516 §4: servers SHOULD respond to regional beer URI variants

TAP_REGISTRY: dict[str, Tap] = {
    "tap-1": Tap(
        id="tap-1", scheme="beer",
        style="IPA", temp=8.0, pressure=2.4, level=75,
        compatible_styles=["IPA", "Ale"],
    ),
    "tap-2": Tap(
        id="tap-2", scheme="beer",
        style="Stout", temp=11.0, pressure=1.8, level=42,
        compatible_styles=["Stout", "Porter"],
    ),
    "tap-3": Tap(
        id="tap-3", scheme="bière",     # French regional URI scheme
        style="Trappist", temp=14.0, pressure=2.1, level=90,
        compatible_styles=["Trappist", "Lambic"],
    ),
    "tap-gdansk": Tap(
        id="tap-gdansk", scheme="piwo",  # Polish — RFC 1516 §1, port 1414 memorial
        style="Lager", temp=4.0, pressure=2.6, level=88,
        compatible_styles=["Lager", "Pilsner"],
    ),
}


def get_tap(tap_id: str) -> Tap | None:
    return TAP_REGISTRY.get(tap_id)


def list_taps() -> list[dict]:
    return [t.to_status_dict() for t in TAP_REGISTRY.values()]
