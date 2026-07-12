"""Unit tests for FastRouter — pure regex routing, no external deps."""

from action.fast_router import FastRouter, RouteResult


def r():
    return FastRouter()


class TestRouting:
    def test_time(self):
        assert r().route("what time is it").tool == "get_time"

    def test_system_info(self):
        assert r().route("system status").tool == "system_info"

    def test_weather_with_city(self):
        res = r().route("weather in London")
        assert res.tool == "weather" and res.args["city"].lower() == "london"

    def test_weather_bare_defaults_city(self):
        res = r().route("what's the weather")
        assert res.tool == "weather" and res.args["city"]

    def test_launch_app(self):
        res = r().route("open chrome")
        assert res.tool == "open_app" and "chrome" in res.args["command"]

    def test_launch_url(self):
        res = r().route("open youtube")
        assert res.tool == "open_url" and "youtube" in res.args["url"]

    def test_list_goals(self):
        assert r().route("what are my goals").tool == "list_goals"

    def test_unknown_falls_through(self):
        assert r().route("explain quantum entanglement to me") is None

    def test_reminder_is_not_launch(self):
        # "^open" guard: "remind me to open the door" must not launch anything
        assert r().route("remind me to open the door") is None


class TestCompound:
    def test_compound_returns_list(self):
        res = r().route("open chrome and open spotify")
        assert isinstance(res, list) and len(res) == 2

    def test_single_arm_not_wrapped(self):
        res = r().route("open chrome and then dance")
        assert isinstance(res, RouteResult)  # only one arm routed


class TestDurationParsing:
    def test_minutes(self):
        assert r()._parse_duration("10 minutes") == 600

    def test_compound_hours_minutes(self):
        # Regression: audit fix #4 — search() gave 3600, finditer() gives 5400
        assert r()._parse_duration("1 hour 30 minutes") == 5400

    def test_compact(self):
        assert r()._parse_duration("1h30m") == 5400

    def test_half_hour(self):
        assert r()._parse_duration("half an hour") == 1800

    def test_seconds(self):
        assert r()._parse_duration("90 seconds") == 90

    def test_no_duration(self):
        assert r()._parse_duration("banana") is None

    def test_timer_route_end_to_end(self):
        res = r().route("set a timer for 2 hours 30 minutes")
        assert res.tool == "set_timer" and res.args["duration_seconds"] == 9000
