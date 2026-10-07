"""Persistent browser transport for Vinted's JavaScript bot checks.

Camoufox is kept on one dedicated thread because its synchronous Playwright
objects are thread-affine, while searches can originate from the scheduler or
FastAPI request workers. The browser profile lives in ``data/`` so Cloudflare
and DataDome session state survives application restarts.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

try:
    from camoufox.pkgman import camoufox_path, launch_path
    from camoufox.sync_api import Camoufox
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    HAVE_CAMOUFOX = True
except ImportError:  # pragma: no cover - optional fallback for local installs
    PlaywrightError = RuntimeError
    PlaywrightTimeout = TimeoutError
    Camoufox = None
    HAVE_CAMOUFOX = False

logger = logging.getLogger(__name__)

_HOME_READY_SELECTOR = 'input[placeholder*="Rechercher"]'
_CHALLENGE_TITLES = ("just a moment", "un instant", "please wait")


class VintedBrowserError(RuntimeError):
    """The persistent browser could not obtain a usable Vinted page."""


class VintedBrowserBroker:
    """Serialize all browser work onto one persistent worker thread."""

    def __init__(self, base_url: str, profile_dir: str | Path) -> None:
        self._base = base_url.rstrip("/")
        self._profile_dir = Path(profile_dir)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vinted-browser"
        )
        self._camoufox = None
        self._context = None
        self._page = None
        self._warmed = False
        self._closed = False

    def fetch(self, path: str, params: dict | None = None) -> str:
        """Return fully rendered HTML, safe to call from any application thread."""
        if self._closed:
            raise VintedBrowserError("Vinted browser broker is closed")
        return self._executor.submit(
            self._fetch_on_browser_thread, path, params
        ).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._reset).result(timeout=15)
        except Exception:  # noqa: BLE001 - interpreter/application shutdown
            logger.debug("Could not close Vinted browser cleanly", exc_info=True)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _fetch_on_browser_thread(self, path: str, params: dict | None) -> str:
        try:
            return self._navigate(path, params)
        except VintedBrowserError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as exc:
            logger.warning("Vinted browser failed; restarting once: %s", exc)
            self._reset()
            try:
                return self._navigate(path, params)
            except (PlaywrightError, PlaywrightTimeout) as retry_exc:
                raise VintedBrowserError(
                    f"Vinted browser failed after restart: {retry_exc}"
                ) from retry_exc

    def _ensure_browser(self) -> None:
        if self._context is not None and self._page is not None:
            return
        if not HAVE_CAMOUFOX or Camoufox is None:
            raise VintedBrowserError(
                "Camoufox is unavailable; install project requirements"
            )
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "persistent_context": True,
            "user_data_dir": str(self._profile_dir.resolve()),
            # Linux uses a real headed browser in a private Xvfb display.
            "headless": "virtual" if os.name != "nt" else False,
            "locale": "fr-FR",
        }
        if os.name == "nt":
            # Microsoft Store Python virtualizes LocalAppData. Resolve the
            # installed binary so the child browser process sees the real path.
            binary = Path(launch_path(camoufox_path(False))).resolve()
            options["executable_path"] = str(binary)
        self._camoufox = Camoufox(**options)
        self._context = self._camoufox.__enter__()
        self._page = self._context.pages[0] if self._context.pages else None
        if self._page is None:
            self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(60_000)
        self._page.set_default_timeout(45_000)
        self._warmed = False

    def _warm_up(self) -> None:
        if self._warmed:
            return
        self._page.goto(self._base + "/", wait_until="domcontentloaded")
        try:
            self._page.locator(_HOME_READY_SELECTOR).first.wait_for(
                state="attached", timeout=45_000
            )
        except PlaywrightTimeout as exc:
            raise VintedBrowserError(
                "Cloudflare challenge did not clear on the Vinted homepage"
            ) from exc
        self._warmed = True

    def _navigate(self, path: str, params: dict | None) -> str:
        self._ensure_browser()
        self._warm_up()
        query = urlencode(params or {}, doseq=True)
        url = f"{self._base}{path}"
        if query:
            url += "?" + query
        self._page.goto(url, wait_until="domcontentloaded")

        self._page.locator("body").wait_for(state="attached", timeout=45_000)
        title = self._page.title().strip().lower()
        if any(marker in title for marker in _CHALLENGE_TITLES):
            raise VintedBrowserError("Vinted returned a Cloudflare challenge")
        return self._page.content()

    def _reset(self) -> None:
        if self._camoufox is not None:
            try:
                self._camoufox.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("Vinted browser context close failed", exc_info=True)
        self._camoufox = self._context = self._page = None
        self._warmed = False


_broker: VintedBrowserBroker | None = None
_broker_lock = threading.Lock()


def fetch_vinted_html(
    base_url: str,
    profile_dir: str | Path,
    path: str,
    params: dict | None = None,
) -> str:
    """Fetch through the process-wide persistent Vinted browser."""
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = VintedBrowserBroker(base_url, profile_dir)
        broker = _broker
    return broker.fetch(path, params)


def close_vinted_browser() -> None:
    global _broker
    with _broker_lock:
        broker, _broker = _broker, None
    if broker is not None:
        broker.close()


atexit.register(close_vinted_browser)
