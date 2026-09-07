"""Connect to every advertising car simultaneously, drive each briefly, report results."""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
import asyncio, logging, sys, time
from anki import protocol as P
from anki.vehicle import Vehicle, discover

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


async def run_car(car: Vehicle, speed: int, seconds: float, report: dict):
    msgs = report.setdefault(car.model, {"msgs": {}, "pos": 0, "err": None})

    def on_msg(mid, dec):
        if mid == P.MSG_LOCALIZATION_POSITION_UPDATE:
            msgs["pos"] += 1
            msgs["last_pos"] = f"piece {dec.piece} loc {dec.location} off {dec.offset_mm:+.1f} spd {dec.speed_mm_s}"
        else:
            msgs["msgs"][f"0x{mid:02X}"] = msgs["msgs"].get(f"0x{mid:02X}", 0) + 1
    car.on_message = on_msg
    try:
        t0 = time.monotonic()
        await car.connect()
        msgs["connect_s"] = round(time.monotonic() - t0, 2)
        await car.request_status()
        await asyncio.sleep(0.8)
        msgs["version"], msgs["battery_mv"] = car.version, car.battery_mv
        await car.set_lights(0x44)
        await car.set_speed(speed, 600)
        await asyncio.sleep(seconds)
        await car.stop()
        await asyncio.sleep(1.0)
        await car.set_lights(0x00)
    except Exception as e:
        msgs["err"] = repr(e)
    finally:
        await car.disconnect()


async def main(speed: int, seconds: float):
    cars = await discover(5.0)
    print(f"found {len(cars)} car(s): " + ", ".join(f"{m}@{d.address}" for d, m in cars))
    vehicles = [Vehicle(d, m) for d, m in cars]
    report: dict = {}
    print(f"connecting to all and driving at {speed} mm/s for {seconds}s ...")
    await asyncio.gather(*(run_car(v, speed, seconds, report) for v in vehicles))
    print()
    for name, r in report.items():
        print(f"{name:<10} connect={r.get('connect_s')}s version={r.get('version')} batt={r.get('battery_mv')}mV "
              f"pos_updates={r['pos']} last={r.get('last_pos','-')} other={r['msgs']} err={r['err']}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 300, float(sys.argv[2]) if len(sys.argv) > 2 else 4.0))
