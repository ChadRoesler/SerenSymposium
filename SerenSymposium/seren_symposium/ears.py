"""
seren_symposium.ears
════════════════════════════════════════════════════════════════════════

Push-to-talk: the microphone, recorded here, handed to the chat two ways -
its words, put in the composer for you to send or fix, and the clip itself,
kept beside the chat like any other file so what was said is not only what
whisper made of it.

    hold      POST /api/ears/start opens the input (sounddevice, PortAudio)
    release   POST /api/ears/stop closes it: the WAV is kept in the chat
              (`keep`), and goes to Lodestar's `hear` route, which wakes
              whisper on a node that has it and answers with the words

WHY HERE AND NOT IN THE WEBVIEW:
    getUserMedia asks for permission and is patchy inside a webview, the
    same reason screen share is taken in Python (seren_symposium.screen).
    PortAudio is one call on Windows, macOS and Linux, and it sees every
    input the OS has - a microphone, and on Windows the "Stereo Mix" or a
    loopback device that is what the desktop is playing, which is how a
    song gets heard and not only described. It is the [ears] extra (part
    of [desktop]); without sounddevice the panel says so, nothing breaks.

WHAT IS RECORDED:
    16 kHz mono 16-bit when the input can (whisper's own rate, the smallest
    file), else the input's own rate and channels; whisper.cpp resamples. A
    clip stops growing at ears.max_seconds and says it was cut.
"""
from __future__ import annotations

import io
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable, Optional

PREFERRED_RATE = 16000
PREFERRED_CHANNELS = 1
SAMPLE_BYTES = 2                    # int16


def available() -> bool:
    try:
        import sounddevice  # noqa: F401
    except (ImportError, OSError):     # OSError: no PortAudio library on this box (Linux without libportaudio2)
        return False
    return True


def devices() -> list[dict[str, Any]]:
    """The inputs this box has, one per name - the host API the default
    input uses first, so one microphone is not listed four times (MME,
    DirectSound, WASAPI, WDM-KS on Windows) - with `default` marked. [] when
    nothing can be asked."""
    if not available():
        return []
    import sounddevice as sd
    try:
        every = sd.query_devices()
        apis = sd.query_hostapis()
        default_idx = sd.default.device[0]
    except Exception:  # noqa: BLE001 - a box with no audio at all is not an error
        return []
    default_api = every[default_idx]["hostapi"] if 0 <= default_idx < len(every) else -1
    rows = [(i, d) for i, d in enumerate(every) if d["max_input_channels"] > 0]
    rows.sort(key=lambda r: (r[1]["hostapi"] != default_api, r[0]))
    seen: dict[str, dict[str, Any]] = {}
    # MME cuts a name at 31 characters, so "Microphone (USB Audio Device)"
    # also turns up as "Microphone (USB Audio Device" - one input, not two.
    twin = lambda a, b: a == b or (min(len(a), len(b)) >= 12 and (a.startswith(b) or b.startswith(a)))
    for i, d in rows:
        name = str(d["name"]).strip()
        row = {"index": i, "name": name, "channels": int(d["max_input_channels"]),
               "rate": int(d["default_samplerate"]), "api": apis[d["hostapi"]]["name"], "default": i == default_idx}
        kept = next((k for k in seen if twin(name, k)), None)
        if kept is None:
            seen[name] = row
        elif len(name) > len(kept):
            # the whole name wins, in the twin's place; the default stays the default
            row["default"] = row["default"] or seen[kept]["default"]
            seen = {(name if k == kept else k): (row if k == kept else v) for k, v in seen.items()}
    return list(seen.values())


def pick(wanted: str, listing: Optional[list[dict[str, Any]]] = None) -> Optional[int]:
    """The input the config names (a case-insensitive part of its name), or
    None for the default. A name nothing matches is the default too, said
    in the log by the caller."""
    wanted = (wanted or "").strip().lower()
    if not wanted:
        return None
    for d in (devices() if listing is None else listing):
        if wanted in d["name"].lower():
            return d["index"]
    return None


# ── the seams a test replaces: what the input can do, and the stream ────────

def _settings(device: Optional[int]) -> dict[str, Any]:
    """{rate, channels, name} the input will be opened with: whisper's own
    16 kHz mono when it can, else its own."""
    import sounddevice as sd
    idx = sd.default.device[0] if device is None else device
    info = sd.query_devices(idx)
    name = str(info["name"]).strip()
    try:
        sd.check_input_settings(device=idx, samplerate=PREFERRED_RATE, channels=PREFERRED_CHANNELS, dtype="int16")
        return {"rate": PREFERRED_RATE, "channels": PREFERRED_CHANNELS, "name": name, "index": idx}
    except Exception:  # noqa: BLE001 - PortAudio says no in its own ways; the device's own settings then
        return {"rate": int(info["default_samplerate"]), "channels": min(2, int(info["max_input_channels"]) or 1),
                "name": name, "index": idx}


def _open_stream(device: Optional[int], rate: int, channels: int, callback: Callable[[bytes], None]) -> Any:
    """An open, started input stream calling `callback` with int16 bytes.
    Has .stop() and .close()."""
    import sounddevice as sd

    def on_audio(indata, frames, _time, status):
        callback(bytes(indata))

    stream = sd.RawInputStream(samplerate=rate, channels=channels, dtype="int16", device=device, callback=on_audio)
    stream.start()
    return stream


# ── a clip ───────────────────────────────────────────────────────────────────

@dataclass
class Clip:
    wav: bytes
    seconds: float
    rate: int
    channels: int
    device: str
    at: float          # when it started
    clipped: bool      # cut at max_seconds

    def info(self) -> dict[str, Any]:
        return {"seconds": self.seconds, "rate": self.rate, "channels": self.channels, "device": self.device,
                "at": self.at, "clipped": self.clipped, "bytes": len(self.wav)}


def to_wav(pcm: bytes, rate: int, channels: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(SAMPLE_BYTES)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


class Recorder:
    """One input at a time: start, then stop (a Clip) or cancel (nothing)."""

    def __init__(self, max_seconds: float = 120.0):
        self.max_seconds = max_seconds
        self._lock = threading.Lock()
        self._stream = None
        self._chunks: list[bytes] = []
        self._size = 0
        self._limit = 0
        self._clipped = False
        self._settings: dict[str, Any] = {}
        self._since = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def state(self) -> dict[str, Any]:
        if not self.recording:
            return {"recording": False}
        return {"recording": True, "since": self._since, "device": self._settings.get("name"),
                "seconds": round(time.time() - self._since, 1)}

    def _on_audio(self, data: bytes) -> None:
        with self._lock:
            if self._stream is None:
                return
            if self._size >= self._limit:
                self._clipped = True
                return
            take = data[: self._limit - self._size]
            if len(take) < len(data):
                self._clipped = True
            self._chunks.append(take)
            self._size += len(take)

    def start(self, device: Optional[int]) -> dict[str, Any]:
        """Open the input. RuntimeError when it cannot be (already
        listening, no such input, PortAudio refusing)."""
        if not available():
            raise RuntimeError("no sounddevice on this machine: pip install \"seren-symposium[ears]\"")
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("already listening")
            try:
                st = _settings(device)
            except Exception as ex:  # noqa: BLE001 - "no such device" comes in PortAudio's own words
                raise RuntimeError(f"no input {device if device is not None else '(default)'}: {ex}") from None
            self._chunks, self._size, self._clipped = [], 0, False
            self._limit = int(self.max_seconds * st["rate"] * st["channels"] * SAMPLE_BYTES)
            self._settings, self._since = st, time.time()
            try:
                self._stream = _open_stream(st["index"], st["rate"], st["channels"], self._on_audio)
            except Exception as ex:  # noqa: BLE001
                raise RuntimeError(f"could not open {st['name']}: {ex}") from None
        return {"recording": True, "since": self._since, "device": st["name"], "rate": st["rate"],
                "channels": st["channels"]}

    def _close(self) -> tuple[bytes, dict[str, Any], float, bool]:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is None:
                raise RuntimeError("not listening")
            pcm, st, since, clipped = b"".join(self._chunks), self._settings, self._since, self._clipped
            self._chunks, self._size = [], 0
        for step in (getattr(stream, "stop", None), getattr(stream, "close", None)):
            try:
                if step:
                    step()
            except Exception:  # noqa: BLE001 - the clip is what matters; a stream that will not close is logged by PortAudio
                pass
        return pcm, st, since, clipped

    def stop(self) -> Clip:
        pcm, st, since, clipped = self._close()
        frame = st["rate"] * st["channels"] * SAMPLE_BYTES
        return Clip(to_wav(pcm, st["rate"], st["channels"]), round(len(pcm) / frame, 2) if frame else 0.0,
                    st["rate"], st["channels"], st["name"], since, clipped)

    def cancel(self) -> None:
        try:
            self._close()
        except RuntimeError:
            pass
