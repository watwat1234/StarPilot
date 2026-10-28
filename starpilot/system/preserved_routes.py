"""Whole-route preserve, set from Galaxy and honored by the deleter.

Stock bookmarks flag one segment with user.preserve, which the deleter keeps with the 2 segments before it
(newest 5 only). A Galaxy preserve flags every segment of the route with its own attribute instead, so the
deleter keeps the whole route and bookmarks don't count against it.
Must not import the deleter: the Galaxy tests stub that module.
"""
import os

from openpilot.common.swaglog import cloudlog

ROUTE_PRESERVE_ATTR_NAME = "user.sp_preserve_route"
ROUTE_PRESERVE_ATTR_VALUE = b"1"
# Galaxy warns when preserved routes take this share of storage or more.
PRESERVED_WARN_PERCENT = 40


def _route_name(segment_dir: str) -> str | None:
  route, _, seg = segment_dir.rpartition("--")
  return route if route and seg.isdigit() else None


def _route_segment_dirs(footage_path: str) -> list[tuple[str, str]]:
  try:
    names = os.listdir(footage_path)
  except OSError:
    return []
  return [(name, route) for name in names if (route := _route_name(name)) is not None]


def is_route_preserved_dir(path: str) -> bool:
  # Read directly: xattr_cache never sees a flag set after its first read of the path.
  try:
    return os.getxattr(path, ROUTE_PRESERVE_ATTR_NAME) == ROUTE_PRESERVE_ATTR_VALUE
  except OSError:
    return False


def _preserved_routes(root: str, dirs: list[tuple[str, str]]) -> set[str]:
  # A route counts as preserved if any of its segments carries the flag.
  routes = set()
  for name, route in dirs:
    if route not in routes and is_route_preserved_dir(os.path.join(root, name)):
      routes.add(route)
  return routes


def preserved_route_names(footage_path: str) -> set[str]:
  return _preserved_routes(footage_path, _route_segment_dirs(footage_path))


def get_preserved_route_segments(root: str, dirs: list[str]) -> set[str]:
  """Deleter hook: the dirs that belong to a preserved route. Never raises; on error, stock behavior."""
  try:
    segment_dirs = [(d, route) for d in dirs if (route := _route_name(d)) is not None]
    routes = _preserved_routes(root, segment_dirs)
    return {d for d, route in segment_dirs if route in routes}
  except Exception:
    cloudlog.exception("preserved_routes: failed to read route preserve flags")
    return set()


def set_route_preserved(footage_path: str, route_name: str, preserved: bool) -> int:
  """Set or clear the flag on every segment of the route. Returns the number of segments found."""
  segments = [name for name, route in _route_segment_dirs(footage_path) if route == route_name]
  for name in segments:
    path = os.path.join(footage_path, name)
    if preserved:
      os.setxattr(path, ROUTE_PRESERVE_ATTR_NAME, ROUTE_PRESERVE_ATTR_VALUE)
    elif is_route_preserved_dir(path):
      os.removexattr(path, ROUTE_PRESERVE_ATTR_NAME)
  return len(segments)


def preserved_routes_bytes(footage_path: str, routes: set[str]) -> int:
  """Size of the files in the segment dirs of the given routes."""
  total = 0
  for name, route in _route_segment_dirs(footage_path):
    if route not in routes:
      continue
    try:
      with os.scandir(os.path.join(footage_path, name)) as entries:
        total += sum(entry.stat(follow_symlinks=False).st_size for entry in entries if entry.is_file(follow_symlinks=False))
    except OSError:
      continue
  return total
