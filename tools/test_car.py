"""Connect to the strongest-signal car and exercise a few commands."""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
import asyncio
import logging
import sys

from anki import protocol as P
from anki.vehicle import Vehicle, discover

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main(speed: int, seconds: float, address: str | None):
    cars = await discover(5.0)
    if not cars:
        print("no cars found"); return
    for d, m in cars:
        print(f"  {d.address}  {m}")
    dev, model = next(((d, m) for d, m in cars if address and d.address.lower() == address.lower()), cars[0])
    car = Vehicle(dev, model)

    def on_msg(msg_id, decoded):
        if msg_id == P.MSG_LOCALIZATION_POSITION_UPDATE:
            print(f"  pos loc={decoded.location} piece={decoded.piece} off={decoded.offset_mm:+.1f} spd={decoded.speed_mm_s}")
        else:
            print(f"  msg 0x{msg_id:02X}: {decoded}")

    car.on_message = on_msg
    print(f"connecting to {car.address} ({model}) ...")
    await car.connect()
    await car.request_status()
    await asyncio.sleep(1.0)
    print(f"version={car.version} battery={car.battery_mv} mV")

    print("lights on (0x44 = front+back)")
    await car.set_lights(0x44)
    await asyncio.sleep(1.0)
    await car.set_lights(0x00)

    if speed > 0:
        print(f"driving at {speed} mm/s for {seconds}s")
        await car.set_speed(speed, 500)
        await asyncio.sleep(seconds)
        print("stop")
        await car.stop()
        await asyncio.sleep(1.5)

    await car.disconnect()
    print("done")


if __name__ == "__main__":
    spd = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    addr = sys.argv[3] if len(sys.argv) > 3 else None
    asyncio.run(main(spd, secs, addr))
