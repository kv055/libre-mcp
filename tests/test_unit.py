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


# -- stale lock files ------------------------------------------------------


def _instance(profile_dir, pid, docs=()):
    """A profile as a server instance leaves it, with `docs` recorded as
    opened and each one locked by that instance."""
    from src.office.soffice import OPENED_FILE, lock_file_for

    profile = profile_dir / f"instance-libre_mcp_{pid}_0123456789ab"
    profile.mkdir()
    (profile / OPENED_FILE).write_text("".join(f"{d}\n" for d in docs))
    for d in docs:
        d.write_text("doc")
        with open(lock_file_for(str(d)), "w") as f:
            f.write(f",me,host,10.11.2026 16:00,file://{profile};")
    return profile


def _dead_pid():
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_release_locks_only_removes_own(tmp_path):
    from src.office.soffice import lock_file_for, release_locks

    docs = tmp_path / "docs"
    docs.mkdir()
    own, foreign = docs / "own.ods", docs / "foreign.ods"
    profile = _instance(tmp_path, 1, [own, foreign])
    # someone else's LibreOffice holds `foreign` now
    with open(lock_file_for(str(foreign)), "w") as f:
        f.write(",me,host,10.11.2026 16:00,file:///home/me/.config/libreoffice/4;")
    # locked by this instance, but never recorded as opened
    stray = docs / "stray.odt"
    with open(lock_file_for(str(stray)), "w") as f:
        f.write(f",me,host,10.11.2026 16:00,file://{profile};")

    removed = release_locks(str(profile))

    assert removed == [lock_file_for(str(own))]
    assert not os.path.exists(lock_file_for(str(own)))
    assert os.path.exists(lock_file_for(str(foreign)))
    assert os.path.exists(lock_file_for(str(stray)))


def test_release_locks_without_record_is_noop(tmp_path):
    from src.office.soffice import release_locks

    assert release_locks(str(tmp_path / "instance-libre_mcp_1_0123456789ab")) == []


def test_reap_releases_dead_instances_only(tmp_path):
    from src.office.soffice import lock_file_for, reap_stale_profiles

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    dead_doc, live_doc = tmp_path / "dead.ods", tmp_path / "live.ods"
    dead = _instance(profiles, _dead_pid(), [dead_doc])
    live = _instance(profiles, os.getpid(), [live_doc])
    unrelated = profiles / "something-else"
    unrelated.mkdir()

    reap_stale_profiles(str(profiles))

    assert not dead.exists()
    assert not os.path.exists(lock_file_for(str(dead_doc)))
    assert live.exists()
    assert os.path.exists(lock_file_for(str(live_doc)))
    assert unrelated.exists()


def test_reap_spares_orphaned_soffice(tmp_path):
    import subprocess

    from src.office.soffice import lock_file_for, reap_stale_profiles

    doc = tmp_path / "open.ods"
    profile = _instance(tmp_path, _dead_pid(), [doc])
    # the server died, but a process still runs with this profile
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", str(profile)]
    )
    try:
        reap_stale_profiles(str(tmp_path))
    finally:
        orphan.kill()
        orphan.wait()

    assert profile.exists()
    assert os.path.exists(lock_file_for(str(doc)))


def test_reap_keeps_profile_when_asked(tmp_path):
    from src.office.soffice import lock_file_for, reap_stale_profiles

    doc = tmp_path / "kept.ods"
    profile = _instance(tmp_path, _dead_pid(), [doc])

    reap_stale_profiles(str(tmp_path), keep_profile=True)

    assert profile.exists()
    assert not os.path.exists(lock_file_for(str(doc)))


async def test_stop_releases_locks_of_recorded_documents(tmp_path):
    from src.office.soffice import SofficeProcess, lock_file_for

    soffice = SofficeProcess("soffice", "libre_mcp_1_0123456789ab", str(tmp_path))
    os.makedirs(soffice.profile)
    doc = tmp_path / "report.odt"
    doc.write_text("doc")
    soffice.record_opened(str(doc))
    with open(lock_file_for(str(doc)), "w") as f:
        f.write(f",me,host,10.11.2026 16:00,file://{soffice.profile};")

    await soffice.stop()

    assert not os.path.exists(lock_file_for(str(doc)))
    assert not os.path.exists(soffice.profile)
    assert doc.exists()


async def test_session_records_opened_documents(monkeypatch):
    session, _ = _stub_session(
        monkeypatch, [{"doc_id": "doc-1", "kind": "calc", "path": "/d/a.ods"}]
    )
    opened = []
    session._soffice = types.SimpleNamespace(record_opened=opened.append)

    await session.call("open_document", path="/d/a.ods")

    assert opened == ["/d/a.ods"]
