"""The transcript is of the whole utterance; only the embedding is trimmed.

``skip_leading_seconds`` (and extraction) exist to keep a satellite's wake
chime, or a second voice, out of the *voiceprint*. Upstream handed the same
trimmed audio to the speech to text, so a command lost its first second:
"apaga la cochera" came back as "la cochera". The speaker is identified on
the trimmed audio; the words are of everything that was said.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from murdock.config import Settings
from murdock.core.context import AppContext
from murdock.core.db import open_db
from murdock.core.recognition_log import RecognitionLog
from wyoming_murdock.handler import MurdockHandler

_RATE = 16000
_SECOND = _RATE * 2  # bytes of 16 kHz mono 16-bit audio
_UTTERANCE = bytes(range(256)) * (4 * _SECOND // 256)  # four seconds, no repeats at the seam


class _Embedder:
    def __init__(self):
        self.heard = []

    def embed_pcm(self, pcm):
        self.heard.append(pcm)
        return [0.0]


class _Speakers:
    def __init__(self, *, matches):
        self._matches = matches

    def list_speakers(self):
        return [SimpleNamespace(name="rob", role=None)]

    def get_speaker(self, speaker_id):
        return SimpleNamespace(name="rob", role=None)

    def verify_embedding(self, embedding, threshold, **kwargs):
        return SimpleNamespace(
            is_match=self._matches, matched_speaker="rob" if self._matches else None,
            matched_speaker_id=1 if self._matches else None,
            distance=0.1 if self._matches else 0.9, threshold=threshold,
            all_distances={"rob": 0.1 if self._matches else 0.9},
            used_satellite_profile=False,
        )


def _session(tmp_path, monkeypatch, *, matches, skip_seconds=1.0):
    db = open_db(tmp_path / "m.db")
    embedder = _Embedder()
    ctx = AppContext(
        settings=Settings(), db=db, embedder=embedder, vad=None,
        speakers=_Speakers(matches=matches), unknown=None,
        ha=SimpleNamespace(configured=False), mqtt=SimpleNamespace(connected=False),
        recognition=RecognitionLog(db),
    )
    ctx.stt_services.create("Main", "voxtral", {"api_key": "k"})
    ctx.set_skip_leading_seconds(skip_seconds)
    ctx.set_require_match(False)
    ctx.set_enable_extraction(False)
    monkeypatch.setattr(ctx, "detect_whisper", lambda audio: None)
    monkeypatch.setattr(ctx, "get_min_liveness_score", lambda: 0.0)

    async def no_media(satellite_id, area):
        return 0.0

    monkeypatch.setattr(ctx, "compute_media_tightening", no_media)

    handler = MurdockHandler(
        SimpleNamespace(upstream_uri="tcp://unused:1"), ctx, SimpleNamespace(), SimpleNamespace(),
    )
    transcribed, shadowed, sent = [], [], []

    async def transcript(audio_16k):
        transcribed.append(audio_16k)
        return "apaga la cochera"

    async def send(text, *, label):
        sent.append(text)

    async def close(*, send_stop):
        return None

    monkeypatch.setattr(handler, "_get_transcript", transcript)
    monkeypatch.setattr(handler, "_send_transcript_to_satellite", send)
    monkeypatch.setattr(handler, "_close_upstream", close)
    monkeypatch.setattr(handler, "_spawn_shadow", lambda event_id, audio: shadowed.append(audio))
    handler._audio_buffer = bytearray(_UTTERANCE)
    handler._stream_start = time.monotonic()
    return handler, embedder, transcribed, shadowed, sent


@pytest.mark.parametrize("matches", [True, False], ids=["recognised", "unknown"])
def test_the_transcript_is_of_the_whole_utterance_and_the_embedding_of_the_trimmed(
    tmp_path, monkeypatch, matches,
):
    handler, embedder, transcribed, shadowed, sent = _session(
        tmp_path, monkeypatch, matches=matches,
    )

    asyncio.run(handler._finish_session())

    assert transcribed == [_UTTERANCE]
    assert embedder.heard == [_UTTERANCE[_SECOND:]]
    assert shadowed == [_UTTERANCE]
    assert sent == ["apaga la cochera"]


def test_with_nothing_to_trim_both_hear_the_same_audio(tmp_path, monkeypatch):
    handler, embedder, transcribed, _, _ = _session(
        tmp_path, monkeypatch, matches=True, skip_seconds=0.0,
    )

    asyncio.run(handler._finish_session())

    assert transcribed == [_UTTERANCE]
    assert embedder.heard == [_UTTERANCE]
