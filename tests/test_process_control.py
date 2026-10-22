"""Ending a wrapper's descendants when the wrapper is signalled first."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil

from agent_monitor import process_control

# A wrapper (a CGM runner, say) with one child in a session of its own (a
# Claude Code or Codex call).  The wrapper dies at once on SIGTERM.  The
# child ("plain") dies at once too, or ("graceful") takes 0.3 s to settle,
# writes "ended" and exits.  The wrapper prints the child's pid; the child
# writes "ready" once it takes signals.
_WRAPPER = r"""
import subprocess, sys, time

child = {
    "plain": "import time; open('ready', 'w').close(); time.sleep(60)",
    "graceful": (
        "import signal, sys, time\n"
        "def settle(*_):\n"
        "    time.sleep(0.3)\n"
        "    open('ended', 'w').close()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, settle)\n"
        "open('ready', 'w').close()\n"
        "time.sleep(60)\n"
    ),
}[sys.argv[1]]
process = subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
print(process.pid, flush=True)
time.sleep(60)
"""

# Both ways of listing a wrapper's descendants: psutil, and the stdlib
# fallback through /proc for environments without psutil.
WALKS = {
    "psutil": process_control.terminate_descendants,
    "proc": process_control._terminate_linux_descendants,
}


def _gone(process: psutil.Process) -> bool:
    """Whether ``process`` has exited, within 5 s."""
    try:
        process.wait(5)
    except psutil.TimeoutExpired:
        return False
    return True


def _kill(process: psutil.Process) -> None:
    """Kill ``process`` if it still runs (never a recycled pid)."""
    try:
        process.kill()
    except psutil.Error:
        pass


class TerminateDescendantsTests(unittest.TestCase):
    def _wrapper(self, child: str) -> tuple[subprocess.Popen, psutil.Process, Path]:
        """Start the wrapper; returns it, its child and their directory."""
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        wrapper = subprocess.Popen(
            [sys.executable, "-c", _WRAPPER, child], cwd=directory, stdout=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        self.addCleanup(wrapper.wait, 5)
        self.addCleanup(wrapper.kill)
        assert wrapper.stdout is not None
        self.addCleanup(wrapper.stdout.close)
        process = psutil.Process(int(wrapper.stdout.readline()))
        self.addCleanup(_kill, process)
        deadline = time.monotonic() + 10
        while not (directory / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((directory / "ready").exists(), "the child never started")
        return wrapper, process, directory

    @contextmanager
    def _slow_listing(self, walk: str):
        """Listing the descendants takes 0.5 s more (the console busy)."""
        if walk == "psutil":
            children = psutil.Process.children

            def listing(process, *args, **kwargs):
                time.sleep(0.5)
                return children(process, *args, **kwargs)

            with patch.object(psutil.Process, "children", listing):
                yield
            return

        class SlowProc(type(Path())):
            def iterdir(self):
                time.sleep(0.5)
                return super().iterdir()

        with patch.object(process_control, "Path", SlowProc):
            yield

    def test_the_children_of_a_wrapper_signalled_first_are_still_ended(self) -> None:
        # Signalled between the listing and its children, the wrapper dies at
        # once and they lose their ancestry: they were listed already.
        for walk, terminate in WALKS.items():
            with self.subTest(walk=walk):
                wrapper, child, _directory = self._wrapper("plain")
                signalled: list[int] = []

                def signal_wrapper() -> None:
                    signalled.append(wrapper.pid)
                    os.killpg(wrapper.pid, signal.SIGTERM)

                with self._slow_listing(walk):
                    terminate(wrapper.pid, force=False, signal_wrapper=signal_wrapper)
                self.assertEqual(signalled, [wrapper.pid])
                self.assertEqual(wrapper.wait(5), -signal.SIGTERM)
                self.assertTrue(_gone(child), "the child outlived its wrapper")

    def test_a_child_whose_wrapper_died_still_gets_its_grace(self) -> None:
        # Reparented once its wrapper died, the child is still the process
        # that was listed: it settles on SIGTERM rather than being killed.
        for walk, terminate in WALKS.items():
            with self.subTest(walk=walk):
                wrapper, child, directory = self._wrapper("graceful")
                terminate(
                    wrapper.pid, force=False,
                    signal_wrapper=lambda: os.killpg(wrapper.pid, signal.SIGTERM),
                )
                self.assertTrue((directory / "ended").exists(), "the child was killed before it settled")
                self.assertTrue(_gone(child))

    def test_the_wrapper_is_signalled_once_whatever_it_left(self) -> None:
        # Without descendants, exited, or even reaped: it is still signalled
        # once (its group may hold other processes).
        for walk, terminate in WALKS.items():
            with self.subTest(walk=walk):
                lone = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                        start_new_session=True)
                self.addCleanup(lone.wait, 5)
                self.addCleanup(lone.kill)
                calls: list[str] = []
                terminate(lone.pid, force=False, signal_wrapper=lambda: calls.append("running"))
                lone.kill()
                os.waitid(os.P_PID, lone.pid, os.WEXITED | os.WNOWAIT)  # exited, not reaped
                terminate(lone.pid, force=False, signal_wrapper=lambda: calls.append("exited"))
                # Linux pids stay below 2**22, so no process has this one.
                terminate(1 << 22, force=False, signal_wrapper=lambda: calls.append("reaped"))
                self.assertEqual(calls, ["running", "exited", "reaped"])


if __name__ == "__main__":
    unittest.main()
