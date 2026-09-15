
"""
famistudio_rumble.py
FamiStudio Text -> Nintendo Switch Joy-Con / Pro Controller HD-rumble player.

Dependencies:
    pip install hidapi

This module intentionally implements the Switch HID rumble packet itself rather
than depending on a high-level rumble library.

Public API:
    load(path)
    play()
    pause()
    stop()
    get_state() -> 0 stopped, 1 playing, 2 paused
    set_master_pitch(a4_hz)
    test_master_pitch(channel)
    set_channel(index, channel)
    all_notes_off()
    get_pos() -> seconds
    get_battery_level(index) -> raw Switch battery/connection byte

Actuator mapping:
    0: Square1
    1: Square2
    2: Triangle
    3: Noise
    4: MMC5Square1
    5: MMC5Square2

A Joy-Con contributes one actuator. A Pro Controller contributes two.
Controllers are assigned actuator slots in enumeration order. The left/right
side of a Pro Controller maps to its two slots.

The renderer is frame-quantized at 30 Hz, matching FamiStudio tempo data.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError as exc:
    raise ImportError("famistudio_rumble requires 'hidapi' (pip install hidapi)") from exc


# ----------------------------- configuration -----------------------------

VID_NINTENDO = 0x057E
PID_JOYCON_L = 0x2006
PID_JOYCON_R = 0x2007
PID_PRO = 0x2009
PID_JOYCON_GRIP = 0x200E

FPS = 30.0
FAMISTUDIO_NTSC_FPS = 60.0
MAX_AMP_CODE = 0xC8       # requested safe maximum high-band amplitude code
DEFAULT_A4 = 440.0

# The user's six logical actuators.
CHANNELS = (
    "Square1",
    "Square2",
    "Triangle",
    "Noise",
    "MMC5Square1",
    "MMC5Square2",
)

MUSICAL_NOTE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")
ATTR_RE = re.compile(r'([A-Za-z0-9_]+)="((?:""|[^"])*)"')


# ----------------------------- data model -------------------------------

@dataclass
class FSNote:
    time: int
    value: str
    duration: int = 0
    instrument: Optional[str] = None
    volume: Optional[int] = None
    arpeggio: Optional[str] = None
    # FamiStudio notes have attack enabled by default; an explicit Attack="False"
    # means the instrument envelopes continue instead of restarting.
    attack: bool = True
    fine_pitch: int = 0
    slide_target: Optional[str] = None
    cut_delay: int = 0
    note_delay: int = 0


@dataclass
class Pattern:
    name: str
    notes: List[FSNote] = field(default_factory=list)


@dataclass
class PatternInstance:
    time: int
    pattern: str


@dataclass
class Channel:
    type: str
    patterns: Dict[str, Pattern] = field(default_factory=dict)
    instances: List[PatternInstance] = field(default_factory=list)


@dataclass
class Envelope:
    type: str
    values: List[int]
    loop: int = -1
    release: int = -1
    relative: bool = False


@dataclass
class Instrument:
    name: str
    envelopes: Dict[str, Envelope] = field(default_factory=dict)


@dataclass
class Song:
    name: str
    length: int
    loop_point: int
    pattern_length: int
    beat_length: int
    note_length: int
    groove: List[int]
    channels: List[Channel] = field(default_factory=list)
    custom_settings: Dict[int, Tuple[int, int, int, List[int], str]] = field(default_factory=dict)
    # column -> (length, note_length, beat_length, groove, groove_padding_mode)


@dataclass
class Controller:
    path: object
    product_id: int
    kind: str
    dev: object
    is_bluetooth: bool
    packet_no: int = 0
    slots: int = 1
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            try:
                self.dev.close()
            finally:
                self.closed = True


@dataclass
class RenderEvent:
    start: int
    end: int
    note: Optional[str]
    volume: int
    instrument: Optional[Instrument]
    arpeggio: Optional[Envelope]
    fine_pitch: int
    slide_target: Optional[str] = None
    duty: int = 2
    volume_env_start: int = 0


# ----------------------------- text parser -------------------------------

def _unquote(s: str) -> str:
    return s.replace('""', '"')


def _parse_line(line: str) -> Tuple[str, Dict[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return "", {}
    first = line.split(None, 1)[0]
    attrs = {}
    for m in ATTR_RE.finditer(line[len(first):]):
        attrs[m.group(1)] = _unquote(m.group(2))
    return first, attrs


def _ints(s: str) -> List[int]:
    """Parse FamiStudio comma-separated integer lists."""
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[,;]", s) if p.strip()]
    try:
        return [int(x) for x in parts]
    except ValueError:
        # Compatibility with older hyphen-separated exports.
        if re.fullmatch(r"\d+(?:-\d+)+", s):
            return [int(x) for x in s.split("-")]
        return []


def _bool(s: Optional[str], default=False) -> bool:
    if s is None:
        return default
    return s.lower() in ("true", "1", "yes")


def _make_envelope(obj: str, a: Dict[str, str]) -> Envelope:
    return Envelope(
        type=a["Type"],
        values=_ints(a.get("Values", "")),
        loop=int(a.get("Loop", "-1")),
        release=int(a.get("Release", "-1")),
        relative=_bool(a.get("Relative"), False),
    )


def _parse_famistudio_text(text: str) -> Tuple[Dict[str, Instrument], Dict[str, Envelope], List[Song], str]:
    """
    Parses the documented line-oriented FamiStudio Text representation.
    Nesting is inferred from object type, not indentation.
    """
    instruments: Dict[str, Instrument] = {}
    arpeggios: Dict[str, Envelope] = {}
    songs: List[Song] = []
    tempo_mode = "FamiStudio"

    current_instrument: Optional[Instrument] = None
    current_song: Optional[Song] = None
    current_channel: Optional[Channel] = None
    current_pattern: Optional[Pattern] = None

    for raw in text.splitlines():
        obj, a = _parse_line(raw)
        if not obj:
            continue

        if obj == "Project":
            tempo_mode = a.get("TempoMode", "FamiStudio")
            continue

        if obj == "Arpeggio" and "Name" in a:
            arpeggios[a["Name"]] = Envelope(
                type="Arpeggio", values=_ints(a.get("Values", "")),
                loop=int(a.get("Loop", "-1")))
            continue

        if obj == "Instrument":
            current_instrument = Instrument(a.get("Name", ""))
            instruments[current_instrument.name] = current_instrument
            current_song = None
            current_channel = None
            current_pattern = None
            continue

        if obj == "Envelope" and current_instrument is not None:
            env = _make_envelope(obj, a)
            current_instrument.envelopes[env.type] = env
            continue

        if obj == "Song":
            current_song = Song(
                name=a.get("Name", ""),
                length=int(a.get("Length", "0")),
                loop_point=int(a.get("LoopPoint", "-1")),
                pattern_length=int(a.get("PatternLength", "0")),
                beat_length=int(a.get("BeatLength", "0")),
                note_length=int(a.get("NoteLength", "1")),
                groove=_ints(a.get("Groove", "")),
            )
            songs.append(current_song)
            current_instrument = None
            current_channel = None
            current_pattern = None
            continue

        if obj == "PatternCustomSettings" and current_song is not None:
            t = int(a["Time"])
            current_song.custom_settings[t] = (
                int(a.get("Length", current_song.pattern_length)),
                int(a.get("NoteLength", current_song.note_length)),
                int(a.get("BeatLength", current_song.beat_length)),
                _ints(a.get("Groove", "")) or list(current_song.groove),
                a.get("GroovePaddingMode", "End"),
            )
            continue

        if obj == "Channel" and current_song is not None:
            current_channel = Channel(a["Type"])
            current_song.channels.append(current_channel)
            current_pattern = None
            continue

        if obj == "Pattern" and current_channel is not None:
            current_pattern = Pattern(a["Name"])
            current_channel.patterns[current_pattern.name] = current_pattern
            continue

        if obj == "Note" and current_pattern is not None:
            current_pattern.notes.append(FSNote(
                time=int(a["Time"]),
                value=a.get("Value", "Stop"),
                duration=int(a.get("Duration", "0")),
                instrument=a.get("Instrument"),
                volume=int(a["Volume"]) if "Volume" in a else None,
                arpeggio=a.get("Arpeggio"),
                attack=_bool(a.get("Attack")),
                fine_pitch=int(a.get("FinePitch", "0")),
                slide_target=a.get("SlideTarget"),
                cut_delay=int(a.get("CutDelay", "0")),
                note_delay=int(a.get("NoteDelay", "0")),
            ))
            continue

        if obj == "PatternInstance" and current_channel is not None:
            current_channel.instances.append(
                PatternInstance(int(a["Time"]), a["Pattern"])
            )
            continue

    if not songs:
        raise ValueError("No Song object was found in the FamiStudio text file.")
    return instruments, arpeggios, songs, tempo_mode


# ----------------------------- musical math ------------------------------

NOTE_BASE = {
    "C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3,
    "E": 4, "F": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8,
    "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11,
}


def note_to_midi(note: str) -> Optional[int]:
    m = MUSICAL_NOTE.match(note.strip())
    if not m:
        return None
    key = (m.group(1).upper() + m.group(2).upper())
    if key not in NOTE_BASE:
        return None
    return (int(m.group(3)) + 1) * 12 + NOTE_BASE[key]


def note_to_hz(note: str, a4_hz: float) -> Optional[float]:
    midi = note_to_midi(note)
    if midi is None:
        return None
    return a4_hz * (2.0 ** ((midi - 69) / 12.0))


def envelope_value(env: Optional[Envelope], frame: int, default: int) -> int:
    if env is None or not env.values:
        return default
    n = len(env.values)
    if frame < n:
        return env.values[frame]
    if env.loop >= 0 and env.loop < n:
        span = n - env.loop
        if span > 0:
            return env.values[env.loop + ((frame - env.loop) % span)]
    return env.values[-1]


def _pitch_envelope_semitones(env: Optional[Envelope], frame: int) -> float:
    # FamiStudio text stores pitch-envelope values in semitone units.
    # Relative pitch envelopes are offsets; absolute envelopes are interpreted
    # as the stored pitch offset at the current frame for this player.
    return float(envelope_value(env, frame, 0))


def _arpeggio_offset(env: Optional[Envelope], frame: int) -> int:
    return envelope_value(env, frame, 0)


# -------------------------- timeline construction ------------------------

def _column_settings(song: Song, column: int) -> Tuple[int, int, int, List[int], str]:
    """Return the effective FamiStudio tempo settings for a pattern column."""
    if column in song.custom_settings:
        return song.custom_settings[column]
    return (
        song.pattern_length,
        song.note_length,
        song.beat_length,
        list(song.groove),
        "End",
    )


def _groove_note_lengths(note_count: int, note_length: int, groove: List[int]) -> List[int]:
    """Expand a FamiStudio groove into one frame length per note.

    In FamiStudio tempo mode, Groove is the actual repeating sequence of
    frame lengths between note positions.  A 7,6,6 groove therefore makes
    note positions 0,7,13,19,... rather than treating every note as 7 frames.
    """
    if note_count <= 0:
        return []
    if groove:
        return [max(1, int(groove[i % len(groove)])) for i in range(note_count)]
    return [max(1, int(note_length))] * note_count


def _pattern_span(song: Song, column: int) -> int:
    length, note_len, _, groove, _padding = _column_settings(song, column)
    return sum(_groove_note_lengths(length, note_len, groove))


def _column_start(song: Song, column: int) -> int:
    # PatternInstance.Time is a pattern-column index.  Each preceding column
    # consumes its effective number of groove frames.
    return sum(_pattern_span(song, i) for i in range(column))

def _channel_events(
    song: Song,
    channel: Channel,
    instruments: Dict[str, Instrument],
    arpeggios: Dict[str, Envelope],
) -> List[RenderEvent]:
    events: List[RenderEvent] = []
    # FamiStudio instrument envelopes are stateful: notes normally restart
    # them (Attack=True), while notes with Attack=False continue the previous
    # envelope when the instrument is compatible. Keep a frame cursor so the
    # volume envelope is evaluated at the correct position.
    envelope_cursor = 0
    previous_instrument = None
    for inst in channel.instances:
        p = channel.patterns.get(inst.pattern)
        if p is None:
            continue
        base = _column_start(song, inst.time)
        ordered = sorted(p.notes, key=lambda n: n.time)
        for idx, n in enumerate(ordered):
            start = base + n.time + n.note_delay
            if n.duration > 0:
                end = start + max(1, n.duration - n.note_delay)
            elif idx + 1 < len(ordered):
                end = base + ordered[idx + 1].time
            else:
                end = base + _pattern_span(song, inst.time)

            if n.cut_delay > 0:
                end = min(end, start + n.cut_delay)

            if end <= start:
                continue

            inst_obj = instruments.get(n.instrument or "")
            arp = None
            if n.arpeggio:
                # Explicit note arpeggio name is represented by an Arpeggio
                # object in FamiStudio text. We resolve it below by attaching
                # the object through a temporary lookup in _render_channel.
                pass

            note_instrument_changed = inst_obj is not previous_instrument
            env_start = 0 if n.attack or note_instrument_changed else envelope_cursor
            events.append(RenderEvent(
                start=start,
                end=end,
                note=None if n.value in ("Stop", "Release") else n.value,
                volume=n.volume if n.volume is not None else 15,
                instrument=inst_obj,
                arpeggio=arpeggios.get(n.arpeggio) if n.arpeggio else arp,
                fine_pitch=n.fine_pitch,
                slide_target=n.slide_target,
                duty=2,
                volume_env_start=env_start,
            ))
            envelope_cursor = env_start + max(0, end - start)
            previous_instrument = inst_obj
    return sorted(events, key=lambda e: e.start)


def _find_event(events: List[RenderEvent], frame: int) -> Optional[RenderEvent]:
    # Event lists are normally short enough that a linear search is fine for
    # a GUI music player; use the last matching event.
    active = None
    for e in events:
        if e.start > frame:
            break
        if e.start <= frame < e.end:
            active = e
    return active


# --------------------------- Switch HID rumble ---------------------------

def _encode_frequency(freq_hz: float) -> Tuple[int, int]:
    """
    Return (high_band_9bit, low_band_7bit).

    The Switch encoding is logarithmic:
        encoded = round(log2(freq / 10) * 32)

    HF field begins at encoded 0x60 and LF at 0x40.
    """
    f = max(40.875885, min(1252.572266, float(freq_hz)))
    encoded = int(round(math.log2(f / 10.0) * 32.0))
    lf = max(0x01, min(0x7F, encoded - 0x40))
    hf = max(0x0000, min(0x01FC, (encoded - 0x60) * 4))
    return hf, lf


# Exact SDL safe amplitude thresholds, expressed as the 16-bit input scale
# used by SDL's Nintendo HID implementation. The final code tops out at 0xC8.
_HFA_THRESHOLDS = [
    0, 514, 775, 921, 1096, 1303, 1550, 1843, 2192, 2606, 3100,
    3686, 4383, 5213, 6199, 7372, 7698, 8039, 8395, 8767, 9155, 9560,
    9984, 10426, 10887, 11369, 11873, 12398, 12947, 13520, 14119, 14744,
    15067, 15397, 15734, 16079, 16431, 16790, 17158, 17534, 17918, 18310,
    18711, 19121, 19540, 19967, 20405, 20851, 21308, 21775, 22251, 22739,
    23236, 23745, 24265, 24797, 25340, 25894, 26462, 27041, 27633, 28238,
    28856, 29488, 30134, 30794, 31468, 32157, 32861, 33581, 34316, 35068,
    35836, 36620, 37422, 38242, 39079, 39935, 40809, 41703, 42616, 43549,
    44503, 45477, 46473, 47491, 48531, 49593, 50679, 51789, 52923, 54082,
    55266, 56476, 57713, 58977, 60268, 61588, 62936, 64315, 65535
]
_HFA_CODES = [i * 2 for i in range(101)]

_LFA_CODES = [
    0x0040, 0x8040, 0x0041, 0x8041, 0x0042, 0x8042, 0x0043, 0x8043,
    0x0044, 0x8044, 0x0045, 0x8045, 0x0046, 0x8046, 0x0047, 0x8047,
    0x0048, 0x8048, 0x0049, 0x8049, 0x004A, 0x804A, 0x004B, 0x804B,
    0x004C, 0x804C, 0x004D, 0x804D, 0x004E, 0x804E, 0x004F, 0x804F,
    0x0050, 0x8050, 0x0051, 0x8051, 0x0052, 0x8052, 0x0053, 0x8053,
    0x0054, 0x8054, 0x0055, 0x8055, 0x0056, 0x8056, 0x0057, 0x8057,
    0x0058, 0x8058, 0x0059, 0x8059, 0x005A, 0x805A, 0x005B, 0x805B,
    0x005C, 0x805C, 0x005D, 0x805D, 0x005E, 0x805E, 0x005F, 0x805F,
    0x0060, 0x8060, 0x0061, 0x8061, 0x0062, 0x8062, 0x0063, 0x8063,
    0x0064, 0x8064, 0x0065, 0x8065, 0x0066, 0x8066, 0x0067, 0x8067,
    0x0068, 0x8068, 0x0069, 0x8069, 0x006A, 0x806A, 0x006B, 0x806B,
    0x006C, 0x806C, 0x006D, 0x806D, 0x006E, 0x806E, 0x006F, 0x806F,
    0x0070, 0x8070, 0x0071, 0x8071, 0x0072
]


def _nearest_threshold_index(v: int) -> int:
    lo, hi = 0, len(_HFA_THRESHOLDS) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if v <= _HFA_THRESHOLDS[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _encode_amplitude(norm: float) -> Tuple[int, int]:
    """
    Convert 0..1 to (HF amplitude byte, LF amplitude 9-bit field), using the
    same safe table used by SDL. 1.0 maps to HF 0xC8 / LF 0x0072.
    """
    norm = max(0.0, min(1.0, float(norm)))
    v = int(round(norm * 65535.0))
    idx = _nearest_threshold_index(v)
    return min(MAX_AMP_CODE, _HFA_CODES[idx]), _LFA_CODES[idx]


def _neutral_rumble() -> bytes:
    return bytes((0x00, 0x01, 0x40, 0x40))


def _noise_rumble4(seed: int, frame: int, amp: float, pitch_hz: float = 0.0) -> bytes:
    """Generate tactile static by stochastic frequency/amplitude shuffling.

    NES Noise is intentionally *not* rendered as a pitched carrier or as a
    conventional drum envelope.  Every HID update gets a new, uncorrelated
    frequency and amplitude selection.  Keeping the selections in the
    controller's useful haptic region makes the LRA mechanically smear the
    discontinuous commands into a rough, broadband/static-like sensation.

    ``seed`` is different for the two sides of a Pro Controller, so its left
    and right actuators receive independent sequences. ``pitch_hz`` is kept
    in the signature for compatibility with the renderer, but noise pitch no
    longer selects a snare/cymbal timbre.
    """
    if amp <= 0.0:
        return _neutral_rumble()

    # Two independent pseudo-random values per packet.  A new frame means a
    # completely new pair of frequency/amplitude choices; there is no held
    # carrier that can turn into a recognizable "bloop".
    r_freq = _noise_state(frame, seed ^ 0x13579BDF)
    r_amp = _noise_state(frame, seed ^ 0x2468ACE0)
    r_lf = _noise_state(frame, seed ^ 0x9E3779B9)

    u_freq = r_freq / 4294967295.0
    u_amp = r_amp / 4294967295.0
    u_lf = r_lf / 4294967295.0

    # Stochastic frequency shuffling across the effective tactile range.
    # Use separate HF/LF choices so both encoded frequency fields move rather
    # than leaving one side of the rumble packet static.
    hf_freq = 125.0 + u_freq * 275.0   # 125..400 Hz
    lf_freq = 80.0 + u_lf * 170.0      # 80..250 Hz

    # Independently shuffle amplitude every packet as well.  Keep a useful
    # floor so the static does not collapse into regularly spaced silence,
    # while still allowing strong mechanical micro-shocks.  The caller's
    # FamiStudio envelope remains the master amplitude control.
    hf_norm = amp * (0.55 + 0.45 * u_amp)
    # Derive a second amplitude value from the same fresh random state without
    # making the two bands identical.
    u_amp_lf = ((_noise_state(frame, seed ^ 0xC2B2AE35) & 0xFFFFFFFF) / 4294967295.0)
    lf_norm = amp * (0.35 + 0.50 * u_amp_lf)

    hf, _ = _encode_frequency(hf_freq)
    _, lf = _encode_frequency(lf_freq)
    ha, _ = _encode_amplitude(hf_norm)
    _, la = _encode_amplitude(lf_norm)

    return bytes((
        hf & 0xFF,
        ha | ((hf >> 8) & 0x01),
        lf | ((la >> 8) & 0x80),
        la & 0xFF,
    ))

def _rumble4(freq: float, amp: float, noise: bool = False, frame: int = 0, seed: int = 0, noise_pitch_hz: float = 0.0) -> bytes:
    if amp <= 0.0:
        return _neutral_rumble()
    if noise:
        return _noise_rumble4(seed, frame, amp, noise_pitch_hz)
    hf, lf = _encode_frequency(freq)
    ha, la = _encode_amplitude(amp)
    return bytes((
        hf & 0xFF,
        ha | ((hf >> 8) & 0x01),
        lf | ((la >> 8) & 0x80),
        la & 0xFF,
    ))


def _is_bt(info: dict) -> bool:
    # HIDAPI reports a bus_type enum on recent builds. Bluetooth is commonly
    # 0x0005 in HIDAPI, but product/path also works as a fallback.
    bus = info.get("bus_type")
    # HIDAPI versions differ here: some expose SDL-style bus values
    # (Bluetooth == 2), while older builds/platforms may expose Linux
    # transport values (Bluetooth == 5).  Accept both.
    if bus is not None and str(bus).lower() in ("bluetooth", "2", "0x2", "5", "0x5"):
        return True
    path = str(info.get("path", ""))
    return "bluetooth" in path.lower() or "bth" in path.lower()


def _controller_kind(pid: int) -> Tuple[str, int]:
    if pid == PID_JOYCON_L:
        return "joycon-left", 1
    if pid == PID_JOYCON_R:
        return "joycon-right", 1
    if pid == PID_PRO:
        return "pro", 2
    if pid == PID_JOYCON_GRIP:
        return "grip", 1
    return "unknown", 0


def _send(dev: Controller, payload: bytearray) -> None:
    """Send a Switch HID output report and fail loudly on short writes."""
    n = dev.dev.write(payload)
    if n is None or n < 0:
        raise OSError("hid_write failed")
    if n != len(payload):
        raise OSError(f"short HID write: {n}/{len(payload)} bytes")


def _test_rumble_packet(dev: Controller, side: int, freq_hz: Optional[float] = None,
                        noise: bool = False, frame: int = 0) -> bytearray:
    """Build a test rumble for one physical controller slot."""
    if side < 0 or side >= dev.slots:
        raise IndexError("invalid controller side")
    if noise:
        strong = _rumble4(160.0, 1.0, noise=True, frame=frame, seed=0x4E4F4953 + side, noise_pitch_hz=freq_hz or _master_pitch)
    else:
        freq = _master_pitch if freq_hz is None else freq_hz
        amp = 1.0
        strong = _rumble4(freq, amp)
    neutral = _neutral_rumble()
    if dev.slots == 1:
        rumble = strong
    elif side == 0:
        rumble = strong + neutral
    else:
        rumble = neutral + strong
    return _packet(dev, 0x10, rumble)


def _packet(dev: Controller, report_id: int, rumble: bytes,
            subcmd: Optional[int] = None,
            subdata: bytes = b"") -> bytearray:
    size = 49 if dev.is_bluetooth else 64
    b = bytearray(size)
    b[0] = report_id
    b[1] = dev.packet_no & 0x0F
    dev.packet_no = (dev.packet_no + 1) & 0x0F
    b[2:10] = rumble[:8].ljust(8, b"\x00")
    if subcmd is not None:
        b[10] = subcmd
        b[11:11 + len(subdata)] = subdata[:size - 11]
    return b


def _setup_controller(c: Controller) -> None:
    """
    Setup sequence based on the Switch HID protocol used by SDL/Linux.

    Bluetooth controllers need no proprietary USB handshake. USB Pro/Grip
    devices use the 0x80 proprietary handshake sequence before normal packets.
    """
    if not c.is_bluetooth:
        for cmd in (0x02, 0x03, 0x02):
            p = bytearray(64)
            p[0] = 0x80
            p[1] = cmd
            _send(c, p)
            time.sleep(0.01)
        p = bytearray(64)
        p[0] = 0x80
        p[1] = 0x04
        _send(c, p)
        time.sleep(0.02)

    # Enable vibration. We don't require the reply for playback.
    _send(c, _packet(c, 0x01, _neutral_rumble() * 2, 0x48, b"\x01"))
    time.sleep(0.02)


_last_hid_diagnostics: List[str] = []
_last_play_error: Optional[str] = None


def _enumerate_controllers() -> List[Controller]:
    """Enumerate Nintendo HID devices and keep failures for a useful error."""
    global _last_hid_diagnostics
    found: List[Controller] = []
    _last_hid_diagnostics = []

    # Do not rely on hid.enumerate(VID, PID) filtering: some hidapi Python
    # bindings/backends have historically behaved differently for Bluetooth.
    try:
        infos = hid.enumerate()
    except Exception as exc:
        _last_hid_diagnostics.append(f"hid.enumerate() failed: {exc}")
        return found

    for info in infos:
        vid = int(info.get("vendor_id", 0) or 0)
        pid = int(info.get("product_id", 0) or 0)
        if vid != VID_NINTENDO:
            continue

        kind, slots = _controller_kind(pid)
        if not slots:
            continue

        path = info.get("path")
        label = (
            f"VID={vid:04X} PID={pid:04X} "
            f"product={info.get('product_string')!r} "
            f"manufacturer={info.get('manufacturer_string')!r} "
            f"bus={info.get('bus_type')!r}"
        )

        d = None
        try:
            if path is None:
                raise OSError("HIDAPI returned no device path")

            d = hid.device()
            d.open_path(path)

            c = Controller(
                path=path,
                product_id=pid,
                kind=kind,
                dev=d,
                is_bluetooth=_is_bt(info),
                slots=slots,
            )
            _setup_controller(c)
            found.append(c)
            _last_hid_diagnostics.append(f"OK: {label}")
        except Exception as exc:
            _last_hid_diagnostics.append(f"FAILED: {label}: {exc}")
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass

    return found


def _read_controller_report(c: Controller, timeout_ms: int = 250) -> bytearray:
    """Read one live Switch input report from a controller.

    The standard full controller report (0x30) contains the raw battery and
    connection byte at offset 2.  Input reports are streamed by the controller
    after initialization, so a short blocking read is sufficient here.
    """
    deadline = time.monotonic() + max(1, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        data = c.dev.read(64, remaining)
        if not data:
            continue
        report = bytearray(data)
        if report[0] in (0x30, 0x31, 0x21, 0x3F) and len(report) >= 3:
            return report
    raise TimeoutError(f"Timed out waiting for a battery report from {c.kind}.")


def _battery_raw(c: Controller) -> int:
    """Return the exact raw battery/connection byte sent by the controller."""
    report = _read_controller_report(c)
    return int(report[2])


def _logical_controller_slots() -> List[Tuple[int, Controller, int]]:
    """Return (logical actuator index, controller, side) for each physical slot."""
    result = []
    slot = 0
    for c in _controllers:
        for side in range(c.slots):
            result.append((slot + 1, c, side))
            slot += 1
    return result


def _battery_is_low(raw: int) -> bool:
    """Switch Pro battery levels are 0,2,4,6,8 in the high nibble.

    4 is the controller's low-battery level; 2 is critical and 0 is empty.
    The low nibble contains charging/connection information and is ignored.
    """
    return (raw & 0xF0) <= 0x20


def _write_controller_slots(c: Controller, values: List[Tuple[float, float]]) -> None:
    """
    values has one tuple per physical actuator side represented by this
    controller. Joy-Con uses only the first slot; Pro uses both.
    """
    if c.slots == 1:
        f, a = values[0]
        if f < 0.0:
            r = _rumble4(160.0, a, noise=True, frame=_render_hid_frame, seed=0x4E4F4953, noise_pitch_hz=abs(f))
        else:
            r = _rumble4(f, a)
        packet = _packet(c, 0x10, r + _neutral_rumble())
    else:
        l_f, l_a = values[0]
        r_f, r_a = values[1]
        l = _rumble4(160.0, l_a, noise=True, frame=_render_hid_frame, seed=0x4E4F4953, noise_pitch_hz=abs(l_f)) if l_f < 0.0 else _rumble4(l_f, l_a)
        r = _rumble4(160.0, r_a, noise=True, frame=_render_hid_frame, seed=0x4E4F4954, noise_pitch_hz=abs(r_f)) if r_f < 0.0 else _rumble4(r_f, r_a)
        packet = _packet(c, 0x10, l + r)
    _send(c, packet)


# ------------------------------- engine ----------------------------------

_state = 0
_song: Optional[Song] = None
_instruments: Dict[str, Instrument] = {}
_events: Dict[str, List[RenderEvent]] = {}
_controllers: List[Controller] = []
_master_pitch = DEFAULT_A4
_position = 0.0
_loop_enabled = True
# 1-based actuator index -> 1-based logical channel. Default is 1->1, ..., 6->6.
_channel_map = [1, 2, 3, 4, 5, 6]
_thread: Optional[threading.Thread] = None
_stop_evt = threading.Event()
_wakeup_evt = threading.Event()
_render_hid_frame = 0
_lock = threading.RLock()


def _close_controllers() -> None:
    global _controllers
    for c in _controllers:
        try:
            # stop locally before close
            _write_controller_slots(c, [(160.0, 0.0)] * c.slots)
        except Exception:
            pass
        c.close()
    _controllers = []


def load(path) -> None:
    """
    Clear the old song and parse a new FamiStudio Text file.

    The parser intentionally targets TempoMode="FamiStudio", because that is
    the format whose timing is directly represented at the requested 30 Hz
    frame rate.
    """
    global _song, _instruments, _events, _state, _position
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")

    instruments, arpeggios, songs, tempo_mode = _parse_famistudio_text(text)
    if tempo_mode.lower() != "famistudio":
        raise ValueError(
            "This backend requires a FamiStudio-tempo text export "
            "(TempoMode=\"FamiStudio\"); FamiTracker tempo is not 30-fps frame data."
        )

    with _lock:
        stop()
        _instruments = instruments
        _song = songs[0]
        _events = {}
        for ch in _song.channels:
            evs = _channel_events(_song, ch, _instruments, arpeggios)
            for e in evs:
                if e.arpeggio is None and e.instrument:
                    e.arpeggio = e.instrument.envelopes.get("Arpeggio")
            _events[ch.type] = evs

        _position = 0.0
        _state = 0
        _stop_evt.clear()
        _wakeup_evt.clear()


def _noise_state(frame: int, seed: int) -> int:
    """Deterministic 32-bit pseudo-random state for tactile noise synthesis."""
    x = (seed ^ 0xA3C59AC3) & 0xFFFFFFFF
    x ^= (frame * 0x45D9F3B) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x7FEB352D) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x846CA68B) & 0xFFFFFFFF
    x ^= x >> 16
    return x & 0xFFFFFFFF


def _noise_rumble_value(frame: int, seed: int, local: float = 0.0,
                        duration: float = 10.0, pitch_hz: float = 0.0) -> Tuple[float, float]:
    """Model an NES noise note as a tactile drum hit rather than a tone.

    The important distinction is that a drum needs a sharp transient and a
    decaying burst. Changing the carrier every 30 Hz is not enough: the motor
    can still make each carrier sound like a sequence of bloops. This model
    therefore uses a short, deterministic attack/decay and brief gaps between
    noisy rumble bursts. Lower noise pitches get a snare-like body; higher
    pitches get a brighter cymbal-like burst.
    """
    r = _noise_state(frame, seed)
    u = r / 4294967295.0
    high = pitch_hz >= 600.0

    # One FamiStudio noise note is treated as one drum event. For long notes,
    # after the initial hit, continue with very soft intermittent noise rather
    # than a continuously voiced carrier.
    if local < 0.20:
        # The first 200 ms is the actual drum transient.
        decay = max(0.0, 1.0 - local / (0.20 if not high else 0.28))
        # Make the first few HID frames deliberately different, giving the
        # motor a physical attack instead of a steady pitched vibration.
        if high:
            freq = 900.0 + u * 650.0
            amp = 0.95 * (0.55 + 0.45 * decay)
        else:
            if local < (1.0 / FPS):
                freq = 105.0 + u * 45.0
                amp = 1.0
            else:
                freq = 550.0 + u * 750.0
                amp = 0.92 * (0.45 + 0.55 * decay)

        # Alternate the noisy carrier on/off during the transient. This is
        # much closer to a drum's impulse than holding a carrier continuously.
        if frame & 1 and local > (1.0 / FPS):
            amp *= 0.30
        return freq, amp

    if duration > 0.25 and local < duration:
        # A sustained NES-noise note gets sparse residual texture, not a tone.
        # About half the frames are silent, which prevents a motor from locking
        # onto a recognizable pitch.
        if ((r >> 3) & 0x03) != 0:
            return 180.0, 0.0
        freq = (850.0 + u * 650.0) if high else (450.0 + u * 700.0)
        return freq, 0.22 if high else 0.16

    return 180.0, 0.0

def _channel_value(channel_type: str, frame: int) -> Tuple[float, float]:
    ev = _find_event(_events.get(channel_type, []), frame)
    if ev is None or ev.note is None:
        return 160.0, 0.0

    local = max(0, frame - ev.start)
    envelope_frame = max(0, ev.volume_env_start + local)

    # FamiStudio has two volume controls: the note volume and the
    # instrument Volume envelope.  The envelope is an absolute 0..15
    # envelope, while the note volume acts as a per-note multiplier.  Apply
    # both here so attack/decay/sustain changes in the instrument are audible
    # on every rendered channel, including the synthesized Noise drums.
    note_vol = max(0, min(15, int(ev.volume)))
    vol_env = ev.instrument.envelopes.get("Volume") if ev.instrument else None
    env_vol = envelope_value(vol_env, envelope_frame, 15)
    env_vol = max(0, min(15, int(env_vol)))
    # With no instrument Volume envelope, FamiStudio behaves as a full 15/15
    # envelope. With an envelope, its value is multiplied by the note volume.
    amp = (env_vol / 15.0) * (note_vol / 15.0)

    if channel_type == "Noise":
        # NES noise is not a pitched oscillator. Synthesize a transient/noisy
        # rumble carrier instead of choosing one "noise frequency" per frame.
        seed = note_to_midi(ev.note) or 0x1234
        noise_pitch = note_to_hz(ev.note, _master_pitch) or 0.0
        freq, noise_amp = _noise_rumble_value(
            frame, seed, float(local) / FAMISTUDIO_NTSC_FPS,
            float(max(1, ev.end - ev.start)) / FAMISTUDIO_NTSC_FPS,
            noise_pitch,
        )
        return -freq, amp * noise_amp

    base = note_to_hz(ev.note, _master_pitch)
    if base is None:
        return 160.0, 0.0
    arp = _arpeggio_offset(ev.arpeggio, local)
    pitch_env = ev.instrument.envelopes.get("Pitch") if ev.instrument else None
    pitch = _pitch_envelope_semitones(pitch_env, local)

    # FinePitch is the FamiStudio fine-pitch value. Treat it as a 1/128
    # semitone correction, matching its documented integer range.
    semitones = arp + pitch + (ev.fine_pitch / 128.0)
    freq = base * (2.0 ** (semitones / 12.0))

    # FamiStudio slide notes store the target pitch directly on the note.
    # Interpolate in semitone/frequency space over the note's actual duration
    # so the rumble follows the same start -> target motion instead of
    # remaining stuck on the starting pitch.
    if ev.slide_target:
        target = note_to_hz(ev.slide_target, _master_pitch)
        if target is not None and ev.end > ev.start:
            progress = min(1.0, max(0.0, local / float(ev.end - ev.start)))
            # Preserve any envelope/arpeggio/fine-pitch offset while sliding
            # between the two musical pitches.
            start_log = math.log2(base)
            target_log = math.log2(target)
            slide_base = 2.0 ** (start_log + (target_log - start_log) * progress)
            freq = slide_base * (2.0 ** (semitones / 12.0))

    # Switch rumble's useful frequency range is much higher than the
    # fundamental range of NES square notes.  Render both NES square channels
    # one octave up (8va) while leaving Triangle at its original pitch.
    if channel_type in ("Square1", "Square2", "MMC5Square1", "MMC5Square2"):
        freq *= 2.0

    # Preserve a hard ceiling of 0xC8 through _encode_amplitude().
    return freq, amp


def _render_frame(frame: int) -> None:
    global _last_hid_diagnostics, _render_hid_frame
    _render_hid_frame = frame
    # The physical actuator index is independently mapped to a logical
    # channel. This defaults to 1->1, 2->2, ..., 6->6.
    logical: List[Tuple[float, float]] = []
    for mapped_channel in _channel_map:
        ch = CHANNELS[mapped_channel - 1]
        if ch.startswith("MMC5") and ch not in _events:
            logical.append((160.0, 0.0))
        else:
            logical.append(_channel_value(ch, frame))

    # Assign logical actuators to physical controller slots.
    slot = 0
    for c in _controllers:
        vals = []
        for _ in range(c.slots):
            if slot < len(logical):
                vals.append(logical[slot])
            else:
                vals.append((160.0, 0.0))
            slot += 1
        try:
            _write_controller_slots(c, vals)
        except Exception as exc:
            _last_hid_diagnostics.append(
                f"PLAYBACK HID write failed on {c.kind}: {type(exc).__name__}: {exc}"
            )


def _play_thread() -> None:
    global _position, _state
    next_t = time.monotonic()
    frame = int(round(_position * FAMISTUDIO_NTSC_FPS))
    try:
        _play_thread_body(next_t, frame)
    except Exception as exc:
        # Keep GUI state truthful if rendering itself fails.
        with _lock:
            _state = 0
        try:
            import traceback
            traceback.print_exc()
        except Exception:
            pass

def _play_thread_body(next_t: float, frame: int) -> None:
    global _position, _state, _last_play_error

    while not _stop_evt.is_set():
        with _lock:
            if _state == 2:
                _wakeup_evt.wait(0.1)
                _wakeup_evt.clear()
                next_t = time.monotonic()
                continue
            if _state != 1:
                break
            song = _song

        if song is None:
            break

        # FamiStudio tempo data is expressed in NTSC 60 Hz frames.  The
        # requested rumble renderer runs at 30 Hz, so each HID render tick
        # advances the musical timeline by two FamiStudio frames.  This keeps
        # the song's Groove/tempo intact instead of making a 60-frame second
        # play back as a 30-frame second.
        render_frame = frame
        try:
            _render_frame(render_frame)
        except Exception as exc:
            _last_play_error = f"{type(exc).__name__}: {exc}"
            with _lock:
                _state = 0
            raise

        frame += 2
        with _lock:
            _position = frame / FAMISTUDIO_NTSC_FPS

        # Song Length is the number of pattern columns.
        total_frames = sum(
            _pattern_span(song, i) for i in range(max(0, song.length))
        )
        if total_frames > 0 and frame >= total_frames:
            if _loop_enabled and song.loop_point >= 0 and song.loop_point < song.length:
                frame = _column_start(song, song.loop_point)
                with _lock:
                    _position = frame / FAMISTUDIO_NTSC_FPS
            else:
                # Natural end-of-song is a stopped state, just like stop().
                # Reset the playhead so a subsequent play() starts the song
                # from the beginning instead of immediately ending again.
                with _lock:
                    _state = 0
                    _position = 0.0
                break

        next_t += 1.0 / FPS
        delay = next_t - time.monotonic()
        if delay > 0:
            _stop_evt.wait(delay)
        else:
            next_t = time.monotonic()


def play() -> None:
    global _state, _thread, _controllers, _last_play_error, _last_hid_diagnostics
    with _lock:
        if _song is None:
            raise RuntimeError("No song is loaded.")

        _last_play_error = None
        _last_hid_diagnostics = []

        playable = sum(
            1 for events in _events.values() for e in events if e.note is not None
        )
        if playable == 0:
            raise RuntimeError(
                "The song loaded successfully, but no playable note events were "
                "created. Check the FamiStudio Text export."
            )

        _close_controllers()
        _controllers = _enumerate_controllers()
        if not _controllers:
            detail = "\n".join(_last_hid_diagnostics)
            if detail:
                raise RuntimeError(
                    "No compatible Nintendo Switch controllers could be opened. "
                    "HIDAPI saw the following Nintendo devices:\n" + detail
                )
            raise RuntimeError(
                "No Nintendo Switch controllers were visible to HIDAPI. "
                "Check HIDAPI installation and Linux hidraw/udev permissions."
            )

        try:
            _check_batteries_before_play()
        except Exception:
            _close_controllers()
            _controllers = []
            raise

        _stop_evt.clear()
        _state = 1
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_play_thread, name="FamiStudioRumble", daemon=True)
            _thread.start()
        _wakeup_evt.set()


def set_loop(enabled: bool) -> None:
    """Enable or disable automatic looping at the song loop point.

    When enabled (the default), reaching the FamiStudio LoopPoint repeats
    from that point. When disabled, playback stops at the end of the song.
    """
    global _loop_enabled
    with _lock:
        _loop_enabled = bool(enabled)


def pause() -> None:
    global _state
    with _lock:
        if _state == 1:
            _state = 2
            _wakeup_evt.set()
            all_notes_off()


def stop() -> None:
    global _state, _position, _thread
    with _lock:
        _state = 0
        _stop_evt.set()
        _wakeup_evt.set()
        _position = 0.0
        for c in _controllers:
            try:
                _write_controller_slots(c, [(160.0, 0.0)] * c.slots)
            except Exception:
                pass


def get_state() -> int:
    with _lock:
        return _state


def set_master_pitch(a4_hz: float) -> None:
    global _master_pitch
    if not math.isfinite(a4_hz) or a4_hz <= 0:
        raise ValueError("A4 frequency must be a positive finite number.")
    with _lock:
        _master_pitch = float(a4_hz)


def get_battery_level(index: int) -> int:
    """Return the raw battery/connection byte sent by the mapped controller.

    ``index`` is a one-based physical/logical actuator index (1-6).  A Pro
    Controller has two actuators, so both indices assigned to it report the
    same controller battery byte.  The returned value is the exact byte from
    Switch input report 0x30 byte 2; its high nibble is the battery level and
    its low nibble contains charging/connection flags.
    """
    if index < 1 or index > len(CHANNELS):
        raise ValueError("index must be in the range 1-6")
    with _lock:
        if not _controllers:
            cs = _enumerate_controllers()
            if not cs:
                raise RuntimeError("No compatible Nintendo Switch controllers are connected.")
            globals()["_controllers"] = cs
        for logical_index, c, _side in _logical_controller_slots():
            if logical_index == index:
                return _battery_raw(c)
    raise IndexError("No controller actuator exists at the requested index")


def _check_batteries_before_play() -> None:
    """Raise RuntimeError if any connected actuator reports a low battery."""
    low = []
    # Read once per physical controller, then apply that raw level to each of
    # its logical actuator slots.
    for c in _controllers:
        raw = _battery_raw(c)
        if _battery_is_low(raw):
            for logical_index, controller, _side in _logical_controller_slots():
                if controller is c:
                    low.append(logical_index)
    if low:
        names = ", ".join(str(i) for i in low)
        raise RuntimeError(
            f"Logical actuators {names} have a critically low battery. "
            "Please charge them before attempting to play."
        )


def set_channel(index: int, channel: int) -> None:
    """Route a logical channel (1-6) to an actuator index (1-6).

    By default index 1 plays channel 1, index 2 plays channel 2, and so on.
    Both arguments are one-based. Multiple indices may be assigned the same
    channel; the previous assignment is not automatically cleared.
    """
    if index < 1 or index > len(CHANNELS):
        raise ValueError("index must be in the range 1-6")
    if channel < 1 or channel > len(CHANNELS):
        raise ValueError("channel must be in the range 1-6")
    with _lock:
        _channel_map[index - 1] = channel

def test_master_pitch(channel: int) -> None:
    """Test the physical actuator currently assigned to a logical channel.

    ``channel`` is zero-based: 0=Square1, 1=Square2, 2=Triangle,
    3=Noise, 4=MMC5Square1, 5=MMC5Square2. The channel is resolved through
    the current ``set_channel()`` mapping, so the test follows remapping.
    """
    if channel < 0 or channel >= len(CHANNELS):
        raise ValueError("channel must be in the range 0-5")

    with _lock:
        if not _controllers:
            cs = _enumerate_controllers()
            if not cs:
                raise RuntimeError("No compatible Nintendo Switch controllers are connected.")
            globals()["_controllers"] = cs

        # Find the physical actuator index assigned to this logical channel.
        try:
            logical_index = _channel_map.index(channel + 1)
        except ValueError:
            raise ValueError(f"Channel {channel} is not assigned to any actuator index")

        slot = 0
        for c in _controllers:
            for side in range(c.slots):
                if slot == logical_index:
                    if channel == 2:
                        # Channel 3 / Triangle: one octave below master pitch.
                        packet = _test_rumble_packet(c, side, _master_pitch / 2.0)
                        _send(c, packet)
                        time.sleep(0.15)
                    elif channel == 3:
                        # Channel 4 / Noise: a short burst of actual generated noise.
                        end_time = time.monotonic() + 0.35
                        frame = 0
                        while time.monotonic() < end_time:
                            packet = _test_rumble_packet(c, side, noise=True, frame=frame)
                            _send(c, packet)
                            frame += 1
                            time.sleep(1.0 / FPS)

                        # Stop this actuator immediately. Do not leave the last
                        # noise packet active, otherwise the motor can continue
                        # producing the final tone after the test ends.
                        # Send a genuinely neutral rumble report. Do not send a
                        # zero-frequency encoded rumble here: the Switch rumble
                        # encoding can still leave the previous motor waveform
                        # latched/decaying.  A neutral packet tells the device
                        # to stop both strong and weak motors immediately.
                        _send(c, _packet(c, 0x10, _neutral_rumble() * c.slots))
                    else:
                        packet = _test_rumble_packet(c, side, _master_pitch)
                        _send(c, packet)
                        time.sleep(0.15)
                    return
                slot += 1
        raise IndexError("No controller actuator exists for the mapped channel")


def all_notes_off() -> None:
    with _lock:
        for c in _controllers:
            try:
                _write_controller_slots(c, [(160.0, 0.0)] * c.slots)
            except Exception:
                pass


def get_pos() -> float:
    with _lock:
        return float(_position)


def get_pos_pattern() -> int:
    """Return the current FamiStudio pattern-column number (zero-based).

    PatternInstance.Time is the pattern-column index in the FamiStudio Text
    format. This function converts the current playback position back into
    that column number using the same groove-aware timeline used for playback.

    Returns 0 for the beginning of the song and the current column while
    playing or paused. After natural end-of-song, playback resets to 0, so
    this also returns 0.
    """
    with _lock:
        song = _song
        frame = int(round(_position * FAMISTUDIO_NTSC_FPS))

    if song is None or song.length <= 0:
        return 0

    # Find the pattern column containing the current FamiStudio frame.
    # PatternInstance.Time is a zero-based column index.
    start = 0
    for column in range(song.length):
        span = _pattern_span(song, column)
        if frame < start + span:
            return column
        start += span

    # Clamp an exact/end-of-song position to the final pattern column.
    return max(0, song.length - 1)


__all__ = [
    "load", "play", "pause", "stop", "get_state", "set_master_pitch",
    "test_master_pitch", "set_channel", "all_notes_off", "get_pos", "get_pos_pattern",
    "set_loop",
]
