"""Supervise a soffice process listening for UNO connections.

Each instance gets an isolated UserInstallation profile and a UNIQUE named pipe.
Named pipes (rather than TCP ports) make concurrent instances collision-proof:
there is no shared port to race over, which matters for parallel-worktree dev.
Instances are disposable: on death we relaunch rather than revive a hung bridge.

Live mode (the default) runs soffice visibly; the display environment is
recovered from the desktop session first, since MCP hosts spawn the server
with sanitized variables. With no display anywhere (CI, SSH), live mode
falls back to headless with a warning instead of failing.

Every document an instance opens is recorded in its profile, so the
`.~lock.<name>#` files soffice leaves next to them can be removed once it is
gone. A killed server never runs its own teardown, so the next server start
reaps profiles of dead instances the same way. Stale lock files matter beyond
LibreOffice: sync clients (Nextcloud) lock the file on the server while one
exists, and moves/saves from other devices then fail with 423 Locked.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import uuid

from src.log import get_logger
from src.office.display import ensure_display

log = get_logger(__name__)

# documents this instance opened, one absolute path per line
OPENED_FILE = "opened-documents"

_INSTANCE_RE = re.compile(r"^instance-libre_mcp_(\d+)_[0-9a-f]+$")


def unique_pipe_name() -> str:
    """A pipe name unique across processes and instances on this machine."""
    return f"libre_mcp_{os.getpid()}_{uuid.uuid4().hex[:12]}"


def lock_file_for(path: str) -> str:
    """The lock file LibreOffice keeps next to an open document."""
    head, name = os.path.split(path)
    return os.path.join(head, f".~lock.{name}#")


def release_locks(profile: str) -> list[str]:
    """Remove the lock files of documents opened by the (dead) instance owning
    `profile`. A lock file is only removed when it names this instance's
    profile, so locks held by any other LibreOffice — your own, or another
    server's — are never touched. Returns the removed paths."""
    try:
        with open(os.path.join(profile, OPENED_FILE), encoding="utf-8") as f:
            paths = {line.rstrip("\n") for line in f if line.strip()}
    except OSError:
        return []
    instance = os.path.basename(profile)
    removed = []
    for path in sorted(paths):
        lock = lock_file_for(path)
        try:
            with open(lock, encoding="utf-8", errors="replace") as f:
                owner = f.read()
        except OSError:
            continue  # closed cleanly, or never locked
        if instance not in owner:
            continue
        try:
            os.remove(lock)
            removed.append(lock)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not remove stale lock %s: %s", lock, e)
    if removed:
        log.info("removed stale lock files of %s: %s", instance, removed)
    return removed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_lines() -> list[str] | None:
    """Command lines of all running processes, or None if they can't be read."""
    if sys.platform.startswith("linux"):
        out = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    out.append(f.read().replace(b"\0", b" ").decode(errors="replace"))
            except OSError:
                pass  # exited meanwhile, or not ours to read
        return out
    try:
        ps = subprocess.run(
            ["ps", "-axww", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return ps.stdout.splitlines() if ps.returncode == 0 else None


def reap_stale_profiles(profile_dir: str, keep_profile: bool = False) -> None:
    """Release the lock files of instances that died without tearing down, and
    remove their profiles (unless profiles are kept).

    An instance counts as alive while its server pid runs or any process still
    has its profile on the command line: an orphaned soffice outlives a killed
    server, and its documents are genuinely open. When liveness can't be
    determined, nothing is touched."""
    try:
        entries = sorted(os.listdir(profile_dir))
    except OSError:
        return
    candidates = []
    for entry in entries:
        m = _INSTANCE_RE.match(entry)
        if m and not _pid_alive(int(m.group(1))):
            candidates.append(os.path.join(profile_dir, entry))
    if not candidates:
        return
    cmdlines = _command_lines()
    if cmdlines is None:
        log.warning("cannot list processes; leaving stale profiles alone")
        return
    for profile in candidates:
        if any(profile in cmd for cmd in cmdlines):
            continue
        release_locks(profile)
        if not keep_profile:
            log.info("removing stale profile %s", profile)
            shutil.rmtree(profile, ignore_errors=True)


class SofficeProcess:
    def __init__(
        self,
        soffice_path: str,
        pipe_name: str,
        profile_dir: str,
        keep_profile: bool = False,
        startup_timeout: float = 30.0,
        visible: bool = False,
    ) -> None:
        self.soffice_path = soffice_path
        self.pipe_name = pipe_name
        self.profile = os.path.abspath(
            os.path.join(profile_dir, f"instance-{pipe_name}")
        )
        self.keep_profile = keep_profile
        self.startup_timeout = startup_timeout
        self.visible = visible
        self._proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"uno:pipe,name={self.pipe_name};urp;StarOffice.ComponentContext"

    async def start(self) -> None:
        os.makedirs(self.profile, exist_ok=True)
        accept = f"pipe,name={self.pipe_name};urp;StarOffice.ComponentContext"
        # Live mode shows a real window; otherwise run headless/invisible.
        # Without any display (CI, SSH), live degrades to headless with a
        # warning instead of launching a window that can never appear.
        visible = self.visible
        if visible:
            info = ensure_display()
            if info.kind == "none":
                log.warning(
                    "live mode requested but no display found (%s); "
                    "falling back to headless",
                    info.detail,
                )
                visible = False
            else:
                log.info("live mode on %s (%s)", info.kind, info.detail)
        window = [] if visible else ["--headless", "--invisible"]
        args = [
            self.soffice_path,
            *window,
            "--norestore",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            f"-env:UserInstallation=file://{self.profile}",
            f"--accept={accept}",
        ]
        logf = open(os.path.join(self.profile, "soffice.log"), "ab")
        log.info(
            "launching soffice (pipe=%s, profile=%s)", self.pipe_name, self.profile
        )
        self._proc = subprocess.Popen(
            args, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL
        )
        logf.close()  # the child has its own copy; don't leak the fd

        # Brief settle to catch immediate launch/config failures. True readiness
        # is gated by the worker's connect-retry loop (bounded by startup_timeout).
        await asyncio.sleep(0.5)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"soffice exited early with code {self._proc.returncode}; "
                f"see {self.profile}/soffice.log"
            )

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def record_opened(self, path: str) -> None:
        """Remember an opened document, so its lock file can be released if
        this instance goes away without closing it."""
        try:
            with open(
                os.path.join(self.profile, OPENED_FILE), "a", encoding="utf-8"
            ) as f:
                f.write(os.path.abspath(path) + "\n")
        except OSError as e:
            log.warning("could not record opened document %s: %s", path, e)

    async def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.get_event_loop().run_in_executor(None, proc.wait, 10)
            except Exception:
                proc.kill()
                try:
                    await asyncio.get_event_loop().run_in_executor(None, proc.wait, 10)
                except Exception:
                    pass
        if proc is not None and proc.poll() is None:
            # still running: its locks are live and the profile in use; the
            # next server start reaps both once it is gone
            log.warning("soffice (pid %s) did not exit; keeping profile", proc.pid)
            self._proc = None
            return
        self._proc = None
        release_locks(self.profile)
        if not self.keep_profile and os.path.isdir(self.profile):
            shutil.rmtree(self.profile, ignore_errors=True)
