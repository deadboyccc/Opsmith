"""Compose the instrumental music for the two videos and put it on them. No voice, no samples.

The music is synthesized here from scratch: a warm chord pad, a plucked arpeggio with echoes,
a round bass, light drums and a little reverb, over the progression Am, F, C, G, voiced to
carry on phone speakers. Nothing is licensed or downloaded, and the same command always
writes the same audio.

* ``reel``: upbeat, about 112 BPM. The drums come in after the hook; claps lift the middle.
* ``demo``: calmer, about 92 BPM, sparser and a little quieter, under the captions.

Each piece is fitted to its video: the progression runs for as long as the video, and the
last chord is always A minor, held while the music fades out. The video stream is copied
untouched; only the audio track is written (AAC, loudness-normalized).

    uv run --no-project --with numpy --with imageio-ffmpeg python scripts/soundtrack.py

Run it again after recording ``demo_video.py`` or ``reel_video.py``: they write silent video.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RATE = 44_100
# Am, F, C, G as MIDI notes (chord tones for the pad and arpeggio; root for the bass).
PROGRESSION = ((57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62))
TONIC = PROGRESSION[0]
ENDING_SECONDS = 3.2  # the final A minor, held and faded


@dataclass(frozen=True)
class Style:
    video: str
    bpm: float
    loudness: float  # integrated LUFS of the finished track
    drums_from_bar: int
    claps: bool
    arp_per_beat: int  # 2: eighths, 4: sixteenths
    pump: float  # how much the pad ducks under the kick (0 to 1)


STYLES = {
    "reel": Style("docs/images/reel.mp4", 112, -16.0, 2, True, 4, 0.35),
    "demo": Style("docs/images/demo.mp4", 92, -19.0, 4, False, 2, 0.15),
}


def hz(midi: float) -> float:
    return float(440.0 * 2 ** ((midi - 69) / 12))


def envelope(n: int, attack: float, release: float) -> np.ndarray:
    """Linear attack, flat, linear release, in samples of a note ``n`` long."""
    env = np.ones(n)
    a = min(max(int(attack * RATE), 1), n)
    r = min(max(int(release * RATE), 1), n)
    env[:a] = np.linspace(0, 1, a)
    env[-r:] *= np.linspace(1, 0, r)
    return env


def additive(
    freq: float, n: int, harmonics: int, rolloff: float, detune: float = 0.0
) -> np.ndarray:
    """A soft, band-limited saw-like tone: ``harmonics`` partials falling off as 1/k**rolloff."""
    t = np.arange(n) / RATE
    out = np.zeros(n)
    f = freq * 2 ** (detune / 1200)
    for k in range(1, harmonics + 1):
        if f * k > RATE / 2.5:
            break
        out += np.sin(2 * np.pi * f * k * t) / k**rolloff
    return out


def place(track: np.ndarray, sound: np.ndarray, at: float) -> None:
    start = int(at * RATE)
    if start >= len(track):
        return
    end = min(start + len(sound), len(track))
    track[start:end] += sound[: end - start]


def reverb(signal: np.ndarray, seconds: float = 1.8, seed: int = 7) -> np.ndarray:
    """Convolution with a decaying-noise impulse: a smooth, roomy tail."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    impulse = rng.standard_normal(n) * np.exp(-np.linspace(0, 6.5, n))
    impulse /= np.sqrt(np.sum(impulse**2))
    size = 1 << int(np.ceil(np.log2(len(signal) + n)))
    wet = np.fft.irfft(np.fft.rfft(signal, size) * np.fft.rfft(impulse, size), size)
    return wet[: len(signal)]


def compose(style: Style, duration: float) -> np.ndarray:
    beat = 60.0 / style.bpm
    bar = 4 * beat
    total = int(duration * RATE)
    rng = np.random.default_rng(2026)
    pad_l, pad_r = np.zeros(total), np.zeros(total)
    arp_l, arp_r = np.zeros(total), np.zeros(total)
    bass, drums, kick_env = np.zeros(total), np.zeros(total), np.zeros(total)

    # Bars of the progression, then the final tonic for the last few seconds.
    bars: list[tuple[float, float, tuple[int, ...]]] = []
    start, index = 0.0, 0
    while start + bar <= duration - ENDING_SECONDS + 0.01:
        bars.append((start, bar, PROGRESSION[index % len(PROGRESSION)]))
        start += bar
        index += 1
    bars.append((start, duration - start, TONIC))
    last = len(bars) - 1

    for number, (at, length, chord) in enumerate(bars):
        ending = number == last
        # Pad: the chord an octave down plus the root below it, slightly detuned per side.
        n = int((length + 0.6) * RATE)
        env = envelope(n, 0.25 if number else 1.2, 0.9 if ending else 0.6)
        if ending:
            env *= np.linspace(1, 0, n) ** 1.6
        for note in (*chord, chord[0] - 12):
            place(pad_l, additive(hz(note), n, 6, 1.3, -4) * env * 0.055, at)
            place(pad_r, additive(hz(note), n, 6, 1.3, +4) * env * 0.055, at)
        if ending:
            # One last arpeggiated rise into the held chord, then let it ring.
            for step, note in enumerate((*chord, chord[0] + 12)):
                tone_n = int(1.6 * RATE)
                decay = np.exp(-np.linspace(0, 5, tone_n))
                tone = additive(hz(note + 12), tone_n, 3, 2.2) * decay * 0.07
                place(arp_l if step % 2 else arp_r, tone, at + step * beat / 2)
            continue
        # Arpeggio: chord tones up and down, an octave up, gently plucked.
        pattern = [chord[0], chord[1], chord[2], chord[0] + 12]
        steps = 4 * style.arp_per_beat
        step_len = beat / style.arp_per_beat
        for step in range(steps):
            if number == 0 and step < style.arp_per_beat:  # breathe in before the first note
                continue
            note = pattern[step % 4] + 12
            tone_n = int(0.5 * RATE)
            decay = np.exp(-np.linspace(0, 9, tone_n))
            tone = additive(hz(note), tone_n, 3, 2.2) * decay * 0.05 * envelope(tone_n, 0.004, 0.02)
            place(arp_l if step % 2 else arp_r, tone, at + step * step_len)
        # Bass: root on 1, a push on the and of 2, the fifth on 4 (from the first drum bar).
        if number >= style.drums_from_bar - 1:
            for offset, interval in ((0.0, 0), (1.5, 0), (3.0, 7)):
                tone_n = int(beat * 1.2 * RATE)
                decay = np.exp(-np.linspace(0, 4, tone_n))
                tone = (additive(hz(chord[0] - 12 + interval), tone_n, 4, 1.8) * decay * 0.12
                        * envelope(tone_n, 0.005, 0.05))  # fmt: skip
                place(bass, tone, at + offset * beat)
        # Drums: soft kick on every beat, closed hat on the off-beats, claps on 2 and 4.
        if number >= style.drums_from_bar:
            for b in range(4):
                k_n = int(0.4 * RATE)
                t = np.arange(k_n) / RATE
                sweep = 2 * np.pi * np.cumsum(58 + 90 * np.exp(-t * 30)) / RATE
                body = np.sin(sweep) + 0.25 * np.sin(2 * sweep)  # an overtone phones can play
                kick = body * np.exp(-t * 11) * 0.24
                place(drums, kick, at + b * beat)
                place(kick_env, np.exp(-t * 7), at + b * beat)
                h_n = int(0.06 * RATE)
                noise = rng.standard_normal(h_n)
                hat = np.diff(noise, prepend=0) * np.exp(-np.linspace(0, 8, h_n)) * 0.035
                place(drums, hat, at + (b + 0.5) * beat)
                if style.claps and b in (1, 3) and number % 4 in (1, 2, 3):
                    c_n = int(0.18 * RATE)
                    burst = rng.standard_normal(c_n)
                    clap = (burst - np.roll(burst, 6)) * np.exp(-np.linspace(0, 10, c_n)) * 0.05
                    place(drums, clap, at + b * beat)

    # The pad breathes with the kick, a touch.
    duck = 1 - style.pump * np.clip(kick_env, 0, 1)
    pad_l, pad_r = pad_l * duck, pad_r * duck
    send = (pad_l + pad_r + arp_l + arp_r) * 0.5
    room = reverb(send)
    # Echo on the arpeggio: a dotted eighth, bouncing between the sides.
    echo = int(beat * 0.75 * RATE)
    arp_l = arp_l + 0.35 * np.concatenate([np.zeros(echo), arp_r[:-echo]])
    arp_r = arp_r + 0.35 * np.concatenate([np.zeros(echo), arp_l[:-echo]])
    left = pad_l + arp_l + bass + drums + 0.35 * room
    right = pad_r + arp_r + bass + drums + 0.35 * room
    stereo = np.stack([left, right], axis=1)
    fade_in = int(0.4 * RATE)
    stereo[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
    fade_out = int(2.4 * RATE)
    stereo[-fade_out:] *= np.linspace(1, 0, fade_out)[:, None] ** 1.5
    return np.tanh(stereo * 1.4) / 1.4  # gentle glue, never a hard clip


def write_wav(path: Path, stereo: np.ndarray) -> None:
    peak = float(np.max(np.abs(stereo))) or 1.0
    pcm = (stereo / peak * 0.89 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


def duration_of(ffmpeg: str, video: Path) -> float:
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video)], capture_output=True, text=True, check=False
    )
    found = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
    if found is None:
        raise RuntimeError(f"cannot read the length of {video}")
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def score(name: str, style: Style, ffmpeg: str, work: Path) -> None:
    video = ROOT / style.video
    duration = duration_of(ffmpeg, video)
    wav = work / f"{name}.wav"
    write_wav(wav, compose(style, duration))
    scored = work / f"{name}.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-i", str(wav),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-af", f"loudnorm=I={style.loudness}:TP=-1.5:LRA=9,aresample={RATE}",
         "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(scored)],
        check=True,
    )  # fmt: skip
    shutil.move(scored, video)
    print(f"{style.video}: {duration:.1f} s of music at {style.bpm:g} BPM, {style.loudness:g} LUFS")


def main() -> int:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    names = sys.argv[1:] or list(STYLES)
    with tempfile.TemporaryDirectory() as tmp:
        for name in names:
            score(name, STYLES[name], ffmpeg, Path(tmp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
