from openpilot.common.params import Params


GPS_LOCATION_SERVICES = ("gpsLocationExternal", "gpsLocation")


def get_ublox_location_service(params: Params) -> str:
  # a car GPS feed owns gpsLocationExternal; ubloxd moves to gpsLocation so the topic keeps one publisher
  return "gpsLocation" if params.get_bool("CarGpsAvailable") else "gpsLocationExternal"


def get_gps_location_service(params: Params) -> str:
  if params.get_bool("UbloxAvailable") or params.get_bool("CarGpsAvailable"):
    return "gpsLocationExternal"
  else:
    return "gpsLocation"
