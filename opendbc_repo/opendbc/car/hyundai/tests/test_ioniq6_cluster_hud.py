from types import SimpleNamespace

import numpy as np
import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus
from opendbc.car.structs import CarParams
from opendbc.car.hyundai.hyundaicanfd import CanBus, create_adrv_messages, create_blindspot_status_messages, \
                                             hkg_can_fd_checksum
from opendbc.car.hyundai.ioniq6_cluster_hud import AdjacentCarTracker, LeadDisplayFilter, adjacent_distance, \
                                                   adrv_0x1ea_values, corrected_rel_speed, \
                                                   create_stock_like_blindspot_messages, \
                                                   lead_display_allowed, no_lane_sides, patch_adrv_0x1ea, \
                                                   scc_status_values, select_adjacent_track
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
    stock = scc_status_values(True, True, 20.0)
    assert {k: v for k, v in gap.items() if k not in ("DISTANCE_SETTING", "NEW_SIGNAL_15")} == \
           {k: v for k, v in stock.items() if k not in ("DISTANCE_SETTING", "NEW_SIGNAL_15")}


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

  def test_fallback_frames_do_not_replace_the_held_range(self):
    # f6 seg 15: radar lead at ~91 m drops, the HUD flag stays on 1-2 frames (fallback 20 m), then nothing.
    lead_filter = LeadDisplayFilter()
    _run_filter(lead_filter, [(True, 30)])
    out = [lead_filter.update(True, 20.0, 0.0, range_known=False) for _ in range(2)]
    out += [lead_filter.update(False, 0.0, 0.0) for _ in range(49)]
    assert set(out) == {(True, 69.0, -1.0)}
    assert not lead_filter.update(False, 0.0, 0.0)[0]

  def test_a_lead_with_no_range_ever_still_shows_at_the_fallback(self):
    lead_filter = LeadDisplayFilter()
    out = [lead_filter.update(True, 20.0, 0.0, range_known=False) for _ in range(25)]
    assert out[-1] == (True, 20.0, 0.0)

  def test_a_measured_range_replaces_a_held_fallback(self):
    lead_filter = LeadDisplayFilter()
    for _ in range(25):
      lead_filter.update(True, 20.0, 0.0, range_known=False)
    assert lead_filter.update(True, 55.0, -2.0) == (True, 55.0, -2.0)
    assert lead_filter.update(True, 20.0, 0.0, range_known=False) == (True, 55.0, -2.0)

  def test_showing_on_a_fallback_frame_uses_the_range_measured_while_hidden(self):
    # f8 seg 22: a weak lead reaches the show count on the frame after the radar lead blinked off.
    lead_filter = LeadDisplayFilter()
    out = [lead_filter.update(True, 115.0 - i * 0.1, -4.5) for i in range(19)]
    out.append(lead_filter.update(True, 20.0, 0.0, range_known=False))
    assert not any(visible for visible, _, _ in out[:19])
    assert out[19] == (True, pytest.approx(113.2), -4.5)

  def test_a_range_measured_before_the_count_decays_to_zero_is_forgotten(self):
    lead_filter = LeadDisplayFilter()
    for _ in range(10):
      lead_filter.update(True, 60.0, -1.0)
    for _ in range(10):
      lead_filter.update(False, 0.0, 0.0)
    out = [lead_filter.update(True, 20.0, 0.0, range_known=False) for _ in range(20)]
    assert out[-1] == (True, 20.0, 0.0)

  def test_fallback_range_is_dropped_once_the_lead_rehides(self):
    lead_filter = LeadDisplayFilter()
    _run_filter(lead_filter, [(True, 30), (False, 50)])
    out = [lead_filter.update(True, 20.0, 0.0, range_known=False) for _ in range(20)]
    assert out[-1] == (True, 20.0, 0.0)  # a new lead, nothing measured held over from the old one

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


# Adjacent cars. Stock ADRV_0x1ea frames (route 000000d1--48888e416c) with their decoded distances.
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


def _run_tracker(tracker, frames, active=True, left_bit=False, right_bit=False, no_lane=None):
  # frames: one list of radar points per 20 Hz frame (a new list each frame, as card.py stores)
  # no_lane: passed to the tracker when given, else the default (no gate)
  kwargs = {} if no_lane is None else {"no_lane": no_lane}
  return [tracker.update(active, list(points), V_EGO, left_bit, right_bit, **kwargs) for points in frames]


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
    assert select_adjacent_track(points, V_EGO, 1)[:3] == (12.0, -1.0, 2)
    assert select_adjacent_track(points, V_EGO, 1, exclude_id=2)[:3] == (30.0, 0.0, 1)
    assert select_adjacent_track(points, V_EGO, 1, excluded_ids={2})[:3] == (30.0, 0.0, 1)


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

  def test_oncoming_traffic_is_ignored(self):
    out = _run_tracker(AdjacentCarTracker(), [[_pt(1, 60.0 - 3.75 * i, 3.0, -50.0)] for i in range(16)])
    assert not any(out)

  def test_passed_car_reacquired_under_a_new_id_is_not_drawn_twice(self):
    tracker = AdjacentCarTracker()
    out = _run_tracker(tracker, _approach(1, 15.0, -5.0, 60), left_bit=True)
    assert "left_behind" in out[-1]
    out = _run_tracker(tracker, [[]] + [[_pt(7, 2.5, 3.0, -5.0)]] * 20, left_bit=True)
    assert all("left_behind" in cars and "left_ahead" not in cars for cars in out)

  def test_car_held_alongside_without_the_bit_is_not_passed_again(self):
    # the radar holds a passed car at 2.5 m for 30 s and the bit never comes on: one pass, behind only within the
    # 1.5 s start grace, then drawn alongside ahead
    out = _run_tracker(AdjacentCarTracker(), _approach(1, 15.0, -5.0, 600))
    behind = [i for i, cars in enumerate(out) if "left_behind" in cars]
    assert 0 < len(behind) <= 30 and behind[-1] - behind[0] == len(behind) - 1
    assert not any("left_behind" in cars for cars in out[behind[-1] + 1:])
    assert out[-1] == {"left_ahead": adjacent_distance(2.5)}

  def test_car_held_alongside_with_the_bit_is_passed_again(self):
    out = _run_tracker(AdjacentCarTracker(), _approach(1, 15.0, -5.0, 600), left_bit=True)
    assert sum("left_behind" in cars for cars in out) > 300
    assert not any("left_ahead" in cars and "left_behind" in cars for cars in out)

  def test_stale_points_stay_stale_across_a_re_engage(self):
    tracker = AdjacentCarTracker()
    points = [_pt(1, 20.0, 3.0, 0.0)]
    for _ in range(20):
      tracker.update(True, points, V_EGO, False, False)
    tracker.update(False, points, V_EGO, False, False)
    out = [tracker.update(True, points, V_EGO, False, False) for _ in range(20)]
    assert not any(out)

  def test_reset_when_inactive(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, [[_pt(1, 20.0, 3.0, 0.0)]] * 10)
    assert _run_tracker(tracker, [[_pt(1, 20.0, 3.0, 0.0)]], active=False) == [{}]
    out = _run_tracker(tracker, [[_pt(1, 20.0, 3.0, 0.0)]] * 8)
    assert out[:7] == [{}] * 7


def _radial(track_id, ground_speed, start, frames, y_rel=3.0, end=0.5):
  # a track in the left lane at a constant ground speed, with vRel read along the line of sight as the radar does
  # (alongside, a stationary or oncoming object reads as closing slowly); gone once it is under `end` m
  out = []
  for i in range(frames):
    d = start + (ground_speed - V_EGO) * 0.05 * i
    out.append([_pt(track_id, d, y_rel, (ground_speed - V_EGO) * d / np.hypot(d, y_rel))] if d > end else [])
  return out


class TestAdjacentRadialSpeed:
  # highway drives de/e0 (2026-10-23): barrier posts and oncoming cars read as moving alongside and blinked behind
  def test_correction_recovers_the_closing_speed_alongside(self):
    pt = _pt(1, 4.2, 2.7, -V_EGO * 4.2 / np.hypot(4.2, 2.7))
    assert corrected_rel_speed(pt) == pytest.approx(-V_EGO)
    assert corrected_rel_speed(_pt(1, 0.5, 3.0, -1.0)) == pytest.approx(-2.0)  # capped
    assert corrected_rel_speed(_pt(1, 50.0, 3.0, -1.0)) == pytest.approx(-1.0, abs=0.01)

  @pytest.mark.parametrize("ground_speed", [0.0, -20.0], ids=["stationary", "oncoming"])
  def test_never_drawn(self, ground_speed):
    out = _run_tracker(AdjacentCarTracker(), _radial(1, ground_speed, 30.0, 80) + [[]] * 40)
    assert not any(out)

  def test_a_track_seen_not_moving_stays_excluded_alongside(self):
    # seen stationary at 20 m, then (as a radar artifact) reads ~6 m/s over ground once close, also corrected: not drawn
    frames = [[_pt(1, 20.0, 3.0, -V_EGO)]] * 5 + [[_pt(1, 6.0, 3.0, -V_EGO + 8.0)]] * 20
    assert not any(_run_tracker(AdjacentCarTracker(), frames))

  def test_the_latch_ends_when_the_track_reads_moving_where_the_angle_is_small(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, [[_pt(1, 20.0, 3.0, -V_EGO)]] * 5)
    out = _run_tracker(tracker, [[_pt(1, 12.0, 3.0, -1.0)]] * 10)
    assert out[7] == {"left_ahead": 12.0}

  def test_a_latched_track_unseen_for_2_s_is_forgotten(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, [[_pt(1, 20.0, 3.0, -V_EGO)]] * 5)
    _run_tracker(tracker, [[]] * 39)
    assert 1 in tracker.not_moving
    _run_tracker(tracker, [[]])
    assert 1 not in tracker.not_moving

  def test_no_latch_in_stop_and_go(self):
    # we creep at 1 m/s next to a stopped car, then it pulls away alongside: drawn
    tracker = AdjacentCarTracker()
    for _ in range(10):
      tracker.update(True, [_pt(1, 6.0, 3.0, -1.0)], 1.0, False, False)
    out = [tracker.update(True, [_pt(1, 6.0, 3.0, 3.0)], 1.0, False, False) for _ in range(10)]
    assert "left_ahead" in out[-1]

  @pytest.mark.parametrize("bit, drawn", [(False, False), (True, True)])
  def test_a_fast_pass_reaches_the_behind_slot_only_with_the_bit(self, bit, drawn):
    # passing a car 15 m/s slower: drawn ahead, then behind only with the blind-spot bit
    out = _run_tracker(AdjacentCarTracker(), _radial(1, V_EGO - 15.0, 40.0, 60, end=2.5) + [[]] * 30, left_bit=bit)
    assert any("left_ahead" in cars for cars in out)
    assert any("left_behind" in cars for cars in out) == drawn

  def test_a_normal_pass_without_the_bit_keeps_the_start_grace(self):
    out = _run_tracker(AdjacentCarTracker(), _radial(1, V_EGO - 5.0, 15.0, 60, end=2.5) + [[]] * 30)
    assert any("left_behind" in cars for cars in out)


MIN_LC = 8.94  # MinimumLaneChangeSpeed 20 mph


class TestAdjacentNoLane:
  # drive f6 (2026-10-25): in the left-most lane, barrier reflections at our speed 35-80 m ahead in the left window
  @pytest.mark.parametrize("widths, v_ego, speed, expected", [
    (None, 25.0, MIN_LC, (False, False)),
    ((0.0, 0.0), 25.0, None, (False, False)),
    ((0.0, 0.0), MIN_LC + 0.9, MIN_LC, (False, False)),
    ((0.0, 0.0), MIN_LC + 1.0, MIN_LC, (True, True)),
    ((0.0, 3.5), 25.0, MIN_LC, (True, False)),
    ((3.5, 0.0), 25.0, MIN_LC, (False, True)),
    ((3.5, 3.5), 25.0, MIN_LC, (False, False)),
  ])
  def test_no_lane_sides(self, widths, v_ego, speed, expected):
    assert no_lane_sides(widths, v_ego, speed) == expected

  def test_a_car_on_a_side_with_no_lane_never_shows(self):
    out = _run_tracker(AdjacentCarTracker(), [[_pt(1, 50.0, 3.0, 0.0)]] * 40, no_lane=(True, False))
    assert not any(out)

  def test_a_shown_car_hides_while_its_side_has_no_lane(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, [[_pt(1, 50.0, 3.0, 0.0)]] * 10)
    assert _run_tracker(tracker, [[_pt(1, 50.0, 3.0, 0.0)]] * 5, no_lane=(True, False)) == [{}] * 5
    assert _run_tracker(tracker, [[_pt(1, 50.0, 3.0, 0.0)]]) == [{"left_ahead": 50.0}]

  def test_the_behind_slot_is_not_masked(self):
    # a passed car coasting behind with the bit on stays drawn when its side loses its lane (only the ahead slot is
    # masked; behind needs the blind-spot bit)
    tracker = AdjacentCarTracker()
    out = _run_tracker(tracker, _approach(1, 15.0, -5.0, 80), left_bit=True)
    assert "left_behind" in out[-1]
    out = _run_tracker(tracker, _approach(1, 2.5, 0.0, 10), left_bit=True, no_lane=(True, False))
    assert all("left_behind" in cars for cars in out)

  @pytest.mark.parametrize("no_lane, expected", [
    ((True, False), {"right_ahead": 20.0}), ((False, True), {"left_ahead": 50.0}),
  ])
  def test_the_other_side_is_unaffected(self, no_lane, expected):
    frames = [[_pt(1, 50.0, 3.0, 0.0), _pt(2, 20.0, -3.0, 0.0)]] * 10
    assert _run_tracker(AdjacentCarTracker(), frames, no_lane=no_lane)[-1] == expected

  @pytest.mark.parametrize("frames", [
    [[_pt(1, 20.0, 3.0, 0.0)]] * 10 + [[]] * 20,
    _approach(1, 15.0, -5.0, 80),
    _radial(1, V_EGO - 15.0, 40.0, 60, end=2.5) + [[]] * 30,
  ])
  def test_gate_off_matches_the_default(self, frames):
    off = _run_tracker(AdjacentCarTracker(), frames, left_bit=True, no_lane=(False, False))
    assert off == _run_tracker(AdjacentCarTracker(), frames, left_bit=True)

  def test_a_latched_track_stays_latched_through_a_gated_stretch(self):
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, [[_pt(1, 20.0, 3.0, -V_EGO)]] * 5)
    _run_tracker(tracker, [[_pt(1, 6.0, 3.0, -V_EGO + 8.0)]] * 20, no_lane=(True, False))
    assert 1 in tracker.not_moving
    assert not any(_run_tracker(tracker, [[_pt(1, 6.0, 3.0, -V_EGO + 8.0)]] * 20))

  def test_a_coast_still_ends_when_the_car_reappears_ahead(self):
    # code review: with the side's points dropped, the coast never saw the car pull back ahead and kept it behind
    tracker = AdjacentCarTracker()
    _run_tracker(tracker, _approach(1, 15.0, -5.0, 80), left_bit=True)
    out = _run_tracker(tracker, [[_pt(1, 10.0, 3.0, 1.0)]] * 40, left_bit=True, no_lane=(True, False))
    assert not any(out)

  def test_one_gated_frame_does_not_pass_a_held_car_again(self):
    # code review: a passed car held alongside (no bit) stays drawn ahead; one gated frame used to forget it was
    # passed, so the next frame passed it again into the behind slot without the bit
    frames = _approach(1, 15.0, -5.0, 80) + [[_pt(1, 2.5, 3.0, -5.0)]] * 120
    tracker = AdjacentCarTracker()
    out = [tracker.update(True, list(points), V_EGO, False, False, (i == 150, False)) for i, points in enumerate(frames)]
    assert not any("left_behind" in cars for cars in out[100:])

  def test_the_gate_never_adds_a_slot(self):
    # code review: gating the tracker's input drew behind cars the ungated run didn't. Random tracks, bits and gate
    # stretches on both sides: every frame's slots are a subset of the ungated run's, the same distances
    rng = np.random.default_rng(0)
    for _ in range(50):
      frames, gates, d = [], [], {1: rng.uniform(5, 40), 2: rng.uniform(5, 40)}
      v = {1: rng.uniform(-6, 3), 2: rng.uniform(-6, 3)}
      gate = (False, False)
      for _ in range(300):
        for i in d:
          d[i] = max(d[i] + v[i] * 0.05, 2.5)
          if rng.random() < 0.02:
            v[i] = rng.uniform(-6, 3)
        frames.append([_pt(1, d[1], 3.0, v[1]), _pt(2, d[2], -3.0, v[2])] if rng.random() > 0.05 else [])
        if rng.random() < 0.05:
          gate = (bool(rng.random() < 0.5), bool(rng.random() < 0.5))
        gates.append(gate)
      bits = rng.random((300, 2)) < 0.5
      gated, ungated = AdjacentCarTracker(), AdjacentCarTracker()
      for points, gate, (lb, rb) in zip(frames, gates, bits, strict=True):
        a = gated.update(True, points, V_EGO, lb, rb, gate)
        b = ungated.update(True, points, V_EGO, lb, rb)
        assert a.items() <= b.items()
