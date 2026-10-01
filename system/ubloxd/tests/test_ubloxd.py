import calendar
import struct

import pytest

from cereal import log
from openpilot.system.ubloxd.ubloxd import UbloxMsgParser


def nav_pvt_frame() -> bytes:
  payload = struct.pack(
    "<IHBBBBBBIiBBBBiiiiIIiiiiiiIHB5sihH",
    0, 2026, 9, 30, 15, 4, 5, 0x07, 0, 0,
    3, 0x01, 0, 9,                    # 3D fix, gnssFixOK, 9 satellites
    -1183771577, 339148950, 30000, 30000, 2500, 4000,
    0, 0, 0, 0, 0, 500, 100000, 120, 0, b"\x00" * 5, 0, 0, 0,
  )
  assert len(payload) == 92
  body = bytes([0x01, 0x07]) + struct.pack("<H", len(payload)) + payload
  ck_a = ck_b = 0
  for b in body:
    ck_a = (ck_a + b) & 0xFF
    ck_b = (ck_b + ck_a) & 0xFF
  return b"\xB5\x62" + body + bytes([ck_a, ck_b])


@pytest.mark.parametrize("service", ["gpsLocationExternal", "gpsLocation"])
def test_nav_pvt_publishes_on_the_chosen_service(service):
  parser = UbloxMsgParser(service)
  frames = parser.framer.add_data(0.0, nav_pvt_frame())
  assert len(frames) == 1

  name, dat = parser.parse_frame(frames[0])
  assert name == service
  assert dat.which() == service
  gps = getattr(dat, service)
  assert gps.source == log.GpsLocationData.SensorSource.ublox
  assert gps.hasFix
  assert gps.latitude == pytest.approx(33.914895)
  assert gps.unixTimestampMillis == calendar.timegm((2026, 9, 30, 15, 4, 5, 0, 0, 0)) * 1000


def test_default_service_is_gps_location_external():
  assert UbloxMsgParser().location_service == "gpsLocationExternal"
