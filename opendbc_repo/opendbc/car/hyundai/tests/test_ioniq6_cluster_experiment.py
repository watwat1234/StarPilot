from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.structs import CarParams
from opendbc.car.hyundai.carcontroller import CarController, LongCtrlState
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
  return create_stock_like_blindspot_messages(packer, can_bus, _zero_values(packer, "BLINDSPOTS_FRONT_CORNER_1"),
                                              left, right, left_blinker, right_blinker, drive_gear)


class TestSccStatusValues:
  def test_engaged_with_lead(self):
    values = scc_status_values(True, True, 25.0)
    assert values == {"DISTANCE_SETTING": 6, "NEW_SIGNAL_15": 25.0, "OBJ_STATUS": 6, "NEW_SIGNAL_3": 2}

  @pytest.mark.parametrize("v_ego", [0.0, 5.0, 15.9])
  def test_engaged_with_lead_at_low_speed(self, v_ego):
    assert scc_status_values(True, True, v_ego)["OBJ_STATUS"] == 2

  def test_gas_override_with_lead(self):
    assert scc_status_values(True, True, 25.0, gas_override=True)["OBJ_STATUS"] == 2

  def test_engaged_without_lead_sends_stock_no_lead_values(self):
    values = scc_status_values(True, False, 25.0)
    assert values["OBJ_STATUS"] == 0
    assert values["NEW_SIGNAL_3"] == 0
    assert values["ACC_ObjDist"] == 204.6
    assert values["ACC_ObjRelSpd"] == 34.6
    assert values["ObjValid"] == 1

  def test_disengaged_with_lead_sends_stock_cruise_off_values(self):
    values = scc_status_values(False, True, 25.0)
    assert values == {"DISTANCE_SETTING": 0, "NEW_SIGNAL_15": 204.6, "OBJ_STATUS": 2, "NEW_SIGNAL_3": 1}

  def test_disengaged_without_lead(self):
    values = scc_status_values(False, False, 25.0)
    assert values["DISTANCE_SETTING"] == 0
    assert values["NEW_SIGNAL_15"] == 204.6
    assert values["OBJ_STATUS"] == 0
    assert values["NEW_SIGNAL_3"] == 0
    assert values["ObjValid"] == 1

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
    actual = create_stock_like_blindspot_messages(CANPacker(dbc_name), can_bus, front,
                                                  False, False, False, False, True)[1]
    assert actual == expected


EXPERIMENT_ADDRESSES = (0x1A0, 0x1EA, 0x1BA)
REPLAY_ADDRESSES = (0x31A, 0x3B5)


def _toggles(**experiment):
  return SimpleNamespace(always_on_lateral_lkas=False, force_torque_controller=False, nnff=False, nnff_lite=False,
                         **experiment)


ALL_ON = {"ioniq6_hud_scc_status": True, "ioniq6_hud_adrv_active": True, "ioniq6_hud_bsm_stock": True}
ALL_OFF = dict.fromkeys(ALL_ON, False)


def _controller_msgs(toggles, car=CAR.HYUNDAI_IONIQ_6, lead_visible=True, enabled=True, left_blinker=False, frames=1,
                     rear_corners=None):
  CP = CarParams.new_message()
  CP.carFingerprint = car
  CP.flags = int(HyundaiFlags.CANFD | HyundaiFlags.CANFD_LKA_STEERING | HyundaiFlags.EV)
  CP.openpilotLongitudinalControl = True
  controller = CarController(DBC[CP.carFingerprint], CP)
  controller.frame = 100  # SCC, 0x1EA, 0x1BA and the 0x3B5/0x31A replay are all due
  controller.long_active_ecu = True
  packer = CANPacker(DBC[CP.carFingerprint][Bus.pt])

  cc = SimpleNamespace(
    enabled=enabled, latActive=True, longActive=enabled,
    actuators=SimpleNamespace(longControlState=LongCtrlState.pid, accel=0.0),
    cruiseControl=SimpleNamespace(override=False, cancel=False, resume=False),
    leftBlinker=left_blinker, rightBlinker=False,
    hudControl=SimpleNamespace(leadDistanceBars=3, leadVisible=lead_visible),
  )
  cs = SimpleNamespace(
    stock_lfa_msg=None, stock_lkas_msg=None, stock_lfahda_cluster_msg=None,
    lfa_block_msg={"COUNTER": 0, **{f"BYTE{i}": 0 for i in range(3, 24)}},
    openpilot_lead_visible=lead_visible, openpilot_lead_distance=30.0, openpilot_lead_rel_speed=-1.0,
    left_blindspot_from_radar=True, right_blindspot_from_radar=False,
    blindspots_rear_corners_ts=1, blindspots_front_corner_1_ts=1,
    blindspots_rear_corners=rear_corners or _zero_values(packer, "BLINDSPOTS_REAR_CORNERS"),
    blindspots_front_corner_1=_zero_values(packer, "BLINDSPOTS_FRONT_CORNER_1"),
    out=SimpleNamespace(steeringAngleDeg=0.0, gearShifter=structs.CarState.GearShifter.drive, vEgo=25.0, vEgoRaw=25.0,
                        brakePressed=False, gasPressed=False, standstill=False,
                        cruiseState=SimpleNamespace(available=True)),
  )
  now_nanos = 10 ** 9  # car's own 0x1BA/0x1E5 are stale, so openpilot sends them
  msgs = []
  for _ in range(frames):
    msgs += controller.create_canfd_msgs(now_nanos, True, 0.0, 0.0, 42.0, -1.0, False, cc.hudControl, cs, cc, toggles,
                                         lka_icon=2, lfa_icon=2)
    controller.frame += 1
  return msgs


def _by_address(msgs):
  return {address: dat for address, dat, _ in msgs}


class TestControllerWiring:
  def test_toggles_off_match_missing_toggles(self):
    assert _controller_msgs(_toggles(**ALL_OFF)) == _controller_msgs(_toggles())

  def test_toggles_on_change_only_the_experiment_messages(self):
    off = _by_address(_controller_msgs(_toggles(**ALL_OFF)))
    on = _by_address(_controller_msgs(_toggles(**ALL_ON)))

    assert all(address in off for address in EXPERIMENT_ADDRESSES + REPLAY_ADDRESSES)
    assert not any(address in on for address in REPLAY_ADDRESSES)
    for address in EXPERIMENT_ADDRESSES:
      assert on[address] != off[address]
    unchanged = set(off) - set(EXPERIMENT_ADDRESSES) - set(REPLAY_ADDRESSES)
    assert {address: on[address] for address in unchanged} == {address: off[address] for address in unchanged}

  def test_scc_control_with_lead(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_scc_status=True)))[0x1A0]
    assert dat[11] == 6
    assert dat[13] == 0x28
    assert dat[22] & 0x7 == 6

  def test_scc_control_disengaged_with_lead(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_scc_status=True), enabled=False))[0x1A0]
    assert dat[11] & 0x7 == 0
    assert dat[13] == 0x18
    assert dat[22] & 0x7 == 2
    assert (int.from_bytes(dat[3:5], "little") & 0x7FF) == 300  # openpilot's 30 m lead distance is kept
    assert int.from_bytes(dat[24:26], "little") & 0x7FF == 2046  # NEW_SIGNAL_15 204.6

  def test_scc_control_without_lead(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_scc_status=True), lead_visible=False))[0x1A0]
    assert dat[3:6] == bytes.fromhex("fef77f")
    assert dat[13] == 0x08
    assert dat[22] & 0x7 == 0

  def test_adrv_0x1ea(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_adrv_active=True)))[0x1EA]
    assert dat[3] == 0x08
    assert dat[4] & 0x3 == 1

  def test_rear_corners(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_bsm_stock=True)))[0x1BA]
    assert dat[3:9] == bytes.fromhex("430000008803")
    assert dat[16] == 0x01

  def test_rear_corners_ignore_the_last_car_sent_frame(self):
    packer = CANPacker(DBC[CAR.HYUNDAI_IONIQ_6][Bus.pt])
    warning = {sig: 1 for sig in _zero_values(packer, "BLINDSPOTS_REAR_CORNERS") if sig not in ("CHECKSUM", "COUNTER")}
    toggles = _toggles(ioniq6_hud_bsm_stock=True)
    assert _by_address(_controller_msgs(toggles, rear_corners=warning))[0x1BA] == \
           _by_address(_controller_msgs(toggles))[0x1BA]

  def test_bsm_stock_drops_lane_change_replay_collisions(self):
    def addresses(toggles):
      # the lane-change replay sends 0x3B5 every 20 frames and 0x31A every 100
      return {address for address, _, _ in _controller_msgs(toggles, left_blinker=True, frames=200)}

    off, on = addresses(_toggles(**ALL_OFF)), addresses(_toggles(ioniq6_hud_bsm_stock=True))
    assert {0x3C1, 0x31A, 0x3B5} <= off
    assert 0x3C1 in on
    assert not {0x31A, 0x3B5} & on

  def test_other_cars_ignore_the_toggles(self):
    assert _controller_msgs(_toggles(**ALL_ON), car=CAR.KIA_EV6) == _controller_msgs(_toggles(), car=CAR.KIA_EV6)
