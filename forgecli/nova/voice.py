"""Speech in (faster-whisper) and speech out (Piper, or the built-in Windows voice).

Every heavy import happens lazily so the brain still starts if a package is missing.
"""
from __future__ import annotations

import array
import io
import logging
import math
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

from forgecli.nova.wakeword import apply_corrections, build_prompt, is_phantom, match_wake, mean_logprob
from forgecli.nova.core.config import ROOT, SCRIPTS

log = logging.getLogger("brain.voice")

FRAME_MS = 30


def _raw_audio(pcm: bytes):
    import numpy as np

    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def preprocess(pcm: bytes):
    """Cleans a recording before recognition: removes DC offset and low rumble, then evens out the volume.
    A quiet microphone is one of the most common reasons for misheard words."""
    import numpy as np

    a = _raw_audio(pcm)
    if a.size < 800:
        return a
    a = a - float(a.mean())
    k = 231  # ~14 ms moving average, subtracted = a gentle high-pass filter around 70 Hz
    if a.size > 3 * k:
        cs = np.cumsum(np.concatenate(([0.0], a.astype(np.float64))))
        idx = np.arange(a.size)
        lo, hi = np.clip(idx - k // 2, 0, a.size), np.clip(idx + k // 2 + 1, 0, a.size)
        a = a - ((cs[hi] - cs[lo]) / (hi - lo)).astype(np.float32)
    peak = float(np.percentile(np.abs(a), 99.5))
    if peak > 0.005:  # don't amplify pure background noise
        a = np.clip(a * min(12.0, 0.7 / peak), -1.0, 1.0)
    return a.astype(np.float32)


# ---------------------------------------------------------------- text helpers
_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "approx", "no"}


class SentenceChunker:
    """Cuts a stream of text into speakable pieces as early as sensible.

    The first piece may end at a comma so speech can start sooner; later pieces
    end at sentence boundaries.
    """

    def __init__(self, first_min: int = 25, max_len: int = 140):
        self.buf = ""
        self.first = True
        self.first_min = first_min
        self.max_len = max_len

    def feed(self, piece: str) -> list[str]:
        self.buf += piece
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            chunk = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if chunk:
                out.append(chunk)
                self.first = False
        return out

    def flush(self) -> list[str]:
        rest = self.buf.strip()
        self.buf = ""
        return [rest] if rest else []

    def _is_abbrev(self, prefix: str) -> bool:
        words = prefix.split()
        if not words:
            return False
        return words[-1].lower().strip("(\"'") in _ABBREV

    def _find_cut(self) -> int | None:
        b = self.buf
        for i, ch in enumerate(b):
            if ch == "\n" and b[:i].strip():
                return i + 1
            nxt = b[i + 1:i + 2]
            if ch in ".!?…":
                if nxt == "":
                    continue  # wait: it may be "3.5" or "..." still arriving
                if nxt.isspace() or nxt in "\"')":
                    if ch == "." and self._is_abbrev(b[:i]):
                        continue
                    return i + 1
            elif self.first and ch in ",;:" and i + 1 >= self.first_min and nxt.isspace():
                return i + 1
        if len(b) > self.max_len:
            space = b.rfind(" ", 0, self.max_len)
            if space > 20:
                return space + 1
        return None


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)  # emoji etc.
    text = re.sub(r"[*_`#>~|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if re.search(r"\w", text) else ""


# ---------------------------------------------------------------- audio helpers
def make_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[bytes, int, int]:
    """Return (pcm16 bytes, sample rate, channels) of a 16-bit WAV."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise RuntimeError("expected 16-bit audio")
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


def make_envelope(pcm: bytes, rate: int, channels: int = 1) -> list[float]:
    """Loudness (0..1) every FRAME_MS - the face uses it to move the mouth."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if channels > 1:
        samples = samples[::channels]
    n = max(1, int(rate * FRAME_MS / 1000))
    levels = []
    for i in range(0, len(samples), n):
        seg = samples[i:i + n]
        if seg:
            levels.append(math.sqrt(sum(x * x for x in seg) / len(seg)))
    if not levels:
        return []
    ref = max(sorted(levels)[int(len(levels) * 0.95) - 1 if len(levels) > 1 else 0], 1.0)
    return [round(min(1.0, v / ref), 2) for v in levels]


# ---------------------------------------------------------------- the engine
class VoiceEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg["voice"]
        self.wake_cfg = cfg["wake"]
        self.name = cfg["agent_name"]
        self.prompt = build_prompt(cfg["agent_name"], cfg.get("user_name", ""), self.cfg.get("stt_vocabulary"))
        self.stt_model_used: str | None = None
        self.stt_note: str | None = None
        self.wake_ready = False
        self.wake_error: str | None = None
        self._wake = None
        self.tts_name: str | None = None   # "piper" | "sapi" | None
        self.tts_note: str | None = None   # why a fallback/none is in use
        self.stt_ready = False
        self.stt_error: str | None = None
        self._piper = None
        self._whisper = None

    @property
    def tts_ready(self) -> bool:
        return self.tts_name is not None

    def status(self, loading: bool) -> dict:
        return {
            "loading": loading,
            "tts": self.tts_name,
            "tts_note": self.tts_note,
            "stt_ready": self.stt_ready,
            "stt_error": self.stt_error,
            "stt_model": self.stt_model_used,
            "stt_note": self.stt_note,
            "wake_ready": self.wake_ready,
            "wake_error": self.wake_error,
        }

    # ---- loading (blocking; call from a worker thread) ----------------------
    def load_tts(self) -> None:
        want = str(self.cfg.get("tts_engine", "auto")).lower()
        if want == "off":
            self.tts_note = "Voice output is switched off in config.json."
            return
        notes: list[str] = []
        if want in ("auto", "piper"):
            try:
                self._load_piper()
                self.tts_name = "piper"
                log.info("Voice output: Piper")
                return
            except Exception as exc:
                notes.append(f"Piper isn't ready ({exc})")
                log.warning("Piper unavailable: %s", exc)
        if want in ("auto", "sapi") and os.name == "nt":
            try:
                self._synth_sapi("Ready.")
                self.tts_name = "sapi"
                notes.append("using the built-in Windows voice instead")
                log.info("Voice output: Windows SAPI")
            except Exception as exc:
                notes.append(f"Windows voice failed ({exc})")
                log.warning("SAPI unavailable: %s", exc)
        self.tts_note = "; ".join(notes) or None

    def _load_piper(self) -> None:
        try:
            from piper import PiperVoice
        except ImportError:
            raise RuntimeError("piper-tts isn't installed - run: python tools/install_voice.py") from None
        path = Path(self.cfg["piper_voice"])
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"voice file {path.name} not found - run: python tools/get_models.py")
        cfg_path = Path(str(path) + ".json")
        self._piper = PiperVoice.load(str(path), config_path=str(cfg_path) if cfg_path.exists() else None)
        self._synth_piper("Hello.")  # smoke test so API problems show up now, not mid-sentence

    def _model_source(self, name: str) -> tuple[str, bool]:
        """(path or name, is_local). Models downloaded by tools/get_models.py live in models/whisper-<name>."""
        local = ROOT / "models" / f"whisper-{name}"
        return (str(local), True) if local.exists() else (name, False)

    def load_stt(self) -> None:
        if not self.cfg.get("stt_enabled", True):
            self.stt_error = "switched off in config.json"
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            self.stt_error = "faster-whisper isn't installed - run: python tools/install_voice.py"
            return
        wanted = self.cfg["stt_model"]
        # the best installed model, starting from the one you asked for
        order = [wanted] + [m for m in ("small.en", "base.en", "tiny.en") if m != wanted]
        choice = next((m for m in order if self._model_source(m)[1]), wanted)
        source = self._model_source(choice)[0]
        try:
            self._whisper = WhisperModel(
                source, device="cpu", compute_type="int8", cpu_threads=int(self.cfg.get("stt_threads", 4)))
            self.stt_ready = True
            self.stt_model_used = choice
            if choice != wanted:
                self.stt_note = (f"using the '{choice}' speech model because '{wanted}' isn't downloaded - "
                                 f"run: python tools/get_models.py --whisper {wanted}")
            log.info("Speech recognition ready (%s)", source)
        except Exception as exc:
            self.stt_error = (
                f"couldn't load the speech model '{wanted}' ({exc}). "
                "Run 'python tools/get_models.py' once while online."
            )
            log.warning(self.stt_error)

    def load_wake(self) -> None:
        """A tiny, fast model that only has to recognise "Hey <name>"."""
        if not self.wake_cfg.get("enabled", True):
            self.wake_error = "switched off in config.json"
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            self.wake_error = "faster-whisper isn't installed - run: python tools/install_voice.py"
            return
        name = self.wake_cfg.get("model", "tiny.en")
        source, local = self._model_source(name)
        try:
            self._wake = WhisperModel(source, device="cpu", compute_type="int8", cpu_threads=2)
            self.wake_ready = True
            log.info("Wake-word listening ready (%s)", source)
        except Exception as exc:
            self.wake_error = (f"couldn't load the wake-word model '{name}' ({exc}). "
                               f"Run: python tools/get_models.py --wake-model {name}")
            log.warning(self.wake_error)

    # ---- speech out ---------------------------------------------------------
    def synthesize(self, text: str) -> tuple[bytes, list[float]]:
        if self.tts_name == "piper":
            pcm, rate = self._synth_piper(text)
            return make_wav(pcm, rate), make_envelope(pcm, rate)
        if self.tts_name == "sapi":
            wav = self._synth_sapi(text)
            pcm, rate, ch = read_wav(wav)
            return wav, make_envelope(pcm, rate, ch)
        raise RuntimeError("no voice engine is available")

    def _synth_piper(self, text: str) -> tuple[bytes, int]:
        voice = self._piper
        rate = int(voice.config.sample_rate)
        if hasattr(voice, "synthesize_stream_raw"):  # piper-tts 1.2.x
            return b"".join(voice.synthesize_stream_raw(text)), rate
        chunks = list(voice.synthesize(text))  # piper-tts 1.3+
        if chunks:
            rate = int(getattr(chunks[0], "sample_rate", rate))
        return b"".join(c.audio_int16_bytes for c in chunks), rate

    def _synth_sapi(self, text: str) -> bytes:
        script = SCRIPTS / "sapi_tts.ps1"
        fd_t, txt = tempfile.mkstemp(suffix=".txt")
        fd_w, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd_t)
        os.close(fd_w)
        try:
            Path(txt).write_text(text, encoding="utf-8")
            proc = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(script), "-TextFile", txt, "-WavFile", wav,
                    "-Rate", str(int(self.cfg.get("sapi_rate", 0))),
                ],
                capture_output=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:200] or "powershell failed")
            return Path(wav).read_bytes()
        finally:
            for f in (txt, wav):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ---- speech in ----------------------------------------------------------
    def transcribe_ex(self, pcm: bytes) -> dict:
        """The accurate transcription: {"text", "avg_logprob", "no_speech_prob", "seconds"}."""
        if not self.stt_ready:
            raise RuntimeError(self.stt_error or "speech recognition isn't ready")
        c = self.cfg
        audio = preprocess(pcm) if c.get("stt_preprocess", True) else _raw_audio(pcm)
        seconds = len(audio) / 16000.0
        kwargs = dict(
            language=c.get("stt_language") or None,
            beam_size=int(c.get("stt_beam_size", 5)) if seconds <= 12 else 2,
            condition_on_previous_text=False,
            initial_prompt=self.prompt or None,
            temperature=[0.0, 0.2],
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        if c.get("stt_vad", True):
            kwargs.update(vad_filter=True, vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250})
        segments, _info = self._whisper.transcribe(audio, **kwargs)
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        avg_lp = mean_logprob([s.avg_logprob for s in segs])
        no_speech = max((s.no_speech_prob for s in segs), default=1.0)
        if is_phantom(text, avg_lp, no_speech):
            text = ""
        return {"text": apply_corrections(text, c.get("stt_corrections") or {}), "avg_logprob": avg_lp,
                "no_speech_prob": no_speech, "seconds": seconds}

    def transcribe(self, pcm: bytes) -> str:
        return self.transcribe_ex(pcm)["text"]

    def wake_check(self, pcm: bytes) -> dict:
        """Quick check of the start of what was said: is it "Hey <name> ..."?"""
        if self._wake is None:
            raise RuntimeError(self.wake_error or "wake-word listening isn't ready")
        segments, _info = self._wake.transcribe(
            preprocess(pcm), language="en", beam_size=1, condition_on_previous_text=False,
            initial_prompt=f"Hey {self.name}.", temperature=0.0, vad_filter=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        w = self.wake_cfg
        matched, rest = match_wake(text, self.name, float(w.get("sensitivity", 0.7)),
                                   bool(w.get("allow_bare_name")), tuple(w.get("extra_phrases") or ()))
        return {"wake": matched, "command": bool(re.search(r"\w", rest)), "text": text}

    def strip_wake(self, text: str) -> tuple[bool, str]:
        w = self.wake_cfg
        return match_wake(text, self.name, float(w.get("sensitivity", 0.7)), bool(w.get("allow_bare_name")),
                          tuple(w.get("extra_phrases") or ()))
