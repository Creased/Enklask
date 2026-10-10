"""Persistent browser fallback for Leboncoin's DataDome device check.

The normal connector keeps a lightweight curl_cffi session. When Leboncoin
returns a confirmed DataDome device-check response, a persistent Camoufox page
makes the API request in its own browser context. This lets DataDome's normal
JavaScript run without automating an interactive CAPTCHA or requiring an
account.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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

_SEARCH_URL = "https://api.leboncoin.fr/finder/search"


class LeboncoinBrowserError(RuntimeError):
    """The browser could not obtain a usable Leboncoin API response."""


class LeboncoinBrowserBroker:
    """Serialize Leboncoin browser requests onto one persistent worker thread."""

    def __init__(self, base_url: str, profile_dir: str | Path) -> None:
        self._base = base_url.rstrip("/")
        self._profile_dir = Path(profile_dir)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="leboncoin-browser"
        )
        self._camoufox = None
        self._context = None
        self._page = None
        self._warmed = False
        self._closed = False

    def search(self, body: dict, api_key: str) -> dict:
        if self._closed:
            raise LeboncoinBrowserError("Leboncoin browser broker is closed")
        return self._executor.submit(self._search_on_browser_thread, body, api_key).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._reset).result(timeout=15)
        except Exception:  # noqa: BLE001 - interpreter/application shutdown
            logger.debug("Could not close Leboncoin browser cleanly", exc_info=True)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _search_on_browser_thread(self, body: dict, api_key: str) -> dict:
        try:
            return self._search(body, api_key)
        except LeboncoinBrowserError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as exc:
            logger.warning("Leboncoin browser failed; restarting once: %s", exc)
            self._reset()
            try:
                return self._search(body, api_key)
            except (PlaywrightError, PlaywrightTimeout) as retry_exc:
                raise LeboncoinBrowserError(
                    f"Leboncoin browser failed after restart: {retry_exc}"
                ) from retry_exc

    def _ensure_browser(self) -> None:
        if self._context is not None and self._page is not None:
            return
        if not HAVE_CAMOUFOX or Camoufox is None:
            raise LeboncoinBrowserError(
                "Camoufox is unavailable; install project requirements"
            )
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "persistent_context": True,
            "user_data_dir": str(self._profile_dir.resolve()),
            "headless": "virtual" if os.name != "nt" else False,
            "locale": "fr-FR",
        }
        if os.name == "nt":
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
        self._page.locator("body").wait_for(state="attached", timeout=45_000)
        # Give the site's own DataDome JavaScript time to initialize and update
        # the persistent browser cookie jar.
        self._page.wait_for_timeout(3_000)
        self._warmed = True

    def _search(self, body: dict, api_key: str) -> dict:
        self._ensure_browser()
        self._warm_up()
        result = self._page.evaluate(
            """async ({url, body, apiKey}) => {
              try {
                const response = await fetch(url, {
                  method: 'POST',
                  credentials: 'include',
                  headers: {
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'api_key': apiKey,
                  },
                  body: JSON.stringify(body),
                });
                return {
                  status: response.status,
                  marker: response.headers.get('x-dd-b') ||
                          response.headers.get('x-sf-cc-x-dd-b'),
                  text: await response.text(),
                };
              } catch (error) {
                return {error: String(error)};
              }
            }""",
            {"url": _SEARCH_URL, "body": body, "apiKey": api_key},
        )
        if result.get("error"):
            raise LeboncoinBrowserError(result["error"])
        status = int(result.get("status") or 0)
        text = result.get("text") or ""
        if status != 200:
            marker = result.get("marker")
            detail = "DataDome challenge" if marker else "request failed"
            raise LeboncoinBrowserError(
                f"Leboncoin browser {detail}: HTTP {status}"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LeboncoinBrowserError(
                "Leboncoin browser returned non-JSON search data"
            ) from exc
        if not isinstance(payload, dict):
            raise LeboncoinBrowserError("Leboncoin browser returned invalid search data")
        return payload

    def _reset(self) -> None:
        if self._camoufox is not None:
            try:
                self._camoufox.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("Leboncoin browser context close failed", exc_info=True)
        self._camoufox = self._context = self._page = None
        self._warmed = False


_broker: LeboncoinBrowserBroker | None = None
_broker_lock = threading.Lock()


def fetch_leboncoin_json(
    base_url: str,
    profile_dir: str | Path,
    body: dict,
    api_key: str,
) -> dict:
    """Execute a search in the process-wide persistent browser."""
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = LeboncoinBrowserBroker(base_url, profile_dir)
        broker = _broker
    return broker.search(body, api_key)


def close_leboncoin_browser() -> None:
    global _broker
    with _broker_lock:
        broker, _broker = _broker, None
    if broker is not None:
        broker.close()


atexit.register(close_leboncoin_browser)
