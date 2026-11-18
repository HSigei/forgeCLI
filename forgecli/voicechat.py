"""Talk to Forge: the microphone is transcribed on this PC (faster-whisper) and replies are read aloud
(Piper, or the built-in Windows voice). The engine is Nova's (forgecli/nova/voice.py); this module adds
the terminal side: record one utterance, stop at the pause, speak a reply.

Needs: pip install faster-whisper sounddevice  (and piper-tts for the nicer voice)."""
from __future__ import annotations

import threading
import time

RATE = 16000
BLOCK = 1600                 # 0.1 s
MAX_SECONDS = 60
START_WAIT = 12              # give up when nobody speaks for this long


class VoiceError(RuntimeError):
    pass


class Voice:
    def __init__(self):
        self.engine = None
        self.speak = True
        self._lock = threading.Lock()
        self._play = None

    def load(self, log=lambda s: None):
        with self._lock:
            if self.engine is not None:
                return self.engine
            try:
                import sounddevice  # noqa: F401  (fail now, not mid-recording)
            except ImportError:
                raise VoiceError("the microphone needs sounddevice: pip install sounddevice faster-whisper") from None
            from .nova.core.config import DATA_DIR, load_config
            from .nova.voice import VoiceEngine
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            cfg = load_config()
            cfg["agent_name"] = "Forge"
            eng = VoiceEngine(cfg)
            log("loading speech recognition (first time downloads the model)…")
            eng.load_stt()
            if not eng.stt_ready:
                raise VoiceError(f"speech recognition isn't ready: {eng.stt_error}")
            eng.load_tts()
            self.engine = eng
            return eng

    def status(self) -> str:
        if self.engine is None:
            return "not loaded yet (it loads on first /voice)"
        e = self.engine
        bits = [f"speech in: whisper {e.stt_model_used or '?'}",
                f"speech out: {e.tts_name or 'none'}" + (f" ({e.tts_note})" if e.tts_note else ""),
                f"read replies aloud: {'on' if self.speak else 'off'}"]
        return " · ".join(bits)

    # microphone ---------------------------------------------------------------------------------
    def listen(self, cancelled=lambda: False, on_level=None) -> bytes:
        """Records until a pause after speech. Returns 16 kHz mono int16 PCM (b"" if nobody spoke)."""
        import numpy as np
        import sounddevice as sd
        c = self.engine.cfg
        silence_needed = float(c.get("mic_silence_seconds", 1.2))
        fixed = float(c.get("mic_threshold") or 0)
        device = c.get("mic_device") or None
        chunks, spoken, quiet, started = [], False, 0.0, time.time()
        noise = []
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, device=device) as stream:
            while True:
                if cancelled():
                    raise KeyboardInterrupt
                block, _ = stream.read(BLOCK)
                level = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
                if len(noise) < 5:                 # first half second: how loud is the room?
                    noise.append(level)
                threshold = fixed or max(350.0, 3.0 * (sum(noise) / len(noise)))
                if on_level:
                    on_level(level, threshold)
                loud = level > threshold
                if loud:
                    spoken, quiet = True, 0.0
                elif spoken:
                    quiet += BLOCK / RATE
                if spoken or len(chunks) < 5:
                    chunks.append(block.tobytes())
                else:
                    chunks = chunks[-4:] + [block.tobytes()]   # keep a little lead-in
                if spoken and quiet >= silence_needed:
                    break
                if not spoken and time.time() - started > START_WAIT:
                    return b""
                if time.time() - started > MAX_SECONDS:
                    break
        return b"".join(chunks)

    def transcribe(self, pcm: bytes) -> str:
        return self.engine.transcribe(pcm) if pcm else ""

    # speaker ------------------------------------------------------------------------------------
    def say(self, text: str, cancelled=lambda: False) -> None:
        from .nova.voice import SentenceChunker, clean_for_speech, read_wav
        e = self.engine
        if not (self.speak and e and e.tts_ready and text.strip()):
            return
        import numpy as np
        import sounddevice as sd
        chunker = SentenceChunker()
        parts = chunker.feed(clean_for_speech(text)) + chunker.flush()
        for part in parts:
            if cancelled():
                sd.stop()
                return
            wav, _ = e.synthesize(part)
            pcm, rate, ch = read_wav(wav)
            audio = np.frombuffer(pcm, dtype=np.int16)
            if ch > 1:
                audio = audio.reshape(-1, ch)
            sd.play(audio, rate)
            while sd.get_stream().active:
                if cancelled():
                    sd.stop()
                    return
                time.sleep(0.05)

    def stop(self) -> None:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

