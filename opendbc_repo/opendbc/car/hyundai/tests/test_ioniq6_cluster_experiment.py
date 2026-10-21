import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus
from opendbc.car.structs import CarParams
from opendbc.car.hyundai.hyundaicanfd import CanBus, create_blindspot_status_messages, hkg_can_fd_checksum
from opendbc.car.hyundai.ioniq6_cluster_experiment import adrv_0x1ea_values, create_stock_like_blindspot_messages, \
                                                          scc_status_values
from opendbc.car.hyundai.values import CAR, DBC, HyundaiFlags

# Stock 0x1BA bytes 3-8 (route 000000d1--48888e416c); bytes 9-23 are zero apart from the side lamps in byte 16.
STOCK_REAR_DRIVE = bytes.fromhex("030000008803")
STOCK_REAR_PARK = bytes.fromhex("020000008802")


@pytest.fixture
def ioniq_6():
  CP = CarParams.new_message()
  CP.carFingerprint = CAR.HYUNDAI_IONIQ_6
  CP.flags = int(HyundaiFlags.CANFD | HyundaiFlags.CANFD_LKA_STEERING | HyundaiFlags.EV)
  packer = CANPacker(DBC[CP.carFingerprint][Bus.pt])
  return packer, CanBus(CP)


def _zero_values(packer, name):
  return {sig: 0 for sig in packer.dbc.name_to_msg[name].sigs}


def _rear_corners(packer, can_bus, left=False, right=False, left_blinker=False, right_blinker=False, drive_gear=True):
  return create_stock_like_blindspot_messages(packer, can_bus, _zero_values(packer, "BLINDSPOTS_REAR_CORNERS"),
                                             _zero_values(packer, "BLINDSPOTS_FRONT_CORNER_1"),
                                             left, right, left_blinker, right_blinker, drive_gear)


class TestSccStatusValues:
  def test_engaged_with_lead(self):
    values = scc_status_values(True, True, 25.0)
    assert values == {"DISTANCE_SETTING": 6, "NEW_SIGNAL_15": 25.0, "OBJ_STATUS": 6, "NEW_SIGNAL_3": 2}

  def test_engaged_without_lead_sends_stock_no_lead_values(self):
    values = scc_status_values(True, False, 25.0)
    assert values["OBJ_STATUS"] == 0
    assert values["NEW_SIGNAL_3"] == 0
    assert values["ACC_ObjDist"] == 204.6
    assert values["ACC_ObjRelSpd"] == 34.6
    assert values["ObjValid"] == 1

  def test_disengaged_clears_distance_setting_and_lead(self):
    values = scc_status_values(False, True, 25.0)
    assert values["DISTANCE_SETTING"] == 0
    assert values["OBJ_STATUS"] == 0
    assert values["NEW_SIGNAL_3"] == 0

  @pytest.mark.parametrize("v_ego, headway", [(-1.0, 0.0), (0.0, 0.0), (300.0, 204.6)])
  def test_headway_is_clipped(self, v_ego, headway):
    assert scc_status_values(True, True, v_ego)["NEW_SIGNAL_15"] == headway


def test_adrv_0x1ea_values():
  assert adrv_0x1ea_values(True) == {"SET_ME_1C": 0x08, "NEW_SIGNAL_1": 1}
  assert adrv_0x1ea_values(False) == {"SET_ME_1C": 0x08, "NEW_SIGNAL_1": 0}


class TestStockLikeRearCorners:
  @pytest.mark.parametrize("kwargs, stock_head, byte16", [
    ({}, STOCK_REAR_DRIVE, 0x00),
    ({"left": True}, bytes.fromhex("430000008803"), 0x01),
    ({"right": True}, bytes.fromhex("030100008803"), 0x04),
    ({"left": True, "left_blinker": True}, bytes.fromhex("830000008803"), 0x02),
    ({"drive_gear": False}, STOCK_REAR_PARK, 0x00),
  ])
  def test_matches_stock_payload(self, ioniq_6, kwargs, stock_head, byte16):
    packer, can_bus = ioniq_6
    address, dat, bus = _rear_corners(packer, can_bus, **kwargs)[0]

    assert address == 0x1BA
    assert bus == can_bus.ECAN
    assert dat[3:9] == stock_head
    assert dat[16] == byte16
    assert dat[9:16] == bytes(7)
    assert dat[17:] == bytes(len(dat) - 17)

  def test_checksum_and_counter(self, ioniq_6):
    packer, can_bus = ioniq_6
    first = _rear_corners(packer, can_bus, left=True)[0].dat
    second = _rear_corners(packer, can_bus, left=True)[0].dat

    for dat in (first, second):
      assert int.from_bytes(dat[:2], "little") == hkg_can_fd_checksum(0x1BA, None, bytearray(dat))
    assert second[2] == (first[2] + 1) & 0xFF

  def test_front_corner_matches_existing_builder(self, ioniq_6):
    packer, can_bus = ioniq_6
    dbc_name = DBC[CAR.HYUNDAI_IONIQ_6][Bus.pt]
    rear = _zero_values(packer, "BLINDSPOTS_REAR_CORNERS")
    front = _zero_values(packer, "BLINDSPOTS_FRONT_CORNER_1")
    del front["NEW_SIGNAL_3"]

    expected = create_blindspot_status_messages(CANPacker(dbc_name), can_bus, rear, front)[1]
    actual = create_stock_like_blindspot_messages(CANPacker(dbc_name), can_bus, rear, front,
                                                  False, False, False, False, True)[1]
    assert actual == expected
