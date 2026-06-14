"""Load cookies from Netscape cookies.txt files.

Compatible with the "Get cookies.txt LOCALLY" Chrome extension and any tool that
exports the standard Netscape/Mozilla cookie format.

The extension exports one file per site (e.g. ``www.leboncoin.fr_cookies.txt``),
so loading scans the configured file *and* any ``*cookies*.txt`` siblings in its
directory: drop each site's export into ``data/`` and the right source picks it
up by domain. A single combined ``cookies.txt`` works too.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _cookie_files(cookies_file: str | Path) -> list[Path]:
    """The configured file plus any ``*cookies*.txt`` siblings, deduplicated."""
    p = Path(cookies_file)
    candidates: list[Path] = []
    if p.is_file():
        candidates.append(p)
    directory = p.parent
    if directory.is_dir():
        candidates.extend(sorted(directory.glob("*cookies*.txt")))

    files: list[Path] = []
    seen: set[Path] = set()
    for f in candidates:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            files.append(f)
    return files


def _iter_cookie_rows(path: Path):
    """Yield ``(domain, path, secure, expiry, name, value)`` for live cookies."""
    now = time.time()
    for line in path.read_text("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        c_domain, _subdomains, c_path, secure, c_expiry, c_name, c_value = parts[:7]
        try:
            expiry = int(c_expiry)
        except ValueError:
            expiry = 0
        if expiry and expiry < now:
            continue  # skip expired
        yield c_domain, c_path, secure.upper() == "TRUE", expiry, c_name, c_value


def load_cookies(cookies_file: str | Path, domain: str | None = None) -> httpx.Cookies:
    """Parse cookies.txt file(s) into an httpx cookie jar.

    If *domain* is given (e.g. ``"www.ebay.fr"``), only matching cookies are
    included. Expired cookies are skipped.
    """
    jar = httpx.Cookies()
    count = 0
    for f in _cookie_files(cookies_file):
        for c_domain, c_path, _secure, _expiry, name, value in _iter_cookie_rows(f):
            if domain and not _domain_matches(c_domain, domain):
                continue
            jar.set(name, value, domain=c_domain, path=c_path)
            count += 1
    if count:
        logger.debug("Loaded %d cookies (domain=%s)", count, domain)
    return jar


def load_playwright_cookies(
    cookies_file: str | Path, domain: str | None = None
) -> list[dict]:
    """Parse cookies.txt file(s) into Playwright ``add_cookies`` dicts.

    Used to seed a browser context with a real session (e.g. a DataDome
    clearance cookie) so protected pages load without a challenge.
    """
    out: list[dict] = []
    for f in _cookie_files(cookies_file):
        for c_domain, c_path, secure, expiry, name, value in _iter_cookie_rows(f):
            if domain and not _domain_matches(c_domain, domain):
                continue
            cookie: dict = {
                "name": name,
                "value": value,
                "domain": c_domain,
                "path": c_path,
                "secure": secure,
            }
            if expiry:
                cookie["expires"] = expiry
            out.append(cookie)
    return out


def _domain_matches(cookie_domain: str, target: str) -> bool:
    """Check if a cookie domain covers the target domain.

    ``.ebay.fr`` matches ``www.ebay.fr`` and ``ebay.fr``.
    ``www.ebay.fr`` matches only ``www.ebay.fr``.
    """
    cd = cookie_domain.lstrip(".")
    td = target.lstrip(".")
    return td == cd or td.endswith("." + cd)
