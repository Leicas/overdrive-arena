"""Render the race and pairing screens with fake cars (no Bluetooth) and save PNGs for eyeballing."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pygame  # noqa: E402

from anki import protocol as P  # noqa: E402
from anki.track import TrackScanner  # noqa: E402
from game.game import CarState, Game  # noqa: E402
from game.render import Renderer  # noqa: E402
from game.inputs import Keyboard  # noqa: E402


class FakeVehicle:
    def __init__(self, model, mv):
        self.model = model
        self.battery_mv = mv
        self.connected = True
        self.address = "00:00"
        self.status = None
        self.on_charger = False

    async def send(self, payload): ...
    async def set_speed(self, s, a): ...
    async def change_lane(self, o, h_speed=0, h_accel=0): ...


class FakeBle:
    def fire(self, coro):
        coro.close()


def main(out_dir: Path):
    msgs = [P.parse(bytes.fromhex(l)) for l in (Path(__file__).parent / "x52_lap.hex").read_text().split("\n") if l.strip()]
    sc = TrackScanner()
    for m in msgs:
        sc.on_message(*m)
        if sc.done:
            break
    track = sc.track
    pygame.init()
    screen = pygame.display.set_mode((1280, 760))
    r = Renderer(screen)
    ble = FakeBle()
    cars = [CarState(FakeVehicle("X52", 3870), 0, ble, 800), CarState(FakeVehicle("Mammoth", 4080), 1, ble, 800),
            CarState(FakeVehicle("Dynamo", 3610), 2, ble, 800)]
    game = Game(ble, cars, track, ai_enabled=True, ai_speed=450)
    kb = Keyboard()
    cars[0].source = kb
    game.start()
    loc_msgs = [m for m in msgs if m[0] in (0x27, 0x29)]
    for i, c in enumerate(cars):
        for m in loc_msgs[: 5 + i * 6]:
            c.on_message(*m)
    cars[1].loc.lane_mm = -68
    # battery history: idle high, load sagging -> "weak"
    for k in range(6):
        cars[2].battery.update(3610 + k, 0)
        cars[2].battery.update(3380 + k, 450)
    cars[0].battery.update(3870, 0); cars[0].battery.update(3790, 450)
    cars[2].car.on_charger = True
    cars[2].battery.update(4190, 0, charging=True)
    now = time.monotonic()
    cars[0]._last_speed_sent = 640
    cars[1]._last_speed_sent = 450
    cars[0].laps, cars[0].last_lap_s, cars[0].best_lap_s = 3, 7.42, 6.98
    cars[0].kills = 2
    game.fire(cars[1], now)          # Mammoth shoots whoever is ahead
    game.drop_mine(cars[2], now - 3)
    game.damage(cars[0], cars[2], 20, now, slow=True)
    game.damage(cars[2], cars[0], 100, now)  # destroyed -> boom
    # advance a few frames so trails/effects/smoothing have state
    for _ in range(12):
        for c in cars:
            if c.loc:
                c.loc.advance(0.05)
        r.draw_race(game, "controls line 1\ncontrols line 2")
        time.sleep(0.02)
    pygame.image.save(screen, str(out_dir / "render_race.png"))
    game.phase = "grid"
    cars[0].grid_state, cars[1].grid_state, cars[2].grid_state = "placed", "driving", "off track"
    r.draw_race(game, "controls line 1\ncontrols line 2", overlay="TO THE START LINE  1/3", sub="cars drive themselves to the grid  -  Back / Esc cancels")
    pygame.image.save(screen, str(out_dir / "render_grid.png"))
    game.phase = "countdown"
    r.draw_race(game, "controls line 1\ncontrols line 2", overlay="2", sub="get ready")
    pygame.image.save(screen, str(out_dir / "render_countdown.png"))
    game.phase = "race"
    game.mode = "race"; game.lap_target = 5
    # Synthetic records for visual QA only; Game's default store is in-memory.
    for i, c in enumerate(cars):
        c.car.address = f"demo-car-{i}"
        pad = Keyboard()
        pad.short, pad.name, pad.record_id = f"P{i + 1}", "Wireless Controller", f"demo:{i}"
        c.source = pad
        game.records.record(track, "race", c, "lap", 6.98 + i * .63)
        game.records.record(track, "race", c, "race", 38.24 + i * 2.61, 5)
    r.draw_race(game, "controls line 1\ncontrols line 2")
    pygame.image.save(screen, str(out_dir / "render_racemode.png"))
    r.draw_pairing(game, [kb], {kb: 1}, {}, "RACE mode: laps only, weapons off", "controls")
    pygame.image.save(screen, str(out_dir / "render_pairing_racemode.png"))
    game.mode = "battle"
    kb2 = Keyboard(); kb2.name = "Xbox One S Controller"; kb2.short = "P1"
    r.draw_pairing(game, [kb2, kb], {kb: 1, kb2: 0}, {kb2: cars[0]}, "P1 (Xbox One S Controller) drives X52", "controls line 1\ncontrols line 2")
    pygame.image.save(screen, str(out_dir / "render_pairing.png"))
    print("saved", out_dir / "render_race.png", out_dir / "render_pairing.png")
    print("events:", [e[1] for e in game.events])
    print("battery:", [c.battery.summary() for c in cars])


if __name__ == "__main__":
    main(Path(__file__).parent)
