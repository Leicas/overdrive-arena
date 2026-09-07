"""Send light commands one at a time and see whether the car survives each (some may reboot it)."""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
import asyncio, logging, sys, time
from anki import protocol as P
from anki.vehicle import Vehicle, discover
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

STEPS = [
    ("set_lights 0x44", P.set_lights(0x44)),
    ("lights_on HEAD", P.lights_on(P.LIGHT_HEADLIGHTS)),
    ("lights_off HEAD", P.lights_off(P.LIGHT_HEADLIGHTS)),
    ("engine_color red steady (3ch pattern)", P.engine_color(14, 0, 0)),
    ("flash pattern 3ch (tail,red,frontL)", P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 14, 10), (P.LC_RED, P.EFFECT_FLASH, 0, 14, 10), (P.LC_FRONTL, P.EFFECT_FLASH, 0, 14, 10)])),
    ("engine_color blue steady", P.engine_color(0, 0, 14)),
    ("single channel pattern (7 bytes)", P.lights_pattern([(P.LC_GREEN, P.EFFECT_THROB, 0, 14, 6)])),
    ("engine_color green steady", P.engine_color(0, 14, 0)),
]

async def main(which):
    cars = await discover(5)
    dev, model = next(((d, m) for d, m in cars if m.lower() == which.lower()), cars[0])
    car = Vehicle(dev, model)
    events = []
    car._on_disconnect = lambda _c: events.append(("disconnect", time.monotonic()))
    await car.connect()
    print("connected", model)
    for name, payload in STEPS:
        if not car.connected:
            print("  (link already lost)"); break
        t0 = time.monotonic(); events.clear()
        await car.send(payload)
        await asyncio.sleep(4.0)
        ok = car.connected and not events
        print(f"{'OK ' if ok else 'BAD'}  {name:<42} {payload.hex(' ')}")
        if not ok:
            print("  -> car dropped the link after this command; waiting 6s and reconnecting")
            await asyncio.sleep(6)
            try:
                await car.connect()
                print("  reconnected")
            except Exception as e:
                print("  reconnect failed:", e); break
    await car.disconnect()

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "X52"))
