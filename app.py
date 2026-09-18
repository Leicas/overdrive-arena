"""Anki Overdrive arena: drive the cars with Xbox / PS5 pads or the keyboard, see them live on a
scanned track map, and shoot each other with virtual weapons.

Flow:  connect to every car  ->  PAIRING screen (claim a car per controller, scan the track)
       ->  RACE (Back/Esc returns to pairing)

Gamepad (Xbox names; PS5: Cross=A, Circle=B, Square=X, Triangle=Y, Share=Back, Options=Start):
  RT / LT             throttle / brake          Left stick X   steer between lanes
  LB / RB, D-pad L/R  snap one lane             D-pad up/down  speed limit +/- 100
  X                   fire blaster              Y              drop mine (pairing: scan track)
  A                   U-turn (pairing: claim)   B              emergency stop
  Start               start race                Back           back / quit

Keyboard:  W/S throttle-brake  A/D steer  Q/E lane  F fire  G mine  U u-turn  Space stop
           +/- limit   Enter claim/start   T scan track   Esc back/quit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import pygame
from pygame._sdl2 import controller as sdlc

from anki import protocol as P
from anki.ble_worker import BleWorker
from anki.track import Track, TrackScanner
from anki.vehicle import Vehicle, discover
from game import inputs as I
from game.game import CarState, Game
from game.render import Renderer

log = logging.getLogger("app")

TICK_HZ = 50
TRACK_FILE = Path(__file__).parent / "track.json"
SCAN_SPEED = 380
SCAN_TIMEOUT_S = 90
SCAN_NO_DATA_S = 8.0

PAIRING, SCANNING, GRID, COUNTDOWN, RACE = "pairing", "scanning", "grid", "countdown", "race"

CONTROLS_PAIRING = ("pad:  D-pad/stick move   A take car   B release   X AI level/park   Y scan track   stick-click mode   Start RACE   Back quit\n"
                    "keys: Left/Right move   Enter take car   Backspace release   Tab AI level/park   T scan track   M mode   Space RACE   Esc quit")
CONTROLS_RACE = ("pad: RT gas  LT brake  stick/LB/RB lanes  X fire  Y mine  A u-turn  B stop  D-pad limit  L-stick boost  R-stick recover  Back pairing\n"
                 "key: W/S gas/brake  A/D steer  Q/E lanes  F fire  G mine  U u-turn  Space stop  +/- limit  LShift boost  R recover AI  Esc pairing  F11 fullscreen")


class App:
    def __init__(self, args):
        self.args = args
        self.ble = BleWorker()
        pygame.init()
        sdlc.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        pygame.display.set_caption("Anki Overdrive arena")
        self.renderer = Renderer(self.screen)
        self.keyboard = I.Keyboard()
        self.pads: list[I.Pad] = [] if args.keyboard_only else I.list_pads()
        self.cars: list[CarState] = []
        self.game: Optional[Game] = None
        self.state = PAIRING
        self.cursors: dict[I.InputSource, int] = {}
        self.claims: dict[I.InputSource, CarState] = {}
        self.status = ""
        self.scanners: dict[CarState, TrackScanner] = {}
        self.scan_started = 0.0
        self.identified: dict[int, bool] = {}
        self.click: Optional[tuple[int, int]] = None
        self.lap_choices = (3, 5, 10, 20)

    # ------------------------------------------------------------ setup
    @property
    def sources(self) -> list[I.InputSource]:
        return [*self.pads, self.keyboard]

    def connect_cars(self) -> bool:
        a = self.args
        self.renderer.draw_message("Scanning for cars...", [f"{a.scan:.0f} s BLE scan", "cars must be on and off their charger"])
        found = self.ble.call(discover(a.scan), timeout=a.scan + 10)
        if a.car:
            wanted = [x.lower() for x in a.car]
            found = [(d, m) for d, m in found if d.address.lower() in wanted]
        found = found[: a.max_cars]
        if not found:
            self.renderer.draw_message("No Anki cars found", ["Power a car on and take it off the charger, then restart."])
            time.sleep(3)
            return False
        self.renderer.draw_message("Connecting...", [f"{m} ({d.address})" for d, m in found])

        async def connect(dev, model) -> Optional[Vehicle]:
            car = Vehicle(dev, model)
            try:
                await car.connect()
                await car.request_status()
                return car
            except Exception as e:  # noqa: BLE001
                log.error("%s (%s) failed: %s", model, dev.address, e)
                return None

        async def connect_all():
            vehicles = []
            for device, model in found:
                car = await connect(device, model)
                if car is not None:
                    vehicles.append(car)
            return vehicles

        vehicles = self.ble.call(connect_all(), timeout=20 * len(found) + 10)
        if not vehicles:
            self.renderer.draw_message("Could not connect to any car", [])
            time.sleep(3)
            return False
        for i, v in enumerate(vehicles):
            cs = CarState(v, i, self.ble, self.args.max_speed if self.args.max_speed is not None else 800)
            cs.player_max_speed = self.args.player_max_speed
            cs.ai_difficulty = self.args.ai_difficulty
            cs.extreme_max_speed = self.args.max_speed if self.args.max_speed is not None else 2000
            v.on_message = cs.enqueue_message
            self.cars.append(cs)
        track = Track.load(TRACK_FILE)
        if track:
            log.info("loaded %s: %s", TRACK_FILE.name, track.describe())
        self.game = Game(self.ble, self.cars, track, ai_enabled=not self.args.no_ai, ai_speed=self.args.ai_speed)
        self.game.mode = self.args.mode
        self.game.lap_target = self.args.laps
        for cs in self.cars:
            cs.lights_idle()
        for s in self.sources:
            self.cursors[s] = 0
        return True

    # ------------------------------------------------------------ pairing
    def pairing_tick(self) -> bool:
        g = self.game
        assert g is not None
        n = len(self.cars)
        if self.click is not None:
            pos, self.click = self.click, None
            btns = self.renderer.buttons
            if "scan" in btns and btns["scan"].collidepoint(pos):
                self.start_scan_all()
                return True
            elif "race" in btns and btns["race"].collidepoint(pos):
                if g.can_start(self.claims.values()):
                    self.start_race()
                    return True
                self.status = "assign a player or AI to a connected car off its charger"
            elif "mode" in btns and btns["mode"].collidepoint(pos):
                self.toggle_mode()
            elif "laps" in btns and btns["laps"].collidepoint(pos):
                i = self.lap_choices.index(g.lap_target) if g.lap_target in self.lap_choices else -1
                g.lap_target = self.lap_choices[(i + 1) % len(self.lap_choices)]
        for s in self.sources:
            if not s.alive():
                continue
            if s.pressed(I.BACK):
                return False
            if s.pressed(I.MENU_LEFT):
                self.cursors[s] = (self.cursors.get(s, 0) - 1) % n
            if s.pressed(I.MENU_RIGHT):
                self.cursors[s] = (self.cursors.get(s, 0) + 1) % n
            car = self.cars[self.cursors.get(s, 0)]
            if s.pressed(I.SELECT):
                owner = next((o for o, c in self.claims.items() if c is car), None)
                if owner is s:
                    del self.claims[s]
                    self.status = f"{s.short} released {car.name}"
                elif owner is not None:
                    self.status = f"{car.name} is already driven by {owner.short}"
                else:
                    self.claims.pop(s, None)          # one car per controller
                    self.claims[s] = car
                    self.status = f"{s.short} ({s.name}) drives {car.name}"
                    s.rumble(0.5, 0.5, 200)
            if s.pressed(I.RELEASE) and s in self.claims:
                self.status = f"{s.short} released {self.claims.pop(s).name}"
            if s.pressed(I.TOGGLE_AI):
                if car in self.claims.values():
                    self.status = f"{car.name} has a player; release it first"
                elif not g.ai_enabled:
                    self.status = "AI disabled by --no-ai; restart without that option to enable it"
                else:
                    car.cycle_ai()
                    self.status = f"{car.name}: AI {car.ai_difficulty.title()}" if car.ai_pref else f"{car.name}: parked"
            if s.pressed(I.SCAN):
                self.start_scan_all()
                return True
            if s.pressed(I.TOGGLE_MODE):
                self.toggle_mode()
            if s.pressed(I.READY):
                if not g.can_start(self.claims.values()):
                    self.status = "assign a player or AI to a connected car off its charger"
                else:
                    self.start_race()
                    return True
        self.update_identify_lights()
        return True

    def toggle_mode(self) -> None:
        g = self.game
        assert g is not None
        g.mode = "race" if g.mode == "battle" else "battle"
        self.status = ("RACE mode: laps only, weapons off" if g.mode == "race" else "BATTLE mode: blasters, mines and kills")

    def update_identify_lights(self) -> None:
        """Claimed cars keep their headlights on; a car under someone's cursor blinks so you can spot it."""
        blink = int(time.monotonic() * 1.5) % 2 == 0
        for c in self.cars:
            hovered = any(idx == c.index for s, idx in self.cursors.items() if s.alive() and self.claims.get(s) is not c)
            claimed = c in self.claims.values()
            want = claimed or (hovered and blink)
            if self.identified.get(c.index) != want:
                self.identified[c.index] = want
                c.lights_identify(want)

    def start_race(self) -> None:
        g = self.game
        assert g is not None
        for c in self.cars:
            c.source = None
        for s, c in self.claims.items():
            c.source = s
        self.identified.clear()
        g.begin_grid()
        self.state = GRID
        self.status = ""

    def grid_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        if any(s.pressed(I.BACK) for s in self.sources):
            g.stop_all()
            self.state = PAIRING
            self.status = "grid cancelled (cars stopped)"
            return
        if g.grid_tick(dt):
            placed, total = g.grid_summary()
            if placed == 0:
                g.stop_all()
                self.state = PAIRING
                self.status = "no car reached the start line - are they on the track?"
                return
            g.begin_countdown()
            self.state = COUNTDOWN

    def countdown_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        if any(s.pressed(I.BACK) for s in self.sources):
            g.stop_all()
            self.state = PAIRING
            self.status = "start cancelled (cars stopped)"
            return
        g.tick(dt)
        if g.countdown_left() <= 0:
            g.start()
            self.state = RACE

    # ------------------------------------------------------------ scanning
    def start_scan_all(self) -> None:
        """Every connected car drives and scans; the first closed lap wins. Also reveals which cars are on the rails."""
        g = self.game
        assert g is not None
        g.stop_all()
        self.scanners = {}
        for c in self.cars:
            if not c.car.connected:
                continue
            sc = TrackScanner()
            self.scanners[c] = sc
            if c.charging:
                sc.state = "on the charger"
                continue
            c.extra_listener = sc.on_message
            c.lights_idle()
            self.ble.fire(c.car.set_speed(SCAN_SPEED, 500))
        if not any(sc.state != "on the charger" for sc in self.scanners.values()):
            self.status = "every car is on its charger - take one off and put it on the track to scan"
            self.scanners = {}
            return
        self.scan_started = time.monotonic()
        self.state = SCANNING
        log.info("scanning track with %s", ", ".join(c.name for c in self.scanners))

    def _end_scan(self) -> None:
        for c, _sc in self.scanners.items():
            c.extra_listener = None
            self.ble.fire(c.car.set_speed(0, 2500))
        self.state = PAIRING
        self.identified.clear()

    def scanning_tick(self) -> None:
        g = self.game
        assert g is not None
        cancel = any(s.pressed(I.BACK) for s in self.sources)
        elapsed = time.monotonic() - self.scan_started
        # cars that read nothing after a while are not on the rails: stop them, keep scanning with the others
        for c, sc in self.scanners.items():
            if sc.state == "on the charger":
                continue
            if elapsed > SCAN_NO_DATA_S and sc.pieces_seen == 0 and sc.state != "not on the track":
                sc.state = "not on the track"
                self.ble.fire(c.car.set_speed(0, 2500))
                log.info("%s read no track codes during the scan", c.name)
        winner = next(((c, sc) for c, sc in self.scanners.items() if sc.done and sc.track), None)
        all_dead = all(sc.state in ("not on the track", "on the charger") for sc in self.scanners.values())
        if winner or cancel or all_dead or elapsed > SCAN_TIMEOUT_S:
            off = [c.name for c, sc in self.scanners.items() if sc.state == "not on the track"]
            charging = [c.name for c, sc in self.scanners.items() if sc.state == "on the charger"]
            self._end_scan()
            if winner:
                c, sc = winner
                sc.track.save(TRACK_FILE)  # type: ignore[union-attr]
                g.set_track(sc.track)
                self.status = f"track scanned by {c.name}: {len(sc.track)} pieces"  # type: ignore[arg-type]
                if off:
                    self.status += f"   -   NOT on the track: {', '.join(off)}"
                if charging:
                    self.status += f"   -   on the charger: {', '.join(charging)}"
                log.info("track: %s", sc.track.describe())  # type: ignore[union-attr]
            elif all_dead:
                self.status = "no car read any track codes - put the cars on the rails and scan again"
            else:
                self.status = "scan cancelled" if cancel else "scan timed out without a closed lap"

    # ------------------------------------------------------------ race
    def race_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        for s in self.sources:
            if s.pressed(I.BACK):
                g.stop_all()
                self.state = PAIRING
                self.status = "back to pairing (cars stopped)"
                return
            if s.pressed(I.RECOVER):
                g.retry_ai_recovery()
        g.tick(dt)
        if g.finished and time.monotonic() - g.finish_t > 7.0:
            winner = g.winner
            g.stop_all()
            self.state = PAIRING
            self.status = f"{winner.name} won the race ({winner.driver_label()})" if winner else "race over"

    # ------------------------------------------------------------ main loop
    def handle_events(self) -> bool:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                self.click = ev.pos
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_F11:
                    pygame.display.toggle_fullscreen()
                else:
                    self.keyboard.feed_keydown(ev.key)
            elif ev.type == pygame.CONTROLLERDEVICEADDED:
                try:
                    if sdlc.is_controller(ev.device_index):
                        pad = I.Pad(ev.device_index, len(self.pads) + 1)
                        if all(p.instance_id != pad.instance_id for p in self.pads):
                            self.pads.append(pad)
                            self.cursors[pad] = 0
                            self.status = f"controller connected: {pad.name}"
                        else:
                            pad.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("controller add failed: %s", e)
            elif ev.type == pygame.CONTROLLERDEVICEREMOVED:
                for p in self.pads:
                    if p.instance_id == ev.instance_id:
                        p.mark_removed()
                        car = self.claims.pop(p, None)
                        if car is not None:
                            car.source = None
                            car.hard_stop(time.monotonic())
                            self.status = f"{p.name} disconnected, {car.name} stopped"
        return True

    def run(self) -> int:
        if not self.connect_cars():
            self.shutdown()
            return 1
        assert self.game is not None
        frame_period = 1.0 / TICK_HZ
        dt = frame_period
        t_start = time.monotonic()
        previous_tick = t_start - frame_period
        last_batt = t_start
        if self.args.autostart:
            self.start_race()
        if self.args.autoscan:
            self.start_scan_all()
        running = True
        try:
            while running:
                t0 = time.monotonic()
                dt = min(0.25, max(0.0, t0 - previous_tick))
                previous_tick = t0
                running = self.handle_events()
                for car in self.cars:
                    car.drain_messages()
                for s in self.sources:
                    s.poll(dt)
                if self.state == PAIRING:
                    running = running and self.pairing_tick()
                elif self.state == SCANNING:
                    self.scanning_tick()
                    self.game.tick(dt)  # keeps localizers advancing, no driving (not running)
                elif self.state == GRID:
                    self.grid_tick(dt)
                elif self.state == COUNTDOWN:
                    self.countdown_tick(dt)
                elif self.state == RACE:
                    self.race_tick(dt)
                self.keyboard.end_frame()
                self.maybe_reconnect(t0)
                if t0 - last_batt > 4:
                    for c in self.cars:
                        self.ble.fire(c.car.send(P.battery_request()))
                    last_batt = t0
                self.draw()
                if self.args.shots:
                    self._shot(t0)
                if self.args.run_for and t0 - t_start > self.args.run_for:
                    if self.args.screenshot:
                        pygame.image.save(self.screen, self.args.screenshot)
                    running = False
                time.sleep(max(0.0, frame_period - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def maybe_reconnect(self, now: float) -> None:
        """Cars occasionally drop the link (they reboot); try to get them back every few seconds."""
        if any(getattr(c, "_reconnecting", False) for c in self.cars):
            return  # one reconnect (which scans) at a time, so we do not starve the other links
        for c in self.cars:
            if c.car.connected or now - getattr(c, "_reconnect_t", 0.0) < 5.0:
                continue
            c._reconnecting = True
            c._reconnect_t = now
            self.status = f"reconnecting {c.name}..."

            async def task(cs=c):
                try:
                    await cs.car.reconnect()
                    await cs.car.request_status()
                    cs.hard_stop(time.monotonic())
                    cs.lights_idle()
                    log.info("reconnected %s", cs.name)
                except Exception as e:  # noqa: BLE001
                    log.warning("reconnect %s failed: %s", cs.name, e)
                finally:
                    cs._reconnecting = False

            self.ble.fire(task())

    def _shot(self, now: float) -> None:
        last_state = getattr(self, "_shot_state", None)
        last_t = getattr(self, "_shot_t", 0.0)
        if self.state != last_state or now - last_t > 4.0:
            self._shot_state, self._shot_t = self.state, now
            n = getattr(self, "_shot_n", 0) + 1
            self._shot_n = n
            Path(self.args.shots).mkdir(parents=True, exist_ok=True)
            pygame.image.save(self.screen, str(Path(self.args.shots) / f"{n:03d}_{self.state}.png"))

    def draw(self) -> None:
        g = self.game
        assert g is not None
        if self.state == PAIRING:
            self.renderer.draw_pairing(g, self.sources, self.cursors, self.claims, self.status, CONTROLS_PAIRING)
        elif self.state == SCANNING:
            self.renderer.draw_scan(g, self.scanners, time.monotonic() - self.scan_started)
        elif self.state == GRID:
            placed, total = g.grid_summary()
            self.renderer.draw_race(g, CONTROLS_RACE, overlay=f"TO THE START LINE  {placed}/{total}",
                                    sub="cars drive themselves to the grid  -  Back / Esc cancels")
        elif self.state == COUNTDOWN:
            left = g.countdown_left()
            self.renderer.draw_race(g, CONTROLS_RACE, overlay=f"{int(left) + 1}" if left > 0 else "GO!", sub="get ready")
        elif g.finished and g.winner is not None:
            self.renderer.draw_race(g, CONTROLS_RACE, overlay=f"{g.winner.name} WINS", sub=f"{g.lap_target} laps  -  back to pairing in a moment")
        else:
            self.renderer.draw_race(g, CONTROLS_RACE)
        pygame.display.flip()

    def shutdown(self) -> None:
        log.info("stopping cars...")

        async def stop_all():
            await asyncio.gather(*(c.car.disconnect() for c in self.cars), return_exceptions=True)

        try:
            if self.cars:
                self.ble.call(stop_all(), timeout=10)
        except Exception as e:  # noqa: BLE001
            log.error("shutdown: %r", e)
        for s in self.sources:
            s.close()
        pygame.quit()
        self.ble.stop()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--max-speed", type=int, default=None, help="AI speed ceiling in mm/s (default 800; Extreme automatically uses up to 2000)")
    ap.add_argument("--player-max-speed", type=int, choices=range(200, 2001), metavar="200..2000", default=2000,
                    help="initial controller/keyboard speed limit (default 2000)")
    ap.add_argument("--max-cars", type=int, default=4)
    ap.add_argument("--scan", type=float, default=5.0, help="BLE scan duration in seconds")
    ap.add_argument("--car", action="append", help="only use this car address (repeatable)")
    ap.add_argument("--keyboard-only", action="store_true", help="ignore gamepads")
    ap.add_argument("--ai-difficulty", choices=("easy", "normal", "hard", "extreme"), default="normal", help="initial AI level for every car")
    ap.add_argument("--no-ai", action="store_true", help="unclaimed cars stay parked instead of being driven by AI")
    ap.add_argument("--mode", choices=["battle", "race"], default="battle", help="battle = weapons and kills; race = laps only (default battle)")
    ap.add_argument("--laps", type=int, default=5, help="race mode: laps to win (default 5)")
    ap.add_argument("--ai-speed", type=int, default=None, help="override AI normal straight speed mm/s (default: scales with car limit; Hard uses full limit)")
    ap.add_argument("--accel", type=int, default=600, help="acceleration mm/s^2 (default 600; higher may reboot cars with weak batteries)")
    ap.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--autoscan", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--run-for", type=float, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    ap.add_argument("--shots", help=argparse.SUPPRESS)   # directory: save a PNG on every state change and every 4 s
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


if __name__ == "__main__":
    a = parse_args()
    import game.game as _gg
    _gg.ACCEL_UP = a.accel
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    sys.exit(App(a).run())
