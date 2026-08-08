"""Home Assistant REST client for the Now Playing plugin.

Shared by the plugin itself (rendering) and the refresh task watcher (polling), so
both agree on what counts as "playing" and how a track is identified.
"""

import logging
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image

from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5


class HomeAssistantError(Exception):
    """Home Assistant could not be reached, or answered with an error.

    Kept distinct from "nothing is playing" so the watcher does not mistake an outage
    for playback stopping, which would flap the display on an unreliable network.
    """

# States that mean media is loaded. 'paused' is included conditionally, see find_active.
PLAYING_STATES = ("playing",)
PAUSED_STATES = ("paused",)


@dataclass
class NowPlayingState:
    """A snapshot of what a single media_player entity is playing."""

    entity_id: str
    friendly_name: str
    state: str
    title: str
    artist: str = ""
    album: str = ""
    app_name: str = ""
    entity_picture: str = ""

    def track_key(self):
        """Identifies the track for change detection.

        Deliberately excludes anything time varying (position, volume) so that repeated
        polls of the same track produce the same key, and therefore the same rendered
        image, and therefore no e-ink refresh.
        """
        return f"{self.entity_id}|{self.title}|{self.artist}|{self.album}"

    def to_dict(self):
        return {
            "entity_id": self.entity_id,
            "friendly_name": self.friendly_name,
            "state": self.state,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "app_name": self.app_name,
            "entity_picture": self.entity_picture,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    @classmethod
    def from_entity(cls, entity):
        """Builds a state from a Home Assistant /api/states entity dictionary."""
        attributes = entity.get("attributes") or {}
        return cls(
            entity_id=entity.get("entity_id", ""),
            friendly_name=attributes.get("friendly_name") or entity.get("entity_id", ""),
            state=entity.get("state", ""),
            title=attributes.get("media_title") or "",
            artist=attributes.get("media_artist") or attributes.get("media_album_artist") or "",
            album=attributes.get("media_album_name") or "",
            app_name=attributes.get("app_name") or "",
            entity_picture=attributes.get("entity_picture") or "",
        )


def _normalize_base_url(base_url):
    """Strips trailing slashes so urljoin behaves predictably."""
    if not base_url:
        raise ValueError("Home Assistant URL is not configured.")
    return base_url.rstrip("/")


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _request_json(base_url, token, path, timeout=DEFAULT_TIMEOUT):
    """GETs a Home Assistant API path and returns the decoded JSON body."""
    url = f"{_normalize_base_url(base_url)}{path}"
    session = get_http_session()
    response = session.get(url, headers=_auth_headers(token), timeout=timeout)
    response.raise_for_status()
    return response.json()


def list_media_players(base_url, token, timeout=DEFAULT_TIMEOUT):
    """Returns every media_player entity known to Home Assistant.

    Used to populate the entity picker on the settings page, so it must never raise -
    the settings page has to render even when Home Assistant is unreachable.

    Each entry carries what the player is doing right now, so the picker can show which
    entities are actually usable. Speaker groups and Connect-style sources often report
    `playing` with no media_title at all, and an entity like that can never drive this
    plugin - `usable` marks the difference.

    Returns:
        (entities, error): a list of dicts with entity_id, friendly_name, state, title,
        artist, app_name and usable, active players first and then by name; plus an
        error string (empty when the lookup succeeded).
    """
    if not base_url or not token:
        return [], "Set HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN in your .env file."

    try:
        entities = _request_json(base_url, token, "/api/states", timeout)
    except Exception as e:
        logger.warning(f"Failed to list Home Assistant media players: {e}")
        return [], f"Could not reach Home Assistant at {base_url}: {e}"

    media_players = []
    for entity in entities:
        entity_id = entity.get("entity_id", "")
        if not entity_id.startswith("media_player."):
            continue

        state = NowPlayingState.from_entity(entity)
        is_active = state.state in PLAYING_STATES + PAUSED_STATES
        media_players.append({
            "entity_id": entity_id,
            "friendly_name": state.friendly_name,
            "state": state.state or "unknown",
            "title": state.title,
            "artist": state.artist,
            "app_name": state.app_name,
            "active": is_active,
            # The same test find_active applies, so the picker agrees with the watcher.
            "usable": is_active and bool(state.title.strip()),
        })

    # Whatever is playing right now goes to the top, where it is easy to pick.
    media_players.sort(key=lambda p: (not p["usable"], not p["active"], p["friendly_name"].lower()))
    return media_players, ""


def get_state(base_url, token, entity_id, timeout=DEFAULT_TIMEOUT):
    """Returns the NowPlayingState for a single entity, or None if it doesn't exist.

    A missing entity is a configuration problem with one speaker and is skipped, but
    anything else - no route to host, a timeout, a rejected token - means we do not
    know what is playing, and is raised as HomeAssistantError rather than reported as
    silence.
    """
    try:
        entity = _request_json(base_url, token, f"/api/states/{entity_id}", timeout)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            logger.warning(f"Home Assistant has no entity '{entity_id}'.")
            return None
        raise HomeAssistantError(f"Home Assistant returned {status} for '{entity_id}'.") from e
    except Exception as e:
        raise HomeAssistantError(f"Could not read '{entity_id}' from Home Assistant: {e}") from e

    if not entity or not entity.get("entity_id"):
        return None
    return NowPlayingState.from_entity(entity)


def find_active(base_url, token, entity_ids, include_paused=True, timeout=DEFAULT_TIMEOUT):
    """Returns the first entity that is actively playing something, or None.

    Entities are checked in the order given, so listing the speaker group first makes it
    win over its individual members (casting to a group makes every member report
    playing too).

    A non-empty media_title is required. That filters out TTS announcements, doorbell
    chimes and stream startup blips, which otherwise take over the display for a single
    poll and cost a full e-ink refresh each way.

    Raises:
        HomeAssistantError: if Home Assistant could not be reached. Returning None here
            would be read as "nothing is playing" and hand the display back to the
            playlist on a momentary network blip.
    """
    accepted = PLAYING_STATES + (PAUSED_STATES if include_paused else ())

    for entity_id in entity_ids:
        state = get_state(base_url, token, entity_id, timeout)
        if state is None:
            continue
        if state.state not in accepted:
            continue
        if not state.title.strip():
            logger.debug(f"Ignoring '{entity_id}' in state '{state.state}' with no media title.")
            continue
        return state

    return None


def fetch_artwork(base_url, token, entity_picture, timeout=10):
    """Downloads album art referenced by a media player's entity_picture attribute.

    entity_picture is usually a relative signed path served by Home Assistant
    (/api/media_player_proxy/...?token=...), but some integrations report an absolute
    URL on a third party CDN. The bearer token is only attached when the resolved URL
    is on the Home Assistant host, so it is never leaked to an external host.

    Returns a PIL.Image, or None if there is no artwork or it could not be loaded.
    """
    if not entity_picture:
        return None

    base = _normalize_base_url(base_url)
    url = urljoin(f"{base}/", entity_picture)

    headers = {}
    if urlparse(url).netloc == urlparse(base).netloc:
        headers = _auth_headers(token)

    try:
        session = get_http_session()
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return Image.open(BytesIO(response.content))
    except Exception as e:
        logger.warning(f"Failed to fetch album art from {url}: {e}")
        return None
