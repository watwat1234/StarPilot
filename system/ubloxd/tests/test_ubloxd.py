import calendar
import struct
from types import SimpleNamespace

import pytest

from cereal import log
from openpilot.common.gps import get_ublox_location_service
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


def published_at(service, times):
  parser = UbloxMsgParser(service)
  out = []
  for t in times:
    for frame in parser.framer.add_data(t, nav_pvt_frame()):
      if parser.parse_frame(frame) is not None:
        out.append(t)
  return out


def test_gps_location_is_limited_to_its_declared_1hz():
  times = [i * 0.1 for i in range(30)]  # 10 Hz for 3 s
  assert published_at("gpsLocation", times) == pytest.approx([0.0, 1.0, 2.0])
  assert published_at("gpsLocationExternal", times) == pytest.approx(times)


@pytest.mark.parametrize("car_gps,expected", [(False, "gpsLocationExternal"), (True, "gpsLocation")])
def test_ublox_moves_off_external_with_a_car_gps_feed(car_gps, expected):
  params = SimpleNamespace(get_bool=lambda key: car_gps if key == "CarGpsAvailable" else False)
  assert get_ublox_location_service(params) == expected
