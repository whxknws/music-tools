#!/usr/bin/env python3
"""Interactive chord progression generator with MIDI export."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_NAMES_FLAT = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
FLAT_TO_SHARP = {
    "DB": "C#",
    "EB": "D#",
    "FB": "E",
    "GB": "F#",
    "AB": "G#",
    "BB": "A#",
    "CB": "B",
}

SCALE_INTERVALS: dict[str, tuple[int, ...]] = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
}

PROGRESSION_PATTERNS: dict[int, list[tuple[str, str]]] = {
    4: [
        ("I-IV-V-I", "I-IV-V-I"),
        ("ii-V-I", "ii-V-I-I"),
        ("I-V-vi-IV", "I-V-vi-IV"),
        ("vi-IV-I-V", "vi-IV-I-V"),
        ("I-vi-IV-V", "I-vi-IV-V"),
        ("iii-vi-ii-V", "iii-vi-ii-V"),
    ],
    8: [
        ("I-IV-V-I (x2)", "I-IV-V-I-I-IV-V-I"),
        ("I-V-vi-IV (x2)", "I-V-vi-IV-I-V-vi-IV"),
        ("vi-IV-I-V (x2)", "vi-IV-I-V-vi-IV-I-V"),
        ("ii-V-I extended", "ii-V-I-ii-V-I-I"),
        ("I-vi-IV-V (x2)", "I-vi-IV-V-I-vi-IV-V"),
        ("pop turnaround", "I-V-vi-IV-V-IV-I-V"),
    ],
}

VALID_KEYS = {
    *NOTE_NAMES,
    *[n + "b" for n in ("C", "D", "E", "F", "G", "A", "B")],
    *[n + "#" for n in ("C", "D", "F", "G", "A")],
}

DEFAULT_BPM = 120
DEFAULT_VELOCITY = 100
CHORD_OCTAVE = 4  # middle register for root (C4 = MIDI 60)


@dataclass(frozen=True)
class Chord:
    name: str
    notes: tuple[int, ...]
    duration: float = 1.0  # bars (supports half-bars as 0.5)


_DURATION_RE = re.compile(r"^(.*?)\((\d+)\)$")


def parse_token(token: str) -> tuple[str, int]:
    """Split 'IV(2)' into ('IV', 2). Returns duration=1 if no suffix."""
    m = _DURATION_RE.match(token)
    if m:
        bars = int(m.group(2))
        if bars < 1:
            raise ValueError(f"Duration must be >= 1: {token!r}")
        return m.group(1), bars
    return token, 1


def normalize_key(key: str) -> str:
    cleaned = key.strip().upper().replace("♯", "#").replace("♭", "B")
    if len(cleaned) >= 2 and cleaned[1] in ("#", "B"):
        pitch, accidental = cleaned[0], cleaned[1]
        normalized = pitch + accidental
    else:
        normalized = cleaned[0]

    if normalized in FLAT_TO_SHARP:
        return FLAT_TO_SHARP[normalized]
    if normalized in NOTE_NAMES:
        return normalized
    raise ValueError(f"Unknown key: {key!r}")


def parse_root_midi(key: str) -> int:
    normalized = normalize_key(key)
    return NOTE_NAMES.index(normalized) + 12 * (CHORD_OCTAVE + 1)


SEVENTH_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("maj7", "maj7"),
    ("M7", "maj7"),
    ("m7b5", "halfdim"),
    ("ø7", "halfdim"),
    ("dim7", "dim7"),
    ("7", "dom7"),
)


def parse_roman(numeral: str) -> tuple[int, str, str | None]:
    """Return (scale degree 1-7, quality: major|minor|dim, seventh: None|maj7|dom7|dim7|halfdim)."""
    token = numeral.strip()
    if not token:
        raise ValueError("Empty roman numeral")

    seventh: str | None = None
    for suffix, stype in SEVENTH_SUFFIXES:
        if token.endswith(suffix):
            seventh = stype
            token = token[: -len(suffix)]
            break

    quality = "major"
    if "°" in token or token.lower().endswith("dim"):
        quality = "dim"
        token = token.replace("°", "").replace("dim", "").replace("DIM", "")
    elif token and token[0].islower():
        quality = "minor"
        token = token.upper()

    if seventh == "halfdim":
        quality = "dim"

    roman_map = {"I": 1, "V": 5, "X": 10}
    value = 0
    for i, char in enumerate(token):
        if char not in roman_map:
            raise ValueError(f"Invalid roman numeral: {numeral!r}")
        curr = roman_map[char]
        nxt = roman_map.get(token[i + 1], 0) if i + 1 < len(token) else 0
        value += -curr if nxt > curr else curr

    if value < 1 or value > 7:
        raise ValueError(f"Degree out of range: {numeral!r}")
    return value, quality, seventh


def build_scale(root_midi: int, scale_type: str) -> list[int]:
    intervals = SCALE_INTERVALS[scale_type]
    return [root_midi + interval for interval in intervals]


def triad_quality(root: int, third: int, fifth: int) -> str:
    interval_third = (third - root) % 12
    interval_fifth = (fifth - root) % 12
    if interval_third == 3 and interval_fifth == 6:
        return "dim"
    if interval_third == 3 and interval_fifth == 7:
        return "minor"
    if interval_third == 4 and interval_fifth == 7:
        return "major"
    if interval_third == 4 and interval_fifth == 8:
        return "aug"
    return "major"


SEVENTH_INTERVALS: dict[str, int] = {
    "maj7": 11,
    "dom7": 10,
    "dim7": 9,
    "halfdim": 10,
}


def chord_for_degree(
    scale: list[int],
    degree: int,
    quality: str,
    seventh: str | None = None,
    duration: int = 1,
) -> Chord:
    idx = degree - 1
    root = scale[idx]
    third = scale[(idx + 2) % 7]
    fifth = scale[(idx + 4) % 7]

    if third < root:
        third += 12
    if fifth < third:
        fifth += 12

    actual = triad_quality(root, third, fifth)
    notes = [root, third, fifth]

    if quality == "dim" or (quality == "minor" and actual == "dim"):
        if (fifth - root) % 12 == 7:
            notes[2] -= 1
    elif quality == "major" and actual == "minor":
        notes[1] += 1
        if notes[1] >= notes[2]:
            notes[2] += 12
    elif quality == "minor" and actual == "major":
        notes[1] -= 1

    if seventh is not None:
        seventh_note = root + SEVENTH_INTERVALS[seventh]
        while seventh_note <= notes[2]:
            seventh_note += 12
        notes.append(seventh_note)

    label = ["I", "II", "III", "IV", "V", "VI", "VII"][idx]
    if quality == "minor":
        label = label.lower()
    elif quality == "dim":
        label = label.lower() + "°"
    if seventh == "maj7":
        label += "maj7"
    elif seventh == "dom7":
        label += "7"
    elif seventh == "dim7":
        label += "°7"
    elif seventh == "halfdim":
        label = label.rstrip("°") + "ø7"

    return Chord(name=label, notes=tuple(sorted(notes)), duration=duration)


def progression_to_chords(
    pattern: str, scale: list[int]
) -> list[Chord]:
    tokens = re.split(r"[-\s]+", pattern.strip())
    chords: list[Chord] = []
    for token in tokens:
        if not token:
            continue
        numeral, duration = parse_token(token)
        degree, quality, seventh = parse_roman(numeral)
        chords.append(chord_for_degree(scale, degree, quality, seventh, duration))
    return chords


def sanitize_filename_part(text: str) -> str:
    return re.sub(r"[^\w#°-]+", "", text.replace(" ", ""))


def export_midi(
    chords: list[Chord],
    filepath: str,
    bpm: int = DEFAULT_BPM,
    velocity: int = DEFAULT_VELOCITY,
    velocity_variation: int = 0,   # ±semitones around velocity
    strum_ticks: int = 0,          # ticks between successive notes (0 = block chord)
    strum_up: bool = True,         # True = low→high, False = high→low
) -> None:
    mid = MidiFile(type=1)
    track = MidiTrack()
    mid.tracks.append(track)

    track.append(MetaMessage("track_name", name="Chords", time=0))
    track.append(MetaMessage("set_tempo", tempo=bpm2tempo(bpm), time=0))
    track.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))

    ticks_per_bar = mid.ticks_per_beat * 4

    for chord in chords:
        chord_ticks = int(chord.duration * ticks_per_bar)
        notes = sorted(chord.notes) if strum_up else sorted(chord.notes, reverse=True)
        total_strum = strum_ticks * (len(notes) - 1)

        # Note ons — spread by strum_ticks each
        for i, pitch in enumerate(notes):
            vel = velocity
            if velocity_variation:
                vel = max(1, min(127, velocity + random.randint(-velocity_variation, velocity_variation)))
            track.append(Message("note_on", note=pitch, velocity=vel,
                                 time=strum_ticks if i > 0 else 0))

        # Note offs — all land at the same absolute tick (chord end)
        off_delta = max(1, chord_ticks - total_strum)
        for i, pitch in enumerate(notes):
            track.append(Message("note_off", note=pitch, velocity=0,
                                 time=off_delta if i == 0 else 0))

    mid.save(filepath)


BEGINNER_KEYS = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

MOODS: list[tuple[str, str, str]] = [
    ("Happy / upbeat",    "major",      "bright and cheerful"),
    ("Sad / emotional",   "minor",      "deep and melancholic"),
    ("Soulful / funky",   "dorian",     "groovy with an edge"),
    ("Dreamy / floating", "lydian",     "ethereal, otherworldly"),
    ("Dark / tense",      "phrygian",   "brooding and intense"),
    ("Bluesy / rock",     "mixolydian", "raw and gritty"),
]

_MINOR_SCALES = {"minor", "dorian", "phrygian"}

# Progressions that work naturally in major-family scales
PROGRESSIONS_MAJOR: list[tuple[str, str, str]] = [
    ("Pop anthem",       "I-V-vi-IV",         "used in countless hit songs"),
    ("Happy loop",       "I-IV-V-I",          "the backbone of blues and rock"),
    ("Emotional",        "vi-IV-I-V",         "melancholic and moving"),
    ("Bittersweet",      "I-vi-IV-V",         "nostalgic, classic 60s feel"),
    ("Jazz resolution",  "ii-V-I-I",          "smooth and sophisticated"),
    ("Building tension", "I-iii-IV-V",        "keeps climbing, feels unresolved"),
    ("Royal road",       "IV-V-iii-vi",       "bright and catchy, common in J-pop"),
    ("Axis",             "I-V-vi-iii",        "hopeful and uplifting"),
    ("Blue Moon",        "I-vi-ii-V",         "classic jazz standard feel"),
    ("Gospel",           "I-IV-I-V",          "call and response, soulful"),
    ("Canon",            "I-V-vi-iii-IV-I-IV-V", "Pachelbel's Canon, timeless"),
    ("Andalusian",       "I-VII-VI-V",        "dramatic descending, cinematic"),
]

# Progressions written in natural minor — no quality clash with the scale
PROGRESSIONS_MINOR: list[tuple[str, str, str]] = [
    ("Dark anthem",      "i-VII-VI-VII",  "powerful and dramatic"),
    ("Sad loop",         "i-iv-VII-III",  "classic melancholic minor"),
    ("Emotional",        "i-VI-III-VII",  "moving and bittersweet"),
    ("Bittersweet",      "i-III-VI-VII",  "nostalgic minor feel"),
    ("Minor resolve",    "i-iv-v-i",      "tension that comes home"),
    ("Building dark",    "i-III-VII-VI",  "ascending, dramatic"),
    ("Andalusian",       "i-VII-VI-V",    "flamenco-inspired, fiery descent"),
    ("Haunting",         "i-VI-VII-i",    "circular and eerie"),
    ("Dorian groove",    "i-IV-VII-III",  "soulful with a bright twist"),
    ("Suspense",         "i-v-VI-III",    "tense and cinematic"),
    ("Nocturne",         "i-VII-iv-v",    "late night, deeply melancholic"),
    ("Journey",          "i-III-iv-VII",  "cinematic, slowly building"),
]

TEMPOS: list[tuple[str, int]] = [
    ("Slow",    65),
    ("Relaxed", 85),
    ("Medium",  110),
    ("Upbeat",  130),
    ("Fast",    160),
]


def chord_display_name(chord: Chord, prefer_flats: bool = False) -> str:
    names = NOTE_NAMES_FLAT if prefer_flats else NOTE_NAMES
    root = names[min(chord.notes) % 12]
    name = chord.name
    if "ø" in name:
        quality = "half-dim"
    elif "°7" in name:
        quality = "dim 7th"
    elif "°" in name:
        quality = "diminished"
    elif "9" in name and "maj" in name:
        quality = "major 9th"
    elif "9" in name and name[0].isupper():
        quality = "dominant 9th"
    elif "9" in name:
        quality = "minor 9th"
    elif "maj7" in name:
        quality = "major 7th"
    elif "7" in name and name[0].isupper():
        quality = "dominant 7th"
    elif "7" in name:
        quality = "minor 7th"
    elif name[0].islower():
        quality = "minor"
    else:
        quality = "major"
    return f"{root} {quality}"


def pick(prompt: str, options: list) -> int:
    """Print a numbered menu and return the chosen 0-based index."""
    for i, opt in enumerate(options, start=1):
        label = opt[0] if isinstance(opt, tuple) else opt
        desc  = f"  — {opt[2]}" if isinstance(opt, tuple) and len(opt) == 3 else ""
        print(f"  {i}. {label}{desc}")
    valid = [str(i) for i in range(1, len(options) + 1)]
    while True:
        raw = input(f"{prompt}: ").strip()
        if raw in valid:
            return int(raw) - 1
        print(f"  Please enter a number from 1 to {len(options)}.")


def main() -> None:
    print("Chord Progression Generator\n")
    print("No music knowledge needed — just pick what sounds right.\n")

    print("What note should the music be based around?")
    key_idx = pick("Pick a number", BEGINNER_KEYS)
    key = BEGINNER_KEYS[key_idx]

    print("\nWhat mood are you going for?")
    mood_idx = pick("Pick a number", MOODS)
    mood_label, scale_type, _ = MOODS[mood_idx]

    progressions = PROGRESSIONS_MINOR if scale_type in _MINOR_SCALES else PROGRESSIONS_MAJOR
    print("\nPick a chord progression style:")
    prog_idx = pick("Pick a number", progressions)
    prog_label, pattern, _ = progressions[prog_idx]

    print("\nHow fast should it be?")
    tempo_idx = pick("Pick a number", TEMPOS)
    tempo_label, bpm = TEMPOS[tempo_idx]

    root_midi = parse_root_midi(key)
    scale = build_scale(root_midi, scale_type)
    chords = progression_to_chords(pattern, scale)

    slug = re.sub(r"\W+", "-", f"{key}-{mood_label}-{prog_label}").strip("-").lower()
    filename = f"{slug}.mid"

    export_midi(chords, filename, bpm=bpm)

    prefer_flats = key.endswith("b")
    chord_names = " → ".join(chord_display_name(c, prefer_flats) for c in chords)
    print(f"\nDone! Saved to: {filename}")
    print(f"  Note: {key}  |  Mood: {mood_label}  |  Feel: {prog_label}  |  Tempo: {tempo_label} ({bpm} BPM)")
    print(f"  Chords: {chord_names}")


if __name__ == "__main__":
    main()
