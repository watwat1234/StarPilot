import os

import openpilot.system.loggerd.deleter as deleter
from openpilot.starpilot.system import preserved_routes

PRESERVED = "0000006a--9f0a7bdf9c"
OTHER = "0000006b--9f0a7bdf9d"


def _make_segments(root, route, nums):
  paths = []
  for num in nums:
    path = root / f"{route}--{num}"
    path.mkdir(parents=True)
    paths.append(path)
  return paths


def _has_flag(path):
  return preserved_routes.is_route_preserved_dir(str(path))


def test_set_route_preserved_flags_and_clears_every_segment(tmp_path):
  segments = _make_segments(tmp_path, PRESERVED, (3, 4, 5))
  other = _make_segments(tmp_path, OTHER, (0,))

  assert preserved_routes.set_route_preserved(str(tmp_path), PRESERVED, True) == 3
  assert all(_has_flag(path) for path in segments)
  assert not _has_flag(other[0])
  assert preserved_routes.preserved_route_names(str(tmp_path)) == {PRESERVED}

  assert preserved_routes.set_route_preserved(str(tmp_path), PRESERVED, False) == 3
  assert not any(_has_flag(path) for path in segments)
  # Clearing a route that isn't flagged is fine.
  assert preserved_routes.set_route_preserved(str(tmp_path), PRESERVED, False) == 3
  assert preserved_routes.set_route_preserved(str(tmp_path), "00000099--9f0a7bdf9c", True) == 0
  assert preserved_routes.set_route_preserved(str(tmp_path / "missing"), PRESERVED, True) == 0


def test_set_route_preserved_skips_a_segment_deleted_meanwhile(monkeypatch, tmp_path):
  segments = _make_segments(tmp_path, PRESERVED, (0, 1, 2))
  real_setxattr = os.setxattr

  def setxattr(path, name, value):
    if str(path).endswith("--0"):
      raise FileNotFoundError(path)
    real_setxattr(path, name, value)

  monkeypatch.setattr(preserved_routes.os, "setxattr", setxattr)

  assert preserved_routes.set_route_preserved(str(tmp_path), PRESERVED, True) == 3
  assert [_has_flag(path) for path in segments] == [False, True, True]


def test_route_segments_protected_when_any_segment_is_flagged(tmp_path):
  _make_segments(tmp_path, PRESERVED, (0, 1, 2))
  _make_segments(tmp_path, OTHER, (0, 1))
  os.setxattr(tmp_path / f"{PRESERVED}--2", preserved_routes.ROUTE_PRESERVE_ATTR_NAME, preserved_routes.ROUTE_PRESERVE_ATTR_VALUE)
  dirs = sorted(os.listdir(tmp_path)) + ["boot", "crash"]

  assert preserved_routes.get_preserved_route_segments(str(tmp_path), dirs) == {f"{PRESERVED}--{n}" for n in (0, 1, 2)}


def test_bookmark_flag_alone_does_not_preserve_the_route(tmp_path):
  _make_segments(tmp_path, OTHER, (0, 1, 2))
  os.setxattr(tmp_path / f"{OTHER}--1", deleter.PRESERVE_ATTR_NAME, deleter.PRESERVE_ATTR_VALUE)

  assert preserved_routes.get_preserved_route_segments(str(tmp_path), sorted(os.listdir(tmp_path))) == set()
  assert preserved_routes.preserved_route_names(str(tmp_path)) == set()


def test_hook_never_raises(monkeypatch, tmp_path):
  _make_segments(tmp_path, PRESERVED, (0,))
  logged = []

  def broken(path):
    raise RuntimeError("boom")

  monkeypatch.setattr(preserved_routes, "is_route_preserved_dir", broken)
  monkeypatch.setattr(preserved_routes.cloudlog, "exception", lambda *args, **kwargs: logged.append(args))

  assert preserved_routes.get_preserved_route_segments(str(tmp_path), [f"{PRESERVED}--0"]) == set()
  assert logged


def test_preserved_routes_bytes_counts_only_given_routes(tmp_path):
  preserved = _make_segments(tmp_path, PRESERVED, (0, 1))
  other = _make_segments(tmp_path, OTHER, (0,))
  (preserved[0] / "rlog.zst").write_bytes(b"x" * 100)
  (preserved[1] / "fcamera.hevc").write_bytes(b"x" * 50)
  (other[0] / "rlog.zst").write_bytes(b"x" * 1000)

  assert preserved_routes.preserved_routes_bytes(str(tmp_path), {PRESERVED}) == 150
  assert preserved_routes.preserved_routes_bytes(str(tmp_path), set()) == 0


class _StopAfter:
  """Stands in for the deleter's exit event: stops after n loop iterations."""
  def __init__(self, n):
    self.n = n

  def is_set(self):
    return self.n <= 0

  def wait(self, timeout):
    self.n -= 1


def test_deleter_deletes_preserved_route_last(monkeypatch, tmp_path):
  preserved = _make_segments(tmp_path, PRESERVED, (0, 1))  # older
  other = _make_segments(tmp_path, OTHER, (0, 1))
  preserved_routes.set_route_preserved(str(tmp_path), PRESERVED, True)
  order = [path.name for path in preserved + other]
  monkeypatch.setattr(deleter.Paths, "log_root", staticmethod(lambda: str(tmp_path)))
  monkeypatch.setattr(deleter, "listdir_by_creation", lambda root: [d for d in order if os.path.isdir(os.path.join(root, d))])
  monkeypatch.setattr(deleter, "get_available_bytes", lambda default=None: 0)
  monkeypatch.setattr(deleter, "get_available_percent", lambda default=None: 0)

  deleter.deleter_thread(_StopAfter(2))
  assert all(path.is_dir() for path in preserved)
  assert not any(path.exists() for path in other)

  # Only preserved footage left: still deleted, oldest first, so recording never stalls.
  deleter.deleter_thread(_StopAfter(1))
  assert not preserved[0].exists()
  assert preserved[1].is_dir()
