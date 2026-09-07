"""Game logic: per-car driving (human or AI), virtual weapons, damage and respawn."""
from __future__ import annotations

import logging
import math
from pathlib import Path
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from anki import protocol as P
from anki.ble_worker import BleWorker
from anki.track import CarLocalizer, Track
from anki.vehicle import Vehicle
from . import inputs as I
from .battery import BatteryMonitor

log = logging.getLogger("game")

WARN_COLOR = (255, 176, 70)
PLAYER_COLORS = [(255, 90, 90), (90, 190, 255), (120, 240, 120), (255, 220, 90), (230, 130, 255), (255, 160, 70)]
ENGINE_COLORS = [(14, 0, 0), (0, 4, 14), (0, 14, 0), (14, 10, 0), (12, 0, 14), (14, 5, 0)]
LANES = (-68.0, -23.0, 23.0, 68.0)
MAX_ABS_OFFSET = 68.0

SPEED_SEND_MIN_DELTA = 15
SPEED_KEEPALIVE_S = 0.5
LANE_SEND_MIN_DELTA = 3.0
LANE_SEND_INTERVAL_S = 0.1
LANE_SYNC_IDLE_S = 1.5
STEER_RATE_MM_S = 120.0
# gentle acceleration: hard motor current on tired batteries browns out the BLE radio (cars reboot)
ACCEL_UP = 600
ACCEL_DOWN = 1500
ACCEL_STOP = 2500

MAX_HP = 100
BLASTER_RANGE_MM = 1400.0
BLASTER_COOLDOWN_S = 1.0
BLASTER_DAMAGE = 20
BLASTER_SLOW_FACTOR = 0.45
BLASTER_SLOW_S = 1.5
MINE_COOLDOWN_S = 4.0
MINE_MAX_ACTIVE = 2
MINE_LIFE_S = 45.0
MINE_DAMAGE = 35
MINE_STUN_S = 1.2
MINE_TRIGGER_MM = 120.0
MINE_LANE_MM = 35.0
MINE_ARM_S = 1.0
DEATH_STOP_S = 3.0
GRID_SPEED = 320            # speed used to drive to the start line
GRID_CRAWL = 200            # final approach speed: slow and identical for every car so they stop aligned
GRID_TIMEOUT_S = 12.0       # a car that reads no codes for this long is not on the track
GRID_STOP_FRAC = 0.24       # stop once this far into the start piece (before the finish line at 0.5)
COUNTDOWN_S = 3.0
BATTERY_LOG = Path(__file__).resolve().parents[1] / "battery_log.csv"


@dataclass
class Laser:
    x0: float
    y0: float
    x1: float
    y1: float
    color: tuple
    until: float
    hit: bool


@dataclass
class Effect:
    """Transient visual: 'ring' (hit), 'boom' (destroyed / mine), 'text' (floating damage), 'spawn'."""
    kind: str
    x: float
    y: float
    t0: float
    duration: float
    color: tuple
    text: str = ""


@dataclass
class Mine:
    owner: "CarState"
    progress: float
    lane_mm: float
    x: float
    y: float
    placed_at: float


class CarState:
    """One connected car and everything the game knows about it."""

    def __init__(self, car: Vehicle, index: int, ble: BleWorker, max_speed: int):
        self.car = car
        self.index = index
        self.color = PLAYER_COLORS[index % len(PLAYER_COLORS)]
        self.engine_rgb = ENGINE_COLORS[index % len(ENGINE_COLORS)]
        self.ble = ble
        self.max_speed = max_speed
        self.source: Optional[I.InputSource] = None
        self.ai = False
        self.ai_pref = True                 # pairing screen: unclaimed car -> AI (True) or parked (False)
        self.loc: Optional[CarLocalizer] = None
        self.extra_listener = None          # e.g. the track scanner while scanning
        self.battery = BatteryMonitor(car.model, BATTERY_LOG)
        if car.battery_mv:
            self.battery.update(car.battery_mv, 0)
        self.grid_state = ""              # '', 'driving', 'placed', 'off track', 'u-turn'
        self.grid_lane = 0.0
        self.grid_t0 = 0.0
        self.grid_uturn_t = 0.0
        self.grid_laps = 0
        self.laps = 0
        self.lap_start_t: Optional[float] = None
        self.last_lap_s: Optional[float] = None
        self.best_lap_s: Optional[float] = None
        self._prev_progress: Optional[float] = None
        self._lap_armed = True             # re-armed once the car is well past the start piece

        self.hp = MAX_HP
        self.kills = 0
        self.deaths = 0
        self.target_offset = 0.0
        self.penalty_factor = 1.0
        self.penalty_until = 0.0
        self.stun_until = 0.0
        self.dead_until = 0.0
        self.fire_ready_at = 0.0
        self.mine_ready_at = 0.0
        self.ai_next_lane_t = 0.0
        self.ai_uturn_t = 0.0
        self.lights_restore_at = 0.0

        self._last_speed_sent = 0
        self._last_speed_t = 0.0
        self._last_lane_sent = 0.0
        self._last_lane_t = 0.0
        self._last_steer_t = 0.0
        self._last_offset_told = 0.0
        self._lane_retry_t = 0.0
        self.desired_lane: Optional[float] = None   # sticky lane goal (snap / AI / grid); None while steering freely

    # ---- identity ----
    @property
    def name(self) -> str:
        return self.car.model

    def driver_label(self) -> str:
        if self.source is not None:
            return f"{self.source.short} · {self.source.name}"
        return "AI" if self.ai else "parked"

    # ---- messages (BLE thread) ----
    def on_message(self, msg_id: int, decoded) -> None:
        if msg_id == P.MSG_BATTERY_LEVEL_RESPONSE and isinstance(decoded, dict):
            self.battery.update(int(decoded["battery_mv"]), self._last_speed_sent, charging=bool(self.car.on_charger))
        elif msg_id == P.MSG_STATUS_UPDATE and isinstance(decoded, P.VehicleStatus):
            self.battery.charging = decoded.on_charger
            if decoded.on_charger and self.loc is not None:
                self.loc.on_track = False
        elif msg_id == P.MSG_SPEED_UPDATE and isinstance(decoded, P.SpeedUpdate) and self.loc is not None:
            self.loc.speed = float(decoded.actual_mm_s)
        if self.loc is not None:
            self.loc.on_message(msg_id, decoded)
        if self.extra_listener is not None:
            self.extra_listener(msg_id, decoded)

    def set_track(self, track: Optional[Track]) -> None:
        self.loc = CarLocalizer(track) if track else None

    @property
    def charging(self) -> bool:
        return bool(self.car.on_charger)

    def track_status(self) -> str:
        """'charging' | 'on track' | 'off track' | 'idle' | 'link lost'. A stopped car reads no codes, so its
        position is simply unknown until it moves; only call it off track when it has been driving without codes."""
        if not self.car.connected:
            return "link lost"
        if self.charging:
            return "charging"
        if self.localized():
            return "on track"
        now = time.monotonic()
        moving_for = now - self._last_speed_t if self._last_speed_sent > 0 else 0.0
        if self._last_speed_sent > 0 and moving_for > 3.0:
            return "off track"
        return "idle"

    def localized(self) -> bool:
        return (self.car.connected and self.loc is not None and self.loc.on_track and self.loc.index is not None
                and time.monotonic() - self.loc.last_update < 6.0)

    # ---- status ----
    def is_dead(self, now: float) -> bool:
        return now < self.dead_until

    def speed_sent(self) -> int:
        return self._last_speed_sent

    # ---- commands ----
    def _send(self, payload: bytes) -> None:
        self.ble.fire(self.car.send(payload))

    def command_speed(self, desired: float, now: float) -> None:
        factor = self.penalty_factor if now < self.penalty_until else 1.0
        if now < self.stun_until or now < self.dead_until:
            speed = 0
        else:
            speed = int(round(desired * factor))
        speed = max(0, min(2000, speed))
        accel = ACCEL_STOP if speed == 0 else (ACCEL_DOWN if speed < self._last_speed_sent else ACCEL_UP)
        changed = abs(speed - self._last_speed_sent) >= SPEED_SEND_MIN_DELTA
        zeroing = speed == 0 and self._last_speed_sent != 0
        stale = speed != self._last_speed_sent and now - self._last_speed_t > SPEED_KEEPALIVE_S
        if changed or zeroing or stale:
            self._last_speed_sent, self._last_speed_t = speed, now
            self.ble.fire(self.car.set_speed(speed, accel))
        if speed == 0 and self.loc is not None:
            self.loc.speed = 0.0

    def hard_stop(self, now: float) -> None:
        self._last_speed_sent, self._last_speed_t = 0, now
        self.ble.fire(self.car.set_speed(0, ACCEL_STOP))
        if self.loc is not None:
            self.loc.speed = 0.0

    def participates(self) -> bool:
        return self.source is not None or self.ai

    def steer(self, amount: float, dt: float, now: float) -> None:
        if amount:
            self.target_offset += amount * STEER_RATE_MM_S * dt
            self._last_steer_t = now
            self.desired_lane = None

    def snap_lane(self, direction: int, now: float) -> None:
        if direction < 0:
            cands = [o for o in LANES if o < self.target_offset - 1]
            self.target_offset = max(cands) if cands else LANES[0]
        else:
            cands = [o for o in LANES if o > self.target_offset + 1]
            self.target_offset = min(cands) if cands else LANES[-1]
        self.desired_lane = self.target_offset
        self._last_steer_t = now

    def set_lane(self, lane: float, now: float) -> None:
        """Go to (and stay in) this lane."""
        self.desired_lane = lane
        self.target_offset = lane
        self._last_steer_t = now

    def actual_lane(self) -> Optional[float]:
        """Lane read from the track codes, in the car's own left/right frame."""
        if not self.localized() or self.loc is None:
            return None
        return max(-MAX_ABS_OFFSET, min(MAX_ABS_OFFSET, self.loc.lane_mm * self.loc.direction))

    def _tell_offset(self, lane_car: float) -> None:
        self._last_offset_told = lane_car
        self._send(P.set_offset_from_road_center(lane_car))

    def apply_lane(self, now: float) -> None:
        """Lane control. The car only knows the offset we tell it (0 at connect), so every lane command is
        preceded by a sync of its offset from the track codes; a sticky desired lane is re-tried when the
        codes show the car settled somewhere else."""
        self.target_offset = max(-MAX_ABS_OFFSET, min(MAX_ABS_OFFSET, self.target_offset))
        steering = now - self._last_steer_t < 0.3
        lane_car = self.actual_lane()
        if abs(self.target_offset - self._last_lane_sent) >= LANE_SEND_MIN_DELTA and now - self._last_lane_t > LANE_SEND_INTERVAL_S:
            if lane_car is not None and abs(lane_car - self._last_offset_told) > 6 and now - self._last_lane_t > 0.8:
                self._tell_offset(lane_car)
            self.ble.fire(self.car.change_lane(self.target_offset, h_speed=250 if steering else 400, h_accel=1500))
            self._last_lane_sent, self._last_lane_t = self.target_offset, now
            return
        idle = now - self._last_steer_t > LANE_SYNC_IDLE_S and now - self._last_lane_t > LANE_SYNC_IDLE_S
        if not idle or lane_car is None:
            return
        if self.desired_lane is not None and abs(lane_car - self.desired_lane) > 15 and now - self._lane_retry_t > 2.0:
            # settled in the wrong lane: correct the car's belief and ask again
            self._tell_offset(lane_car)
            self.ble.fire(self.car.change_lane(self.desired_lane, h_speed=400, h_accel=1500))
            self.target_offset = self._last_lane_sent = self.desired_lane
            self._last_lane_t = self._lane_retry_t = now
            return
        if self.desired_lane is None:
            self.target_offset = self._last_lane_sent = lane_car
        if abs(lane_car - self._last_offset_told) > 6:
            self._tell_offset(lane_car)

    def u_turn(self) -> None:
        self._send(P.turn(P.TURN_UTURN, P.TURN_TRIGGER_IMMEDIATE))

    # ---- lights ----
    def lights_idle(self) -> None:
        r, g, b = self.engine_rgb
        self._send(P.engine_color(r, g, b))
        self._send(P.lights_on(P.LIGHT_HEADLIGHTS))

    def lights_flash(self, now: float, seconds: float = 1.0) -> None:
        self._send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 14, 10), (P.LC_RED, P.EFFECT_FLASH, 0, 14, 10),
                                     (P.LC_FRONTL, P.EFFECT_FLASH, 0, 14, 10)]))
        self.lights_restore_at = now + seconds

    def lights_identify(self, on: bool) -> None:
        self._send(P.lights_on(P.LIGHT_HEADLIGHTS | P.LIGHT_FRONTLIGHTS) if on else P.lights_off(P.LIGHT_HEADLIGHTS | P.LIGHT_FRONTLIGHTS))

    def world(self) -> Optional[tuple[float, float]]:
        return self.loc.world() if self.loc else None


class Game:
    def __init__(self, ble: BleWorker, cars: list[CarState], track: Optional[Track], ai_enabled: bool, ai_speed: int):
        self.ble = ble
        self.cars = cars
        self.track = track
        self.ai_enabled = ai_enabled
        self.ai_speed = ai_speed
        self.mines: list[Mine] = []
        self.lasers: list[Laser] = []
        self.fx: list[Effect] = []
        self.events: deque[tuple[float, str, tuple]] = deque(maxlen=8)
        self.race_start_t: Optional[float] = None
        self.phase = "idle"                 # idle | grid | countdown | race
        self.countdown_t0 = 0.0
        self.mode = "battle"                # battle (weapons, kills) | race (laps only, no weapons)
        self.lap_target = 5
        self.winner: Optional[CarState] = None
        self.finish_t = 0.0
        self.running = False
        for c in cars:
            c.set_track(track)

    # ---- lifecycle ----
    def set_track(self, track: Optional[Track]) -> None:
        self.track = track
        for c in self.cars:
            c.set_track(track)
        self.mines.clear()

    # ---- starting grid --------------------------------------------------
    def assign_roles(self) -> None:
        for c in self.cars:
            c.ai = self.ai_enabled and c.source is None and c.ai_pref

    def begin_grid(self) -> None:
        """Send every participating car to the start line, one lane each."""
        now = time.monotonic()
        self.assign_roles()
        self.phase = "grid"
        self.running = False
        lane_i = 0
        for c in self.cars:
            c.lights_idle()
            if not c.participates() or not c.car.connected or self.track is None:
                c.grid_state = ""
                c.hard_stop(now)
                continue
            if c.charging:
                c.grid_state = "charging"
                c.hard_stop(now)
                self.log(f"{c.name} is on its charger - left out of the race", WARN_COLOR)
                continue
            c.grid_lane = LANES[lane_i % len(LANES)]
            lane_i += 1
            c.grid_state = "driving"
            c.grid_t0 = now
            c.grid_uturn_t = 0.0
            c.grid_laps = 0
            c.set_lane(c.grid_lane, now)
            c._last_lane_sent = 999.0     # force a lane command once the car is localized
            c.command_speed(GRID_SPEED, now)
        self.log("forming the grid: cars drive to the start line")

    def grid_tick(self, dt: float) -> bool:
        """Advance the grid phase. Returns True when every participant is placed or given up on."""
        now = time.monotonic()
        for c in self.cars:
            if c.loc:
                c.loc.advance(dt)
        done = True
        for c in self.cars:
            st = c.grid_state
            if st in ("", "placed", "off track", "charging"):
                if st == "placed":
                    c.command_speed(0, now)
                continue
            done = False
            if not c.car.connected:
                c.grid_state = "off track"
                continue
            if not c.localized():
                if now - c.grid_t0 > GRID_TIMEOUT_S:
                    c.grid_state = "off track"
                    c.hard_stop(now)
                    self.log(f"{c.name} is not on the track - left out of the race", WARN_COLOR)
                else:
                    c.command_speed(GRID_SPEED, now)
                continue
            loc = c.loc
            assert loc is not None
            if loc.direction < 0:
                # driving the wrong way round: turn around, then keep going
                if now - c.grid_uturn_t > 4.0:
                    c.u_turn()
                    c.grid_uturn_t = now
                    c.grid_state = "u-turn"
                c.command_speed(GRID_SPEED, now)
                continue
            n = len(self.track.pieces) if self.track else 1
            lane_now = c.actual_lane()
            in_lane = lane_now is not None and abs(lane_now - c.grid_lane) <= 15
            # final approach: crawl from the second half of the last piece so every car enters the start
            # piece at the same slow speed, then stop on the first code of the start piece (same spot in every lane)
            approaching = (loc.index == n - 1 and loc.frac > 0.5) or (loc.index == 0 and loc.frac < 0.8)
            if approaching and in_lane:
                c.grid_state = "approach"
                c.command_speed(GRID_CRAWL, now)
            else:
                c.grid_state = "driving"
                c.command_speed(GRID_SPEED, now)
            c.apply_lane(now)
            # stop on the finish-line bar in the middle of the start piece: it is read in every lane at the
            # same spot and detected even at crawl speed (codes are missed often below ~250 mm/s). The same
            # event ends a lap, so the cars park exactly where a winner stops.
            on_bar = c.grid_state == "approach" and loc.index == 0 and loc.sub_id == 33 and 0.5 <= loc.frac < 0.6
            fallback = loc.index == 0 and loc.frac >= 0.8   # only if the finish-line bar was somehow missed
            if on_bar or fallback:
                if not in_lane and now - c.grid_t0 < GRID_TIMEOUT_S * 2 and c.grid_laps < 2:
                    # wrong lane at the line: do another lap and try again
                    c.grid_laps += 1
                    self.log(f"{c.name} reached the line in lane {lane_now if lane_now is None else round(lane_now):+}, wants {c.grid_lane:+.0f} - one more lap", c.color)
                    c._last_lane_sent = 999.0
                    c.grid_t0 = now
                else:
                    c.hard_stop(now)
                    c.grid_state = "placed"
                    how = "on the finish-line bar" if on_bar else f"by dead reckoning at {loc.frac:.2f}"
                    self.log(f"{c.name} on the start line {how} (lane {lane_now if lane_now is None else round(lane_now):+} / {c.grid_lane:+.0f})", c.color)
        return done

    def grid_summary(self) -> tuple[int, int]:
        placed = sum(1 for c in self.cars if c.grid_state == "placed")
        total = sum(1 for c in self.cars if c.grid_state)
        return placed, total

    def begin_countdown(self) -> None:
        self.phase = "countdown"
        self.countdown_t0 = time.monotonic()
        for c in self.cars:
            if c.grid_state == "placed":
                c.lights_flash(self.countdown_t0, COUNTDOWN_S)

    def countdown_left(self) -> float:
        return max(0.0, COUNTDOWN_S - (time.monotonic() - self.countdown_t0))

    @property
    def weapons_enabled(self) -> bool:
        return self.mode == "battle"

    @property
    def finished(self) -> bool:
        return self.winner is not None

    def wrong_way(self, c: CarState) -> bool:
        return c.localized() and c.loc is not None and c.loc.direction < 0

    def start(self) -> None:
        now = time.monotonic()
        self.running = True
        self.phase = "race"
        self.race_start_t = now
        self.winner = None
        self.finish_t = 0.0
        self.mines.clear()
        self.lasers.clear()
        self.fx.clear()
        for c in self.cars:
            c.hp = MAX_HP
            c.kills = c.deaths = c.laps = 0
            c.last_lap_s = c.best_lap_s = None
            c.lap_start_t = None
            c._prev_progress = None
            c._lap_armed = True
            c.dead_until = c.stun_until = c.penalty_until = 0.0
            if c.grid_state in ("off track", "charging"):
                c.ai = False
                c.source = None
            elif not c.grid_state:
                c.ai = self.ai_enabled and c.source is None and c.ai_pref
            c.ai_next_lane_t = now + random.uniform(3, 6)
            c.lights_idle()
        self.log("race started")

    def stop_all(self) -> None:
        now = time.monotonic()
        self.running = False
        self.phase = "idle"
        for c in self.cars:
            c.grid_state = ""
        for c in self.cars:
            c.hard_stop(now)

    def log(self, text: str, color: tuple = (232, 232, 236)) -> None:
        self.events.appendleft((time.monotonic(), text, color))
        log.info(text)

    def effect(self, kind: str, pos: Optional[tuple[float, float]], color: tuple, duration: float, text: str = "") -> None:
        if pos is None:
            return
        self.fx.append(Effect(kind, pos[0], pos[1], time.monotonic(), duration, color, text))

    def ranking(self) -> list["CarState"]:
        def progress(c: CarState) -> float:
            p = c.loc.progress() if c.localized() and c.loc else None
            return p if p is not None else -1.0
        if self.mode == "race":
            return sorted(self.cars, key=lambda c: (-c.laps, -progress(c), c.index))
        return sorted(self.cars, key=lambda c: (-c.kills, c.deaths, -c.laps, c.index))

    def _track_laps(self, c: CarState, now: float) -> None:
        """Count forward crossings of the finish line (middle of the start piece, progress 0.5)."""
        if not c.localized():
            c._prev_progress = None
            return
        p = c.loc.progress()  # type: ignore[union-attr]
        prev = c._prev_progress
        c._prev_progress = p
        if p is None or prev is None or c.loc.direction < 0:  # type: ignore[union-attr]
            return
        n = len(self.track.pieces) if self.track else 1
        if 1.5 <= p <= n - 0.5:
            c._lap_armed = True   # clearly away from the start piece: the next crossing is a real one
        if c._lap_armed and prev < 0.5 <= p and p - prev < 0.6:
            c._lap_armed = False
            if c.lap_start_t is not None:
                lap = now - c.lap_start_t
                if lap > 2.0:
                    c.laps += 1
                    c.last_lap_s = lap
                    if c.best_lap_s is None or lap < c.best_lap_s:
                        c.best_lap_s = lap
                        self.log(f"{c.name} lap {c.laps}: {lap:.2f}s (best)", c.color)
                    else:
                        self.log(f"{c.name} lap {c.laps}: {lap:.2f}s", c.color)
                    if self.mode == "race" and self.winner is None and c.laps >= self.lap_target:
                        self.winner = c
                        self.finish_t = now
                        self.effect("boom", c.world(), c.color, 1.5)
                        self.log(f"{c.name} WINS the race!", c.color)
            c.lap_start_t = now

    # ---- per frame ----
    def tick(self, dt: float) -> None:
        now = time.monotonic()
        for c in self.cars:
            if c.loc:
                c.loc.advance(dt)
        self.fx = [e for e in self.fx if now - e.t0 < e.duration]
        if not self.running:
            return
        if self.finished:
            # let the others roll for a moment, then everybody stops
            for c in self.cars:
                self._track_laps(c, now)
                if now - self.finish_t > 2.0:
                    c.command_speed(0, now)
                else:
                    c.command_speed(min(c.speed_sent(), 300), now) if c is not self.winner else c.command_speed(0, now)
            return
        for c in self.cars:
            self._track_laps(c, now)
            if c.hp <= 0 and not c.is_dead(now):
                c.hp = MAX_HP
                c.lights_idle()
                self.effect("spawn", c.world(), c.color, 0.8)
                self.log(f"{c.name} respawned", c.color)
            if c.lights_restore_at and now > c.lights_restore_at:
                c.lights_restore_at = 0.0
                c.lights_idle()
            if c.source is not None and c.source.alive():
                self._human(c, dt, now)
            elif c.ai:
                self._ai(c, dt, now)
            else:
                c.command_speed(0, now)
            c.apply_lane(now)
        self._resolve_mines(now)
        self.lasers = [l for l in self.lasers if l.until > now]
        self.mines = [m for m in self.mines if now - m.placed_at < MINE_LIFE_S]

    def _human(self, c: CarState, dt: float, now: float) -> None:
        src = c.source
        assert src is not None
        if src.pressed(I.LIMIT_UP):
            c.max_speed = min(2000, c.max_speed + 100)
        if src.pressed(I.LIMIT_DOWN):
            c.max_speed = max(200, c.max_speed - 100)
        if src.pressed(I.STOP):
            c.hard_stop(now)
            src.rumble(0.8, 0.8, 150)
        else:
            c.command_speed(src.throttle() * (1.0 - src.brake()) * c.max_speed, now)
        c.steer(src.steer(), dt, now)
        if src.pressed(I.LANE_LEFT):
            c.snap_lane(-1, now)
        if src.pressed(I.LANE_RIGHT):
            c.snap_lane(+1, now)
        if src.pressed(I.UTURN):
            c.u_turn()
            src.rumble(0.3, 0.6, 120)
        if src.pressed(I.FIRE):
            self.fire(c, now)
        if src.pressed(I.MINE):
            self.drop_mine(c, now)

    def _ai(self, c: CarState, dt: float, now: float) -> None:
        if self.wrong_way(c):
            # never race against the track direction: turn around (once every few seconds until it sticks)
            if now - c.ai_uturn_t > 4.0:
                c.u_turn()
                c.ai_uturn_t = now
                self.log(f"{c.name} turns around (wrong way)", c.color)
            c.command_speed(min(self.ai_speed, 300), now)
            return
        c.command_speed(self.ai_speed, now)
        if now > c.ai_next_lane_t:
            c.set_lane(random.choice(LANES), now)
            c.ai_next_lane_t = now + random.uniform(4, 9)
        if not self.weapons_enabled:
            return
        if now > c.fire_ready_at and random.random() < dt * 0.6 and self.target_ahead(c) is not None:
            self.fire(c, now)
        if now > c.mine_ready_at and random.random() < dt * 0.06:
            self.drop_mine(c, now)

    # ---- weapons ----
    def target_ahead(self, shooter: CarState) -> Optional[tuple[CarState, float]]:
        if not shooter.localized():
            return None
        best = None
        for o in self.cars:
            if o is shooter or not o.localized() or o.hp <= 0:
                continue
            d = shooter.loc.distance_ahead_mm(o.loc)  # type: ignore[union-attr]
            if d is None or d <= 0 or d > BLASTER_RANGE_MM:
                continue
            if best is None or d < best[1]:
                best = (o, d)
        return best

    def fire(self, shooter: CarState, now: float) -> None:
        if not self.weapons_enabled or now < shooter.fire_ready_at or shooter.is_dead(now) or not shooter.localized():
            return
        shooter.fire_ready_at = now + BLASTER_COOLDOWN_S
        p0 = shooter.world()
        if p0 is None:
            return
        target = self.target_ahead(shooter)
        if target is not None:
            victim, _d = target
            p1 = victim.world() or p0
            self.lasers.append(Laser(p0[0], p0[1], p1[0], p1[1], shooter.color, now + 0.25, True))
            self.damage(victim, shooter, BLASTER_DAMAGE, now, slow=True)
            if shooter.source:
                shooter.source.rumble(0.2, 0.7, 120)
        else:
            a = shooter.loc.heading()  # type: ignore[union-attr]
            p1 = (p0[0] + math.cos(a) * 500, p0[1] + math.sin(a) * 500)
            self.lasers.append(Laser(p0[0], p0[1], p1[0], p1[1], shooter.color, now + 0.15, False))

    def drop_mine(self, owner: CarState, now: float) -> None:
        if not self.weapons_enabled or now < owner.mine_ready_at or owner.is_dead(now) or not owner.localized():
            return
        if sum(1 for m in self.mines if m.owner is owner) >= MINE_MAX_ACTIVE:
            return
        prog = owner.loc.progress()  # type: ignore[union-attr]
        w = owner.world()
        if prog is None or w is None:
            return
        owner.mine_ready_at = now + MINE_COOLDOWN_S
        lane = owner.loc.lane_mm  # type: ignore[union-attr]
        self.mines.append(Mine(owner, prog, lane, w[0], w[1], now))
        self.log(f"{owner.name} dropped a mine", owner.color)

    def _resolve_mines(self, now: float) -> None:
        if not self.track or not self.mines:
            return
        n = len(self.track.pieces)
        avg_len = sum(p.length_mm for p in self.track.pieces) / n
        exploded: list[Mine] = []
        for m in self.mines:
            if now - m.placed_at < MINE_ARM_S:
                continue
            for c in self.cars:
                if c is m.owner or not c.localized() or c.is_dead(now):
                    continue
                prog = c.loc.progress()  # type: ignore[union-attr]
                if prog is None:
                    continue
                d = abs(prog - m.progress) % n
                d = min(d, n - d) * avg_len
                if d < MINE_TRIGGER_MM and abs(c.loc.lane_mm - m.lane_mm) < MINE_LANE_MM:  # type: ignore[union-attr]
                    exploded.append(m)
                    self.effect("boom", (m.x, m.y), (255, 150, 60), 0.9)
                    self.damage(c, m.owner, MINE_DAMAGE, now, stun=True)
                    self.log(f"{c.name} ran into {m.owner.name}'s mine", (255, 170, 80))
                    break
        for m in exploded:
            if m in self.mines:
                self.mines.remove(m)

    def damage(self, victim: CarState, attacker: CarState, amount: int, now: float, slow: bool = False, stun: bool = False) -> None:
        if victim.is_dead(now) or victim.hp <= 0:
            return
        victim.hp = max(0, victim.hp - amount)
        w = victim.world()
        self.effect("ring", w, attacker.color, 0.45)
        self.effect("text", w, (255, 235, 200), 0.9, f"-{amount}")
        if slow:
            victim.penalty_factor = BLASTER_SLOW_FACTOR
            victim.penalty_until = now + BLASTER_SLOW_S
        if stun:
            victim.stun_until = now + MINE_STUN_S
        victim.lights_flash(now, 0.8)
        if victim.source:
            victim.source.rumble(0.9, 0.4, 250)
        if victim.hp <= 0:
            victim.dead_until = now + DEATH_STOP_S
            victim.deaths += 1
            attacker.kills += 1
            victim.lights_flash(now, DEATH_STOP_S)
            self.effect("boom", w, victim.color, 1.2)
            self.log(f"{attacker.name} destroyed {victim.name}!", attacker.color)
        elif slow:
            self.log(f"{attacker.name} hit {victim.name} (-{amount})", attacker.color)
