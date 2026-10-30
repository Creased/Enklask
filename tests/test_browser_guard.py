import os
import subprocess
import threading
import time

import pytest

from app.sources import browser_guard, leboncoin_browser, vinted_browser
from app.sources.browser_guard import LaunchBackoff

posix_only = pytest.mark.skipif(
    not os.path.isdir("/proc") or os.name != "posix", reason="needs Linux /proc"
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_backoff_doubles_up_to_the_cap_and_resets_on_success():
    clock = _Clock()
    backoff = LaunchBackoff(base_seconds=60, max_seconds=300, clock=clock)

    assert backoff.remaining() == 0
    assert [backoff.failed() for _ in range(5)] == [60, 120, 240, 300, 300]
    assert backoff.remaining() == 300

    clock.now += 301
    assert backoff.remaining() == 0

    backoff.failed()
    backoff.succeeded()
    assert backoff.remaining() == 0
    assert backoff.failed() == 60


@posix_only
def test_kill_tree_kills_child_and_grandchildren():
    proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            grandchildren = browser_guard._children_map().get(proc.pid, [])
            if len(grandchildren) == 2:
                break
            time.sleep(0.05)
        assert len(grandchildren) == 2

        assert browser_guard.kill_tree({proc.pid}) == 3
        proc.wait(timeout=5)
        for pid in grandchildren:
            with pytest.raises(OSError):
                os.kill(pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()


@posix_only
def test_kill_tree_ignores_pids_that_are_not_our_children():
    assert browser_guard.kill_tree({1}) == 0


class _FailingCamoufox:
    launches = 0

    def __init__(self, **_options) -> None:
        type(self).launches += 1

    def __enter__(self):
        raise vinted_browser.PlaywrightError("launch failed")

    def __exit__(self, *_args):
        return None


def _fake_processes(monkeypatch, module, spawned_pid=4242):
    """Pretend each launch spawns ``spawned_pid`` and record what gets killed."""
    state = {"launched": False}
    killed: list[set[int]] = []

    def fake_child_pids():
        # The baseline is read before the launch, the leftovers after it.
        if state["launched"]:
            state["launched"] = False
            return {1, spawned_pid}
        state["launched"] = True
        return {1}

    monkeypatch.setattr(module, "child_pids", fake_child_pids)
    monkeypatch.setattr(module, "kill_tree", lambda pids: killed.append(set(pids)))
    return killed


@pytest.mark.parametrize(
    ("module", "call", "error"),
    [
        (
            vinted_browser,
            lambda b: b._fetch_on_browser_thread("/catalog", None),
            vinted_browser.VintedBrowserError,
        ),
        (
            leboncoin_browser,
            lambda b: b._search_on_browser_thread({}, "key"),
            leboncoin_browser.LeboncoinBrowserError,
        ),
    ],
)
def test_failed_launch_kills_leftovers_and_pauses_relaunches(
    monkeypatch, tmp_path, module, call, error
):
    _FailingCamoufox.launches = 0
    monkeypatch.setattr(module, "HAVE_CAMOUFOX", True)
    monkeypatch.setattr(module, "Camoufox", _FailingCamoufox)
    killed = _fake_processes(monkeypatch, module)
    broker_cls = (
        module.VintedBrowserBroker
        if module is vinted_browser
        else module.LeboncoinBrowserBroker
    )
    broker = broker_cls("https://example.test", tmp_path)

    with pytest.raises(error, match="paused"):
        call(broker)

    # One launch, its leftover process killed, and the in-call retry was refused.
    assert _FailingCamoufox.launches == 1
    assert {4242} in killed
    assert broker._camoufox is None and broker._owned_pids == set()
    assert broker._backoff.remaining() > 0

    with pytest.raises(error, match="paused"):
        call(broker)
    assert _FailingCamoufox.launches == 1


@pytest.mark.parametrize(
    ("module", "method", "args", "error"),
    [
        (vinted_browser, "fetch", ("/catalog",), vinted_browser.VintedBrowserError),
        (
            leboncoin_browser,
            "search",
            ({}, "key"),
            leboncoin_browser.LeboncoinBrowserError,
        ),
    ],
)
def test_hung_browser_call_times_out_and_kills_the_browser(
    monkeypatch, tmp_path, module, method, args, error
):
    monkeypatch.setattr(module, "_CALL_TIMEOUT_SECONDS", 0.2)
    killed: list[set[int]] = []
    monkeypatch.setattr(module, "kill_tree", lambda pids: killed.append(set(pids)))
    broker_cls = (
        module.VintedBrowserBroker
        if module is vinted_browser
        else module.LeboncoinBrowserBroker
    )
    broker = broker_cls("https://example.test", tmp_path)
    broker._owned_pids = {777}
    release = threading.Event()
    worker = (
        "_fetch_on_browser_thread"
        if module is vinted_browser
        else "_search_on_browser_thread"
    )
    monkeypatch.setattr(broker, worker, lambda *_a: release.wait(5))

    try:
        with pytest.raises(error, match="hung"):
            getattr(broker, method)(*args)
        assert killed and killed[0] == {777}
    finally:
        release.set()
        broker.close()
