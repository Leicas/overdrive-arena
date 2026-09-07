"""Drive one car for a while and dump raw localization messages (hex) to study the protocol."""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
import asyncio, logging, sys, time
from anki import protocol as P
from anki.vehicle import Vehicle, discover
logging.basicConfig(level=logging.ERROR)

async def main(speed, seconds, which):
    cars = await discover(5.0)
    print("found:", [m for _, m in cars])
    dev, model = next(((d, m) for d, m in cars if m.lower() == which.lower()), cars[0])
    car = Vehicle(dev, model)
    t0 = time.monotonic()
    rows = []
    def raw(_c, data):
        b = bytes(data)
        rows.append((time.monotonic() - t0, b))
    await car.connect()
    # tap the raw notifications in addition to the parsed callback
    await car.client.stop_notify(P.READ_CHAR_UUID)
    await car.client.start_notify(P.READ_CHAR_UUID, raw)
    await car.send(P.set_speed(speed, 500))
    await asyncio.sleep(seconds)
    await car.send(P.set_speed(0, 2500))
    await asyncio.sleep(1.0)
    await car.disconnect()
    print(f"{model}: {len(rows)} messages")
    for t, b in rows:
        mid = b[1] if len(b) > 1 else -1
        tag = {0x27: "POS ", 0x29: "TRAN", 0x2A: "XSEC", 0x2B: "DELO", 0x36: "SPD ", 0x4D: "??4D", 0x3F: "??3F", 0x86: "??86"}.get(mid, f"{mid:02X}  ")
        print(f"{t:6.2f} {tag} len={len(b):2d} {b.hex(' ')}")

asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 400, float(sys.argv[2]) if len(sys.argv) > 2 else 20, sys.argv[3] if len(sys.argv) > 3 else "Dynamo"))
