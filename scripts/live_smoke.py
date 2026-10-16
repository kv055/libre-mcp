"""Manual live-launch smoke test (needs a display; NOT part of `make check`).

Spawns an OfficeSession the way an MCP host would — with display variables
stripped — and asserts the server reconstructs the desktop session itself:

MODE=LIVE (default): assert soffice gets a Wayland display and no --headless
    flag, round-trip text through a scratch doc, idle 60s, then prove the
    bridge survived (post-idle get_text + close).
MODE=FULLENV: same, but keep the ambient display env (control run).
MODE=FALLBACK: point XDG_RUNTIME_DIR at an empty dir and assert the server
    degrades to headless instead of failing.

Takes ~75s (mostly the idle wait). A visible LibreOffice window should appear
during LIVE/FULLENV runs.

Usage:  MODE=LIVE uv run python scripts/live_smoke.py
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODE = os.getenv("MODE", "LIVE")
if MODE != "FULLENV":
    for var in (
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "HYPRLAND_CMD",
        "UWSM_WAIT_VARNAMES",
        "UWSM_FINALIZE_VARNAMES",
    ):
        os.environ.pop(var, None)
if MODE == "FALLBACK":
    empty = os.path.join(tempfile.gettempdir(), "libre-smoke-empty-runtime")
    os.makedirs(empty, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = empty

from src.config import Config  # noqa: E402
from src.office.session import OfficeSession  # noqa: E402

READY_FILE = os.path.join(tempfile.gettempdir(), "libre-smoke-ready")


def proc_text(pid, name):
    with open(f"/proc/{pid}/{name}", "rb") as fh:
        return fh.read().decode("utf-8", "replace")


async def main():
    config = Config()
    # Exercise live mode (and its headless fallback) regardless of the
    # server's configured default.
    config.live = True
    config.profile_dir = os.getenv(
        "VERIFY_PROFILE_DIR",
        os.path.join(tempfile.gettempdir(), "libre-smoke-profiles"),
    )
    config.keep_profile = True
    print("live =", config.live, "mode =", MODE, flush=True)
    session = OfficeSession(config)
    try:
        await session.call("ping")
        pid = session._soffice._proc.pid
        env = proc_text(pid, "environ").split("\0")
        cmd = proc_text(pid, "cmdline").replace("\0", " ")
        wayland = [e for e in env if e.startswith("WAYLAND_DISPLAY=")]
        headless = "--headless" in cmd
        print("soffice pid:", pid, flush=True)
        print("WAYLAND_DISPLAY:", wayland or "MISSING", flush=True)
        print("headless flag:", headless, flush=True)
        if MODE in ("LIVE", "FULLENV"):
            assert wayland, "live soffice has no Wayland display!"
            assert not headless, "live soffice got --headless!"
            doc = await session.call("create_document", kind="writer")
            print("created", doc["doc_id"], flush=True)
            with open(READY_FILE, "w") as fh:
                fh.write(doc["doc_id"])
            await session.call("insert_text", doc_id=doc["doc_id"], text="hello live")
            out = await session.call("get_text", doc_id=doc["doc_id"])
            print("roundtrip:", "hello live" in out["text"], flush=True)
            await asyncio.sleep(60)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    alive = "soffice" in fh.read().decode("utf-8", "replace")
            except FileNotFoundError:
                alive = False
            print("soffice same pid alive:", alive, flush=True)
            out2 = await session.call("get_text", doc_id=doc["doc_id"])
            print(
                "post-idle get_text:",
                "hello live" in out2["text"],
                flush=True,
            )
            assert "hello live" in out2["text"], "bridge died while idle!"
            await session.call("close_document", doc_id=doc["doc_id"], force=True)
            print("post-idle close ok", flush=True)
        else:
            assert headless, "expected headless fallback with no display!"
            print("fallback ok", flush=True)
    finally:
        await session.shutdown()
    print("shutdown ok", flush=True)


asyncio.run(main())
