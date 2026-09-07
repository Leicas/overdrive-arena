import asyncio, logging, sys
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
from anki import protocol as P
from anki.vehicle import Vehicle, discover
logging.basicConfig(level=logging.ERROR)

async def run(car: Vehicle, log: list):
    def on_msg(mid, dec):
        if mid == P.MSG_LOCALIZATION_POSITION_UPDATE:
            log.append(f"{car.model}: piece {dec.piece:>3} loc {dec.location:>2} off {dec.offset_mm:+7.1f} spd {dec.speed_mm_s}")
        elif mid == P.MSG_OFFSET_FROM_ROAD_CENTER_UPDATE:
            log.append(f"{car.model}: OFFSET UPDATE {dec}")
    car.on_message = on_msg
    try:
        await car.connect()
        await car.set_speed(350, 600)
        await asyncio.sleep(2.5)
        log.append(f"{car.model}: >>> change_lane +68 (outer right)")
        await car.change_lane(68.0, 300, 1000)
        await asyncio.sleep(3.0)
        log.append(f"{car.model}: >>> change_lane -68 (outer left)")
        await car.change_lane(-68.0, 300, 1000)
        await asyncio.sleep(3.0)
        log.append(f"{car.model}: >>> change_lane 0 (center)")
        await car.change_lane(0.0, 300, 1000)
        await asyncio.sleep(2.5)
        await car.stop(); await asyncio.sleep(1.0)
    except Exception as e:
        log.append(f"{car.model}: ERR {e!r}")
    finally:
        await car.disconnect()

async def main():
    cars = await discover(5.0)
    print("found:", ", ".join(m for _, m in cars))
    vs = [Vehicle(d, m) for d, m in cars]
    logs = [[] for _ in vs]
    await asyncio.gather(*(run(v, l) for v, l in zip(vs, logs)))
    for l in logs:
        print("\n".join(l)); print("-"*60)
asyncio.run(main())
