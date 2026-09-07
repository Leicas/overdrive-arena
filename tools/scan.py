"""Scan for Anki Overdrive cars and print what we see."""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))  # run from anywhere
import asyncio
import sys

from bleak import BleakScanner

from anki.protocol import SERVICE_UUID, parse_local_name


async def main(timeout: float = 8.0):
    print(f"Scanning {timeout:.0f}s for Anki cars (service {SERVICE_UUID})...")
    found = {}

    def cb(device, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_anki = SERVICE_UUID in uuids
        if is_anki or (adv.local_name and "drive" in adv.local_name.lower()):
            if device.address not in found:
                print(f"  + {device.address}  rssi={adv.rssi:>4}  name={parse_local_name(adv.local_name)!r}"
                      f"  raw_name={adv.local_name!r}  mfr={ {k: v.hex() for k, v in adv.manufacturer_data.items()} }")
            found[device.address] = (device, adv)

    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    if not found:
        print("No Anki cars found. Make sure a car is powered on and OFF its charger.")
    else:
        print(f"{len(found)} car(s) found.")
    return found


if __name__ == "__main__":
    t = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    asyncio.run(main(t))
