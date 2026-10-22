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

# Stock BLINDSPOTS_REAR_CORNERS sets two bits that are not DBC signals.
REAR_CORNERS_UNDEFINED_BITS = (25, 65)


def scc_status_values(enabled: bool, lead_visible: bool, v_ego: float, gas_override: bool = False,
                      distance_bars: int | None = None, t_follow: float | None = None) -> dict:
  # distance_bars and t_follow (round 2, gap toggle) replace stock's fixed 6 and 1.0 s with openpilot's personality
  # bars (1-3) and follow time. t_follow is 0 while openpilot is disengaged, so 1.0 s is kept then.
  if enabled:
    headway = t_follow if t_follow else STOCK_HEADWAY_S
    values = {
      "DISTANCE_SETTING": STOCK_DISTANCE_SETTING if distance_bars is None else int(distance_bars),
      "NEW_SIGNAL_15": float(np.clip(v_ego * headway, 0.0, NO_LEAD_DISTANCE)),
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


class LeadDisplayFilter:
  """Debounces the lead shown on the cluster (round 2, lead filter toggle), updated once per SCC_CONTROL frame (50 Hz).

  openpilot's lead often flips on and off for under 1 s when it is weak and far away; stock's lead runs last ~10 s.
  The lead is shown after SHOW_FRAMES of continuous lead and hidden after HIDE_FRAMES without one. While held, the
  last distance and relative speed are repeated.
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
    if lead_visible:
      self.on_frames += 1
      self.off_frames = 0
      if self.visible or self.on_frames >= self.SHOW_FRAMES:
        self.visible = True
        self.distance, self.rel_speed = distance, rel_speed
    else:
      self.on_frames = 0
      if self.visible:
        self.off_frames += 1
        if self.off_frames >= self.HIDE_FRAMES:
          self.visible = False

    if self.visible:
      return True, self.distance, self.rel_speed
    return False, distance, rel_speed


def adrv_0x1ea_values(enabled: bool) -> dict:
  # Stock sends 0x08 in byte 3 (openpilot 0x1c) and sets NEW_SIGNAL_1 while active.
  return {"SET_ME_1C": 0x08, "NEW_SIGNAL_1": int(enabled)}


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
  crc = hkg_can_fd_checksum(address, None, dat)
  dat[0] = crc & 0xFF
  dat[1] = (crc >> 8) & 0xFF

  return [
    CanData(address, bytes(dat), bus),
    packer.make_can_msg("BLINDSPOTS_FRONT_CORNER_1", CAN.ECAN, front),
  ]
