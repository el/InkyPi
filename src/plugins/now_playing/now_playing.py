import base64
import logging
import os
from io import BytesIO

from dotenv import load_dotenv

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.now_playing.home_assistant import (
    NowPlayingState,
    fetch_artwork,
    find_active,
    list_media_players,
)
from utils.image_utils import resize_image

logger = logging.getLogger(__name__)

HA_URL_KEY = "HOME_ASSISTANT_URL"
HA_TOKEN_KEY = "HOME_ASSISTANT_TOKEN"

DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 5

# Album art is a square this fraction of the panel's short edge. Must match the art
# dimensions in now_playing.css, so the browser never has to rescale what Pillow
# produced - upscaling soft cast artwork looks noticeably worse on e-ink.
ART_SIZE_RATIO_LANDSCAPE = 0.62
ART_SIZE_RATIO_PORTRAIT = 0.74


def load_home_assistant_env():
    """Reads the Home Assistant URL and token from the .env file.

    Mirrors Config.load_env_key, but plugins are constructed with only their
    plugin-info config, so there is no device_config available in
    generate_settings_template.
    """
    load_dotenv(override=True)
    return os.getenv(HA_URL_KEY, ""), os.getenv(HA_TOKEN_KEY, "")


def get_entity_ids(settings):
    """Extracts the selected media_player entity ids from plugin settings.

    Checkbox groups arrive as 'entityIds[]' lists via parse_form, but a single ticked
    box can arrive as a bare string, and older/manually edited configs may use the
    comma separated fallback field.
    """
    raw = settings.get("entityIds[]") or settings.get("entityIds") or []
    if isinstance(raw, str):
        raw = [raw]

    entity_ids = [e.strip() for e in raw if e and e.strip()]

    manual = settings.get("manualEntityIds", "")
    if manual:
        for entity_id in manual.split(","):
            entity_id = entity_id.strip()
            if entity_id and entity_id not in entity_ids:
                entity_ids.append(entity_id)

    return entity_ids


def get_poll_interval(settings):
    """Returns the watcher poll interval in seconds, clamped to something sane."""
    try:
        interval = int(settings.get("pollInterval", DEFAULT_POLL_INTERVAL))
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, interval)


def include_paused(settings):
    return str(settings.get("includePaused", "true")).lower() != "false"


class NowPlaying(BasePlugin):
    """Displays the track currently playing on a Home Assistant media player."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()

        base_url, token = load_home_assistant_env()
        media_players, discovery_error = list_media_players(base_url, token)

        template_params["media_players"] = media_players
        template_params["discovery_error"] = discovery_error
        template_params["home_assistant_url"] = base_url
        template_params["default_poll_interval"] = DEFAULT_POLL_INTERVAL
        template_params["api_key"] = {
            "required": True,
            "service": "Home Assistant",
            "expected_key": HA_TOKEN_KEY,
        }
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        base_url, token = load_home_assistant_env()
        if not base_url or not token:
            raise RuntimeError(
                f"Home Assistant is not configured. Set {HA_URL_KEY} and {HA_TOKEN_KEY} in your .env file."
            )

        # The refresh task watcher has already polled Home Assistant, so it passes the
        # state through rather than making us fetch it a second time.
        state = settings.get("_state")
        if isinstance(state, dict):
            state = NowPlayingState.from_dict(state)
        elif state is None:
            entity_ids = get_entity_ids(settings)
            if not entity_ids:
                raise RuntimeError("No media players selected, choose at least one in the plugin settings.")
            state = find_active(base_url, token, entity_ids, include_paused(settings))

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params = {
            "plugin_settings": settings,
            "state": state,
            "show_source": str(settings.get("showSource", "true")).lower() != "false",
        }

        if state is not None:
            width, height = dimensions
            ratio = ART_SIZE_RATIO_PORTRAIT if width < height else ART_SIZE_RATIO_LANDSCAPE
            art_size = int(min(dimensions) * ratio)
            template_params["album_art"] = self.build_album_art(base_url, token, state, art_size)

        image = self.render_image(dimensions, "now_playing.html", "now_playing.css", template_params)
        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

    def build_album_art(self, base_url, token, state, art_size):
        """Downloads the album art and returns it as a data URI, or None.

        The rendered HTML is loaded from a temp file over file://, so a remote <img src>
        would make rendering depend on headless Chromium being able to reach Home
        Assistant. Embedding the bytes keeps rendering self contained. Resizing here
        rather than in CSS also keeps the downscale in Pillow, which is kinder to the
        e-ink panel than the browser's scaler.
        """
        artwork = fetch_artwork(base_url, token, state.entity_picture)
        if artwork is None:
            return None

        try:
            if artwork.mode not in ("RGB", "L"):
                artwork = artwork.convert("RGB")
            artwork = resize_image(artwork, (art_size, art_size))

            buffer = BytesIO()
            artwork.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except Exception as e:
            logger.warning(f"Failed to prepare album art for '{state.entity_id}': {e}")
            return None
