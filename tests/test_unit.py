"""Fast unit tests: pure helpers, config, and the worker-protocol client.

No LibreOffice needed. The `uno_worker` helpers are pure stdlib logic, but the
module imports UNO bridge modules at the top level, so tests stub `uno` /
`unohelper` / `com.sun.star.*` in sys.modules before importing it. Only the
pure helpers are exercised here; UNO-touching behavior stays in the live
integration tests.
"""

import os
import sys
import types

import pytest

from src.config import Config, ConfigException

pytestmark = pytest.mark.asyncio


def _load_worker_helpers():
    if "src.office.uno_worker" in sys.modules:
        return sys.modules["src.office.uno_worker"]
    sys.modules["uno"] = types.ModuleType("uno")
    helper = types.ModuleType("unohelper")
    helper.Base = type("Base", (), {})
    sys.modules["unohelper"] = helper
    for name in (
        "com",
        "com.sun",
        "com.sun.star",
        "com.sun.star.beans",
        "com.sun.star.util",
    ):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["com.sun.star.beans"].PropertyValue = object
    sys.modules["com.sun.star.util"].XModifyListener = type("XModifyListener", (), {})
    import src.office.uno_worker as worker

    return worker


def test_color_six_digit():
    w = _load_worker_helpers()
    assert w._color("#ff0000") == 0xFF0000
    assert w._color("00ff00") == 0x00FF00
    assert w._color(None) is None
    assert w._color(0x123456) == 0x123456


def test_color_three_digit_expands():
    w = _load_worker_helpers()
    assert w._color("#fff") == 0xFFFFFF
    assert w._color("#c41") == 0xCC4411


def test_color_rejects_garbage():
    w = _load_worker_helpers()
    for bad in ("red", "#12", "#12345", "#1234567", "0x12345g", "", "  "):
        with pytest.raises(ValueError):
            w._color(bad)
    for bad in (-1, 0x1000000):
        with pytest.raises(ValueError):
            w._color(bad)


def test_col_to_index():
    w = _load_worker_helpers()
    assert w._col_to_index("A") == 0
    assert w._col_to_index("Z") == 25
    assert w._col_to_index("AA") == 26
    assert w._col_to_index("AB") == 27
    assert w._col_to_index("XFD") == 16383
    assert w._col_to_index("a") == 0
    assert w._col_to_index("  c ") == 2


def test_col_to_index_rejects_garbage():
    w = _load_worker_helpers()
    for bad in ("", "A1", "1", "A:B:C", "A-", "A B", "XFE"):
        with pytest.raises(ValueError):
            w._col_to_index(bad)


def test_config_rejects_bad_timeout(monkeypatch):
    monkeypatch.setenv("LIBRE_MCP_STARTUP_TIMEOUT", "not-a-number")
    with pytest.raises(ConfigException):
        Config()


def test_profile_dir_defaults_outside_cwd(monkeypatch):
    monkeypatch.delenv("LIBRE_MCP_PROFILE_DIR", raising=False)
    default = Config().profile_dir
    assert default.endswith(os.path.join("libre-mcp", "profiles"))
    assert os.path.abspath(os.getcwd()) not in os.path.abspath(default)


async def test_worker_call_times_out(tmp_path):
    from src.office.worker import WorkerClient, WorkerError

    stub = tmp_path / "stub_worker.py"
    stub.write_text("import time\ntime.sleep(30)\n")
    client = WorkerClient(sys.executable, str(stub), url="dummy", op_timeout=0.3)
    try:
        with pytest.raises(WorkerError) as exc:
            await client.start()
        assert exc.value.fatal
        assert "timed out" in exc.value.message
    finally:
        await client.stop()
    assert not client.is_alive()
