# Load images from an album

## Immich

<a href="https://immich.app/">Link to Immich</a>

Create an album in Immich<br>
Create an API Key with the following permissions: asset.read, asset.download, album.read<br>
Store the key in the .env file with IMMICH_KEY=1234<br>

## Google Photos (unofficial, use at your own risk)

Google shut down the official Photos API's shared-album access in March 2025
(https://developers.google.com/photos/support/updates), so there is no supported,
public way to read an existing album. This provider instead calls the same
undocumented internal endpoint that photos.google.com's own web page uses to page
through an album, following the approach used by community projects like
`xob0t/google-photos-toolkit` and Home Assistant's `album_slideshow` integration.

No API key or Google account sign-in is needed - only a public shared album link.

- **No official support or stability guarantee.** Google can change their internal
  page/endpoint layout at any time without notice, which would break this provider
  until it's updated.
- New photos added to the album are picked up automatically (unlike the official
  Picker API, which requires the user to manually re-pick photos) since the album
  is re-scraped periodically, but the photo list is cached for up to 6 hours to
  avoid hammering Google's servers on every display refresh.

Setup:
1. In the Google Photos app or website, open the album you want to display.
2. Tap **Share** &rarr; **Create link**, and make sure link sharing is turned on.
3. Paste the resulting link (`https://photos.app.goo.gl/...` or `https://photos.google.com/share/...`) into the plugin's "Shared Album Link" field.

