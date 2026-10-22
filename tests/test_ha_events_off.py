"""HA_FIRE_EVENTS=false: no event POST, so a non-admin token logs no 401.

``POST /api/events`` needs an admin token in Home Assistant; a non-admin one
(the right kind for Murdock) is answered 401 on every utterance. The helper
and entity pushes are service calls a non-admin token may make, and go on.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from murdock.config import Settings
from murdock.core.ha_integration import HomeAssistantClient


def _push(fire_events=None):
    requests = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path.startswith("/api/events/"):
            return httpx.Response(401, text="401: Unauthorized")
        return httpx.Response(200, json=[])

    kwargs = {} if fire_events is None else {"fire_events": fire_events}
    ha = HomeAssistantClient(
        "http://ha.local:8123", "token", input_text_entity="input_text.charlie_speaker",
        **kwargs,
    )
    ha._client = httpx.AsyncClient(
        base_url="http://ha.local:8123", transport=httpx.MockTransport(answer),
    )
    asyncio.run(ha.push_recognition(
        speaker="rob", confidence=0.9, satellite_id="ipad", is_known=True,
    ))
    return requests


def test_off_skips_the_event_and_keeps_the_speaker_helper(caplog):
    with caplog.at_level(logging.WARNING, logger="murdock.ha"):
        requests = _push(fire_events=False)

    assert requests == [
        "POST /api/services/input_text/set_value",  # cleared first
        "POST /api/services/input_text/set_value",  # then the speaker
    ]
    assert "401" not in caplog.text


def test_on_is_upstreams_behaviour(caplog):
    with caplog.at_level(logging.WARNING, logger="murdock.ha"):
        requests = _push()

    assert requests[-1] == "POST /api/events/speaker_recognition_detected"
    assert "HA event fire failed (401)" in caplog.text


def test_the_setting_is_read_from_the_environment(monkeypatch):
    assert Settings().ha_fire_events is True
    monkeypatch.setenv("HA_FIRE_EVENTS", "false")
    assert Settings().ha_fire_events is False
