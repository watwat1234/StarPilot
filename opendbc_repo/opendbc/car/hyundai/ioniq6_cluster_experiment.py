"""Temporary Ioniq 6 cluster HUD experiment (branch experiment/ioniq6-cluster-hud, never merged).

Under openpilot longitudinal the ADAS ECU is off, so openpilot sends the messages the cluster reads to draw the
lead car, the adjacent-lane cars and the blind-spot icon. Stock sends status values in them that openpilot does
not. These helpers return the stock values, behind temporary toggles, to find which ones the cluster needs.
"""
import numpy as np

from opendbc.car import CanData
from opendbc.car.hyundai.hyundaicanfd import hkg_can_fd_checksum

# Stock SCC_CONTROL with cruise on (route 000000d1--48888e416c). The DBC range of DISTANCE_SETTING ([0|3]) is stale.
STOCK_DISTANCE_SETTING = 6
STOCK_LEAD_OBJ_STATUS = 6  # 5, 6 or 7 at speed depending on lead distance; 6 is the most common
STOCK_HEADWAY_S = 1.0  # NEW_SIGNAL_15 = vEgo * 1.0 s
NO_LEAD_DISTANCE = 204.6
NO_LEAD_REL_SPEED = 34.6

# Stock BLINDSPOTS_REAR_CORNERS sets two bits that are not DBC signals.
REAR_CORNERS_UNDEFINED_BITS = (25, 65)


def scc_status_values(enabled: bool, lead_visible: bool, v_ego: float) -> dict:
  values = {
    "DISTANCE_SETTING": STOCK_DISTANCE_SETTING if enabled else 0,
    "NEW_SIGNAL_15": float(np.clip(v_ego * STOCK_HEADWAY_S, 0.0, NO_LEAD_DISTANCE)),
  }
  # Same lead rule as create_ccnc_acc_control: a lead only counts while engaged.
  if enabled and lead_visible:
    values["OBJ_STATUS"] = STOCK_LEAD_OBJ_STATUS
    values["NEW_SIGNAL_3"] = 2
  else:
    values["OBJ_STATUS"] = 0
    values["NEW_SIGNAL_3"] = 0
    values["ACC_ObjDist"] = NO_LEAD_DISTANCE
    values["ACC_ObjRelSpd"] = NO_LEAD_REL_SPEED
    values["ObjValid"] = 1
  return values


def adrv_0x1ea_values(enabled: bool) -> dict:
  # Stock sends 0x08 in byte 3 (openpilot 0x1c) and sets NEW_SIGNAL_1 while active.
  return {"SET_ME_1C": 0x08, "NEW_SIGNAL_1": int(enabled)}


def create_stock_like_blindspot_messages(packer, CAN, rear_values, front_corner_values,
                                         left_blindspot, right_blindspot, left_blinker, right_blinker,
                                         drive_gear) -> list[CanData]:
  rear = {k: v for k, v in rear_values.items() if k not in ("CHECKSUM", "COUNTER")}
  front = {k: v for k, v in front_corner_values.items() if k not in ("CHECKSUM", "COUNTER")}
  left_state = 2 if left_blindspot and left_blinker else (1 if left_blindspot else 0)
  right_state = 2 if right_blindspot and right_blinker else (1 if right_blindspot else 0)

  # Stock keeps the BCW/BCA status fields constant while in drive. Only the side fields follow detection.
  rear.update({
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
  })
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
