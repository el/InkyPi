import logging
import time
from random import choice

from PIL import Image, ImageColor, ImageOps
from utils.http_client import get_http_session
from plugins.base_plugin.base_plugin import BasePlugin
from plugins.image_album.google_photos_provider import fetch_album_photo_urls, GooglePhotosScrapeError
from utils.image_utils import pad_image_blur

logger = logging.getLogger(__name__)

# Re-scraping an album walks every page of Google's undocumented endpoint, so we
# cache the resulting photo list on the plugin instance and only refresh it
# periodically rather than on every display refresh.
GOOGLE_PHOTOS_CACHE_TTL_SECONDS = 6 * 60 * 60


class ImmichProvider:
    def __init__(self, base_url: str, key: str, image_loader):
        self.base_url = base_url
        self.key = key
        self.headers = {"x-api-key": self.key}
        self.image_loader = image_loader
        self.session = get_http_session()

    def get_album_id(self, album: str) -> str:
        logger.debug(f"Fetching albums from {self.base_url}")
        r = self.session.get(f"{self.base_url}/api/albums", headers=self.headers)
        r.raise_for_status()
        albums = r.json()

        matching_albums = [a for a in albums if a["albumName"] == album]
        if not matching_albums:
            raise RuntimeError(f"Album '{album}' not found.")

        return matching_albums[0]["id"]

    def get_assets(self, album_id: str) -> list[dict]:
        """Fetch all assets from album."""
        all_items = []
        page_items = [1]
        page = 1

        logger.debug(f"Fetching assets from album {album_id}")
        while page_items:
            body = {
                "albumIds": [album_id],
                "size": 1000,
                "page": page
            }
            r2 = self.session.post(f"{self.base_url}/api/search/metadata", json=body, headers=self.headers)
            r2.raise_for_status()
            assets_data = r2.json()

            page_items = assets_data.get("assets", {}).get("items", [])
            all_items.extend(page_items)
            page += 1

        logger.debug(f"Found {len(all_items)} total assets in album")
        return all_items

    def get_image(self, album: str, dimensions: tuple[int, int], resize: bool = True) -> Image.Image | None:
        """
        Get a random image from the album.

        Args:
            album: Album name
            dimensions: Target dimensions (width, height)
            resize: Whether to let loader resize (False when padding will be applied)

        Returns:
            PIL Image or None on error
        """
        try:
            logger.info(f"Getting id for album '{album}'")
            album_id = self.get_album_id(album)
            logger.info(f"Getting assets from album id {album_id}")
            assets = self.get_assets(album_id)

            if not assets:
                logger.error(f"No assets found in album '{album}'")
                return None

        except Exception as e:
            logger.error(f"Error retrieving album data from {self.base_url}: {e}")
            return None

        # Select random asset
        selected_asset = choice(assets)
        asset_id = selected_asset["id"]
        asset_url = f"{self.base_url}/api/assets/{asset_id}/original"

        logger.info(f"Selected random asset: {asset_id}")
        logger.debug(f"Downloading from: {asset_url}")

        # Use adaptive image loader for memory-efficient processing
        # Let loader resize when requested (when no padding will be applied)
        img = self.image_loader.from_url(
            asset_url,
            dimensions,
            timeout_ms=40000,
            resize=resize,
            headers=self.headers
        )

        if not img:
            logger.error(f"Failed to load image {asset_id} from Immich")
            return None

        logger.info(f"Successfully loaded image: {img.size[0]}x{img.size[1]}")
        return img


class ImageAlbum(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['api_key'] = {
            "required": True,
            "service": "Immich",
            "expected_key": "IMMICH_KEY"
        }
        return template_params

    def _get_google_photos_urls(self, settings, share_url):
        """Return photo URLs for this shared album, re-scraping only if stale or changed.

        The scraped list is cached on the plugin instance's settings (persisted to
        device.json like image_upload's rotation index) since a full scrape pages
        through Google's undocumented endpoint and is too slow/heavy to repeat on
        every single display refresh.
        """
        cache = settings.get('googlePhotosCache') or {}
        cache_age = time.time() - cache.get('fetchedAt', 0)
        if (cache.get('shareUrl') == share_url and cache.get('urls')
                and cache_age < GOOGLE_PHOTOS_CACHE_TTL_SECONDS):
            logger.debug(f"Using cached Google Photos album ({len(cache['urls'])} photos, {cache_age:.0f}s old)")
            return cache['urls']

        try:
            urls = fetch_album_photo_urls(get_http_session(), share_url)
        except GooglePhotosScrapeError as e:
            logger.error(f"Failed to scrape Google Photos album: {e}")
            raise RuntimeError(str(e))

        settings['googlePhotosCache'] = {
            "shareUrl": share_url,
            "urls": urls,
            "fetchedAt": time.time()
        }
        return urls

    def generate_image(self, settings, device_config):
        logger.info("=== Image Album Plugin: Starting image generation ===")

        orientation = device_config.get_config("orientation")
        dimensions = device_config.get_resolution()

        if orientation == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(f"Vertical orientation detected, dimensions: {dimensions[0]}x{dimensions[1]}")

        img = None
        album_provider = settings.get("albumProvider")
        logger.info(f"Album provider: {album_provider}")

        # Check padding options to determine resize strategy
        use_padding = settings.get('padImage') == "true"
        background_option = settings.get('backgroundOption', 'blur')
        logger.debug(f"Settings: pad_image={use_padding}, background_option={background_option}")

        match album_provider:
            case "Immich":
                key = device_config.load_env_key("IMMICH_KEY")
                if not key:
                    logger.error("Immich API Key not configured")
                    raise RuntimeError("Immich API Key not configured.")

                url = settings.get('url')
                if not url:
                    logger.error("Immich URL not provided")
                    raise RuntimeError("Immich URL is required.")

                album = settings.get('album')
                if not album:
                    logger.error("Album name not provided")
                    raise RuntimeError("Album name is required.")

                logger.info(f"Immich URL: {url}")
                logger.info(f"Album: {album}")

                provider = ImmichProvider(url, key, self.image_loader)
                # Let loader resize when no padding needed, otherwise load full-size for padding
                img = provider.get_image(album, dimensions, resize=not use_padding)

                if not img:
                    logger.error("Failed to retrieve image from Immich")
                    raise RuntimeError("Failed to load image, please check logs.")
            case "Google Photos":
                share_url = settings.get('googlePhotosUrl')
                if not share_url:
                    logger.error("Google Photos share URL not provided")
                    raise RuntimeError("Google Photos shared album link is required.")

                logger.info(f"Google Photos share URL: {share_url}")

                photo_urls = self._get_google_photos_urls(settings, share_url)
                selected_url = choice(photo_urls)
                logger.info(f"Selected random photo from {len(photo_urls)} cached album photo(s)")

                # Let loader resize when no padding needed, otherwise load full-size for padding
                img = self.image_loader.from_url(
                    selected_url,
                    dimensions,
                    timeout_ms=40000,
                    resize=not use_padding
                )

                if not img:
                    logger.error("Failed to load image from Google Photos")
                    raise RuntimeError("Failed to load image, please check logs.")
            case _:
                logger.error(f"Unknown album provider: {album_provider}")
                raise RuntimeError(f"Unsupported album provider: {album_provider}")

        if img is None:
            logger.error("Image is None after provider processing")
            raise RuntimeError("Failed to load image, please check logs.")

        # Apply padding if requested (image was loaded at full size)
        if use_padding:
            logger.debug(f"Applying padding with {background_option} background")
            if background_option == "blur":
                img = pad_image_blur(img, dimensions)
            else:
                background_color = ImageColor.getcolor(
                    settings.get('backgroundColor') or "white",
                    img.mode
                )
                img = ImageOps.pad(img, dimensions, color=background_color, method=Image.Resampling.LANCZOS)
        # else: loader already resized to fit with proper aspect ratio

        logger.info("=== Image Album Plugin: Image generation complete ===")
        return img
