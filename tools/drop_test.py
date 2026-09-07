"""Find what makes cars drop the BLE link: drive all cars 20 s under one variant and count disconnects."""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
import asyncio, logging, sys, time
from anki import protocol as P
from anki.vehicle import Vehicle, discover
logging.basicConfig(level=logging.ERROR)

async def run(car: Vehicle, variant: str, drops: dict):
    drops[car.model] = []
    car._on_disconnect = lambda _c: drops[car.model].append(round(time.monotonic() - t0[0], 1))
    t0 = [time.monotonic()]
    try:
        await car.connect()
        t0[0] = time.monotonic()
        if variant == "engine_color":
            await car.send(P.engine_color(14, 0, 0)); await car.send(P.lights_on(P.LIGHT_HEADLIGHTS))
        await car.send(P.set_speed(450, 500))
        for i in range(20):
            await asyncio.sleep(1.0)
            if not car.connected:
                break
            if variant == "set_offset" and i % 2 == 1:
                await car.send(P.set_offset_from_road_center(22.5 if i % 4 == 1 else -22.5))
            if variant == "lane_changes" and i % 3 == 2:
                await car.send(P.change_lane(68.0 if i % 2 else -68.0, 400, 1500))
        if car.connected:
            await car.send(P.set_speed(0, 2500)); await asyncio.sleep(1)
    except Exception as e:
        drops[car.model].append(f"ERR {e!r}")
    finally:
        await car.disconnect()

async def main(variant):
    cars = await discover(6)
    print(f"[{variant}] cars:", [m for _, m in cars])
    drops = {}
    await asyncio.gather(*(run(Vehicle(d, m), variant, drops) for d, m in cars))
    print(f"[{variant}] drops (s after start):", drops)

asyncio.run(main(sys.argv[1]))
