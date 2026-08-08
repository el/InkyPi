# Now Playing

Shows the track currently playing on your speakers, read from Home Assistant, and takes
over the display while music is playing.

## How it works

The plugin runs in "watch only" mode by default. The refresh task polls Home Assistant
on its own short interval (30s by default) and:

- when one of your selected media players starts playing, the Now Playing screen
  pre-empts whatever the playlist was showing
- when playback stops, the normal playlist resumes immediately rather than waiting out
  the rest of the plugin cycle interval
- a watch-only instance never appears in the normal playlist rotation

The rendered image contains nothing time-varying — no progress bar, no elapsed time, no
clock. That is deliberate: InkyPi skips the display update when the rendered image is
identical to the one already on screen, so polling every 30 seconds costs one e-ink
refresh per song rather than one every 30 seconds. Don't add a progress indicator.

## Setup

### 1. Create a Home Assistant long-lived access token

In Home Assistant, click your user name in the sidebar, open the **Security** tab, scroll
to **Long-lived access tokens** and click **Create token**. Copy the token — Home
Assistant only shows it once.

### 2. Add the credentials

Add both values on InkyPi's **API Keys** page (or directly to the `.env` file in the
project root):

```
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=your-long-lived-access-token
```

The URL must be reachable from the Raspberry Pi. Use the IP address if `.local`
hostnames do not resolve on your network.

### 3. Configure the plugin

Open the Now Playing plugin in the web UI. Your `media_player` entities are listed
automatically. Tick the ones you want watched, then add the instance to a playlist.

Entities are checked in the order they are listed, and the first one that is playing
wins. Put your **speaker group** above its individual members — casting to a group makes
every member report playing too, and the group entity carries the friendlier name.

## Settings

| Setting | Default | Notes |
| --- | --- | --- |
| Speakers to watch | none | Auto-discovered from Home Assistant |
| Additional Entity IDs | empty | Comma-separated fallback if an entity isn't listed |
| Poll Interval | 30s | How often Home Assistant is checked (minimum 5s) |
| Watch Only | on | Take over the display; stay out of playlist rotation |
| Keep Showing When Paused | on | Off returns to the playlist as soon as you pause |
| Show Speaker and App Name | on | Footer line under the track details |

## Notes and limitations

- Media with no `media_title` is ignored. This filters out TTS announcements and
  doorbell chimes, which would otherwise take over the display for a single poll.
- Album art comes from the media player's `entity_picture` attribute. Most cast apps
  serve fairly low resolution art (~300px), so expect some softness on larger panels.
  The layout falls back to a typography-only variant when there is no art.
- The access token is only sent to the configured Home Assistant host. If an integration
  reports an absolute album-art URL on a third-party CDN, the art is fetched without the
  token attached.
- Requires no extra Python dependencies beyond what InkyPi already installs.

## Status

Actively maintained.
