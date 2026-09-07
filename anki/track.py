"""Track model for Anki Overdrive.

* piece tables (location id -> lane offset + progress slot), from Anki-Partydrive's offsetInfo.json
* TrackScanner: builds a closed loop from one car's position/transition messages
* Track: pieces laid out on a 560 mm grid, world coordinates for (piece, fraction, lane)
* CarLocalizer: per-car estimate of where it is on the track

Frame conventions: x right, y down (screen-like). Headings 0=E, 1=S, 2=W, 3=N; a right turn is +1.
Lane offsets in mm, negative = left of the driving direction (Anki convention).
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import protocol as P

DATA_FILE = Path(__file__).parent / "data" / "offsetInfo.json"

CELL_MM = 560.0                       # one straight piece
CURVE_RADIUS_MM = CELL_MM / 2         # centerline radius, measured from wheel travel (~280 mm)
HEADINGS = ((1, 0), (0, 1), (-1, 0), (0, -1))

START_ID = 34         # PreFinishLine; the physical start piece reports 34 then 33
FINISH_ID = 33

# kinds
STRAIGHT, TURN, START, CRISSCROSS, JUMP_RAMP, JUMP_LANDING, FNF, UNKNOWN = (
    "Straight", "Turn", "Start", "CrissCross", "JumpRamp", "JumpLanding", "FnFSpecial", "Unknown")


# ---------------------------------------------------------------- piece tables
class _Slot:
    __slots__ = ("offset_mm", "index", "count")

    def __init__(self, offset_mm: float, index: int, count: int):
        self.offset_mm, self.index, self.count = offset_mm, index, count


_TABLE: dict[int, dict] = {}


def _load_table() -> None:
    if _TABLE:
        return
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for key, piece in raw.items():
        slots: dict[int, _Slot] = {}
        for entry in piece["offsets"]:
            ids = entry["ids"]
            for idx, loc in enumerate(ids):
                if loc >= 0:
                    slots[loc] = _Slot(entry["offset"] * 1000.0, idx, len(ids))
        _TABLE[int(key)] = {"type": piece["type"], "slots": slots}


def piece_type(piece_id: int) -> str:
    _load_table()
    t = _TABLE.get(piece_id, {}).get("type", UNKNOWN)
    if t in ("PreFinishLine", "FinishLine"):
        return START
    return t


def locate(piece_id: int, location: int, reverse_parsing: bool) -> Optional[tuple[float, float]]:
    """(lane_mm in the car's frame, fraction along the piece in the *print* orientation) or None.

    The table's offsets are for a car reading the codes forward; when the car reads them reversed
    its left/right swap. Empirically the table sign is opposite to the car's convention, hence the minus.
    """
    _load_table()
    slot = _TABLE.get(piece_id, {}).get("slots", {}).get(location)
    if slot is None:
        return None
    lane = slot.offset_mm if reverse_parsing else -slot.offset_mm
    frac_print = (slot.index + 0.5) / slot.count
    return lane, frac_print


# ---------------------------------------------------------------- track model
@dataclass
class Piece:
    id: int
    kind: str
    turn: Optional[str] = None       # 'L' / 'R' for turns
    reversed: bool = False           # parsing flag seen by the scanning car (defines "forward")
    cell: tuple[int, int] = (0, 0)
    heading_in: int = 0
    heading_out: int = 0
    cells: int = 1                   # jump ramps span 2 cells (ramp + gap)

    @property
    def length_mm(self) -> float:
        if self.kind == TURN:
            return math.pi / 2 * CURVE_RADIUS_MM
        return CELL_MM * self.cells

    def length_at(self, lane_mm: float) -> float:
        if self.kind == TURN:
            r = CURVE_RADIUS_MM - lane_mm if self.turn == "R" else CURVE_RADIUS_MM + lane_mm
            return math.pi / 2 * max(60.0, r)
        return self.length_mm


class Track:
    def __init__(self, pieces: list[Piece]):
        self.pieces = pieces
        self.closed = self._layout()

    def __len__(self) -> int:
        return len(self.pieces)

    # --- layout ---------------------------------------------------------
    def _layout(self) -> bool:
        x, y, h = 0, 0, 0
        for p in self.pieces:
            p.cell, p.heading_in = (x, y), h
            if p.kind == TURN and p.turn in ("L", "R"):
                h = (h + (1 if p.turn == "R" else -1)) % 4
            p.heading_out = h
            dx, dy = HEADINGS[p.heading_in] if p.kind == TURN else HEADINGS[h]
            # a turn occupies the cell entered along the old heading and leaves along the new one
            if p.kind == TURN:
                nx, ny = HEADINGS[p.heading_out]
                x, y = x + nx, y + ny
            else:
                x, y = x + dx * p.cells, y + dy * p.cells
        return (x, y, h) == (0, 0, 0)

    def bounds(self) -> tuple[float, float, float, float]:
        xs = [p.cell[0] for p in self.pieces] + [p.cell[0] + (p.cells - 1) * HEADINGS[p.heading_out][0] for p in self.pieces]
        ys = [p.cell[1] for p in self.pieces] + [p.cell[1] + (p.cells - 1) * HEADINGS[p.heading_out][1] for p in self.pieces]
        return (min(xs) * CELL_MM, min(ys) * CELL_MM, (max(xs) + 1) * CELL_MM, (max(ys) + 1) * CELL_MM)

    # --- geometry -------------------------------------------------------
    def world_point(self, index: int, frac: float, lane_mm: float = 0.0) -> tuple[float, float]:
        """Centerline point (mm) of piece `index` at fraction `frac` in the track's forward direction,
        shifted laterally by lane_mm (negative = left of forward)."""
        p = self.pieces[index % len(self.pieces)]
        frac = min(1.0, max(0.0, frac))
        cx = p.cell[0] * CELL_MM + CELL_MM / 2
        cy = p.cell[1] * CELL_MM + CELL_MM / 2
        hin = HEADINGS[p.heading_in]
        if p.kind != TURN or p.turn not in ("L", "R"):
            h = HEADINGS[p.heading_out]
            ex, ey = cx - h[0] * CELL_MM / 2, cy - h[1] * CELL_MM / 2
            rx, ry = -h[1], h[0]  # right-hand normal (y down)
            L = CELL_MM * p.cells
            return ex + h[0] * L * frac + rx * lane_mm, ey + h[1] * L * frac + ry * lane_mm
        hout = HEADINGS[p.heading_out]
        ex, ey = cx - hin[0] * CELL_MM / 2, cy - hin[1] * CELL_MM / 2       # entry point
        ccx, ccy = ex + hout[0] * CURVE_RADIUS_MM, ey + hout[1] * CURVE_RADIUS_MM  # arc center (cell corner)
        a0 = math.atan2(ey - ccy, ex - ccx)
        sweep = math.pi / 2 if p.turn == "R" else -math.pi / 2  # y-down: right turn = clockwise = +angle
        r = CURVE_RADIUS_MM - lane_mm if p.turn == "R" else CURVE_RADIUS_MM + lane_mm
        a = a0 + sweep * frac
        return ccx + r * math.cos(a), ccy + r * math.sin(a)

    def heading_at(self, index: int, frac: float) -> float:
        """Forward direction angle (radians, y down) at that point."""
        p = self.pieces[index % len(self.pieces)]
        if p.kind != TURN or p.turn not in ("L", "R"):
            h = HEADINGS[p.heading_out]
            return math.atan2(h[1], h[0])
        h0 = HEADINGS[p.heading_in]
        base = math.atan2(h0[1], h0[0])
        return base + (math.pi / 2 if p.turn == "R" else -math.pi / 2) * frac

    def piece_polyline(self, index: int, lane_mm: float = 0.0, steps: int = 12) -> list[tuple[float, float]]:
        p = self.pieces[index]
        n = steps if p.kind == TURN else 2
        return [self.world_point(index, i / n, lane_mm) for i in range(n + 1)]

    # --- persistence ----------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({"version": 1, "closed": self.closed, "pieces": [asdict(p) for p in self.pieces]}, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Track":
        data = json.loads(text)
        pieces = []
        for d in data["pieces"]:
            d = dict(d)
            d["cell"] = tuple(d.get("cell", (0, 0)))
            pieces.append(Piece(**d))
        return cls(pieces)

    def save(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["Track"]:
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def describe(self) -> str:
        parts = []
        for p in self.pieces:
            if p.kind == TURN:
                parts.append(f"turn{p.turn or '?'}({p.id})")
            elif p.kind == START:
                parts.append("START")
            else:
                parts.append(f"{p.kind.lower()}({p.id})")
        return " -> ".join(parts) + ("  [closed]" if self.closed else "  [does not close]")


def build_track(raw: list[dict]) -> Track:
    """raw: [{id, turn, reversed}] as recorded by the scanner for one lap starting at piece 34.
    Merges 34+33 into one Start piece and assigns kinds."""
    pieces: list[Piece] = []
    i = 0
    while i < len(raw):
        r = raw[i]
        pid = r["id"]
        kind = piece_type(pid) if pid is not None else UNKNOWN
        if pid == START_ID and i + 1 < len(raw) and raw[i + 1]["id"] == FINISH_ID:
            pieces.append(Piece(START_ID, START, None, r["reversed"]))
            i += 2
            continue
        if pid == FINISH_ID and pieces and pieces[-1].kind == START:
            i += 1
            continue
        turn = r.get("turn") if kind == TURN else None
        cells = 2 if kind == JUMP_RAMP else 1
        pieces.append(Piece(pid if pid is not None else -1, kind, turn, r["reversed"], cells=cells))
        i += 1
    return Track(pieces)


# ---------------------------------------------------------------- scanner
class TrackScanner:
    """Feed it one car's parsed messages; it records piece ids between transition bars and,
    once it has driven from the start piece back to the start piece, builds a Track."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "waiting for start piece"
        self.current: list[tuple[int, bool]] = []      # (piece id, reverse flag) seen since last transition
        self.lap: list[dict] = []
        self.laps_tried = 0
        self.track: Optional[Track] = None
        self.recording = False
        self.pieces_seen = 0
        self.last_msg_t = 0.0
        # calibration: does "reverse parsing" on a turn mean right or left? learned from wheel data
        self._rev_turn: Counter = Counter()

    def on_message(self, msg_id: int, decoded) -> None:
        with self.lock:
            self.last_msg_t = time.monotonic()
            if msg_id == P.MSG_LOCALIZATION_POSITION_UPDATE and isinstance(decoded, P.PositionUpdate):
                self.current.append((decoded.piece, decoded.reverse_parsing))
                self.pieces_seen += 1
            elif msg_id == P.MSG_LOCALIZATION_TRANSITION_UPDATE and isinstance(decoded, P.TransitionUpdate):
                self._close_piece(decoded)
            elif msg_id == P.MSG_VEHICLE_DELOCALIZED:
                self.state = "car left the track - restarting"
                self.current.clear()
                self.lap.clear()
                self.recording = False

    def _close_piece(self, tr: P.TransitionUpdate) -> None:
        if not self.current:
            if self.recording:
                self.lap.append({"id": None, "turn": tr.turn, "reversed": False})
            return
        pid = Counter(p for p, _ in self.current).most_common(1)[0][0]
        rev = Counter(r for _, r in self.current).most_common(1)[0][0]
        self.current.clear()
        kind = piece_type(pid)
        turn = tr.turn if kind == TURN else None
        if kind == TURN and turn:
            self._rev_turn[(rev, turn)] += 1
        if kind == TURN and turn is None:  # wheels inconclusive: fall back to the learned flag mapping
            for (r, t), _n in self._rev_turn.most_common():
                if r == rev:
                    turn = t
                    break
        entry = {"id": pid, "turn": turn, "reversed": rev}

        if pid == START_ID:
            if self.recording and len(self.lap) >= 3:
                self.laps_tried += 1
                track = build_track(self.lap)
                if track.closed:
                    self.track = track
                    self.state = f"done: {len(track)} pieces"
                    self.recording = False
                    return
                self.state = f"lap {self.laps_tried} did not close ({track.describe()}), retrying"
            self.lap = [entry]
            self.recording = True
            self.state = "recording lap"
            return
        if self.recording:
            self.lap.append(entry)
            self.state = f"recording lap: {len(self.lap)} pieces"

    @property
    def done(self) -> bool:
        return self.track is not None


# ---------------------------------------------------------------- localizer
class CarLocalizer:
    """Where is this car on the track? Updated from its messages, dead-reckoned in between."""

    def __init__(self, track: Track):
        self.track = track
        self.index: Optional[int] = None    # piece index
        self.frac = 0.0                     # fraction along the piece, in track-forward terms
        self.lane_mm = 0.0                  # lateral offset in track-forward terms (negative = left)
        self.direction = 1                  # +1 driving with the track's forward direction, -1 against
        self.speed = 0.0
        self.sub_id: Optional[int] = None   # 34 / 33 while on the start piece
        self.last_code_piece: Optional[int] = None   # piece id of the last code read since the last bar
        self.last_code_t = 0.0
        self.last_update = time.monotonic()
        self.on_track = False

    # ---- message handling (called from the BLE thread) ----
    def on_message(self, msg_id: int, decoded) -> None:
        if msg_id == P.MSG_LOCALIZATION_POSITION_UPDATE and isinstance(decoded, P.PositionUpdate):
            self._on_position(decoded)
        elif msg_id == P.MSG_LOCALIZATION_TRANSITION_UPDATE and isinstance(decoded, P.TransitionUpdate):
            self._on_transition(decoded)
        elif msg_id == P.MSG_VEHICLE_DELOCALIZED:
            self.on_track = False

    def _match(self, pid: int) -> Optional[int]:
        pieces = self.track.pieces
        want = START_ID if pid in (START_ID, FINISH_ID) else pid
        n = len(pieces)
        if self.index is not None:
            for step in (0, 1, 2, -1):
                k = (self.index + step * self.direction) % n
                if pieces[k].id == want:
                    return k
        for k, p in enumerate(pieces):
            if p.id == want:
                return k
        return None

    def _on_position(self, pos: P.PositionUpdate) -> None:
        k = self._match(pos.piece)
        if k is None:
            return
        piece = self.track.pieces[k]
        located = locate(pos.piece, pos.location, pos.reverse_parsing)
        self.index = k
        self.last_code_piece = pos.piece
        self.last_code_t = time.monotonic()
        self.on_track = True
        self.speed = float(pos.speed_mm_s)
        self.last_update = time.monotonic()
        # direction relative to the scan: same reverse flag -> same direction
        self.direction = 1 if pos.reverse_parsing == piece.reversed else -1
        if located is None:
            return
        lane_car, frac_print = located
        # fraction in the track-forward frame: the scanning car read the codes with piece.reversed
        frac = 1.0 - frac_print if piece.reversed else frac_print
        if piece.kind == START:
            self.sub_id = pos.piece
            half = 0.0 if pos.piece == START_ID else 0.5
            frac = half + 0.5 * frac
        self.frac = frac
        self.lane_mm = lane_car if self.direction > 0 else -lane_car

    def _on_transition(self, _tr: P.TransitionUpdate) -> None:
        if self.index is None:
            return
        piece = self.track.pieces[self.index]
        n = len(self.track.pieces)
        if piece.kind == START:
            # the start piece has a bar between its 34 and 33 halves (the finish line). If no code was read
            # in the current half (common at low speed) decide from the dead-reckoned position instead.
            if self.direction > 0 and (self.sub_id == START_ID or (self.sub_id is None and self.frac < 0.85)):
                self.frac, self.sub_id = 0.5, FINISH_ID
                self.last_update = time.monotonic()
                return
            if self.direction < 0 and (self.sub_id == FINISH_ID or (self.sub_id is None and self.frac > 0.15)):
                self.frac, self.sub_id = 0.5, START_ID
                self.last_update = time.monotonic()
                return
        self.sub_id = None
        self.last_code_piece = None
        self.index = (self.index + self.direction) % n
        self.frac = 0.03 if self.direction > 0 else 0.97
        self.last_update = time.monotonic()

    # ---- main-thread helpers ----
    def advance(self, dt: float) -> None:
        if self.index is None or self.speed <= 0:
            return
        piece = self.track.pieces[self.index]
        self.frac += self.direction * self.speed * dt / piece.length_at(self.lane_mm)
        self.frac = min(0.985, max(0.015, self.frac))

    def progress(self) -> Optional[float]:
        """Position along the loop in piece units, track-forward."""
        if self.index is None:
            return None
        return (self.index + self.frac) % len(self.track.pieces)

    def world(self) -> Optional[tuple[float, float]]:
        if self.index is None:
            return None
        return self.track.world_point(self.index, self.frac, self.lane_mm)

    def heading(self) -> float:
        if self.index is None:
            return 0.0
        a = self.track.heading_at(self.index, self.frac)
        return a if self.direction > 0 else a + math.pi

    def distance_ahead_mm(self, other: "CarLocalizer") -> Optional[float]:
        """How far ahead of me (along my driving direction) the other car is, in mm; None if unknown."""
        a, b = self.progress(), other.progress()
        if a is None or b is None:
            return None
        n = len(self.track.pieces)
        d = ((b - a) * self.direction) % n
        return d * (sum(p.length_mm for p in self.track.pieces) / n)
