from io import BytesIO

import pytest
from PIL import Image

import requests

from model import Playlist
from plugins.now_playing import home_assistant
from plugins.now_playing.home_assistant import (
    HomeAssistantError,
    NowPlayingState,
    fetch_artwork,
    find_active,
)
from plugins.now_playing.now_playing import (
    get_entity_ids,
    get_paused_timeout,
    get_poll_interval,
    include_paused,
)
from refresh_task import NowPlayingWatcher

BASE_URL = "http://homeassistant.local:8123"
TOKEN = "test-token"


def make_entity(entity_id, state, title="Song", artist="Artist", **attributes):
    """Builds a Home Assistant /api/states entity dictionary."""
    attrs = {"friendly_name": entity_id, "media_title": title, "media_artist": artist}
    attrs.update(attributes)
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


@pytest.fixture
def states(monkeypatch):
    """Stubs home_assistant.get_state with an in-memory entity table."""
    table = {}

    def fake_get_state(base_url, token, entity_id, timeout=5):
        entity = table.get(entity_id)
        return NowPlayingState.from_entity(entity) if entity else None

    monkeypatch.setattr(home_assistant, "get_state", fake_get_state)
    return table


class TestFindActive:

    def test_returns_the_playing_entity(self, states):
        states["media_player.group"] = make_entity("media_player.group", "idle")
        states["media_player.kitchen"] = make_entity("media_player.kitchen", "playing", title="Teenage Dirtbag")

        state = find_active(BASE_URL, TOKEN, ["media_player.group", "media_player.kitchen"])

        assert state.entity_id == "media_player.kitchen"
        assert state.title == "Teenage Dirtbag"

    def test_respects_entity_order(self, states):
        """Casting to a group makes every member report playing; the group must win."""
        states["media_player.group"] = make_entity("media_player.group", "playing")
        states["media_player.kitchen"] = make_entity("media_player.kitchen", "playing")

        state = find_active(BASE_URL, TOKEN, ["media_player.group", "media_player.kitchen"])

        assert state.entity_id == "media_player.group"

    def test_paused_counts_when_included(self, states):
        states["media_player.group"] = make_entity("media_player.group", "paused")

        assert find_active(BASE_URL, TOKEN, ["media_player.group"], include_paused=True) is not None

    def test_paused_ignored_when_excluded(self, states):
        states["media_player.group"] = make_entity("media_player.group", "paused")

        assert find_active(BASE_URL, TOKEN, ["media_player.group"], include_paused=False) is None

    @pytest.mark.parametrize("title", ["", "   ", None])
    def test_media_without_a_title_is_ignored(self, states, title):
        """Filters out TTS announcements and chimes, which would flash the panel."""
        states["media_player.group"] = make_entity("media_player.group", "playing", title=title)

        assert find_active(BASE_URL, TOKEN, ["media_player.group"]) is None

    def test_idle_and_off_entities_are_ignored(self, states):
        states["media_player.group"] = make_entity("media_player.group", "idle")
        states["media_player.tv"] = make_entity("media_player.tv", "off")

        assert find_active(BASE_URL, TOKEN, ["media_player.group", "media_player.tv"]) is None

    def test_unknown_entity_is_skipped(self, states):
        states["media_player.kitchen"] = make_entity("media_player.kitchen", "playing")

        state = find_active(BASE_URL, TOKEN, ["media_player.typo", "media_player.kitchen"])

        assert state.entity_id == "media_player.kitchen"


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


class TestReachabilityIsNotSilence:
    """An unreachable Home Assistant must never look like "nothing is playing".

    These patch the transport rather than get_state/find_active, so the real error
    handling in between is what gets exercised.
    """

    @pytest.mark.parametrize("failure", [
        ConnectionError("no route to host"),
        requests.Timeout("timed out"),
        http_error(401),
        http_error(500),
    ])
    def test_transport_failures_raise(self, monkeypatch, failure):
        def boom(*args, **kwargs):
            raise failure

        monkeypatch.setattr(home_assistant, "_request_json", boom)

        with pytest.raises(HomeAssistantError):
            find_active(BASE_URL, TOKEN, ["media_player.group"])

    def test_a_missing_entity_is_skipped_rather_than_raising(self, monkeypatch):
        """A typo in one entity id should not take down the whole check."""
        def respond(base_url, token, path, timeout=5):
            if path.endswith("media_player.typo"):
                raise http_error(404)
            return make_entity("media_player.kitchen", "playing")

        monkeypatch.setattr(home_assistant, "_request_json", respond)

        state = find_active(BASE_URL, TOKEN, ["media_player.typo", "media_player.kitchen"])

        assert state.entity_id == "media_player.kitchen"


class TestTrackKey:

    def test_is_stable_across_polls_of_the_same_track(self):
        """The no-flicker rule: identical track -> identical key -> identical image."""
        first = NowPlayingState.from_entity(make_entity("media_player.group", "playing"))
        second = NowPlayingState.from_entity(make_entity("media_player.group", "playing"))

        assert first.track_key() == second.track_key()

    def test_pausing_does_not_change_the_key(self):
        playing = NowPlayingState.from_entity(make_entity("media_player.group", "playing"))
        paused = NowPlayingState.from_entity(make_entity("media_player.group", "paused"))

        assert playing.track_key() == paused.track_key()

    @pytest.mark.parametrize("attribute,value", [
        ("title", "Another Song"),
        ("artist", "Another Artist"),
        ("album", "Another Album"),
    ])
    def test_changes_when_the_track_changes(self, attribute, value):
        first = NowPlayingState.from_entity(make_entity("media_player.group", "playing"))
        second = NowPlayingState.from_entity(make_entity("media_player.group", "playing"))
        setattr(second, attribute, value)

        assert first.track_key() != second.track_key()

    def test_round_trips_through_a_dict(self):
        """The watcher hands the state to the plugin as a dict."""
        state = NowPlayingState.from_entity(make_entity("media_player.group", "playing"))

        assert NowPlayingState.from_dict(state.to_dict()) == state


class FakeSession:
    """Captures the headers of the last request and returns a small PNG."""

    def __init__(self):
        self.requested_url = None
        self.requested_headers = None

        buffer = BytesIO()
        Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
        self.content = buffer.getvalue()

    def get(self, url, headers=None, timeout=None):
        self.requested_url = url
        self.requested_headers = headers or {}
        return self

    def raise_for_status(self):
        pass


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(home_assistant, "get_http_session", lambda: fake)
    return fake


class TestFetchArtwork:

    def test_sends_the_token_to_home_assistant(self, session):
        image = fetch_artwork(BASE_URL, TOKEN, "/api/media_player_proxy/media_player.group?token=abc")

        assert image is not None
        assert session.requested_url == f"{BASE_URL}/api/media_player_proxy/media_player.group?token=abc"
        assert session.requested_headers["Authorization"] == f"Bearer {TOKEN}"

    def test_never_sends_the_token_to_a_third_party_host(self, session):
        """Some integrations report album art on an external CDN."""
        fetch_artwork(BASE_URL, TOKEN, "https://i.scdn.co/image/abc123")

        assert session.requested_url == "https://i.scdn.co/image/abc123"
        assert "Authorization" not in session.requested_headers

    def test_returns_none_without_artwork(self, session):
        assert fetch_artwork(BASE_URL, TOKEN, "") is None
        assert session.requested_url is None


class TestListMediaPlayers:

    def test_returns_an_error_rather_than_raising_when_unreachable(self, monkeypatch):
        """The settings page has to render even when Home Assistant is down."""
        def boom(*args, **kwargs):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(home_assistant, "_request_json", boom)
        players, error = home_assistant.list_media_players(BASE_URL, TOKEN)

        assert players == []
        assert "Could not reach Home Assistant" in error

    def test_reports_missing_credentials(self):
        players, error = home_assistant.list_media_players("", "")

        assert players == []
        assert "HOME_ASSISTANT_URL" in error

    def test_filters_to_media_players_and_lists_usable_ones_first(self, monkeypatch):
        """The picker has to surface whatever is actually playing, since that is the
        entity the user needs to tick."""
        monkeypatch.setattr(home_assistant, "_request_json", lambda *a, **k: [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
            {"entity_id": "media_player.zone", "state": "idle",
             "attributes": {"friendly_name": "Zone"}},
            # Playing but no title: the speaker-group / Spotify Connect case.
            {"entity_id": "media_player.group", "state": "playing",
             "attributes": {"friendly_name": "Speakers", "app_name": "Spotify"}},
            {"entity_id": "media_player.attic", "state": "playing",
             "attributes": {"friendly_name": "Attic", "media_title": "Idioteque",
                            "media_artist": "Radiohead"}},
        ])

        players, error = home_assistant.list_media_players(BASE_URL, TOKEN)

        assert error == ""
        assert [p["entity_id"] for p in players] == [
            "media_player.attic",   # usable: playing with a title
            "media_player.group",   # active but untitled
            "media_player.zone",    # idle
        ]

        usable, untitled, idle = players
        assert (usable["usable"], usable["active"]) == (True, True)
        assert usable["title"] == "Idioteque"
        assert (untitled["usable"], untitled["active"]) == (False, True)
        assert untitled["app_name"] == "Spotify"
        assert (idle["usable"], idle["active"]) == (False, False)

    def test_picker_agrees_with_find_active(self, monkeypatch):
        """Anything the picker marks usable must be something find_active would pick,
        otherwise the settings page recommends entities that render nothing."""
        entities = [
            {"entity_id": "media_player.group", "state": "playing",
             "attributes": {"friendly_name": "Speakers"}},
            {"entity_id": "media_player.attic", "state": "paused",
             "attributes": {"friendly_name": "Attic", "media_title": "Idioteque"}},
            {"entity_id": "media_player.zone", "state": "off", "attributes": {}},
        ]
        by_id = {e["entity_id"]: e for e in entities}
        monkeypatch.setattr(home_assistant, "_request_json", lambda base, tok, path, timeout=5:
                            entities if path == "/api/states" else by_id[path.rsplit("/", 1)[-1]])

        players, _ = home_assistant.list_media_players(BASE_URL, TOKEN)

        for player in players:
            found = find_active(BASE_URL, TOKEN, [player["entity_id"]], include_paused=True)
            assert (found is not None) == player["usable"], player["entity_id"]


class TestSettingsParsing:

    def test_reads_the_checkbox_list(self):
        settings = {"entityIds[]": ["media_player.group", "media_player.kitchen"]}

        assert get_entity_ids(settings) == ["media_player.group", "media_player.kitchen"]

    def test_reads_a_single_checkbox_posted_as_a_string(self):
        assert get_entity_ids({"entityIds[]": "media_player.group"}) == ["media_player.group"]

    def test_appends_manual_entity_ids_without_duplicating(self):
        settings = {
            "entityIds[]": ["media_player.group"],
            "manualEntityIds": " media_player.group , media_player.office ,",
        }

        assert get_entity_ids(settings) == ["media_player.group", "media_player.office"]

    def test_no_selection(self):
        assert get_entity_ids({}) == []

    @pytest.mark.parametrize("value,expected", [
        (None, 30), ("", 30), ("45", 45), (45, 45), ("not a number", 30), ("1", 5), ("-10", 5),
    ])
    def test_poll_interval_is_clamped(self, value, expected):
        settings = {} if value is None else {"pollInterval": value}

        assert get_poll_interval(settings) == expected

    @pytest.mark.parametrize("value,expected", [
        (None, True), ("true", True), ("false", False), ("False", False),
    ])
    def test_include_paused(self, value, expected):
        settings = {} if value is None else {"includePaused": value}

        assert include_paused(settings) is expected

    @pytest.mark.parametrize("value,expected", [
        (None, 15 * 60),        # default
        ("30", 30 * 60),
        (30, 30 * 60),
        ("0", 0),               # hold indefinitely
        ("-5", 0),
        ("not a number", 15 * 60),
    ])
    def test_paused_timeout_in_seconds(self, value, expected):
        settings = {} if value is None else {"pausedTimeout": value}

        assert get_paused_timeout(settings) == expected


def make_playlist(*plugins):
    return Playlist("Default", "00:00", "24:00", plugins=[
        {"plugin_id": plugin_id, "name": name, "plugin_settings": settings, "refresh": {}}
        for plugin_id, name, settings in plugins
    ])


class TestWatchOnlyRotation:

    def test_watch_only_instances_are_skipped(self):
        playlist = make_playlist(
            ("now_playing", "Speakers", {"watchOnly": "true"}),
            ("clock", "Clock", {}),
            ("weather", "Weather", {}),
        )

        names = [playlist.get_next_plugin().name for _ in range(4)]

        assert names == ["Clock", "Weather", "Clock", "Weather"]

    def test_returns_none_when_every_instance_is_watch_only(self):
        playlist = make_playlist(("now_playing", "Speakers", {"watchOnly": "true"}))

        assert playlist.get_next_plugin() is None

    def test_watch_only_off_stays_in_the_rotation(self):
        playlist = make_playlist(
            ("now_playing", "Speakers", {"watchOnly": "false"}),
            ("clock", "Clock", {}),
        )

        names = [playlist.get_next_plugin().name for _ in range(2)]

        assert sorted(names) == ["Clock", "Speakers"]


class StubInstance:
    """Minimal stand-in for a PluginInstance."""

    def __init__(self, settings=None):
        self.plugin_id = "now_playing"
        self.name = "Speakers"
        self.settings = settings or {
            "entityIds[]": ["media_player.group"],
            "watchOnly": "true",
        }


@pytest.fixture
def watcher(monkeypatch):
    monkeypatch.setattr(
        "plugins.now_playing.now_playing.load_home_assistant_env",
        lambda: (BASE_URL, TOKEN),
    )
    return NowPlayingWatcher(device_config=None)


class TestNowPlayingWatcher:

    def test_reports_the_active_track(self, watcher, states):
        states["media_player.group"] = make_entity("media_player.group", "playing")

        state, just_stopped = watcher.poll(StubInstance())

        assert state.title == "Song"
        assert just_stopped is False

    def test_asks_for_the_playlist_back_when_playback_stops(self, watcher, states):
        states["media_player.group"] = make_entity("media_player.group", "playing")
        watcher.poll(StubInstance())

        states["media_player.group"] = make_entity("media_player.group", "idle")

        assert watcher.poll(StubInstance())[1] is True

    def test_keeps_asking_until_the_playlist_actually_takes_over(self, watcher, states):
        """A one-shot signal strands the last track on the panel whenever the resume
        refresh does not land - no active playlist, nothing but watch-only instances."""
        states["media_player.group"] = make_entity("media_player.group", "playing")
        watcher.poll(StubInstance())
        states["media_player.group"] = make_entity("media_player.group", "idle")

        assert watcher.poll(StubInstance())[1] is True
        assert watcher.poll(StubInstance())[1] is True, "resume must be retried, not dropped"
        assert watcher.poll(StubInstance())[1] is True

        watcher.mark_released()

        assert watcher.poll(StubInstance())[1] is False, "stop asking once the playlist has it"

    def test_does_not_ask_for_a_resume_it_never_took_over_for(self, watcher, states):
        states["media_player.group"] = make_entity("media_player.group", "idle")

        assert watcher.poll(StubInstance()) == (None, False)


class TestPausedHold:
    """A player parked in `paused` must not own the display forever.

    Cast groups and Music Assistant players routinely sit in `paused` rather than going
    idle when playback stops, which is the difference between "keep the track up while I
    grab a drink" and "the panel is stuck on last night's album".
    """

    def paused_instance(self, timeout_minutes):
        return StubInstance(settings={
            "entityIds[]": ["media_player.group"],
            "watchOnly": "true",
            "pausedTimeout": str(timeout_minutes),
        })

    def test_a_short_pause_keeps_the_track_on_screen(self, watcher, states, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(home_assistant, "get_state", lambda *a, **k: NowPlayingState.from_entity(
            make_entity("media_player.group", "paused")))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(15)
        assert watcher.poll(instance)[0] is not None

        clock[0] += 14 * 60
        state, should_resume = watcher.poll(instance)
        assert state is not None, "still paused within the hold window"
        assert should_resume is False

    def test_a_long_pause_releases_the_display(self, watcher, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(home_assistant, "get_state", lambda *a, **k: NowPlayingState.from_entity(
            make_entity("media_player.group", "paused")))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(15)
        watcher.poll(instance)

        clock[0] += 16 * 60
        state, should_resume = watcher.poll(instance)
        assert state is None, "an indefinitely paused player must give the display back"
        assert should_resume is True

    def test_resuming_playback_restarts_the_clock(self, watcher, monkeypatch):
        clock = [1000.0]
        entity = {"value": make_entity("media_player.group", "paused")}
        monkeypatch.setattr(home_assistant, "get_state",
                            lambda *a, **k: NowPlayingState.from_entity(entity["value"]))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(15)
        watcher.poll(instance)

        clock[0] += 14 * 60
        entity["value"] = make_entity("media_player.group", "playing")
        watcher.poll(instance)

        clock[0] += 60
        entity["value"] = make_entity("media_player.group", "paused")
        watcher.poll(instance)
        clock[0] += 14 * 60

        assert watcher.poll(instance)[0] is not None, "the pause window restarts after playing"

    def test_a_released_pause_is_not_picked_straight_back_up(self, watcher, monkeypatch):
        """Re-acquiring a still-paused player flips the panel once per timeout period,
        which is worse than the stuck display it was meant to fix."""
        clock = [1000.0]
        monkeypatch.setattr(home_assistant, "get_state", lambda *a, **k: NowPlayingState.from_entity(
            make_entity("media_player.group", "paused")))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(15)
        watcher.poll(instance)
        clock[0] += 16 * 60
        assert watcher.poll(instance)[0] is None
        watcher.mark_released()  # the playlist has taken the display back

        for _ in range(3):
            clock[0] += 16 * 60
            state, should_resume = watcher.poll(instance)
            assert state is None, "a still-paused player must stay released"
            assert should_resume is False, "and must not keep demanding the playlist"

    def test_pressing_play_again_takes_the_display_back(self, watcher, monkeypatch):
        clock = [1000.0]
        entity = {"value": make_entity("media_player.group", "paused")}
        monkeypatch.setattr(home_assistant, "get_state",
                            lambda *a, **k: NowPlayingState.from_entity(entity["value"]))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(15)
        watcher.poll(instance)
        clock[0] += 16 * 60
        watcher.poll(instance)
        watcher.mark_released()

        entity["value"] = make_entity("media_player.group", "playing")

        assert watcher.poll(instance)[0] is not None, "resuming playback must take over again"

    def test_zero_holds_indefinitely(self, watcher, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(home_assistant, "get_state", lambda *a, **k: NowPlayingState.from_entity(
            make_entity("media_player.group", "paused")))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(0)
        watcher.poll(instance)

        clock[0] += 24 * 60 * 60
        assert watcher.poll(instance)[0] is not None

    def test_playing_is_never_timed_out(self, watcher, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(home_assistant, "get_state", lambda *a, **k: NowPlayingState.from_entity(
            make_entity("media_player.group", "playing")))
        monkeypatch.setattr("refresh_task.time.monotonic", lambda: clock[0])

        instance = self.paused_instance(15)
        watcher.poll(instance)

        clock[0] += 10 * 60 * 60
        assert watcher.poll(instance)[0] is not None, "a long album is not a stalled pause"

    def test_an_outage_is_not_treated_as_playback_stopping(self, watcher, monkeypatch):
        """Otherwise a network blip would flap the display off the track and back."""
        monkeypatch.setattr(
            home_assistant, "_request_json",
            lambda *a, **k: make_entity("media_player.group", "playing"),
        )
        assert watcher.poll(StubInstance())[0] is not None

        def boom(*args, **kwargs):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(home_assistant, "_request_json", boom)

        assert watcher.poll(StubInstance()) == (None, False)

    def test_playback_still_resumes_the_playlist_after_an_outage(self, watcher, monkeypatch):
        """The stop transition must survive an outage in the middle of it."""
        monkeypatch.setattr(
            home_assistant, "_request_json",
            lambda *a, **k: make_entity("media_player.group", "playing"),
        )
        watcher.poll(StubInstance())

        def boom(*args, **kwargs):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(home_assistant, "_request_json", boom)
        watcher.poll(StubInstance())

        # Home Assistant comes back, and by then the music really has stopped.
        monkeypatch.setattr(
            home_assistant, "_request_json",
            lambda *a, **k: make_entity("media_player.group", "idle"),
        )

        assert watcher.poll(StubInstance())[1] is True

    def test_no_instance_configured(self, watcher):
        assert watcher.poll(None) == (None, False)

    def test_instance_without_entities_is_ignored(self, watcher):
        assert watcher.poll(StubInstance(settings={"watchOnly": "true"})) == (None, False)
