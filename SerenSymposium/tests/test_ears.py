"""
Ears: push-to-talk through the microphone, recorded here; the clip kept
beside the chat, its words from whisper THROUGH Lodestar.

The input is scripted (seren_symposium.ears has two seams, what the input
can do and the stream); Lodestar is a MockTransport. Pinned:

- start opens the input the yaml names (else the default), stop closes it
  and answers with the clip kept in the chat (a WAV, marked `ears`, with
  how long it is) and the words Lodestar's /api/v1/hear gave back - the
  clip went up as the multipart it takes, in Symposium's name
- the clip rides with a message like any file, and whoever answers is told
  it is a recording; served back, it plays (no download disposition)
- a Lodestar that cannot hear it still leaves the clip, and says why
- keep: false keeps nothing; too short is not sent; cancel keeps nothing
- off, unavailable, not listening, already listening: 409s that say so;
  a clip with nowhere to go (no such chat) is a 404
- the clip stops growing at max_seconds and says it was cut
"""
from __future__ import annotations

import io
import json
import re
import wave

import httpx
import pytest

from seren_symposium import ears
from seren_symposium.config import SymposiumConfig

RATE, CH = 16000, 1


class FakeStream:
    """What PortAudio would hand us: `feed(seconds)` plays that much silence
    into the recorder's callback."""

    def __init__(self, callback):
        self.callback, self.stopped, self.closed = callback, False, False

    def feed(self, seconds: float, rate: int = RATE, channels: int = CH):
        self.callback(b"\0" * int(seconds * rate * channels * 2))

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


@pytest.fixture
def mic(monkeypatch):
    """An input called "USB Microphone" (index 3) that can do 16 kHz mono,
    and the default input (index 1)."""
    box = {"stream": None, "opened": []}
    listing = [{"index": 1, "name": "Microphone (Realtek)", "channels": 2, "rate": 48000, "api": "WASAPI", "default": True},
               {"index": 3, "name": "USB Microphone", "channels": 1, "rate": 44100, "api": "WASAPI", "default": False}]
    monkeypatch.setattr(ears, "available", lambda: True)
    monkeypatch.setattr(ears, "devices", lambda: listing)

    def settings(device):
        idx = 1 if device is None else device
        hit = next((d for d in listing if d["index"] == idx), None)
        if hit is None:
            raise ValueError(f"Error querying device {idx}")
        return {"rate": RATE, "channels": CH, "name": hit["name"], "index": idx}

    def open_stream(device, rate, channels, callback):
        box["opened"].append((device, rate, channels))
        box["stream"] = FakeStream(callback)
        return box["stream"]

    monkeypatch.setattr(ears, "_settings", settings)
    monkeypatch.setattr(ears, "_open_stream", open_stream)
    return box


def lodestar(tc, text=" Hello, Wren.", status=200):
    """A Lodestar whose /api/v1/hear is scripted; returns what it was sent."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": request.read(),
                     "content_type": request.headers.get("content-type", ""),
                     "auth": request.headers.get("authorization", "")})
        if status != 200:
            return httpx.Response(status, json={"error": "whisper did not come up" if status == 503 else "unauthorized"})
        return httpx.Response(200, json={"ok": True, "text": text, "node": "jetson", "seconds": 0.8, "model": "small.en"})
    tc.app.state.client = httpx.AsyncClient(base_url="http://127.0.0.1:6361", transport=httpx.MockTransport(handle),
                                            headers={"Authorization": "Bearer sym-token"})
    return seen


def config(**ears_kw) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.lodestar.bearer_token = "sym-token"
    for k, v in ears_kw.items():
        setattr(cfg.ears, k, v)
    return cfg


def wav_info(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


# ── the whole thing ──────────────────────────────────────────────────────────

def test_hold_say_release_the_clip_is_kept_and_the_words_come_back(make_client, mic, tmp_path):
    tc = make_client(config(device="usb micro"))
    seen = lodestar(tc)
    sid = tc.post("/api/sessions", json={"folder": ""}).json()["id"]

    assert tc.get("/api/config").json()["ears"] == {
        "enabled": True, "available": True, "device": "usb micro", "language": "en", "max_seconds": 120.0,
        "devices": ears.devices()}
    assert tc.get("/api/ears").json()["recording"] is False

    r = tc.post("/api/ears/start", json={})
    assert r.status_code == 200, r.text
    assert r.json()["device"] == "USB Microphone" and mic["opened"] == [(3, RATE, CH)], "the yaml's input, 16 kHz mono"
    assert tc.get("/api/ears").json()["recording"] is True
    mic["stream"].feed(1.5)

    out = tc.post("/api/ears/stop", json={"session": sid}).json()
    assert out["text"] == "Hello, Wren." and out["node"] == "jetson" and "error" not in out
    assert out["seconds"] == 1.5 and out["device"] == "USB Microphone" and out["clipped"] is False
    assert mic["stream"].stopped and mic["stream"].closed
    f = out["file"]
    assert re.fullmatch(r"ears-\d{8}-\d{6}\.wav", f["orig"]) and f["type"] == "audio/wav"
    assert f["ears"] is True and f["seconds"] == 1.5 and f["device"] == "USB Microphone"
    data = (tmp_path / "sessions" / f"{sid}.files" / f["name"]).read_bytes()
    assert wav_info(data) == (RATE, CH, 2, RATE * 3 // 2)

    (sent,) = seen
    assert sent["path"] == "/api/v1/hear" and sent["auth"] == "Bearer sym-token", "to Lodestar, with the bearer"
    assert sent["content_type"].startswith("multipart/form-data")
    for part in (b'name="file"; filename="clip.wav"', data, b'name="language"\r\n\r\nen', b'name="holder"\r\n\r\nsymposium'):
        assert part in sent["body"], part[:40]

    # served back, it plays: no download disposition, still nosniff
    got = tc.get(f"/api/sessions/{sid}/files/{f['name']}")
    assert got.status_code == 200 and got.headers["content-type"].startswith("audio/")
    assert "content-disposition" not in got.headers and got.headers["x-content-type-options"] == "nosniff"


def test_the_clip_rides_with_a_message_and_the_voice_is_told_what_it_is(make_client, mic, tmp_path):
    from seren_symposium import attachments
    from seren_symposium.sessions import SessionStore
    tc = make_client(config())
    lodestar(tc)
    sid = tc.post("/api/sessions", json={"folder": ""}).json()["id"]
    tc.post("/api/ears/start", json={})
    mic["stream"].feed(2)
    f = tc.post("/api/ears/stop", json={"session": sid}).json()["file"]
    # the message names the stored entry, as the shell does; the mark and the length survive the trip
    from seren_symposium.app import _named_files
    store = SessionStore(tmp_path / "sessions")
    (entry,) = _named_files(store, sid, [f])
    assert entry["ears"] is True and entry["seconds"] == 2.0 and entry["device"] == "Microphone (Realtek)"
    assert "a recording from the microphone, 2s" in attachments.for_claude([entry], tmp_path)
    assert "[attached a recording from the microphone:" in attachments.for_local([entry], store, sid, 1000)


def test_a_lodestar_that_cannot_hear_still_leaves_the_clip_and_says_why(make_client, mic):
    tc = make_client(config())
    lodestar(tc, status=503)
    sid = tc.post("/api/sessions", json={"folder": ""}).json()["id"]
    tc.post("/api/ears/start", json={})
    mic["stream"].feed(1)
    out = tc.post("/api/ears/stop", json={"session": sid}).json()
    assert out["file"]["ears"] is True and out["text"] == "" and out["error"] == "whisper did not come up"

    lodestar(tc, status=401)
    tc.post("/api/ears/start", json={})
    mic["stream"].feed(1)
    out = tc.post("/api/ears/stop", json={"session": sid}).json()
    assert out["error"] == "lodestar rejected our bearer token" and "hint" in out

    def down(request):
        raise httpx.ConnectError("no", request=request)
    tc.app.state.client = httpx.AsyncClient(base_url="http://127.0.0.1:6361", transport=httpx.MockTransport(down))
    tc.post("/api/ears/start", json={})
    mic["stream"].feed(1)
    out = tc.post("/api/ears/stop", json={"session": sid}).json()
    assert out["file"] and out["error"].startswith("lodestar unreachable")


def test_keep_false_keeps_nothing_too_short_is_not_sent_and_cancel_drops_it(make_client, mic, tmp_path):
    tc = make_client(config())
    seen = lodestar(tc)
    sid = tc.post("/api/sessions", json={"folder": ""}).json()["id"]

    tc.post("/api/ears/start", json={})
    mic["stream"].feed(1)
    out = tc.post("/api/ears/stop", json={"session": sid, "keep": False}).json()
    assert "file" not in out and out["text"] == "Hello, Wren." and len(seen) == 1
    assert not list((tmp_path / "sessions").glob(f"{sid}.files/*"))

    tc.post("/api/ears/start", json={})
    mic["stream"].feed(0.1)
    out = tc.post("/api/ears/stop", json={"session": sid}).json()
    assert out["file"] and out["text"] == "" and out["error"] == "too short to hear" and len(seen) == 1

    tc.post("/api/ears/start", json={"device": 3})
    mic["stream"].feed(1)
    assert tc.post("/api/ears/cancel", json={}).json() == {"ok": True, "recording": False}
    assert mic["stream"].closed and tc.get("/api/ears").json()["recording"] is False
    assert len(list((tmp_path / "sessions").glob(f"{sid}.files/*"))) == 1 and len(seen) == 1


def test_refusals_say_what_is_wrong(make_client, mic, monkeypatch):
    tc = make_client(config())
    lodestar(tc)
    assert tc.post("/api/ears/stop", json={}).status_code == 409, "not listening"
    assert tc.post("/api/ears/start", json={"device": "mic"}).status_code == 400
    assert tc.post("/api/ears/start", json={"device": 99}).status_code == 409, "no such input"
    assert tc.post("/api/ears/start", json={}).status_code == 200
    assert tc.post("/api/ears/start", json={}).status_code == 409, "already listening"
    mic["stream"].feed(1)
    r = tc.post("/api/ears/stop", json={"session": "nope"})
    assert r.status_code == 404 and "no such session" in r.json()["error"]
    assert tc.get("/api/ears").json()["recording"] is False, "closed all the same"

    off = make_client(config(enabled=False))
    assert off.post("/api/ears/start", json={}).status_code == 409 and off.get("/api/config").json()["ears"]["devices"] == []

    monkeypatch.setattr(ears, "available", lambda: False)
    r = tc.post("/api/ears/start", json={})
    assert r.status_code == 409 and "seren-symposium[ears]" in r.json()["hint"]


def test_a_clip_stops_growing_at_max_seconds_and_says_so(mic):
    rec = ears.Recorder(max_seconds=2)
    rec.start(None)
    mic["stream"].feed(5)
    clip = rec.stop()
    assert clip.clipped and clip.seconds == 2.0 and wav_info(clip.wav)[3] == 2 * RATE


def test_the_devices_list_is_one_per_name_with_the_default_marked(monkeypatch):
    """What PortAudio sees on a Windows box: the same microphone through
    four host APIs; the one the default input uses is the one kept."""
    import sys
    import types
    fake = types.SimpleNamespace()
    fake.query_devices = lambda idx=None: [
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "default_samplerate": 44100.0, "hostapi": 0},
        {"name": "USB Microphone", "max_input_channels": 1, "default_samplerate": 44100.0, "hostapi": 0},
        {"name": "Speakers", "max_input_channels": 0, "default_samplerate": 44100.0, "hostapi": 0},
        {"name": "USB Microphone", "max_input_channels": 2, "default_samplerate": 48000.0, "hostapi": 2},
        {"name": "Stereo Mix (Realtek)", "max_input_channels": 2, "default_samplerate": 48000.0, "hostapi": 3},
        {"name": "Microphone (Realtek HD Audio M", "max_input_channels": 2, "default_samplerate": 44100.0, "hostapi": 0},
        {"name": "Microphone (Realtek HD Audio Mic input)", "max_input_channels": 2, "default_samplerate": 44100.0, "hostapi": 2},
    ]
    fake.query_hostapis = lambda: [{"name": "MME"}, {"name": "DirectSound"}, {"name": "Windows WASAPI"}, {"name": "WDM-KS"}]
    fake.default = types.SimpleNamespace(device=[5, 2])      # the default input is MME's cut-short twin
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    assert ears.available()
    assert ears.devices() == [
        {"index": 0, "name": "Microsoft Sound Mapper - Input", "channels": 2, "rate": 44100, "api": "MME", "default": False},
        # the same name through two APIs: the first on the default input's API is the one kept
        {"index": 1, "name": "USB Microphone", "channels": 1, "rate": 44100, "api": "MME", "default": False},
        {"index": 6, "name": "Microphone (Realtek HD Audio Mic input)", "channels": 2, "rate": 44100, "api": "Windows WASAPI", "default": True},
        {"index": 4, "name": "Stereo Mix (Realtek)", "channels": 2, "rate": 48000, "api": "WDM-KS", "default": False},
    ]
    assert ears.pick("stereo mix") == 4 and ears.pick("") is None and ears.pick("nope") is None
