from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.structs import CarParams
from opendbc.car.hyundai.carcontroller import CarController, LongCtrlState
from opendbc.car.hyundai.hyundaicanfd import CanBus, create_adrv_messages, create_blindspot_status_messages, \
                                             hkg_can_fd_checksum
from opendbc.car.hyundai.ioniq6_cluster_experiment import AdjacentCarTracker, LeadDisplayFilter, adjacent_distance, \
                                                          adrv_0x1ea_values, create_stock_like_blindspot_messages, \
                                                          lead_display_allowed, patch_adrv_0x1ea, scc_status_values, \
                                                          select_adjacent_track
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
    values = scc_status_values(True, True, 22.0)
    assert values == {"DISTANCE_SETTING": 6, "NEW_SIGNAL_15": 22.0, "OBJ_STATUS": 6, "NEW_SIGNAL_3": 2}

  # Most common stock OBJ_STATUS per speed band, engaged with a lead (1 at 14-16 m/s, where stock mostly sends 0).
  @pytest.mark.parametrize("v_ego, obj_status", [(0.0, 2), (11.9, 2), (12.0, 1), (15.9, 1), (16.0, 7), (19.9, 7),
                                                 (20.0, 6), (24.9, 6), (25.0, 5), (40.0, 5)])
  def test_engaged_lead_status_follows_stock_speed_bands(self, v_ego, obj_status):
    assert scc_status_values(True, True, v_ego)["OBJ_STATUS"] == obj_status

  @pytest.mark.parametrize("v_ego, obj_status", [(5.0, 2), (22.0, 6)])
  def test_gas_override_with_lead(self, v_ego, obj_status):
    values = scc_status_values(True, True, v_ego, gas_override=True)
    assert values["OBJ_STATUS"] == obj_status  # same speed bands as engaged
    assert values["NEW_SIGNAL_3"] == 1

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


class TestSccGap:
  @pytest.mark.parametrize("bars", [1, 2, 3])
  def test_engaged_sends_personality_bars(self, bars):
    assert scc_status_values(True, True, 20.0, distance_bars=bars)["DISTANCE_SETTING"] == bars

  @pytest.mark.parametrize("follow_distance, t_follow, headway", [
    (41.0, 1.45, 41.0),  # openpilot's target gap
    (3.0, 1.45, 6.0),  # lead pulling away fast: not under the stop distance
    (-12.0, 1.45, 6.0),
    (0.0, 1.45, 35.0),  # no tracked lead: vEgo x tFollow + 6 m
    (None, 1.45, 35.0),
    (0.0, 0.0, 20.0),  # disengaged: stock 1.0 s
    (None, None, 20.0),
  ])
  def test_engaged_gap(self, follow_distance, t_follow, headway):
    values = scc_status_values(True, True, 20.0, t_follow=t_follow, follow_distance=follow_distance)
    assert values["NEW_SIGNAL_15"] == pytest.approx(headway)

  @pytest.mark.parametrize("follow_distance, t_follow", [(300.0, 1.6), (0.0, 1.6)])
  def test_headway_is_clipped(self, follow_distance, t_follow):
    values = scc_status_values(True, True, 150.0, t_follow=t_follow, follow_distance=follow_distance)
    assert values["NEW_SIGNAL_15"] == 204.6

  @pytest.mark.parametrize("lead_visible", [True, False])
  def test_disengaged_unchanged(self, lead_visible):
    assert scc_status_values(False, lead_visible, 20.0, distance_bars=3, t_follow=1.5, follow_distance=40.0) == \
           scc_status_values(False, lead_visible, 20.0)

  def test_only_gap_fields_change(self):
    gap = scc_status_values(True, True, 20.0, distance_bars=2, t_follow=1.4)
    round_1 = scc_status_values(True, True, 20.0)
    assert {k: v for k, v in gap.items() if k not in ("DISTANCE_SETTING", "NEW_SIGNAL_15")} == \
           {k: v for k, v in round_1.items() if k not in ("DISTANCE_SETTING", "NEW_SIGNAL_15")}


DRIVE_GEAR, OTHER_GEAR = True, False


class TestLeadDisplayAllowed:
  @pytest.mark.parametrize("v_ego, camera_prob", [(0.0, 1.0), (2.0, 0.9), (10.0, 0.0)])
  def test_never_outside_drive(self, v_ego, camera_prob):
    assert not lead_display_allowed(OTHER_GEAR, v_ego, camera_prob)

  @pytest.mark.parametrize("v_ego, camera_prob, allowed", [
    (2.0, 0.3, False),
    (2.0, 0.9, True),
    (10.0, 0.0, True),
    (2.99, 0.5, False),  # boundaries: under 3 m/s, more than 0.5 needed
    (2.99, 0.51, True),
    (3.0, 0.0, True),
  ])
  def test_slow_leads_need_the_camera(self, v_ego, camera_prob, allowed):
    assert lead_display_allowed(DRIVE_GEAR, v_ego, camera_prob) == allowed


def _run_filter(lead_filter, pattern):
  # pattern: (lead_visible, frames) runs at 50 Hz; returns the outputs, one per frame
  out = []
  for lead_visible, frames in pattern:
    for i in range(frames):
      out.append(lead_filter.update(lead_visible, 40.0 + i, -1.0) if lead_visible else
                 lead_filter.update(False, 0.0, 0.0))
  return out


class TestLeadDisplayFilter:
  def test_short_blip_never_shows(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 15), (False, 10)])  # 0.3 s
    assert not any(visible for visible, _, _ in out)

  def test_shows_after_0_4_s(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 25)])
    assert [visible for visible, _, _ in out] == [False] * 19 + [True] * 6
    assert out[19] == (True, 59.0, -1.0)
    assert out[-1] == (True, 64.0, -1.0)

  def test_hidden_frames_pass_the_inputs_through(self):
    assert LeadDisplayFilter().update(True, 30.0, 2.0) == (False, 30.0, 2.0)

  def test_0_9_s_gap_is_bridged_with_the_last_lead(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 30), (False, 45), (True, 1)])
    assert all(visible for visible, _, _ in out[19:])
    assert set(out[30:75]) == {(True, 69.0, -1.0)}
    assert out[75] == (True, 40.0, -1.0)

  def test_1_1_s_gap_hides(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 30), (False, 55)])
    assert [visible for visible, _, _ in out[30:]] == [True] * 49 + [False] * 6

  def test_shows_again_only_after_0_4_s(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 30), (False, 50), (True, 25)])
    assert [visible for visible, _, _ in out[80:]] == [False] * 19 + [True] * 6

  def test_short_dropouts_do_not_keep_a_lead_hidden(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 19), (False, 1)] * 3)
    assert out[-1][0]
    assert [visible for visible, _, _ in out].index(True) < 30

  def test_alternating_flicker_never_shows(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 5), (False, 5)] * 20)
    assert not any(visible for visible, _, _ in out)

  def test_rare_blips_do_not_keep_a_lead_shown(self):
    out = _run_filter(LeadDisplayFilter(), [(True, 30)] + [(False, 44), (True, 1)] * 3)
    assert not out[-1][0]
    assert out[-1] == (False, 40.0, -1.0)  # the blip's own values pass through once hidden

  def test_reset(self):
    lead_filter = LeadDisplayFilter()
    _run_filter(lead_filter, [(True, 30)])
    lead_filter.reset()
    assert lead_filter.update(False, 0.0, 0.0) == (False, 0.0, 0.0)


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
                     rear_corners=None, override=False, t_follow=1.5, lead_frames=None, toggle_frames=None,
                     distance_bars=3, follow_distance=0.0, gear=structs.CarState.GearShifter.drive, v_ego=25.0,
                     camera_prob=1.0, gear_frames=None, points=None):
  # lead_frames / toggle_frames / gear_frames: one value per frame, in place of lead_visible / toggles / gear
  # points: radar points, stored as a new list each frame (as card.py does on each radar frame)
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
    cruiseControl=SimpleNamespace(override=override, cancel=False, resume=False),
    leftBlinker=left_blinker, rightBlinker=False,
    hudControl=SimpleNamespace(leadDistanceBars=distance_bars, leadVisible=lead_visible),
  )
  cs = SimpleNamespace(
    stock_lfa_msg=None, stock_lkas_msg=None, stock_lfahda_cluster_msg=None,
    lfa_block_msg={"COUNTER": 0, **{f"BYTE{i}": 0 for i in range(3, 24)}},
    openpilot_lead_visible=lead_visible, openpilot_lead_distance=30.0, openpilot_lead_rel_speed=-1.0,
    openpilot_t_follow=t_follow, openpilot_follow_distance=follow_distance, openpilot_lead_camera_prob=camera_prob,
    left_blindspot_from_radar=True, right_blindspot_from_radar=False,
    blindspots_rear_corners_ts=1, blindspots_front_corner_1_ts=1,
    blindspots_rear_corners=rear_corners or _zero_values(packer, "BLINDSPOTS_REAR_CORNERS"),
    blindspots_front_corner_1=_zero_values(packer, "BLINDSPOTS_FRONT_CORNER_1"),
    out=SimpleNamespace(steeringAngleDeg=0.0, gearShifter=gear, vEgo=v_ego, vEgoRaw=v_ego,
                        brakePressed=False, gasPressed=False, standstill=False,
                        cruiseState=SimpleNamespace(available=True)),
  )
  now_nanos = 10 ** 9  # car's own 0x1BA/0x1E5 are stale, so openpilot sends them
  msgs = []
  for i in range(frames):
    if lead_frames is not None:
      cs.openpilot_lead_visible = cc.hudControl.leadVisible = lead_frames[i]
    if gear_frames is not None:
      cs.out.gearShifter = gear_frames[i]
    if points is not None:
      cs.adjacent_radar_points = list(points)
    frame_toggles = toggle_frames[i] if toggle_frames is not None else toggles
    msgs += controller.create_canfd_msgs(now_nanos, True, 0.0, 0.0, 42.0, -1.0, False, cc.hudControl, cs, cc,
                                         frame_toggles, lka_icon=2, lfa_icon=2)
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
    assert dat[22] & 0x7 == 5  # vEgo 25 m/s

  def test_scc_control_disengaged_with_lead(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_scc_status=True), enabled=False))[0x1A0]
    assert dat[11] & 0x7 == 0
    assert dat[13] == 0x18
    assert dat[22] & 0x7 == 2
    assert (int.from_bytes(dat[3:5], "little") & 0x7FF) == 300  # openpilot's 30 m lead distance is kept
    assert int.from_bytes(dat[24:26], "little") & 0x7FF == 2046  # NEW_SIGNAL_15 204.6

  def test_scc_control_gas_override_with_lead(self):
    dat = _by_address(_controller_msgs(_toggles(ioniq6_hud_scc_status=True), override=True))[0x1A0]
    assert (dat[8] >> 4) & 0x7 == 2  # ACCMode override
    assert dat[13] == 0x18  # NEW_SIGNAL_3 1
    assert dat[22] & 0x7 == 5

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
    assert _controller_msgs(_toggles(**ALL_ON, **ROUND_2_ON), car=CAR.KIA_EV6) == \
           _controller_msgs(_toggles(), car=CAR.KIA_EV6)


ROUND_2_ON = {"ioniq6_hud_scc_gap": True, "ioniq6_hud_lead_filter": True}
SCC_ON = {"ioniq6_hud_scc_status": True}
GAP_BYTES = (11, 24, 25)  # DISTANCE_SETTING, NEW_SIGNAL_15


def _scc_frames(msgs):
  return [dat for address, dat, _ in msgs if address == 0x1A0]


def _scc_lead_shown(dat):
  return dat[3:6] != bytes.fromhex("fef77f")


class TestRound2Wiring:
  def test_round_2_toggles_off_match_round_1(self):
    round_1 = _toggles(**ALL_ON)
    off = _toggles(**ALL_ON, **dict.fromkeys(ROUND_2_ON, False))
    assert _controller_msgs(off, frames=60) == _controller_msgs(round_1, frames=60)

  def test_round_2_toggles_need_scc_status(self):
    assert _controller_msgs(_toggles(**ROUND_2_ON), frames=60) == _controller_msgs(_toggles(), frames=60)

  @pytest.mark.parametrize("bars", [1, 2, 3])
  def test_gap_sends_bars_and_follow_time(self, bars):
    toggles = _toggles(**SCC_ON, ioniq6_hud_scc_gap=True)
    msgs = _controller_msgs(toggles, t_follow=1.5, distance_bars=bars)
    dat = _by_address(msgs)[0x1A0]
    assert dat[11] & 0x7 == bars
    assert int.from_bytes(dat[24:26], "little") & 0x7FF == 435  # 25 m/s x 1.5 s + 6 m

  def test_gap_falls_back_to_stock_headway_without_follow_time(self):
    dat = _by_address(_controller_msgs(_toggles(**SCC_ON, ioniq6_hud_scc_gap=True), t_follow=0.0))[0x1A0]
    assert int.from_bytes(dat[24:26], "little") & 0x7FF == 250

  def test_gap_changes_only_gap_bytes(self):
    gap = _by_address(_controller_msgs(_toggles(**SCC_ON, ioniq6_hud_scc_gap=True)))
    scc = _by_address(_controller_msgs(_toggles(**SCC_ON)))
    assert {a: d for a, d in gap.items() if a != 0x1A0} == {a: d for a, d in scc.items() if a != 0x1A0}
    changed = {i for i in range(2, len(scc[0x1A0])) if gap[0x1A0][i] != scc[0x1A0][i]}
    assert changed and changed <= set(GAP_BYTES)

  def test_lead_filter_holds_back_a_new_lead_for_0_4_s(self):
    toggles = _toggles(**SCC_ON, ioniq6_hud_lead_filter=True)
    shown = [_scc_lead_shown(dat) for dat in _scc_frames(_controller_msgs(toggles, frames=50))]
    assert shown == [False] * 19 + [True] * 6

  def test_lead_filter_bridges_a_short_gap(self):
    toggles = _toggles(**SCC_ON, ioniq6_hud_lead_filter=True)
    lead_frames = [True] * 60 + [False] * 90 + [False] * 20
    scc = _scc_frames(_controller_msgs(toggles, frames=len(lead_frames), lead_frames=lead_frames))
    shown = [_scc_lead_shown(dat) for dat in scc]
    assert shown == [False] * 19 + [True] * 11 + [True] * 45 + [True] * 4 + [False] * 6
    assert scc[40][3:5] == scc[29][3:5]  # last distance held

  def test_lead_filter_resets_when_off(self):
    on = _toggles(**SCC_ON, ioniq6_hud_lead_filter=True)
    toggle_frames = [on] * 60 + [_toggles(**SCC_ON)] * 2 + [on] * 2
    lead_frames = [True] * 62 + [False] * 2
    scc = _scc_frames(_controller_msgs(on, frames=64, lead_frames=lead_frames, toggle_frames=toggle_frames))
    assert _scc_lead_shown(scc[29])
    assert not _scc_lead_shown(scc[-1])  # without the reset the lead would still be held


GEAR = structs.CarState.GearShifter
FILTER_ON = {**SCC_ON, "ioniq6_hud_lead_filter": True}


class TestRound2bWiring:
  def test_gap_sends_openpilot_target_gap(self):
    dat = _by_address(_controller_msgs(_toggles(**SCC_ON, ioniq6_hud_scc_gap=True), follow_distance=48.0))[0x1A0]
    assert int.from_bytes(dat[24:26], "little") & 0x7FF == 480

  def test_follow_distance_ignored_without_gap(self):
    toggles = _toggles(**SCC_ON)
    assert _controller_msgs(toggles, follow_distance=48.0) == _controller_msgs(toggles)

  @pytest.mark.parametrize("gear", [GEAR.reverse, GEAR.park, GEAR.neutral])
  def test_filter_hides_the_lead_outside_drive(self, gear):
    scc = _scc_frames(_controller_msgs(_toggles(**FILTER_ON), frames=100, gear=gear, v_ego=1.0))
    assert not any(_scc_lead_shown(dat) for dat in scc)

  def test_held_lead_clears_on_the_first_frame_in_reverse(self):
    gear_frames = [GEAR.drive] * 60 + [GEAR.reverse] * 4
    scc = _scc_frames(_controller_msgs(_toggles(**FILTER_ON), frames=64, gear_frames=gear_frames))
    assert _scc_lead_shown(scc[29])
    assert not _scc_lead_shown(scc[30])

  def test_slow_radar_only_lead_never_shows(self):
    scc = _scc_frames(_controller_msgs(_toggles(**FILTER_ON), frames=100, v_ego=2.0, camera_prob=0.3))
    assert not any(_scc_lead_shown(dat) for dat in scc)

  def test_slow_camera_supported_lead_shows_after_0_4_s(self):
    scc = _scc_frames(_controller_msgs(_toggles(**FILTER_ON), frames=50, v_ego=2.0, camera_prob=0.9))
    assert [_scc_lead_shown(dat) for dat in scc] == [False] * 19 + [True] * 6

  def test_fast_radar_only_lead_shows(self):
    scc = _scc_frames(_controller_msgs(_toggles(**FILTER_ON), frames=50, camera_prob=0.0))
    assert _scc_lead_shown(scc[-1])

  @pytest.mark.parametrize("gap", [False, True])
  def test_filter_off_ignores_gear_and_camera(self, gap):
    toggles = _toggles(**SCC_ON, ioniq6_hud_scc_gap=gap)
    kwargs = {"frames": 60, "v_ego": 2.0}
    drive = _scc_frames(_controller_msgs(toggles, gear=GEAR.drive, camera_prob=1.0, **kwargs))
    for gear, camera_prob in [(GEAR.reverse, 1.0), (GEAR.park, 0.0), (GEAR.drive, 0.0)]:
      assert _scc_frames(_controller_msgs(toggles, gear=gear, camera_prob=camera_prob, **kwargs)) == drive
    assert all(_scc_lead_shown(dat) for dat in drive)

  def test_other_cars_ignore_the_rules(self):
    toggles = _toggles(**ALL_ON, **ROUND_2_ON)
    kwargs = {"car": CAR.KIA_EV6, "frames": 60, "gear": GEAR.reverse, "v_ego": 1.0, "follow_distance": 48.0}
    assert _controller_msgs(toggles, camera_prob=0.0, **kwargs) == _controller_msgs(_toggles(), **kwargs)


# Round 3, adjacent cars. Stock ADRV_0x1ea frames (route 000000d1--48888e416c) with their decoded distances.
STOCK_0X1EA = {
  "empty": ("ffc049080100000000000000000000ff000000000000000000000000000f0f00", {}),
  "left_ahead": ("5089170801c026001e010000000000ff000000000000000000000000000f0f00", {"left_ahead": 15.5}),
  "left_behind": ("38c28f080100000000000000000000ff00a0011e0100000000000000000f0f00", {"left_behind": 5.2}),
  "right_behind": ("8b256f080100000000000000000000ff00000000b8e1010100000000000f0f00", {"right_behind": 5.5}),
  "all": ("5ed8830801402b001ee9051e010000ff0000021e31e4010100000000000f0f00",
          {"left_ahead": 17.3, "right_ahead": 18.9, "left_behind": 6.4, "right_behind": 13.4}),
}


def _packed_0x1ea(packer, can_bus):
  return next(msg for msg in create_adrv_messages(packer, can_bus, 0) if msg[0] == 0x1EA)


class TestPatchAdrv0x1ea:
  @pytest.mark.parametrize("case", STOCK_0X1EA)
  def test_matches_stock(self, ioniq_6, case):
    stock_hex, cars = STOCK_0X1EA[case]
    stock = bytes.fromhex(stock_hex)
    assert hkg_can_fd_checksum(0x1EA, None, bytearray(stock)) == int.from_bytes(stock[:2], "little")
    address, dat, bus = patch_adrv_0x1ea(_packed_0x1ea(*ioniq_6), True, cars)
    assert (address, bus) == (0x1EA, ioniq_6[1].ECAN)
    assert dat[3:] == stock[3:]  # byte 2 is the counter
    assert hkg_can_fd_checksum(address, None, bytearray(dat)) == int.from_bytes(dat[:2], "little")

  def test_inactive_writes_no_cars(self, ioniq_6):
    packed = _packed_0x1ea(*ioniq_6)
    dat = patch_adrv_0x1ea(packed, False, STOCK_0X1EA["all"][1])[1]
    expected = bytearray(packed[1])
    expected[3] = 0x08
    assert dat[2:] == bytes(expected[2:])
    assert hkg_can_fd_checksum(0x1EA, None, bytearray(dat)) == int.from_bytes(dat[:2], "little")

  def test_active_bit_is_cleared_when_inactive(self, ioniq_6):
    packer, can_bus = ioniq_6
    packed = next(msg for msg in create_adrv_messages(packer, can_bus, 0, adrv_0x1ea_extra=adrv_0x1ea_values(True))
                  if msg[0] == 0x1EA)
    assert packed[1][4] & 1
    assert patch_adrv_0x1ea(packed, False, {})[1][4] & 1 == 0

  def test_distance_saturates_at_the_field_width(self, ioniq_6):
    dat = patch_adrv_0x1ea(_packed_0x1ea(*ioniq_6), True, {"right_behind": 30.0})[1]
    assert (int.from_bytes(dat, "little") >> 163) & 0xFF == 0xFF
    assert dat[21] & 0xE0 == 0xE0 and dat[22] == 1 and dat[23] == 1


def _pt(track_id, d_rel, y_rel, v_rel):
  return SimpleNamespace(trackId=track_id, dRel=d_rel, yRel=y_rel, vRel=v_rel)


V_EGO = 25.0


def _run_tracker(tracker, frames, active=True, left_bit=False, right_bit=False):
  # frames: one list of radar points per 20 Hz frame (a new list each frame, as card.py stores)
  return [tracker.update(active, list(points), V_EGO, left_bit, right_bit) for points in frames]


class TestAdjacentSelection:
  def test_distance_conversion(self):
    assert adjacent_distance(3.0) == pytest.approx(1.25)
    assert adjacent_distance(8.0) == pytest.approx(7.125)
    assert adjacent_distance(20.0) == 20.0
    assert adjacent_distance(0.5) == 0.0

  @pytest.mark.parametrize("y_rel, side, found", [
    (3.0, 1, True), (-3.0, -1, True), (3.0, -1, False), (-3.0, 1, False),
    (1.0, 1, False), (5.0, 1, False), (-1.5, -1, False), (-4.5, -1, False),
  ])
  def test_lane_window(self, y_rel, side, found):
    assert (select_adjacent_track([_pt(1, 20.0, y_rel, 0.0)], V_EGO, side) is not None) == found

  def test_parked_cars_are_ignored(self):
    assert select_adjacent_track([_pt(1, 20.0, 3.0, -V_EGO + 1.0)], V_EGO, 1) is None
    assert select_adjacent_track([_pt(1, 20.0, 3.0, -V_EGO + 3.0)], V_EGO, 1) is not None

  def test_range(self):
    assert select_adjacent_track([_pt(1, 85.0, 3.0, 0.0)], V_EGO, 1) is None
    assert select_adjacent_track([_pt(1, -1.0, 3.0, 0.0)], V_EGO, 1) is None

  def test_nearest_wins_and_exclude(self):
    points = [_pt(1, 30.0, 3.0, 0.0), _pt(2, 12.0, 3.2, -1.0), _pt(3, 5.0, 0.0, 0.0)]
    assert select_adjacent_track(points, V_EGO, 1) == (12.0, -1.0, 2)
    assert select_adjacent_track(points, V_EGO, 1, exclude_id=2) == (30.0, 0.0, 1)


def _approach(track_id, start, rel_speed, frames, y_rel=3.0):
  # a car in the left lane whose radar distance changes by rel_speed; the radar keeps it at 2.5 m once alongside
  return [[_pt(track_id, max(start + rel_speed * 0.05 * i, 2.5), y_rel, rel_speed)] for i in range(frames)]


class TestAdjacentCarTracker:
  def test_blip_never_shows(self):
    out = _run_tracker(AdjacentCarTracker(), [[_pt(1, 20.0, 3.0, 0.0)]] * 7 + [[]] * 10)
    assert not any(out)

  def test_shows_after_0_4_s_on_its_side(self):
    out = _run_tracker(AdjacentCarTracker(), [[_pt(1, 20.0, -3.0, 0.0)]] * 10)
    assert out[:7] == [{}] * 7
    assert out[7:] == [{"right_ahead": 20.0}] * 3

  def test_0_9_s_gap_is_bridged_and_1_s_hides(self):
    out = _run_tracker(AdjacentCarTracker(), [[_pt(1, 20.0, 3.0, 0.0)]] * 10 + [[]] * 20)
    assert out[10:29] == [{"left_ahead": 20.0}] * 19
    assert out[29] == {}

  def test_stale_points_count_as_none(self):
    tracker = AdjacentCarTracker()
    points = [_pt(1, 20.0, 3.0, 0.0)]
    out = [tracker.update(True, points, V_EGO, False, False) for _ in range(40)]
    assert out[9] == {"left_ahead": 20.0}
    assert out[-1] == {}  # the same list for 0.5 s, then 1 s of hold

  def test_blindspot_bit_never_creates_a_car(self):
    out = _run_tracker(AdjacentCarTracker(), [[]] * 100, left_bit=True, right_bit=True)
    assert not any(out)

  def test_pass_ahead_coast_behind_then_dropped_when_the_bit_clears(self):
    tracker = AdjacentCarTracker()
    # passing at 5 m/s from 15 m ahead; the radar hangs at 2.5 m alongside while the drawn car keeps moving back
    out = _run_tracker(tracker, _approach(1, 15.0, -5.0, 80), left_bit=True)
    slots = [next(iter(cars)) if cars else None for cars in out]
    first_behind = slots.index("left_behind")
    assert slots[:7] == [None] * 7
    assert slots[7:first_behind] == ["left_ahead"] * (first_behind - 7)
    assert slots[first_behind:] == ["left_behind"] * (80 - first_behind)
    ahead = [cars["left_ahead"] for cars in out[7:first_behind]]
    behind = [cars["left_behind"] for cars in out[first_behind:]]
    assert ahead == sorted(ahead, reverse=True) and min(ahead) >= 0.0
    assert behind == sorted(behind) and behind[-1] > 2.0
    # the bit clears: kept for 1 s, then dropped
    out = _run_tracker(tracker, [[]] * 25)
    assert all("left_behind" in cars for cars in out[:19])
    assert out[-1] == {}

  def test_behind_needs_the_bit_within_1_5_s(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, _approach(1, 15.0, -5.0, 60))
    out = _run_tracker(tracker, [[]] * 60)
    assert any("left_behind" in cars for cars in out)
    assert out[-1] == {}

  def test_behind_is_capped_and_ends_past_the_cap(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, _approach(1, 15.0, -5.0, 60))
    out = _run_tracker(tracker, [[]] * 120, left_bit=True)
    behind = [cars["left_behind"] for cars in out if "left_behind" in cars]
    assert max(behind) <= 19.4
    assert out[-1] == {}

  def test_coast_ends_at_the_time_limit(self):
    tracker = AdjacentCarTracker()
    # a slow pass (0.6 m/s) with the bit on throughout: still behind at 10 s, gone after
    out = _run_tracker(tracker, _approach(1, 6.0, -0.6, 160), left_bit=True)
    out += _run_tracker(tracker, [[]] * 140, left_bit=True)
    coast_start = next(i for i, cars in enumerate(out) if cars and cars["left_ahead"] < 3.0)
    assert "left_behind" in out[coast_start + 199]
    assert out[coast_start + 201] == {}

  def test_slow_pass_without_the_bit_is_not_drawn_behind(self):
    tracker = AdjacentCarTracker()
    # the radar loses the car alongside; it reaches the behind slot 5 s into the coast, long after the 1.5 s grace
    out = _run_tracker(tracker, _approach(1, 6.0, -0.6, 110)) + _run_tracker(tracker, [[]] * 90)
    assert any(cars.get("left_ahead", 3.0) < 1.0 for cars in out)
    assert not any("left_behind" in cars for cars in out)

  def test_a_new_car_ahead_shows_while_the_passed_car_is_behind(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, _approach(1, 15.0, -5.0, 60))
    _run_tracker(tracker, [[]] * 15, left_bit=True)
    out = _run_tracker(tracker, [[_pt(2, 30.0, 3.0, -1.0)]] * 10, left_bit=True)
    assert out[-1]["left_ahead"] == 30.0 and "left_behind" in out[-1]

  def test_the_passed_car_seen_ahead_again_ends_the_coast(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, _approach(1, 15.0, -5.0, 60))
    out = _run_tracker(tracker, [[_pt(1, 10.0, 3.0, 2.0)]] * 10, left_bit=True)
    assert "left_behind" not in out[-1]
    assert out[-1] == {"left_ahead": adjacent_distance(10.0)}

  def test_a_car_is_never_in_both_slots(self):
    tracker = AdjacentCarTracker()
    out = _run_tracker(tracker, _approach(1, 15.0, -5.0, 60) + [[_pt(1, 2.5, 3.0, -5.0)]] * 40, left_bit=True)
    assert not any("left_ahead" in cars and "left_behind" in cars for cars in out)

  def test_reset_when_inactive(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, [[_pt(1, 20.0, 3.0, 0.0)]] * 10)
    assert _run_tracker(tracker, [[_pt(1, 20.0, 3.0, 0.0)]], active=False) == [{}]
    out = _run_tracker(tracker, [[_pt(1, 20.0, 3.0, 0.0)]] * 8)
    assert out[:7] == [{}] * 7


ADJACENT_ON = {"ioniq6_hud_adjacent_cars": True}
LEFT_CAR = [_pt(1, 20.0, 3.0, 0.0)]


def _0x1ea_frames(msgs):
  return [dat for address, dat, _ in msgs if address == 0x1EA]


class TestRound3Wiring:
  @pytest.mark.parametrize("base", [ALL_OFF, {**ALL_ON, **ROUND_2_ON}])
  def test_switch_off_matches_round_2b(self, base):
    off = _controller_msgs(_toggles(**base, ioniq6_hud_adjacent_cars=False), frames=60, points=LEFT_CAR)
    assert off == _controller_msgs(_toggles(**base), frames=60)

  @pytest.mark.parametrize("base", [ALL_OFF, ALL_ON])
  def test_switch_on_changes_only_0x1ea(self, base):
    off = _controller_msgs(_toggles(**base), frames=60, points=LEFT_CAR)
    on = _controller_msgs(_toggles(**base, **ADJACENT_ON), frames=60, points=LEFT_CAR)
    assert [m for m in on if m[0] != 0x1EA] == [m for m in off if m[0] != 0x1EA]
    assert len(_0x1ea_frames(on)) == len(_0x1ea_frames(off)) == 12
    assert _0x1ea_frames(on) != _0x1ea_frames(off)

  def test_left_car_shows_after_0_4_s(self):
    frames = _0x1ea_frames(_controller_msgs(_toggles(**ADJACENT_ON), frames=60, points=LEFT_CAR))
    assert all(dat[3] == 0x08 and dat[4] & 1 for dat in frames)
    assert [dat[8] for dat in frames] == [0] * 7 + [0x1E] * 5
    assert (int.from_bytes(frames[-1], "little") >> 46) & 0x7FF == 200
    assert all(hkg_can_fd_checksum(0x1EA, None, bytearray(dat)) == int.from_bytes(dat[:2], "little") for dat in frames)

  @pytest.mark.parametrize("kwargs", [{"enabled": False}, {"gear": GEAR.reverse}])
  def test_nothing_drawn_when_disengaged_or_outside_drive(self, kwargs):
    frames = _0x1ea_frames(_controller_msgs(_toggles(**ALL_ON, **ADJACENT_ON), frames=60, points=LEFT_CAR, **kwargs))
    assert all(dat[3] == 0x08 and dat[4] & 1 == 0 and not any(dat[5:15]) for dat in frames)

  def test_blindspot_bit_alone_draws_nothing(self):
    # the harness reports the left blind-spot bit on every frame
    frames = _0x1ea_frames(_controller_msgs(_toggles(**ADJACENT_ON), frames=60, points=[]))
    assert len(frames) == 12
    assert all(dat[4] & 1 and dat[5:15] == bytes(10) and dat[16:28] == bytes(12) for dat in frames)

  def test_other_cars_ignore_the_switch(self):
    kwargs = {"car": CAR.KIA_EV6, "frames": 60, "points": LEFT_CAR}
    assert _controller_msgs(_toggles(**ADJACENT_ON), **kwargs) == _controller_msgs(_toggles(), **kwargs)
