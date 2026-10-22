"""Temporary Ioniq 6 cluster HUD experiment (branch experiment/ioniq6-cluster-hud, never merged).

Under openpilot longitudinal the ADAS ECU is off, so openpilot sends the messages the cluster reads to draw the
lead car, the adjacent-lane cars and the blind-spot icon. Stock sends status values in them that openpilot does
not. These helpers return the stock values, behind temporary toggles, to find which ones the cluster needs.
"""
import numpy as np

from opendbc.car import CanData
from opendbc.car.hyundai.hyundaicanfd import hkg_can_fd_checksum

# Stock SCC_CONTROL on the gen5w Ioniq 6 (route 000000d1--48888e416c). The DBC range of DISTANCE_SETTING ([0|3])
# is stale. Cruise on: DISTANCE_SETTING 6, NEW_SIGNAL_15 = vEgo * 1.0 s (7 and 1.79 s after one gap change, mapping
# unknown), and with a lead NEW_SIGNAL_3 2 and OBJ_STATUS by speed (STOCK_LEAD_OBJ_STATUS_BANDS). Gas override with
# a lead: same OBJ_STATUS bands, NEW_SIGNAL_3 1 (every frame). Cruise off: DISTANCE_SETTING 0, NEW_SIGNAL_15 204.6,
# and a lead is still reported, with OBJ_STATUS 2 or 4 (2 most common) and NEW_SIGNAL_3 1.
STOCK_DISTANCE_SETTING = 6
# (upper vEgo bound in m/s, OBJ_STATUS), most common stock value per band. Stock sends mostly 0 at 14-16 m/s; 1 is
# kept there so the lead is still reported.
STOCK_LEAD_OBJ_STATUS_BANDS = ((12.0, 2), (16.0, 1), (20.0, 7), (25.0, 6), (float("inf"), 5))
STOCK_CRUISE_OFF_LEAD_OBJ_STATUS = 2
STOCK_HEADWAY_S = 1.0
NO_LEAD_DISTANCE = 204.6
NO_LEAD_REL_SPEED = 34.6
# openpilot's stop distance (STOP_DISTANCE in selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py, which opendbc
# cannot import). Its target gap in steady following is vEgo * tFollow + this.
STOP_DISTANCE_M = 6.0

# Round 2b, lead filter toggle: under this speed a lead is shown only with camera support. On route d8 every slow
# radar-only lead was a parked car, cross traffic or a blip; at speed radar-only stretches lasted at most 1.6 s.
SLOW_LEAD_SPEED = 3.0
SLOW_LEAD_MIN_CAMERA_PROB = 0.5

# Stock BLINDSPOTS_REAR_CORNERS sets two bits that are not DBC signals.
REAR_CORNERS_UNDEFINED_BITS = (25, 65)


def scc_status_values(enabled: bool, lead_visible: bool, v_ego: float, gas_override: bool = False,
                      distance_bars: int | None = None, t_follow: float | None = None,
                      follow_distance: float | None = None) -> dict:
  # distance_bars, t_follow and follow_distance (gap toggle) replace stock's fixed 6 and 1.0 s with openpilot's
  # personality bars (1-3) and target gap. follow_distance (desiredFollowDistance) shrinks and goes negative while a
  # lead pulls away fast, so it is kept at STOP_DISTANCE_M or more. It is 0 unless openpilot tracks a lead; then the
  # gap is estimated from t_follow, and t_follow is 0 while disengaged, so 1.0 s is kept then.
  if enabled:
    if follow_distance:
      gap = max(follow_distance, STOP_DISTANCE_M)
    elif t_follow:
      gap = v_ego * t_follow + STOP_DISTANCE_M
    else:
      gap = v_ego * STOCK_HEADWAY_S
    values = {
      "DISTANCE_SETTING": STOCK_DISTANCE_SETTING if distance_bars is None else int(distance_bars),
      "NEW_SIGNAL_15": float(np.clip(gap, 0.0, NO_LEAD_DISTANCE)),
    }
  else:
    values = {"DISTANCE_SETTING": 0, "NEW_SIGNAL_15": NO_LEAD_DISTANCE}

  # With a lead, openpilot's own distance and relative speed are kept.
  if lead_visible:
    if enabled:
      values["OBJ_STATUS"] = next(status for top, status in STOCK_LEAD_OBJ_STATUS_BANDS if v_ego < top)
    else:
      values["OBJ_STATUS"] = STOCK_CRUISE_OFF_LEAD_OBJ_STATUS
    values["NEW_SIGNAL_3"] = 2 if enabled and not gas_override else 1
  else:
    values["OBJ_STATUS"] = 0
    values["NEW_SIGNAL_3"] = 0
    values["ACC_ObjDist"] = NO_LEAD_DISTANCE
    values["ACC_ObjRelSpd"] = NO_LEAD_REL_SPEED
    values["ObjValid"] = 1
  return values


def lead_display_allowed(drive_gear: bool, v_ego: float, camera_prob: float) -> bool:
  # Round 2b, lead filter toggle. Stock never shows a lead outside drive; on d8 reversing showed a radar-only object
  # stuck about 2.5 m ahead. Slow radar-only leads are hidden too (see SLOW_LEAD_SPEED). Only ever hides a lead.
  return drive_gear and (v_ego >= SLOW_LEAD_SPEED or camera_prob > SLOW_LEAD_MIN_CAMERA_PROB)


class LeadDisplayFilter:
  """Debounces the lead shown on the cluster (round 2, lead filter toggle), updated once per SCC_CONTROL frame (50 Hz).

  openpilot's lead often flips on and off for under 1 s when it is weak and far away; stock's lead runs last ~10 s.
  The lead is shown after SHOW_FRAMES of continuous lead and hidden after HIDE_FRAMES without one. The counters are
  leaky (a frame of the other kind takes one off, it does not reset them), so a lead seen most of the time with short
  dropouts still shows, and one that only blips now and then still hides. While held, the last distance and relative
  speed are repeated.
  """
  SHOW_FRAMES = 20  # 0.4 s
  HIDE_FRAMES = 50  # 1.0 s

  def __init__(self):
    self.reset()

  def reset(self):
    self.visible = False
    self.on_frames = 0
    self.off_frames = 0
    self.distance = 0.0
    self.rel_speed = 0.0

  def update(self, lead_visible: bool, distance: float, rel_speed: float) -> tuple[bool, float, float]:
    if self.visible:
      self.off_frames = max(self.off_frames - 1, 0) if lead_visible else self.off_frames + 1
      if self.off_frames >= self.HIDE_FRAMES:
        self.visible = False
        self.on_frames = 0
    else:
      self.on_frames = self.on_frames + 1 if lead_visible else max(self.on_frames - 1, 0)
      if self.on_frames >= self.SHOW_FRAMES:
        self.visible = True
        self.off_frames = 0
    if self.visible and lead_visible:
      self.distance, self.rel_speed = distance, rel_speed

    if self.visible:
      return True, self.distance, self.rel_speed
    return False, distance, rel_speed


def adrv_0x1ea_values(enabled: bool) -> dict:
  # Stock sends 0x08 in byte 3 (openpilot 0x1c) and sets NEW_SIGNAL_1 while active.
  return {"SET_ME_1C": 0x08, "NEW_SIGNAL_1": int(enabled)}


def _with_checksum(address: int, dat: bytearray, bus: int) -> CanData:
  # After raw bit edits on a packed frame: recompute the CAN-FD CRC in bytes 0-1 (the counter in byte 2 is kept).
  crc = hkg_can_fd_checksum(address, None, dat)
  dat[0] = crc & 0xFF
  dat[1] = (crc >> 8) & 0xFF
  return CanData(address, bytes(dat), bus)


def create_stock_like_blindspot_messages(packer, CAN, front_corner_values,
                                         left_blindspot, right_blindspot, left_blinker, right_blinker,
                                         drive_gear) -> list[CanData]:
  front = {k: v for k, v in front_corner_values.items() if k not in ("CHECKSUM", "COUNTER")}
  left_state = 2 if left_blindspot and left_blinker else (1 if left_blindspot else 0)
  right_state = 2 if right_blindspot and right_blinker else (1 if right_blindspot else 0)

  # Stock keeps the BCW/BCA status fields constant while in drive. Only the side fields follow detection. Built
  # from zero (no copy of the last car-sent 0x1BA), so no stale warning, sound or brake field is repeated.
  rear = {
    "BCW_Sta": int(drive_gear),
    "BCW_IndSta": 1,
    "BCA_OnOffEquip2Sta": 2,
    "BCA_Sta": int(drive_gear),
    "BCW_LtIndSta": left_state,
    "BCW_RtIndSta": right_state,
    "OSMrrLamp_LtIndSta": left_state,
    "OSMrrLamp_RtIndSta": right_state,
    "FL_INDICATOR": 0,
    "FR_INDICATOR": 0,
  }
  # Same front-corner payload as create_blindspot_status_messages.
  if "NEW_SIGNAL_3" not in front:
    front["NEW_SIGNAL_3"] = 1

  address, dat, bus = packer.make_can_msg("BLINDSPOTS_REAR_CORNERS", CAN.ECAN, rear)
  dat = bytearray(dat)
  for bit in REAR_CORNERS_UNDEFINED_BITS:
    dat[bit // 8] |= 1 << (bit % 8)
  return [
    _with_checksum(address, dat, bus),
    packer.make_can_msg("BLINDSPOTS_FRONT_CORNER_1", CAN.ECAN, front),
  ]


# Round 3, adjacent cars toggle. Stock's ADAS ECU draws one car per next lane ahead and behind through ADRV_0x1ea.
# Under OP long the front radar only sees cars ahead (no rear distance on any panda bus), so cars ahead come from the
# radar, and a car we pass is carried back into the behind slot by dead reckoning while that side's blind-spot bit
# stays on. The bit never creates a car. Constants fitted on stock route d1 (wat-dev-notes progress.md, round 3
# gate 1): ahead precision 0.90/0.91 and recall 0.96 (left/right), behind precision 0.95.
ADJACENT_DT = 0.05  # updated with each 0x1EA frame (20 Hz)
ADJACENT_LANE_Y = (2.0, 3.8)  # |yRel| window of the next lane, m (yRel > 0 is left)
ADJACENT_MAX_RADAR_DISTANCE = 80.0
ADJACENT_MOVING_SPEED = 2.0  # |vEgo + vRel| above this, m/s; parked cars and poles are not drawn
# Stock's distance runs short of the radar's while a car is alongside: dRel minus this offset, clamped at 0.
ADJACENT_NEAR_OFFSET = ((4.0, 12.0), (1.75, 0.0))
ADJACENT_AHEAD_MAX = 79.5  # stock's largest ahead distance
ADJACENT_BEHIND_MAX = 19.4  # stock draws behind cars under 20.0 m (20.0 is its idle value)
ADJACENT_SHOW_FRAMES = 8  # 0.4 s
ADJACENT_HIDE_FRAMES = 20  # 1.0 s
# The radar keeps a car we pass at dRel 2-4 m while it is alongside, so the coast starts on distance, not on loss.
ADJACENT_COAST_START = 3.0  # m, converted distance
ADJACENT_COAST_CLOSING = -0.5  # vRel below this, m/s
ADJACENT_COAST_REAPPEAR = 6.0  # the coasting track seen ahead beyond this again ends the coast, m
ADJACENT_BIT_START_GRACE = 1.5  # stock's blind-spot bit came on 0.5-1 s after a car went behind, s
ADJACENT_BIT_OFF_GRACE = 1.0  # s
ADJACENT_COAST_LIMIT = 10.0  # s
ADJACENT_POINTS_STALE_FRAMES = 10  # radar points unchanged this long count as no points (0.5 s)

# ADRV_0x1ea slot layout (wat-dev-notes issue "ioniq6-cluster-bsm-replay", 0x1EA surround layout): Intel start bit
# and width of the distance (0.1 m), and the companion bytes stock sets with a car in the slot. NEW_SIGNAL_2/3
# overlap the left-ahead distance in the DBC, so the slots are written as raw bits after packing.
ADRV_0X1EA_ADDR = 0x1EA
ADRV_0X1EA_SLOTS = {
  "left_ahead": (46, 11, ((8, 0x1E), (9, 0x01))),
  "right_ahead": (75, 11, ((11, 0x1E), (12, 0x01))),
  "left_behind": (139, 11, ((19, 0x1E), (20, 0x01))),
  "right_behind": (163, 8, ((21, 0xE0), (22, 0x01), (23, 0x01))),
}


def adjacent_distance(d_rel: float) -> float:
  return max(d_rel - float(np.interp(d_rel, *ADJACENT_NEAR_OFFSET)), 0.0)


def select_adjacent_track(points, v_ego: float, side_sign: int, exclude_id=None, min_distance: float = 0.0):
  """Nearest radar track ahead in the next lane on one side (+1 left, -1 right) moving our way: (dRel, vRel, trackId).

  Oncoming traffic and stopped objects are skipped. min_distance is in stock's coordinate (adjacent_distance).
  """
  best = None
  for pt in points:
    if pt.trackId == exclude_id or not (ADJACENT_LANE_Y[0] < side_sign * pt.yRel < ADJACENT_LANE_Y[1]):
      continue
    if (0.0 < pt.dRel < ADJACENT_MAX_RADAR_DISTANCE and pt.vRel + v_ego > ADJACENT_MOVING_SPEED and
        adjacent_distance(pt.dRel) >= min_distance):
      if best is None or pt.dRel < best[0]:
        best = (pt.dRel, pt.vRel, pt.trackId)
  return best


class AdjacentLane:
  """One side: the car drawn ahead (debounced like LeadDisplayFilter) and a passed car coasting behind."""
  def __init__(self, side_sign: int):
    self.side_sign = side_sign
    self.reset()

  def reset(self):
    self.visible = False
    self.on_frames = 0
    self.off_frames = 0
    self.distance = 0.0
    self.rel_speed = 0.0
    self.coast = None  # position of the passed car, m (+ ahead, - behind)
    self.coast_id = None
    self.passed_id = None  # the last passed car's track while the radar still holds it alongside
    self.coast_rel_speed = 0.0
    self.coast_time = 0.0
    self.bit_seen = False
    self.bit_off_time = 0.0

  def _coast_step(self, points, blindspot: bool):
    self.coast += self.coast_rel_speed * ADJACENT_DT
    self.coast_time += ADJACENT_DT
    # The grace runs from the start of the pass: on d1, passed cars that drifted back slowly without the bit coming on
    # were mostly not drawn behind by stock.
    if blindspot:
      self.bit_seen = True
      self.bit_off_time = 0.0
    else:
      self.bit_off_time += ADJACENT_DT

    bit_grace = ADJACENT_BIT_OFF_GRACE if self.bit_seen else ADJACENT_BIT_START_GRACE
    reappeared = any(pt.trackId == self.coast_id and adjacent_distance(pt.dRel) > ADJACENT_COAST_REAPPEAR
                     for pt in points)
    if (reappeared or self.coast_time > ADJACENT_COAST_LIMIT or -self.coast > ADJACENT_BEHIND_MAX or
        (self.coast < 0.0 and self.bit_off_time > bit_grace)):
      self.passed_id = None if reappeared else self.coast_id
      self.coast = None
      self.coast_id = None

  def update(self, points, v_ego: float, blindspot: bool) -> tuple[float | None, float | None]:
    """Returns (ahead, behind) distances in stock's coordinate, None for an empty slot."""
    if self.coast is not None:
      self._coast_step(points, blindspot)
    if self.passed_id is not None and not any(pt.trackId == self.passed_id and
                                              adjacent_distance(pt.dRel) <= ADJACENT_COAST_REAPPEAR for pt in points):
      self.passed_id = None

    if self.coast is not None:
      # Any track alongside is taken to be the coasting car, also if the radar re-acquired it under a new id.
      track = select_adjacent_track(points, v_ego, self.side_sign, exclude_id=self.coast_id,
                                    min_distance=ADJACENT_COAST_START)
    else:
      track = select_adjacent_track(points, v_ego, self.side_sign)
    if track is not None:
      distance, rel_speed, track_id = adjacent_distance(track[0]), track[1], track[2]

    if self.visible:
      if track is None:
        self.off_frames += 1
        if self.off_frames >= ADJACENT_HIDE_FRAMES:
          self.visible = False
          self.on_frames = 0
      else:
        self.off_frames = max(self.off_frames - 1, 0)
        self.distance, self.rel_speed = distance, rel_speed
        # A car the radar still holds alongside after its coast ended stays drawn ahead (stock often drew it there)
        # and is passed again only while the blind-spot bit is on, so it can't reach the behind slot without the bit.
        if (self.coast is None and distance < ADJACENT_COAST_START and rel_speed < ADJACENT_COAST_CLOSING and
            (track_id != self.passed_id or blindspot)):
          # We are passing it: dead reckoning from here, and the ahead slot is free for the next car.
          self.coast, self.coast_id, self.coast_rel_speed = distance, track_id, rel_speed
          self.coast_time, self.bit_seen, self.bit_off_time = 0.0, blindspot, 0.0
          self.visible = False
          self.on_frames = 0
    else:
      self.on_frames = self.on_frames + 1 if track is not None else max(self.on_frames - 1, 0)
      if track is not None:
        self.distance, self.rel_speed = distance, rel_speed
      if self.on_frames >= ADJACENT_SHOW_FRAMES:
        self.visible = True
        self.off_frames = 0

    ahead = min(self.distance, ADJACENT_AHEAD_MAX) if self.visible else None
    behind = None
    if self.coast is not None:
      if self.coast >= 0.0:
        ahead = self.coast  # the passed car keeps the ahead slot until it is alongside
      else:
        behind = min(-self.coast, ADJACENT_BEHIND_MAX)
    return ahead, behind


class AdjacentCarTracker:
  """Both sides, updated once per 0x1EA frame. Cars are drawn only while active (engaged, in drive)."""
  def __init__(self):
    self.left = AdjacentLane(1)
    self.right = AdjacentLane(-1)

    self.last_points = None
    self.stale_frames = 0

  def reset(self):
    self.left.reset()
    self.right.reset()

  def update(self, active: bool, points, v_ego: float, left_blindspot: bool, right_blindspot: bool) -> dict:
    # card.py stores each new radar frame's point list; the same list coming back means no new radar frame. Counted
    # while inactive too, so a list that went stale before a re-engage is not taken as new.
    if points is self.last_points:
      self.stale_frames += 1
    else:
      self.last_points = points
      self.stale_frames = 0

    if not active:
      self.reset()
      return {}
    if points is None or self.stale_frames >= ADJACENT_POINTS_STALE_FRAMES:
      points = []

    left_ahead, left_behind = self.left.update(points, v_ego, left_blindspot)
    right_ahead, right_behind = self.right.update(points, v_ego, right_blindspot)
    cars = {"left_ahead": left_ahead, "right_ahead": right_ahead, "left_behind": left_behind,
            "right_behind": right_behind}
    return {slot: distance for slot, distance in cars.items() if distance is not None}


def patch_adrv_0x1ea(msg: CanData, active: bool, cars: dict) -> CanData:
  """Writes the active bit, stock's byte 3 and the car slots into a packed ADRV_0x1ea frame; recomputes the CRC.

  byte 3 and the active bit are the values adrv_0x1ea_values sends (active = engaged), so both switches agree.
  Empty slots are left at zero (stock sends zeros there too). Cars are written only while active.
  """
  address, dat, bus = msg
  dat = bytearray(dat)
  dat[3] = 0x08
  dat[4] = (dat[4] & 0xFE) | int(active)
  if active:
    raw = int.from_bytes(dat, "little")
    for slot, distance in cars.items():
      start, width, companions = ADRV_0X1EA_SLOTS[slot]
      value = min(int(round(distance * 10)), (1 << width) - 1)
      raw = (raw & ~(((1 << width) - 1) << start)) | (value << start)
      for byte, bits in companions:
        raw |= bits << (8 * byte)
    dat = bytearray(raw.to_bytes(len(dat), "little"))
  return _with_checksum(address, dat, bus)
