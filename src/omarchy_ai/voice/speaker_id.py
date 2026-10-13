"""Who is speaking, from the voice alone, on this machine.

A conversation can come from anyone in the house, and the live model hears
only audio: it cannot tell the people apart, so it cannot know whose
calendar, computer or preferences "my" means. Here each stretch of speech
is compared with the voices the user enrolled, and the session tells the
model when the speaker is recognized or changes (gemini_live._speaker_watch).

Everything is local. The model is WeSpeaker's ResNet34 speaker-embedding
network (VoxCeleb, ONNX, 26 MB), run with the onnxruntime this project
already has for the wake word, on 80-bin Kaldi filterbank features computed
here in numpy; no audio leaves the machine and nothing is kept but one
averaged embedding per person.

Who the people are, and how the assistant should treat each of them, is the
user's own text in ~/.config/omarchy-ai/people.yaml (see people()); the model
gets a person's entry with the news that they are speaking.

Enrolment: one folder per person with a few WAV recordings of them talking,
    ~/.config/omarchy-ai/speakers/<name>/*.wav
then `omarchy-ai-settings enroll-speakers`, which downloads the model once
and writes STATE_DIR/speakers.json. 3-5 clips of 3-10 seconds each are
enough; more make the average steadier.

Measured 2026-10-05 on two people's four recordings each (2-5 s, a speaker's
far-field microphone): held-out clip 8 of 8 right, 1.5 s pieces 22 of 23;
the right person scored 0.48-0.67 (one 1.8 s clip 0.11), the other at most
0.26. 270 ms of one CPU core for 3.5 s of audio.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
import wave
from pathlib import Path

import numpy as np

from ..config import CONFIG_DIR, STATE_DIR

log = logging.getLogger("omarchy_ai.voice.speaker_id")

MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
MODEL_PATH = STATE_DIR / "models" / "voxceleb_resnet34_LM.onnx"
SPEAKERS_DIR = CONFIG_DIR / "speakers"
EMBEDDINGS_PATH = STATE_DIR / "speakers.json"
RATE = 16000
# Below MATCH the voice is nobody enrolled; a match must also beat the next
# person by MARGIN, so two similar voices give "not sure" and not a guess
# (see the measurements above: right 0.48+, wrong at most 0.26).
MATCH = 0.38
MARGIN = 0.10
# Speech needed for a decision, and how much of the latest speech is judged.
MIN_SECONDS = 1.5
WINDOW_SECONDS = 4.0
# This long without speech ends an utterance (Tracker.latest()).
PAUSE_SECONDS = 0.6
# Keep soft onsets before speech and a little quiet after its last loud frame.
PRE_ROLL_SECONDS = 0.2
HANGOVER_SECONDS = 0.2
# A recording a person adds of themself in a conversation (remember()).
ADD_MIN_SECONDS = 6.0
ADD_MAX_SECONDS = 15.0
PEOPLE_PATH = CONFIG_DIR / "people.yaml"


def fbank(samples: np.ndarray) -> np.ndarray:
    """Kaldi-style log mel filterbank (25 ms frames every 10 ms, 80 bins,
    Povey window, pre-emphasis 0.97, no dither) with the mean removed over
    time: what WeSpeaker's models were trained on. `samples` is int16-scaled."""
    frame, shift, fft, bins = 400, 160, 512, 80
    x = samples.astype(np.float32)
    count = 1 + (len(x) - frame) // shift
    frames = x[np.arange(frame)[None, :] + shift * np.arange(count)[:, None]]
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    frames = frames * np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame) / (frame - 1)), 0.85)
    power = np.abs(np.fft.rfft(frames, fft, axis=1)) ** 2

    def mel(hz):
        return 1127.0 * np.log(1 + hz / 700.0)
    edges = np.linspace(mel(20.0), mel(RATE / 2), bins + 2)
    bin_mel = mel(np.arange(fft // 2 + 1) * RATE / fft)
    left, center, right = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    weights = np.maximum(0, np.minimum((bin_mel - left) / (center - left), (right - bin_mel) / (right - center)))
    features = np.log(np.maximum(power @ weights.T.astype(np.float32), np.finfo(np.float32).eps))
    return (features - features.mean(axis=0, keepdims=True)).astype(np.float32)


def read_wav(path: Path) -> np.ndarray:
    """A recording as mono 16 kHz int16 samples, whatever it was saved as."""
    with wave.open(str(path), "rb") as handle:
        rate, width, channels = handle.getframerate(), handle.getsampwidth(), handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: only 16-bit WAV recordings are supported")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        positions = np.arange(int(len(samples) * RATE / rate)) * (rate / RATE)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    return samples.astype("<i2")


def decide(scores: dict[str, float]) -> str | None:
    """The enrolled person these similarity scores point to, or None."""
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
    return best if score >= MATCH and score - runner_up >= MARGIN else None


class Identifier:
    """The model and the enrolled voices; both load on first use."""

    def __init__(self, model_path: Path = MODEL_PATH, embeddings_path: Path = EMBEDDINGS_PATH):
        self.model_path, self.embeddings_path = Path(model_path), Path(embeddings_path)
        self._session = None
        self._known: dict[str, np.ndarray] | None = None
        self._lock = threading.Lock()

    def known(self) -> dict[str, np.ndarray]:
        if self._known is None:
            try:
                data = json.loads(self.embeddings_path.read_text())
                self._known = {str(name): np.asarray(vector, dtype=np.float32) for name, vector in data.items()}
            except (OSError, ValueError, TypeError):
                self._known = {}
        return self._known

    def ready(self) -> bool:
        return bool(self.known()) and self.model_path.is_file()

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._lock:
            if self._session is None:
                import onnxruntime
                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = 1  # a conversation's audio threads come first
                self._session = onnxruntime.InferenceSession(str(self.model_path), options,
                                                             providers=["CPUExecutionProvider"])
            vector = self._session.run(None, {self._session.get_inputs()[0].name: fbank(samples)[None]})[0][0]
        return vector / (np.linalg.norm(vector) + 1e-9)

    def identify(self, samples: np.ndarray) -> tuple[str | None, dict[str, float]]:
        """(the enrolled person or None, every person's similarity)."""
        vector = self.embed(samples)
        scores = {name: float(vector @ known) for name, known in self.known().items()}
        return decide(scores), scores


def download_model(path: Path = MODEL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response, open(partial, "wb") as out:
        while block := response.read(1 << 20):
            out.write(block)
    partial.replace(path)


def enroll(speakers_dir: Path = SPEAKERS_DIR, identifier: Identifier | None = None) -> dict:
    """Average each person's recordings into one embedding and save them.
    Returns {"speakers": {name: clips}, "skipped": [why, ...]}."""
    identifier = identifier or Identifier()
    if not identifier.model_path.is_file():
        download_model(identifier.model_path)
    speakers, skipped, vectors = {}, [], {}
    for person in sorted(p for p in Path(speakers_dir).iterdir() if p.is_dir()) if Path(speakers_dir).is_dir() else []:
        clips = []
        for recording in sorted(person.glob("*.wav")):
            try:
                samples = read_wav(recording)
                if len(samples) < RATE:
                    raise ValueError(f"{recording.name}: shorter than a second")
                clips.append(identifier.embed(samples))
            except (OSError, ValueError, wave.Error) as error:
                skipped.append(f"{person.name}: {error}")
        if clips:
            mean = np.mean(clips, axis=0)
            vectors[person.name] = (mean / (np.linalg.norm(mean) + 1e-9)).tolist()
            speakers[person.name] = len(clips)
    if vectors:
        identifier.embeddings_path.parent.mkdir(parents=True, exist_ok=True)
        partial = identifier.embeddings_path.with_suffix(".part")
        partial.write_text(json.dumps(vectors))
        partial.chmod(0o600)
        partial.replace(identifier.embeddings_path)
    return {"speakers": speakers, "skipped": skipped}


def people(path: Path | None = None) -> dict[str, dict]:
    """The household, from the user's own file. Each key is a person's
    enrolment folder name (or the name they will get), each value free-form:

        people:
          dana:
            name: Dana
            relation: the user's daughter
            age: 9
            treat: Short, simple answers. Encourage her questions.

    A top-level `style:` is the household's own words on how personal the
    assistant should be with all of them (house_style()).

    Missing or malformed: nobody, and recognition still names the speaker."""
    entries = _household(path).get("people")
    return {str(key).lower(): value for key, value in (entries if isinstance(entries, dict) else {}).items()
            if isinstance(value, dict)}


def house_style(path: Path | None = None) -> str:
    return str(_household(path).get("style") or "").strip()[:900]


def _household(path: Path | None) -> dict:
    import yaml
    try:
        data = yaml.safe_load((path or PEOPLE_PATH).read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def describe(key: str, entry: dict | None) -> str:
    """One person in a sentence or two, for the live model."""
    entry = entry or {}
    name = str(entry.get("name") or key.capitalize())
    facts = [str(entry[field]) for field in ("relation",) if entry.get(field)]
    if entry.get("age") not in (None, ""):
        facts.append(f"{entry['age']} years old")
    text = name + (f" ({', '.join(facts)})" if facts else "")
    for label, field in (("About them", "about"), ("How to treat them", "treat")):
        if entry.get(field):
            text += f". {label}: {str(entry[field]).strip().rstrip('.')}"
    return text[:900]


def remember(name: str, samples: np.ndarray, source: str, identifier: Identifier | None = None,
             speakers_dir: Path | None = None) -> tuple[bool, str]:
    """Add what a person just said to their own recordings and enrol again.

    Only for someone already enrolled, and only when this very speech is
    recognized as them: otherwise anyone could say "remember my voice, I am
    <name>" and be taken for that person from then on."""
    identifier = identifier or Identifier()
    speakers_dir = Path(speakers_dir or SPEAKERS_DIR)
    key = "".join(ch for ch in str(name).lower() if ch.isalnum() or ch in "-_")
    if not key or key not in identifier.known():
        return False, (f"{name} has no enrolled voice yet. A first recording is added by the owner as a file in "
                       f"{speakers_dir}/<name>/, not from a conversation.")
    if len(samples) < ADD_MIN_SECONDS * RATE:
        return False, (f"Only {len(samples) / RATE:.0f} seconds of their speech so far. Ask them to keep talking for "
                       "about ten seconds (reading something aloud is fine), then call this again.")
    samples = samples[-int(ADD_MAX_SECONDS * RATE):]
    heard, scores = identifier.identify(samples)
    if heard != key:
        return False, (f"This voice is not recognized as {name}, so nothing was saved: a person's recordings can "
                       "only be added by that person.")
    folder = speakers_dir / key
    folder.mkdir(parents=True, exist_ok=True)
    import time
    path = folder / f"live-{time.strftime('%Y%m%d-%H%M%S')}-{''.join(c for c in source if c.isalnum()) or 'mic'}.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.astype("<i2").tobytes())
    result = enroll(speakers_dir, identifier)
    identifier._known = None  # the next judgement uses the new average
    log.info("Speaker recognition: added %.0fs for %s from %s (score %.2f); %d recordings now", len(samples) / RATE,
             key, source, scores.get(key, 0.0), result["speakers"].get(key, 0))
    return True, (f"Saved {len(samples) / RATE:.0f} seconds as a new recording of {name} from this microphone; "
                  f"{result['speakers'].get(key, 0)} recordings now. It applies from the next conversation.")


class Tracker:
    """One conversation's speech, judged as it accumulates.

    add() takes every microphone frame and distinguishes loud user speech,
    quiet microphone audio, and the assistant's echo. A loud frame opens an
    utterance; quiet audio within it and a short pre-roll are kept, but echo
    never is. due() hands back the latest few seconds once enough retained
    audio has arrived."""

    def __init__(self):
        self._speech: list[bytes] = []
        self._fresh = 0      # retained samples since the last judgement
        self._recent: list[bytes] = []  # the longer stretch a new recording is cut from
        self._latest: list[bytes] = []  # the speech since the last pause
        self._quiet = 0      # samples without speech since then
        self._open = False
        self._pre_roll: list[tuple[bytes | None, int]] = []
        self._pending: list[tuple[bytes | None, int]] = []
        self.heard = self.loud = self.kept = 0  # samples: not her echo, of those loud, retained (logged)
        self.speaker: str | None = None

    @staticmethod
    def _tail(frames: list[tuple[bytes | None, int]], samples: int) -> list[tuple[bytes | None, int]]:
        """The last `samples` of timed frames, trimming a boundary frame."""
        result = list(frames)
        excess = sum(length for _, length in result) - samples
        while result and excess >= result[0][1]:
            excess -= result.pop(0)[1]
        if result and excess:
            chunk, length = result[0]
            result[0] = (chunk[excess * 2:] if chunk is not None else None, length - excess)
        return result

    def _keep(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._speech.append(chunk)
        self._recent.append(chunk)
        self._latest.append(chunk)
        self._fresh += len(chunk) // 2
        self.kept += len(chunk) // 2
        for kept, seconds in ((self._speech, WINDOW_SECONDS), (self._recent, ADD_MAX_SECONDS),
                              (self._latest, ADD_MAX_SECONDS)):
            while kept and sum(len(c) for c in kept) - len(kept[0]) >= int(seconds * RATE) * 2:
                kept.pop(0)

    def _close(self) -> None:
        hangover = int(HANGOVER_SECONDS * RATE)
        elapsed = 0
        for chunk, samples in self._pending:
            if elapsed >= hangover:
                break
            take = min(samples, hangover - elapsed)
            if chunk is not None:
                self._keep(chunk[:take * 2])
            elapsed += samples
        self._pre_roll = self._tail(self._pending, int(PRE_ROLL_SECONDS * RATE))
        self._pending = []
        self._open = False

    def add(self, chunk: bytes, speech: bool, *, echo: bool = False) -> None:
        samples = len(chunk) // 2
        loud = speech and not echo
        timed = (None if echo else chunk, samples)
        self.heard += 0 if echo else samples
        self.loud += samples if loud else 0
        if not loud:
            if self._open:
                self._pending.append(timed)
                self._quiet += samples
                if self._quiet >= PAUSE_SECONDS * RATE:
                    self._close()
            else:
                self._quiet += samples
                self._pre_roll.append(timed)
                self._pre_roll = self._tail(self._pre_roll, int(PRE_ROLL_SECONDS * RATE))
            return
        if not self._open:
            self._latest = []
            for held, _ in self._pre_roll:
                if held is not None:
                    self._keep(held)
            self._pre_roll = []
            self._open = True
        else:
            for held, _ in self._pending:
                if held is not None:
                    self._keep(held)
            self._pending = []
        self._quiet = 0
        self._keep(chunk)

    def recent(self) -> np.ndarray:
        """The latest retained conversation audio, up to ADD_MAX_SECONDS."""
        return np.frombuffer(b"".join(self._recent), dtype="<i2")

    def latest(self) -> tuple[np.ndarray, float]:
        """(the utterance being said or just said: retained audio since the
        last pause of PAUSE_SECONDS, seconds since its last loud frame). For
        a judgement of one utterance alone, whoever spoke before it."""
        return np.frombuffer(b"".join(self._latest), dtype="<i2"), self._quiet / RATE

    def due(self) -> np.ndarray | None:
        # The first judgement comes quickly; later ones only confirm or
        # notice another person, so they wait for a full window of new speech.
        needed = MIN_SECONDS if self.speaker is None else WINDOW_SECONDS
        if self._fresh < needed * RATE:
            return None
        self._fresh = 0
        return np.frombuffer(b"".join(self._speech), dtype="<i2")
