"""The Intel wheel check accepts only wheels a standard Intel Python can install."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_intel_wheels", Path(__file__).resolve().parents[1] / "tools" / "check_intel_wheels.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_intel_and_universal_wheels_fit():
    assert checker.wheel_fits("numpy-2.5.4-cp313-cp313-macosx_10_13_x86_64.whl", "cp313")
    assert checker.wheel_fits("websockets-17.4-cp313-cp313-macosx_10_13_universal2.whl", "cp313")
    assert checker.wheel_fits("psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl", "cp313")
    assert checker.wheel_fits("anyio-4.16.0-py3-none-any.whl", "cp313")


def test_wheels_a_standard_intel_python_cannot_install_are_refused():
    assert not checker.wheel_fits("psutil-7.2.2-cp313-cp313t-macosx_10_13_x86_64.whl", "cp313")
    assert not checker.wheel_fits("numpy-2.5.4-cp313-cp313-macosx_11_0_arm64.whl", "cp313")
    assert not checker.wheel_fits("numpy-2.5.4-cp312-cp312-macosx_10_13_x86_64.whl", "cp313")
    assert not checker.wheel_fits("numpy-2.5.4-cp313-cp313-manylinux_2_28_x86_64.whl", "cp313")
