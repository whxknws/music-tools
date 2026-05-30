#!/usr/bin/env python3
"""GUI front-end for the chord progression generator."""

import collections
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import rtmidi

RHYTHM_PATTERNS: list[tuple[str, list[float]]] = [
    # Even
    ("1 bar each",           [1]),
    ("2 bars each",          [2]),
    ("4 bars each",          [4]),
    ("Half bar each",        [0.5]),
    # Alternating
    ("Long → Short (2-1)",   [2, 1]),
    ("Short → Long (1-2)",   [1, 2]),
    # Mixed with halves
    ("Breakdown (2-1-½-½)",  [2, 1, 0.5, 0.5]),
    ("Build up (½-½-1-2)",   [0.5, 0.5, 1, 2]),
    ("Gallop (1-½-½-1)",     [1, 0.5, 0.5, 1]),
    ("Syncopated (½-1-½-2)", [0.5, 1, 0.5, 2]),
    # Longer groupings
    ("Descending (4-2-1-1)", [4, 2, 1, 1]),
    ("Ascending (1-1-2-4)",  [1, 1, 2, 4]),
    ("Varied (2-1-1-2)",     [2, 1, 1, 2]),
    ("Stutter (½-½-½-2)",    [0.5, 0.5, 0.5, 2]),
]

from chord_generator import (
    BEGINNER_KEYS,
    MOODS,
    PROGRESSIONS_MAJOR,
    PROGRESSIONS_MINOR,
    TEMPOS,
    _MINOR_SCALES,
    build_scale,
    chord_display_name,
    export_midi,
    parse_root_midi,
    progression_to_chords,
    sanitize_filename_part,
)

CHANNEL = 0
VELOCITY = 100
CLOCK_PPQN = 24  # MIDI clock pulses per quarter note


class ChordApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Chord Generator")
        self.resizable(False, False)

        self._stop_event = threading.Event()
        self._play_thread: threading.Thread | None = None
        self._midi_out: rtmidi.MidiOut | None = None
        self._midi_in: rtmidi.MidiIn | None = None
        self._tempo_bpm: float = float(TEMPOS[2][1])
        self._smooth_bpm: float = float(TEMPOS[2][1])
        # deque auto-discards old entries; stores inter-pulse delta_times from rtmidi C++ layer
        self._pulse_times: collections.deque = collections.deque(maxlen=CLOCK_PPQN * 4)
        self._pulse_count: int = 0   # counts pulses so we can rate-limit display updates
        self._chords: list = []
        self._halfstep_shifts: list[int] = []

        self._build_ui()
        self._setup_midi()
        self._on_mood_change()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build_ui(self) -> None:
        pad = {"padx": 16, "pady": 6}

        ttk.Label(self, text="Chord Generator", font=("", 17, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(18, 10)
        )

        # Note
        ttk.Label(self, text="Note:").grid(row=1, column=0, sticky="e", **pad)
        self.key_var = tk.StringVar(value=BEGINNER_KEYS[0])
        ttk.Combobox(
            self, textvariable=self.key_var, values=BEGINNER_KEYS,
            state="readonly", width=30,
        ).grid(row=1, column=1, sticky="w", **pad)
        self.key_var.trace_add("write", lambda *_: self._refresh())

        # Mood
        ttk.Label(self, text="Mood:").grid(row=2, column=0, sticky="e", **pad)
        self.mood_var = tk.StringVar(value=MOODS[0][0])
        ttk.Combobox(
            self, textvariable=self.mood_var, values=[m[0] for m in MOODS],
            state="readonly", width=30,
        ).grid(row=2, column=1, sticky="w", **pad)
        self.mood_var.trace_add("write", lambda *_: self._on_mood_change())

        # Progression
        ttk.Label(self, text="Progression:").grid(row=3, column=0, sticky="e", **pad)
        self.prog_var = tk.StringVar()
        self._prog_combo = ttk.Combobox(
            self, textvariable=self.prog_var, state="readonly", width=30,
        )
        self._prog_combo.grid(row=3, column=1, sticky="w", **pad)
        self.prog_var.trace_add("write", lambda *_: self._refresh())

        # Tempo
        ttk.Label(self, text="Tempo:").grid(row=4, column=0, sticky="e", **pad)
        self.tempo_var = tk.StringVar(value=TEMPOS[2][0])
        ttk.Combobox(
            self, textvariable=self.tempo_var, values=[t[0] for t in TEMPOS],
            state="readonly", width=30,
        ).grid(row=4, column=1, sticky="w", **pad)
        self.tempo_var.trace_add("write", lambda *_: self._refresh())

        # Chord type
        ttk.Label(self, text="Chord type:").grid(row=5, column=0, sticky="e", **pad)
        self.chord_type_var = tk.StringVar(value="Triads")
        ttk.Combobox(
            self, textvariable=self.chord_type_var,
            values=["Triads", "Seventh chords", "9th chords"],
            state="readonly", width=30,
        ).grid(row=5, column=1, sticky="w", **pad)
        self.chord_type_var.trace_add("write", lambda *_: self._refresh())

        # Rhythm pattern
        ttk.Label(self, text="Rhythm:").grid(row=7, column=0, sticky="e", **pad)
        self.rhythm_var = tk.StringVar(value=RHYTHM_PATTERNS[0][0])
        ttk.Combobox(
            self, textvariable=self.rhythm_var,
            values=[p[0] for p in RHYTHM_PATTERNS],
            state="readonly", width=30,
        ).grid(row=7, column=1, sticky="w", **pad)
        self.rhythm_var.trace_add("write", lambda *_: self._refresh())

        # Velocity variation
        ttk.Label(self, text="Velocity variation:").grid(row=8, column=0, sticky="e", **pad)
        self.vel_var_var = tk.StringVar(value="Off")
        ttk.Combobox(
            self, textvariable=self.vel_var_var,
            values=["Off", "Subtle (±8)", "Medium (±15)", "Strong (±25)"],
            state="readonly", width=30,
        ).grid(row=8, column=1, sticky="w", **pad)

        # Strum
        ttk.Label(self, text="Strum:").grid(row=9, column=0, sticky="e", **pad)
        self.strum_var = tk.StringVar(value="Off")
        ttk.Combobox(
            self, textvariable=self.strum_var,
            values=[
                "Off",
                "Up — Slow",
                "Up — Medium",
                "Up — Fast",
                "Down — Slow",
                "Down — Medium",
                "Down — Fast",
            ],
            state="readonly", width=30,
        ).grid(row=9, column=1, sticky="w", **pad)

        # Chromatic variation toggle
        self.halfstep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self,
            text="Chromatic variation  (random ±½ step per chord on Randomize)",
            variable=self.halfstep_var,
        ).grid(row=10, column=0, columnspan=2, pady=(4, 0))

        # Ableton sync
        sync_frame = ttk.Frame(self)
        sync_frame.grid(row=11, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 0))
        self.sync_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sync_frame, text="Sync to Ableton", variable=self.sync_var,
                        command=self._on_sync_toggle).pack(side="left")
        self._sync_port_var = tk.StringVar(value="")
        self._sync_port_cb = ttk.Combobox(sync_frame, textvariable=self._sync_port_var,
                                           state="readonly", width=20)
        self._sync_port_cb.pack(side="left", padx=(8, 0))
        self._populate_midi_inputs()

        ttk.Separator(self, orient="horizontal").grid(
            row=12, column=0, columnspan=2, sticky="ew", pady=8, padx=12
        )

        # Play / Stop / Export
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=13, column=0, columnspan=2, pady=(0, 4))
        self._play_btn = ttk.Button(btn_frame, text="▶  Play", command=self._play, width=13)
        self._play_btn.pack(side="left", padx=6)
        ttk.Button(btn_frame, text="■  Stop", command=self._stop, width=13).pack(side="left", padx=6)

        ttk.Button(self, text="Export .mid", command=self._export, width=34).grid(
            row=14, column=0, columnspan=2, pady=(2, 4)
        )

        ttk.Button(self, text="🎲  Randomize", command=self._randomize, width=34).grid(
            row=15, column=0, columnspan=2, pady=(0, 10)
        )

        # Chord names
        self._chord_label = ttk.Label(
            self, text="", foreground="#444", wraplength=360, justify="center",
        )
        self._chord_label.grid(row=16, column=0, columnspan=2, pady=(0, 4))

        # Status bar
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, foreground="#999").grid(
            row=17, column=0, columnspan=2, pady=(0, 12)
        )

    def _vel_variation(self) -> int:
        return {"Off": 0, "Subtle (±8)": 8, "Medium (±15)": 15, "Strong (±25)": 25}.get(
            self.vel_var_var.get(), 0
        )

    def _strum_settings(self) -> tuple[int, bool]:
        """Return (strum_ticks, strum_up). 0 ticks = off."""
        val = self.strum_var.get()
        if val == "Off":
            return 0, True
        strum_up = val.startswith("Up")
        ticks = {"Slow": 60, "Medium": 30, "Fast": 12}.get(val.split("— ")[-1], 30)
        return ticks, strum_up

    def _add_sevenths(self, chords: list) -> list:
        from dataclasses import replace as dc_replace
        result = []
        for chord in chords:
            root = min(chord.notes)
            is_major = chord.name[0].isupper() and "°" not in chord.name
            interval = 11 if is_major else 10
            seventh = root + interval
            while seventh <= max(chord.notes):
                seventh += 12
            suffix = "maj7" if is_major else "7"
            new_notes = tuple(sorted(chord.notes + (seventh,)))
            result.append(dc_replace(chord, name=chord.name + suffix, notes=new_notes))
        return result

    def _add_ninths(self, chords: list) -> list:
        from dataclasses import replace as dc_replace
        result = []
        for chord in self._add_sevenths(chords):
            root = min(chord.notes)
            ninth = root + 14  # major 9th (2 semitones + octave)
            while ninth <= max(chord.notes):
                ninth += 12
            new_notes = tuple(sorted(chord.notes + (ninth,)))
            result.append(dc_replace(chord, name=chord.name + "9", notes=new_notes))
        return result

    def _randomize(self) -> None:
        self.key_var.set(random.choice(BEGINNER_KEYS))
        self.mood_var.set(random.choice(MOODS)[0])
        self.prog_var.set(random.choice(self._prog_combo["values"]))
        self.tempo_var.set(random.choice(TEMPOS)[0])
        self.chord_type_var.set(random.choice(["Triads", "Seventh chords", "9th chords"]))
        self.rhythm_var.set(random.choice(RHYTHM_PATTERNS)[0])
        self.vel_var_var.set(random.choice(["Off", "Subtle (±8)", "Medium (±15)", "Strong (±25)"]))
        self.strum_var.set(random.choice([
            "Off", "Up — Slow", "Up — Medium", "Up — Fast",
            "Down — Slow", "Down — Medium", "Down — Fast",
        ]))
        if self.halfstep_var.get():
            # Each chord: 40% chance of shifting up, 40% down, 20% unchanged
            n = len(self._chords) or 4
            self._halfstep_shifts = [
                random.choices([-1, 0, 1], weights=[40, 20, 40])[0]
                for _ in range(n)
            ]
            self._refresh()
        else:
            self._halfstep_shifts = []
        self._status_var.set("Randomized!")

    def _on_mood_change(self) -> None:
        mood_label = self.mood_var.get()
        scale_type = next(m[1] for m in MOODS if m[0] == mood_label)
        progs = PROGRESSIONS_MINOR if scale_type in _MINOR_SCALES else PROGRESSIONS_MAJOR
        labels = [p[0] for p in progs]
        self._prog_combo["values"] = labels
        self.prog_var.set(labels[0])

    def _get_state(self) -> tuple:
        mood_label = self.mood_var.get()
        scale_type = next(m[1] for m in MOODS if m[0] == mood_label)
        progs = PROGRESSIONS_MINOR if scale_type in _MINOR_SCALES else PROGRESSIONS_MAJOR
        prog_label = self.prog_var.get()
        pattern = next(p[1] for p in progs if p[0] == prog_label)
        bpm = next(t[1] for t in TEMPOS if t[0] == self.tempo_var.get())
        return self.key_var.get(), scale_type, pattern, bpm

    def _refresh(self) -> None:
        try:
            key, scale_type, pattern, bpm = self._get_state()
            self._tempo_bpm = bpm
            durations = next(p[1] for p in RHYTHM_PATTERNS if p[0] == self.rhythm_var.get())
            scale = build_scale(parse_root_midi(key), scale_type)
            from dataclasses import replace
            base_chords = progression_to_chords(pattern, scale)
            chord_type = self.chord_type_var.get()
            if chord_type == "Seventh chords":
                base_chords = self._add_sevenths(base_chords)
            elif chord_type == "9th chords":
                base_chords = self._add_ninths(base_chords)
            shifts = self._halfstep_shifts
            self._chords = [
                replace(
                    c,
                    duration=durations[i % len(durations)],
                    notes=tuple(n + (shifts[i] if i < len(shifts) else 0) for n in c.notes),
                )
                for i, c in enumerate(base_chords)
            ]
            prefer_flats = key.endswith("b")
            self._chord_label.config(
                text="  →  ".join(chord_display_name(c, prefer_flats) for c in self._chords)
            )
        except Exception as exc:
            self._chord_label.config(text=f"Error: {exc}")

    def _setup_midi(self) -> None:
        try:
            self._midi_out = rtmidi.MidiOut()
            ports = self._midi_out.get_ports()
            if ports:
                self._midi_out.open_port(0)
                self._status_var.set(f"MIDI → {ports[0]}")
            else:
                self._midi_out.open_virtual_port("Chord Generator")
                self._status_var.set(
                    "No MIDI ports found — enable IAC Driver in Audio MIDI Setup"
                )
        except Exception as exc:
            self._status_var.set(f"MIDI unavailable: {exc}")

    def _populate_midi_inputs(self) -> None:
        tmp = rtmidi.MidiIn()
        ports = tmp.get_ports()
        del tmp
        if ports:
            self._sync_port_cb["values"] = ports
            self._sync_port_var.set(ports[0])
        else:
            self._sync_port_cb["values"] = ["No input ports found"]
            self._sync_port_var.set("No input ports found")

    def _on_sync_toggle(self) -> None:
        if self.sync_var.get():
            self._start_sync()
        else:
            self._stop_sync()

    def _start_sync(self) -> None:
        port_name = self._sync_port_var.get()
        try:
            self._midi_in = rtmidi.MidiIn()
            ports = self._midi_in.get_ports()
            if port_name not in ports:
                self._status_var.set("Sync port not found")
                self.sync_var.set(False)
                return
            self._pulse_times.clear()
            self._pulse_count = 0
            self._midi_in.open_port(ports.index(port_name))
            self._midi_in.ignore_types(sysex=True, timing=False, active_sense=True)
            self._midi_in.set_callback(self._on_clock)
            self._sync_port_cb.config(state="disabled")
            self._status_var.set(f"Waiting for Ableton clock on {port_name}…")
        except Exception as exc:
            self._status_var.set(f"Sync error: {exc}")
            self.sync_var.set(False)

    def _stop_sync(self) -> None:
        if self._midi_in:
            self._midi_in.cancel_callback()
            self._midi_in.close_port()
            del self._midi_in
            self._midi_in = None
        self._pulse_times.clear()
        self._pulse_count = 0
        self._sync_port_cb.config(state="readonly")
        self._status_var.set("Sync off")

    def _on_clock(self, message, data=None) -> None:
        """rtmidi callback — runs on rtmidi's internal thread.

        delta_time comes from CoreMIDI hardware packet timestamps (C++ layer),
        so it is immune to Python GIL / mouse-event delays.

        Two key changes vs naive approach:
        - EMA alpha=0.05 (very aggressive) so a single noisy reading moves
          the display by < 0.5 BPM and never changes the rounded integer.
        - Display is updated only once per beat (every CLOCK_PPQN pulses)
          instead of every pulse, so the tkinter label queue stays empty
          even while the mouse is moving.
        """
        msg_bytes, delta_time = message   # delta_time in seconds, CoreMIDI hardware time
        if not msg_bytes:
            return
        status = msg_bytes[0]

        if status == 0xF8:  # MIDI Clock pulse
            if delta_time > 0:
                self._pulse_times.append(delta_time)  # deque auto-drops oldest
            self._pulse_count += 1

            # ── Tempo: update every pulse once we have 2 beats of data ──────────
            if len(self._pulse_times) >= CLOCK_PPQN * 2:
                # list() snapshot avoids mutation race while summing
                snapshot = list(self._pulse_times)
                elapsed = sum(snapshot[-(CLOCK_PPQN * 2):])  # last 2 beats
                if elapsed > 0:
                    raw_bpm = 120.0 / elapsed
                    if 20.0 <= raw_bpm <= 300.0:
                        # alpha=0.05: a ±10 BPM spike shifts smooth by < 0.5
                        self._smooth_bpm = 0.05 * raw_bpm + 0.95 * self._smooth_bpm
                        self._tempo_bpm = self._smooth_bpm

            # ── Display: once per beat — avoids flooding the tkinter queue ──────
            if self._pulse_count % CLOCK_PPQN == 0 and self._smooth_bpm > 0:
                display = round(self._smooth_bpm)
                try:
                    self.after(0, lambda b=display: self._status_var.set(
                        f"Ableton sync: {b} BPM"
                    ))
                except tk.TclError:
                    pass  # window already destroyed

        elif status in (0xFA, 0xFB):  # Start / Continue
            self._pulse_times.clear()
            self._pulse_count = 0

        elif status == 0xFC:  # Stop
            try:
                self.after(0, lambda: self._status_var.set("Ableton: stopped"))
            except tk.TclError:
                pass

    def _play(self) -> None:
        if self._play_thread and self._play_thread.is_alive():
            return
        if not self._midi_out:
            messagebox.showerror("No MIDI", "No MIDI output available.")
            return
        self._stop_event.clear()
        strum_ticks, strum_up = self._strum_settings()
        self._play_thread = threading.Thread(
            target=self._play_loop,
            args=(list(self._chords), self._vel_variation(),
                  strum_ticks / 1000.0, strum_up),
            daemon=True,
        )
        self._play_thread.start()
        self._status_var.set("Playing…")

    def _play_loop(self, chords: list,
                   vel_variation: int, strum_delay: float, strum_up: bool) -> None:
        while not self._stop_event.is_set():
            for chord in chords:
                if self._stop_event.is_set():
                    break
                # Read tempo fresh each chord so sync changes take effect immediately
                spb = 60.0 / self._tempo_bpm * 4
                notes = sorted(chord.notes) if strum_up else sorted(chord.notes, reverse=True)
                for i, note in enumerate(notes):
                    if i > 0 and strum_delay:
                        time.sleep(strum_delay)
                    vel = max(1, min(127, VELOCITY + (
                        random.randint(-vel_variation, vel_variation) if vel_variation else 0
                    )))
                    self._midi_out.send_message([0x90 | CHANNEL, note, vel])
                deadline = time.monotonic() + float(chord.duration) * spb
                while time.monotonic() < deadline:
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.01)
                for note in chord.notes:
                    self._midi_out.send_message([0x80 | CHANNEL, note, 0])
        try:
            self.after(0, lambda: self._status_var.set("Stopped."))
        except tk.TclError:
            pass  # window already destroyed — ignore

    def _stop(self) -> None:
        self._stop_event.set()
        if self._midi_out:
            for note in range(128):
                self._midi_out.send_message([0x80 | CHANNEL, note, 0])

    def _export(self) -> None:
        try:
            key, scale_type, pattern, bpm = self._get_state()
            filename = (
                sanitize_filename_part(key)
                + sanitize_filename_part(scale_type)
                + "_"
                + sanitize_filename_part(pattern)
                + ".mid"
            )
            strum_ticks, strum_up = self._strum_settings()
            export_midi(self._chords, filename, bpm=bpm,
                        velocity_variation=self._vel_variation(),
                        strum_ticks=strum_ticks, strum_up=strum_up)
            self._status_var.set(f"Saved: {filename}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def destroy(self) -> None:
        # 1. Signal the play thread to stop immediately.
        self._stop_event.set()

        # 2. Kill the MIDI clock callback first — it calls self.after() and
        #    must not fire after tkinter starts tearing down.
        if self._midi_in:
            try:
                self._midi_in.cancel_callback()
                self._midi_in.close_port()
            except Exception:
                pass
            del self._midi_in
            self._midi_in = None

        # 3. Wait for the play thread to exit (up to 1 s) so it won't touch
        #    self._midi_out or call self.after() after we close everything.
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=1.0)

        # 4. All-notes-off and close MIDI output.
        if self._midi_out:
            try:
                for note in range(128):
                    self._midi_out.send_message([0x80 | CHANNEL, note, 0])
                self._midi_out.close_port()
            except Exception:
                pass
            self._midi_out = None

        super().destroy()


if __name__ == "__main__":
    ChordApp().mainloop()
