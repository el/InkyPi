"""Fetches photos from a public Google Photos shared album link.

Google discontinued the Photos Library API's shared-album access in March
2025 (https://developers.google.com/photos/support/updates), so there is no
supported, public API for reading an existing album. This module instead
calls the same undocumented internal endpoint
(``batchexecute``/``snAcKc``) that photos.google.com's own web client uses
to page through an album's contents.

Because this relies on reverse-engineered internals rather than a documented
API, Google can change the page or endpoint layout at any time without
notice and silently break this provider. Parsing is deliberately defensive
(malformed items/pages are skipped rather than failing the whole fetch), but
there's no guarantee of long-term stability - this is not an officially
supported integration.

Ported from the approach used by community projects such as
`xob0t/google-photos-toolkit` and the Home Assistant `album_slideshow`
integration (https://github.com/eyalgal/album_slideshow).
"""
import json
import logging
import re
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# The AF_dataServiceRequests block embedded in the share page's HTML carries
# the snAcKc request payload: "snAcKc",ext:...,request:["<albumKey>",null,null,"<authKey>"]
_REQUEST_RE = re.compile(
    r"snAcKc[^}]*?request:\s*\[\s*\"([A-Za-z0-9_-]+)\"\s*,\s*null\s*,\s*null\s*,\s*\"([A-Za-z0-9_-]+)\"",
    re.DOTALL,
)

# XSSI prefix Google prepends to batchexecute responses.
_XSSI_PREFIX = ")]}'"

# Hard ceiling, matches the upstream Google album item limit.
_MAX_ITEMS = 20_000

# Strip any existing "=w...-h..." suffix from a Google CDN url so we can
# attach our own size hint.
_SIZE_SUFFIX_RE = re.compile(r"=[wh]\d+(?:-[a-z0-9]+)*$", re.IGNORECASE)

_VIDEO_DURATION_KEY = 76647426  # presence indicates a video; we skip those


class GooglePhotosScrapeError(Exception):
    """Raised when a shared album's photos cannot be fetched or parsed."""


def fetch_album_photo_urls(session, share_url: str, timeout: float = 30.0) -> list[str]:
    """Fetch every photo URL in a public Google Photos shared album.

    Args:
        session: A requests.Session (or compatible) to issue requests with.
        share_url: A public Google Photos share link. Both short links
            (photos.app.goo.gl/...) and direct links (photos.google.com/share/...)
            work, since short links simply redirect.
        timeout: Per-request timeout in seconds.

    Returns:
        A list of directly-downloadable, sized image URLs. Videos are excluded.

    Raises:
        GooglePhotosScrapeError: If the album keys can't be located (link is
            invalid, private, or Google changed their page layout) or no
            photos could be parsed at all.
    """
    album_key, auth_key = _fetch_album_keys(session, share_url, timeout)

    urls: list[str] = []
    seen: set[str] = set()
    page_id = None
    page_no = 0
    while True:
        page_no += 1
        try:
            page_urls, page_id = _fetch_album_page(session, album_key, auth_key, page_id, timeout)
        except Exception as e:
            logger.warning(
                f"Google Photos scrape: page {page_no} request failed ({e}); "
                f"returning {len(urls)} photo(s) found so far"
            )
            break

        added = 0
        for url in page_urls:
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
            added += 1
            if len(urls) >= _MAX_ITEMS:
                break

        logger.debug(
            f"Google Photos scrape: page {page_no} returned {len(page_urls)} item(s) "
            f"({added} new), running total {len(urls)}"
        )

        if not page_id or len(urls) >= _MAX_ITEMS or added == 0:
            break

    if not urls:
        raise GooglePhotosScrapeError(
            "No photos could be found in this shared album. Make sure link "
            "sharing is turned on and the link is correct."
        )

    logger.info(f"Google Photos scrape: found {len(urls)} photo(s) across {page_no} page(s)")
    return urls


def _fetch_album_keys(session, share_url: str, timeout: float) -> tuple[str, str]:
    """Fetch the share URL's HTML and extract the album/auth keys."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        response = session.get(share_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        html = response.text
    except Exception as e:
        raise GooglePhotosScrapeError(f"Could not open shared album link: {e}") from e

    match = _REQUEST_RE.search(html)
    if not match:
        raise GooglePhotosScrapeError(
            "Could not read this Google Photos shared album. Make sure link "
            "sharing is turned on and the link is correct."
        )
    return match.group(1), match.group(2)


def _fetch_album_page(
    session, album_key: str, auth_key: str, page_id: str | None, timeout: float
) -> tuple[list[str], str | None]:
    """Call the snAcKc RPC once. Returns (photo_urls, next_page_id)."""
    inner = json.dumps([album_key, page_id, None, auth_key])
    envelope = json.dumps([[["snAcKc", inner, None, "generic"]]])
    form = f"f.req={quote(envelope)}"

    url = (
        "https://photos.google.com/u/0/_/PhotosUi/data/batchexecute"
        f"?rpcids=snAcKc&source-path=/share/{quote(album_key)}"
    )
    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://photos.google.com",
        "Referer": f"https://photos.google.com/share/{album_key}?key={auth_key}",
    }
    response = session.post(url, data=form, headers=headers, timeout=timeout)
    response.raise_for_status()
    return _parse_batchexecute_page(response.text)


def _parse_batchexecute_page(body: str) -> tuple[list[str], str | None]:
    """Parse a batchexecute response for one snAcKc call.

    Format (per line, after the XSSI prefix):
      [["wrb.fr", "snAcKc", "<json-encoded inner>", null, null, "generic"], ...]
    Where the inner data is itself JSON, shaped as:
      inner[1] = list of raw album items
      inner[2] = next page id (str), or absent/empty when there are no more pages
    """
    text = body.lstrip()
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX):]

    line = text.strip().split("\n", 1)[0]
    if not line:
        return [], None

    try:
        outer = json.loads(line)
    except json.JSONDecodeError:
        return [], None

    inner_json = None
    for entry in outer:
        if isinstance(entry, list) and len(entry) >= 3 and entry[0] == "wrb.fr":
            inner_json = entry[2]
            break
    if not isinstance(inner_json, str):
        return [], None

    try:
        inner = json.loads(inner_json)
    except json.JSONDecodeError:
        return [], None

    raw_items = inner[1] if len(inner) > 1 and isinstance(inner[1], list) else []
    next_page = inner[2] if len(inner) > 2 and isinstance(inner[2], str) and inner[2] else None

    urls = []
    for raw in raw_items:
        url = _parse_album_item(raw)
        if url:
            urls.append(url)
    return urls, next_page


def _parse_album_item(raw: Any) -> str | None:
    """Extract a sized, directly-downloadable photo URL from one raw item entry.

    Layout: [mediaKey, [url, width, height, ...], captured_ms, ..., {<numeric metadata keys>}]
    """
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    visual = raw[1]
    if not isinstance(visual, list) or len(visual) < 3:
        return None

    url = visual[0]
    if not isinstance(url, str) or not url.startswith("http"):
        return None

    # Skip videos: the last element is a dict with key 76647426 (duration) when present.
    if isinstance(raw[-1], dict) and (_VIDEO_DURATION_KEY in raw[-1] or "76647426" in raw[-1]):
        return None

    width = visual[1] if isinstance(visual[1], int) else None
    height = visual[2] if isinstance(visual[2], int) else None
    return _normalize_size(url, width, height)


def _normalize_size(url: str, width: int | None, height: int | None) -> str:
    """Strip any existing size suffix and request a size capped at 4K."""
    base = _SIZE_SUFFIX_RE.sub("", url)
    if not width or not height:
        return f"{base}=w1920-h1080"
    longest = max(width, height)
    if longest > 3840:
        scale = 3840 / longest
        width = max(1, round(width * scale))
        height = max(1, round(height * scale))
    return f"{base}=w{width}-h{height}"
