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
from src.office.worker import WorkerError

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
    sys.modules["com.sun.star.util"].CloseVetoException = type(
        "CloseVetoException", (Exception,), {}
    )
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


class _FakeWindow:
    def __init__(self, failures=0):
        self.failures = failures
        self.set_visible_calls = 0
        self.to_front_calls = 0

    def setVisible(self, visible):
        assert visible
        self.set_visible_calls += 1
        if self.set_visible_calls <= self.failures:
            raise RuntimeError("window not realizable yet")

    def toFront(self):
        self.to_front_calls += 1


class _FakeDoc:
    def __init__(self, window):
        self._window = window

    def getCurrentController(self):
        return self

    def getFrame(self):
        return self

    def getContainerWindow(self):
        return self._window


def test_show_maps_window():
    w = _load_worker_helpers()
    worker = w.Worker("dummy-url")
    window = _FakeWindow()
    worker._show(_FakeDoc(window))
    assert window.set_visible_calls == 1


def test_show_never_calls_to_front():
    # toFront() on a Wayland container window disposes the URP bridge,
    # failing every later op on the connection — and the compositor owns
    # focus anyway. setVisible alone must map the window.
    w = _load_worker_helpers()
    worker = w.Worker("dummy-url")
    window = _FakeWindow()
    worker._show(_FakeDoc(window))
    assert window.to_front_calls == 0


def test_show_retries_transient_failure():
    w = _load_worker_helpers()
    worker = w.Worker("dummy-url")
    window = _FakeWindow(failures=2)
    worker._show(_FakeDoc(window))
    assert window.set_visible_calls == 3
    assert window.to_front_calls == 0


def test_show_never_raises():
    w = _load_worker_helpers()
    worker = w.Worker("dummy-url")
    window = _FakeWindow(failures=999)
    worker._show(_FakeDoc(window))  # must not raise
    assert window.set_visible_calls == w._SHOW_ATTEMPTS


class _FakeWorkerClient:
    def __init__(self, behavior):
        self._behavior = behavior
        self.calls = []

    async def call(self, op, **args):
        self.calls.append((op, args))
        action = self._behavior[len(self.calls) - 1]
        if isinstance(action, Exception):
            raise action
        return action

    async def stop(self):
        pass


def _stub_session(monkeypatch, first, second=None):
    from src.office.session import OfficeSession

    session = OfficeSession(Config())
    events = []

    async def fake_ensure():
        events.append("ensure")
        if session._worker is None or session._soffice is None:
            await fake_teardown()
            await fake_start()

    async def fake_teardown():
        events.append("teardown")
        session._worker = None
        session._soffice = None

    async def fake_start():
        events.append("start")
        session._worker = _FakeWorkerClient(second or [{"pong": True}])
        session._soffice = object()

    monkeypatch.setattr(session, "_ensure_started", fake_ensure)
    monkeypatch.setattr(session, "_teardown_locked", fake_teardown)
    monkeypatch.setattr(session, "_start_locked", fake_start)
    session._worker = _FakeWorkerClient(first)
    session._soffice = object()
    return session, events


async def test_fatal_doc_op_keeps_healthy_session(monkeypatch):
    # Only the document is gone (e.g. closed in the live window): the op
    # raises, but the live bridge — and the session — stay up.
    session, events = _stub_session(
        monkeypatch,
        [
            WorkerError("DisposedException", "doc gone", fatal=True),
            {"pong": True, "components": 2},
        ],
    )
    with pytest.raises(WorkerError):
        await session.call("get_text", doc_id="doc-1")
    assert events == ["ensure"]
    assert session._worker is not None


async def test_fatal_doc_op_tears_down_dead_bridge(monkeypatch):
    # Bridge and document both gone: the failing doc op raises (its handle
    # is gone), but the teardown lets the NEXT call rebuild instead of
    # reusing the dead worker.
    session, events = _stub_session(
        monkeypatch,
        [
            WorkerError("DisposedException", "bridge gone", fatal=True),
            WorkerError("DisposedException", "bridge gone", fatal=True),
        ],
    )
    with pytest.raises(WorkerError):
        await session.call("get_text", doc_id="doc-1")
    assert events == ["ensure", "teardown"]
    assert session._worker is None
    out = await session.call("ping")
    assert out == {"pong": True}
    assert events == ["ensure", "teardown", "ensure", "teardown", "start"]


def test_ping_reports_desktop_components():
    w = _load_worker_helpers()
    worker = w.Worker("dummy-url")

    class _Enum:
        def __init__(self):
            self.left = 3

        def hasMoreElements(self):
            return self.left > 0

        def nextElement(self):
            self.left -= 1
            return object()

    class _Components:
        def createEnumeration(self):
            return _Enum()

    class _Desktop:
        def getComponents(self):
            return _Components()

    worker.desktop = _Desktop()
    assert worker.op_ping({}) == {"pong": True, "components": 3}


class _VetoDoc:
    def __init__(self, CloseVeto):
        self._CloseVeto = CloseVeto
        self.disposed = False

    def isModified(self):
        return False

    def close(self, _save):
        raise self._CloseVeto("Controller disagree")

    def dispose(self):
        self.disposed = True


def test_close_force_disposes_on_veto():
    import sys

    w = _load_worker_helpers()
    CloseVeto = sys.modules["com.sun.star.util"].CloseVetoException
    worker = w.Worker("dummy-url")
    doc = _VetoDoc(CloseVeto)
    worker.docs["doc-1"] = doc
    assert worker.op_close_document({"doc_id": "doc-1", "force": True}) == {
        "closed": "doc-1"
    }
    assert doc.disposed
    assert "doc-1" not in worker.docs


def test_close_veto_without_force_raises_and_stays_tracked():
    import sys

    w = _load_worker_helpers()
    CloseVeto = sys.modules["com.sun.star.util"].CloseVetoException
    worker = w.Worker("dummy-url")
    doc = _VetoDoc(CloseVeto)
    worker.docs["doc-1"] = doc
    with pytest.raises(CloseVeto):
        worker.op_close_document({"doc_id": "doc-1"})
    assert not doc.disposed
    assert worker.docs["doc-1"] is doc


async def test_fatal_docless_op_restarts_and_retries(monkeypatch):
    session, events = _stub_session(
        monkeypatch,
        [WorkerError("DisposedException", "bridge gone", fatal=True)],
        [{"doc_id": "doc-9"}],
    )
    out = await session.call("create_document", kind="writer")
    assert out == {"doc_id": "doc-9"}
    assert events == ["ensure", "teardown", "start"]


async def test_nonfatal_error_raises_without_teardown(monkeypatch):
    session, events = _stub_session(
        monkeypatch, [WorkerError("unknown_op", "nope", fatal=False)]
    )
    with pytest.raises(WorkerError):
        await session.call("get_text", doc_id="doc-1")
    assert events == ["ensure"]
    assert session._worker is not None
