import json

import pytest

from acacia.geometry import (
    Calibration,
    CalibrationError,
    CushionMask,
    Polygon,
    View,
    load_calibration,
    parse_polygons,
    polygon_area,
    save_calibration,
    validate_points,
)

SQUARE = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
VIEW = View(640, 360, 0, "/stream2")


def calibration(view=VIEW):
    return Calibration(polygons=[Polygon("a", "seat", True, SQUARE)], view=view)


def test_area():
    assert polygon_area(SQUARE) == pytest.approx(0.36)


@pytest.mark.parametrize("points, message", [
    ([[0.1, 0.1], [0.2, 0.2]], "3 to"),
    ([[0.1, 0.1], [0.2, 0.2], [1.2, 0.5]], "inside the frame"),
    ([[0.1, 0.1], [0.2, 0.2], [-0.1, 0.5]], "inside the frame"),
    ([[0.1, 0.1], [0.2, 0.2], [float("nan"), 0.5]], "inside the frame"),
    ([[0.1, 0.1], [0.1, 0.1], [0.5, 0.5]], "duplicate"),
    ([[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]], "degenerate"),
    ([[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.5]], "cross"),
    ("nonsense", "3 to"),
    ([["a", 1], [0, 0], [1, 1]], "\\[x, y\\]"),
    ([[True, 0.1], [0.5, 0.2], [0.3, 0.9]], "\\[x, y\\]"),
])
def test_invalid_geometry_is_rejected(points, message):
    with pytest.raises(CalibrationError, match=message):
        validate_points(points)


def test_concave_polygon_is_valid():
    assert validate_points([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.5, 0.4], [0.1, 0.9]])


def test_status_requires_enabled_polygons():
    empty = Calibration(polygons=[Polygon("a", "seat", False, SQUARE)], view=VIEW)
    assert empty.status(VIEW)[0] == "missing"


def test_status_valid_when_view_matches():
    assert calibration().status(VIEW)[0] == "valid"


def test_size_only_change_keeps_calibration_valid():
    assert calibration().status(View(1280, 720, 0, "/stream2"))[0] == "valid"


@pytest.mark.parametrize("live", [
    View(640, 360, 90, "/stream2"),   # rotation
    View(640, 480, 0, "/stream2"),    # crop or aspect
    View(640, 360, 0, "/stream1"),    # different stream
])
def test_changed_view_invalidates(live):
    assert calibration().status(live)[0] == "invalid"


def test_manual_invalidation_wins_until_redrawn():
    cal = calibration()
    cal.invalidated = True
    cal.invalid_reason = "camera moved"
    assert cal.status(VIEW) == ("invalid", "camera moved")


def test_status_unverified_without_live_video():
    assert calibration().status(None)[0] == "unverified"


def test_round_trip_and_revision(tmp_path):
    path = tmp_path / "calibration.json"
    cal = calibration()
    save_calibration(path, cal)
    save_calibration(path, cal)
    loaded = load_calibration(path)
    assert loaded.revision == 2
    assert loaded.view == VIEW
    assert loaded.polygons[0].points == SQUARE
    assert loaded.status(VIEW)[0] == "valid"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["version"] == 1


def test_saved_timestamp_has_a_timezone_offset(tmp_path):
    cal = calibration()
    save_calibration(tmp_path / "c.json", cal)
    assert cal.saved_at[-6] in "+-"


def test_parse_rejects_too_many():
    with pytest.raises(CalibrationError):
        parse_polygons([{"points": SQUARE}] * 33)


def test_mask_measurements_scale_with_resolution():
    polygons = [Polygon("a", "seat", True, SQUARE)]
    small, large = CushionMask(polygons, 320, 180), CushionMask(polygons, 1920, 1080)
    assert large.width <= 640
    for mask in (small, large):
        assert mask.contains(0.5, 0.5)
        assert not mask.contains(0.1, 0.1)
        assert mask.fraction(0.3, 0.3, 0.7, 0.7) == pytest.approx(1.0)
        assert mask.fraction(0.0, 0.0, 0.1, 0.1) == 0.0
        assert mask.fraction(0.0, 0.3, 0.4, 0.7) == pytest.approx(0.5, abs=0.03)


def test_mask_covered_fraction():
    mask = CushionMask([Polygon("a", "seat", True, SQUARE)], 640, 360)
    assert mask.covered_fraction([]) == 0.0
    assert mask.covered_fraction([(0.0, 0.0, 1.0, 1.0)]) == pytest.approx(1.0)
    assert mask.covered_fraction([(0.2, 0.2, 0.5, 0.8)]) == pytest.approx(0.5, abs=0.03)
