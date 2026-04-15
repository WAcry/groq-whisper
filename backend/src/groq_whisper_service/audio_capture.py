from __future__ import annotations

import argparse
import math
import queue
import signal
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pyaudiowpatch as pyaudio


SAMPLE_FORMAT = pyaudio.paInt16
SAMPLE_WIDTH_BYTES = 2
DEFAULT_DURATION_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
FRAMES_PER_BUFFER = 960  # ~20 ms @ 48 kHz, easier on Python than 10 ms callbacks
OUTPUT_RATE = 48_000
OUTPUT_CHANNELS = 2

# Conservative mixing defaults. These are intentionally light.
MIC_HIGH_PASS_HZ = 80.0
MIC_TARGET_DBFS = -23.0
MIC_GATE_DBFS = -55.0
MIC_MAX_BOOST_DB = 12.0
MIC_MIN_GAIN_DB = -6.0
SPEAKER_GAIN = 0.88
PEAK_CEILING = 0.98
VOICE_ACTIVITY_DBFS = -35.0
DUCKING_DB = 3.0


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    sample_rate: int
    channels: int
    is_loopback: bool


@dataclass(frozen=True)
class DevicePair:
    mic: DeviceInfo
    speaker_loopback: DeviceInfo


@dataclass(frozen=True)
class ChunkRecord:
    generation: int
    channels: int
    sample_rate: int
    start_time: float
    data: bytes


@dataclass(frozen=True)
class Segment:
    generation: int
    channels: int
    sample_rate: int
    start_time: float
    audio: np.ndarray  # float32, shape=(frames, channels)


@dataclass(frozen=True)
class MixedAudioWindow:
    audio: np.ndarray  # float32, shape=(frames, channels)
    sample_rate: int
    start_time: float
    end_time: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_time - self.start_time)


class StatusCounter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mic_callback_statuses = 0
        self.speaker_callback_statuses = 0
        self.mic_queue_drops = 0
        self.speaker_queue_drops = 0

    def inc(self, attr: str) -> None:
        with self.lock:
            setattr(self, attr, getattr(self, attr) + 1)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "mic_callback_statuses": self.mic_callback_statuses,
                "speaker_callback_statuses": self.speaker_callback_statuses,
                "mic_queue_drops": self.mic_queue_drops,
                "speaker_queue_drops": self.speaker_queue_drops,
            }


def int16_bytes_to_float32(data: bytes, channels: int) -> np.ndarray:
    raw = np.frombuffer(data, dtype=np.int16)
    if raw.size == 0:
        return np.zeros((0, channels), dtype=np.float32)
    frames = raw.size // channels
    if frames <= 0:
        return np.zeros((0, channels), dtype=np.float32)
    trimmed = raw[: frames * channels].reshape(frames, channels)
    return (trimmed.astype(np.float32) / 32768.0)


def chunk_frame_count(chunk: ChunkRecord) -> int:
    bytes_per_frame = SAMPLE_WIDTH_BYTES * max(1, chunk.channels)
    if bytes_per_frame <= 0:
        return 0
    return len(chunk.data) // bytes_per_frame


def ensure_2d(x: np.ndarray) -> np.ndarray:
    if x.ndim == 1:
        return x[:, None]
    return x


def rms_dbfs(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64) + 1e-12))
    return 20.0 * math.log10(max(rms, 1e-9))


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def highpass_filter_mono(x: np.ndarray, cutoff_hz: float, sample_rate: int) -> np.ndarray:
    if x.size == 0:
        return x.copy()
    dt = 1.0 / float(sample_rate)
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    alpha = rc / (rc + dt)
    y = np.empty_like(x)
    prev_x = float(x[0])
    prev_y = 0.0
    y[0] = 0.0
    for i in range(1, x.shape[0]):
        current_x = float(x[i])
        current_y = alpha * (prev_y + current_x - prev_x)
        y[i] = current_y
        prev_x = current_x
        prev_y = current_y
    return y


def apply_agc(
    x: np.ndarray,
    sample_rate: int,
    target_dbfs: float = MIC_TARGET_DBFS,
    gate_dbfs: float = MIC_GATE_DBFS,
    max_boost_db: float = MIC_MAX_BOOST_DB,
    min_gain_db: float = MIC_MIN_GAIN_DB,
    block_ms: float = 20.0,
    attack: float = 0.16,
    release: float = 0.05,
) -> np.ndarray:
    if x.size == 0:
        return x.copy()

    block_frames = max(1, int(round((block_ms / 1000.0) * sample_rate)))
    out = np.empty_like(x)
    current_gain_db = 0.0

    for start in range(0, x.shape[0], block_frames):
        end = min(start + block_frames, x.shape[0])
        block = x[start:end]
        level_dbfs = rms_dbfs(block)
        if level_dbfs < gate_dbfs:
            target_gain_db = 0.0
        else:
            target_gain_db = float(np.clip(target_dbfs - level_dbfs, min_gain_db, max_boost_db))
        coeff = attack if target_gain_db > current_gain_db else release
        current_gain_db += (target_gain_db - current_gain_db) * coeff
        out[start:end] = block * db_to_linear(current_gain_db)

    return out


def build_ducking_envelope(
    mic_mono: np.ndarray,
    sample_rate: int,
    speech_threshold_dbfs: float = VOICE_ACTIVITY_DBFS,
    ducking_db: float = DUCKING_DB,
    block_ms: float = 20.0,
    attack: float = 0.35,
    release: float = 0.08,
) -> np.ndarray:
    if mic_mono.size == 0 or ducking_db <= 0.0:
        return np.ones(mic_mono.shape[0], dtype=np.float32)

    block_frames = max(1, int(round((block_ms / 1000.0) * sample_rate)))
    out = np.empty(mic_mono.shape[0], dtype=np.float32)
    duck_gain = db_to_linear(-ducking_db)
    current_gain = 1.0

    for start in range(0, mic_mono.shape[0], block_frames):
        end = min(start + block_frames, mic_mono.shape[0])
        level = rms_dbfs(mic_mono[start:end])
        target_gain = duck_gain if level >= speech_threshold_dbfs else 1.0
        coeff = attack if target_gain < current_gain else release
        current_gain += (target_gain - current_gain) * coeff
        out[start:end] = current_gain

    return out


def resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    audio = ensure_2d(np.asarray(audio, dtype=np.float32))
    if audio.shape[0] == 0 or src_rate == dst_rate:
        return audio.copy()

    src_frames = audio.shape[0]
    dst_frames = max(1, int(round(src_frames * float(dst_rate) / float(src_rate))))
    src_index = np.arange(src_frames, dtype=np.float64)
    dst_index = np.linspace(0.0, src_frames - 1, num=dst_frames, dtype=np.float64)

    out = np.empty((dst_frames, audio.shape[1]), dtype=np.float32)
    for ch in range(audio.shape[1]):
        out[:, ch] = np.interp(dst_index, src_index, audio[:, ch]).astype(np.float32)
    return out


def pad_to_same_length(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = ensure_2d(a)
    b = ensure_2d(b)
    frames = max(a.shape[0], b.shape[0])
    if a.shape[0] < frames:
        pad = np.zeros((frames - a.shape[0], a.shape[1]), dtype=np.float32)
        a = np.vstack([a, pad])
    if b.shape[0] < frames:
        pad = np.zeros((frames - b.shape[0], b.shape[1]), dtype=np.float32)
        b = np.vstack([b, pad])
    return a, b


def mono_to_stereo(x: np.ndarray) -> np.ndarray:
    x = ensure_2d(x)
    if x.shape[1] != 1:
        raise ValueError("Expected mono input")
    return np.repeat(x, 2, axis=1)


def speaker_to_stereo(x: np.ndarray) -> np.ndarray:
    x = ensure_2d(x)
    if x.shape[1] == 1:
        return np.repeat(x, 2, axis=1)
    if x.shape[1] == 2:
        return x
    # Surround/default device edge case: fold all channels to mono so we keep content,
    # then duplicate to stereo. It is conservative but avoids channel mismatch issues.
    mono = np.mean(x, axis=1, dtype=np.float32, keepdims=True)
    return np.repeat(mono, 2, axis=1)


def peak_limit_by_scaling(x: np.ndarray, ceiling: float = PEAK_CEILING) -> np.ndarray:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak <= 0.0 or peak <= ceiling:
        return x.astype(np.float32, copy=False)
    return (x * (ceiling / peak)).astype(np.float32, copy=False)


def write_wav_file(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    audio = ensure_2d(np.asarray(audio, dtype=np.float32))
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = np.round(clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(pcm16.shape[1])
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())


class DualAudioRecorder:
    def __init__(
        self,
        duration_seconds: float,
        output_path: Path,
        poll_interval_seconds: float,
    ) -> None:
        self.duration_seconds = duration_seconds
        self.output_path = output_path
        self.poll_interval_seconds = poll_interval_seconds

        self.p = pyaudio.PyAudio()
        self.stop_event = threading.Event()
        self.restart_event = threading.Event()
        self.state_lock = threading.Lock()
        self.status = StatusCounter()

        self.current_pair: Optional[DevicePair] = None
        self.pending_pair: Optional[DevicePair] = None
        self.last_device_signature: Optional[tuple[int, int]] = None
        self.mic_stream = None
        self.speaker_stream = None
        self.stream_generation = 0

        self.mic_queue: "queue.Queue[ChunkRecord]" = queue.Queue(maxsize=4096)
        self.speaker_queue: "queue.Queue[ChunkRecord]" = queue.Queue(maxsize=4096)
        self.mic_chunks: list[ChunkRecord] = []
        self.speaker_chunks: list[ChunkRecord] = []
        self.storage_lock = threading.Lock()

    def get_default_pair(self) -> DevicePair:
        wasapi = self.p.get_host_api_info_by_type(pyaudio.paWASAPI)
        mic_raw = self.p.get_device_info_by_index(wasapi["defaultInputDevice"])
        speaker_raw = self.p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loopback_raw = self._resolve_loopback_device(speaker_raw)

        mic = DeviceInfo(
            index=int(mic_raw["index"]),
            name=str(mic_raw["name"]),
            sample_rate=int(round(float(mic_raw["defaultSampleRate"]))),
            channels=max(1, int(mic_raw["maxInputChannels"])),
            is_loopback=bool(mic_raw.get("isLoopbackDevice", False)),
        )
        speaker = DeviceInfo(
            index=int(loopback_raw["index"]),
            name=str(loopback_raw["name"]),
            sample_rate=int(round(float(loopback_raw["defaultSampleRate"]))),
            channels=max(1, int(loopback_raw["maxInputChannels"])),
            is_loopback=bool(loopback_raw.get("isLoopbackDevice", True)),
        )
        return DevicePair(mic=mic, speaker_loopback=speaker)

    def _resolve_loopback_device(self, speaker_raw: dict) -> dict:
        if speaker_raw.get("isLoopbackDevice"):
            return speaker_raw

        speaker_name = str(speaker_raw["name"])
        best_match = None
        for candidate in self.p.get_loopback_device_info_generator():
            candidate_name = str(candidate["name"])
            if speaker_name == candidate_name:
                return candidate
            if speaker_name in candidate_name and best_match is None:
                best_match = candidate

        if best_match is None:
            raise RuntimeError(
                "Could not find a WASAPI loopback device for the current default speaker endpoint"
            )
        return best_match

    @staticmethod
    def _device_signature(pair: DevicePair) -> tuple[int, int]:
        return (pair.mic.index, pair.speaker_loopback.index)

    @staticmethod
    def _resolve_chunk_start_time(time_info: dict, frame_count: int, sample_rate: int) -> float:
        buffer_duration = frame_count / float(sample_rate)

        adc_time = time_info.get("input_buffer_adc_time")
        if isinstance(adc_time, (int, float)) and math.isfinite(adc_time):
            return float(adc_time)

        current_time = time_info.get("current_time")
        if isinstance(current_time, (int, float)) and math.isfinite(current_time):
            return float(current_time) - buffer_duration

        return time.perf_counter() - buffer_duration

    def watch_default_devices(self) -> None:
        while not self.stop_event.wait(self.poll_interval_seconds):
            try:
                pair = self.get_default_pair()
                signature = self._device_signature(pair)
                with self.state_lock:
                    last_signature = self.last_device_signature
                    if last_signature is None:
                        self.last_device_signature = signature
                        continue
                    if signature == last_signature:
                        continue
                    self.pending_pair = pair
                    self.last_device_signature = signature
                self.restart_event.set()
                print(
                    "[switch] default devices changed -> "
                    f"mic={pair.mic.name!r}, speaker={pair.speaker_loopback.name!r}"
                )
            except Exception as exc:
                print(f"[warn] default-device watcher failed: {exc}")

    def open_streams(self, pair: DevicePair) -> None:
        self.stream_generation += 1
        generation = self.stream_generation

        mic_rate = pair.mic.sample_rate
        speaker_rate = pair.speaker_loopback.sample_rate
        mic_channels = pair.mic.channels
        speaker_channels = pair.speaker_loopback.channels

        def mic_callback(in_data, frame_count, time_info, status):
            if status:
                self.status.inc("mic_callback_statuses")
            start_time = self._resolve_chunk_start_time(dict(time_info or {}), frame_count, mic_rate)
            try:
                self.mic_queue.put_nowait(
                    ChunkRecord(
                        generation=generation,
                        channels=mic_channels,
                        sample_rate=mic_rate,
                        start_time=start_time,
                        data=in_data,
                    )
                )
            except queue.Full:
                self.status.inc("mic_queue_drops")
            return (None, pyaudio.paContinue)

        def speaker_callback(in_data, frame_count, time_info, status):
            if status:
                self.status.inc("speaker_callback_statuses")
            start_time = self._resolve_chunk_start_time(dict(time_info or {}), frame_count, speaker_rate)
            try:
                self.speaker_queue.put_nowait(
                    ChunkRecord(
                        generation=generation,
                        channels=speaker_channels,
                        sample_rate=speaker_rate,
                        start_time=start_time,
                        data=in_data,
                    )
                )
            except queue.Full:
                self.status.inc("speaker_queue_drops")
            return (None, pyaudio.paContinue)

        print(
            f"[open] generation={generation} "
            f"mic={pair.mic.name!r} {mic_rate}Hz/{mic_channels}ch "
            f"speaker(loopback)={pair.speaker_loopback.name!r} {speaker_rate}Hz/{speaker_channels}ch"
        )

        self.mic_stream = self.p.open(
            format=SAMPLE_FORMAT,
            channels=mic_channels,
            rate=mic_rate,
            frames_per_buffer=FRAMES_PER_BUFFER,
            input=True,
            input_device_index=pair.mic.index,
            stream_callback=mic_callback,
            start=True,
        )
        self.speaker_stream = self.p.open(
            format=SAMPLE_FORMAT,
            channels=speaker_channels,
            rate=speaker_rate,
            frames_per_buffer=FRAMES_PER_BUFFER,
            input=True,
            input_device_index=pair.speaker_loopback.index,
            stream_callback=speaker_callback,
            start=True,
        )

        with self.state_lock:
            self.current_pair = pair
            self.pending_pair = None
            self.last_device_signature = self._device_signature(pair)

    def close_streams(self) -> None:
        for attr_name in ("mic_stream", "speaker_stream"):
            stream = getattr(self, attr_name)
            if stream is None:
                continue
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            setattr(self, attr_name, None)

    def _flush_queues_to_storage(self) -> None:
        mic_chunks: list[ChunkRecord] = []
        while True:
            try:
                mic_chunks.append(self.mic_queue.get_nowait())
            except queue.Empty:
                break
        speaker_chunks: list[ChunkRecord] = []
        while True:
            try:
                speaker_chunks.append(self.speaker_queue.get_nowait())
            except queue.Empty:
                break
        if mic_chunks or speaker_chunks:
            with self.storage_lock:
                self.mic_chunks.extend(mic_chunks)
                self.speaker_chunks.extend(speaker_chunks)

    def _restart_streams_if_needed(self) -> None:
        if not self.restart_event.is_set():
            return

        self.restart_event.clear()
        self._flush_queues_to_storage()
        with self.state_lock:
            pair = self.pending_pair or self.current_pair
        if pair is None:
            return

        print(
            "[restart] reopening streams -> "
            f"mic={pair.mic.name!r} {pair.mic.sample_rate}Hz/{pair.mic.channels}ch "
            f"speaker={pair.speaker_loopback.name!r} "
            f"{pair.speaker_loopback.sample_rate}Hz/{pair.speaker_loopback.channels}ch"
        )
        self.close_streams()
        self.open_streams(pair)

    def run(self) -> Path:
        try:
            initial_pair = self.get_default_pair()
            self.open_streams(initial_pair)

            watcher = threading.Thread(target=self.watch_default_devices, daemon=True)
            watcher.start()

            deadline = time.perf_counter() + self.duration_seconds
            print(f"[info] recording for {self.duration_seconds:.1f} seconds")

            while not self.stop_event.is_set():
                self._flush_queues_to_storage()
                self._restart_streams_if_needed()

                remaining = deadline - time.perf_counter()
                if remaining <= 0.0:
                    break
                self.stop_event.wait(min(0.02, remaining))
        except KeyboardInterrupt:
            print("[info] stop requested, finalizing partial capture")
        finally:
            self.stop_event.set()
            self.close_streams()
            self._flush_queues_to_storage()
            try:
                self.p.terminate()
            except Exception:
                pass

        return self.render_and_write()

    @staticmethod
    def _chunks_to_segments(chunks: list[ChunkRecord], track_name: str) -> list[Segment]:
        if not chunks:
            print(f"[diag] {track_name}: no chunks captured")
            return []

        ordered = sorted(chunks, key=lambda item: (item.start_time, item.generation))
        segments: list[Segment] = []
        format_split_count = 0
        gap_split_count = 0
        max_gap_seconds = 0.0
        current_generation = ordered[0].generation
        current_channels = ordered[0].channels
        current_rate = ordered[0].sample_rate
        current_start_time = ordered[0].start_time
        current_end_time = current_start_time + (
            chunk_frame_count(ordered[0]) / float(max(1, current_rate))
        )
        parts: list[bytes] = [ordered[0].data]

        def flush_current() -> None:
            if not parts:
                return
            joined = b"".join(parts)
            audio = int16_bytes_to_float32(joined, current_channels)
            segments.append(
                Segment(
                    generation=current_generation,
                    channels=current_channels,
                    sample_rate=current_rate,
                    start_time=current_start_time,
                    audio=audio,
                )
            )

        for chunk in ordered[1:]:
            signature = (chunk.generation, chunk.channels, chunk.sample_rate)
            current_signature = (current_generation, current_channels, current_rate)
            chunk_duration = chunk_frame_count(chunk) / float(max(1, chunk.sample_rate))
            gap_tolerance = max(
                chunk_duration * 1.5,
                FRAMES_PER_BUFFER / float(max(1, chunk.sample_rate)),
            )
            gap_seconds = chunk.start_time - current_end_time

            if signature != current_signature or gap_seconds > gap_tolerance:
                if signature != current_signature:
                    format_split_count += 1
                if gap_seconds > gap_tolerance:
                    gap_split_count += 1
                    max_gap_seconds = max(max_gap_seconds, gap_seconds)
                flush_current()
                parts = [chunk.data]
                current_generation = chunk.generation
                current_channels = chunk.channels
                current_rate = chunk.sample_rate
                current_start_time = chunk.start_time
            else:
                parts.append(chunk.data)
            current_end_time = chunk.start_time + chunk_duration
        flush_current()
        print(
            f"[diag] {track_name}: chunks={len(ordered)} segments={len(segments)} "
            f"format_splits={format_split_count} gap_splits={gap_split_count} "
            f"max_gap_ms={max_gap_seconds * 1000.0:.1f}"
        )
        return segments

    @staticmethod
    def _render_track_from_segments(
        segments: list[Segment],
        out_channels: int,
        global_start_time: float,
        track_name: str,
    ) -> np.ndarray:
        if not segments:
            print(f"[diag] render {track_name}: empty track")
            return np.zeros((0, out_channels), dtype=np.float32)

        rendered_segments: list[tuple[int, np.ndarray]] = []
        total_frames = 0

        for segment in segments:
            audio = ensure_2d(segment.audio)
            if out_channels == 1:
                audio = np.mean(audio, axis=1, dtype=np.float32, keepdims=True)
            else:
                audio = speaker_to_stereo(audio)
            audio = resample_linear(audio, segment.sample_rate, OUTPUT_RATE)

            start_frame = max(
                0,
                int(round((segment.start_time - global_start_time) * OUTPUT_RATE)),
            )
            end_frame = start_frame + audio.shape[0]
            rendered_segments.append((start_frame, audio))
            total_frames = max(total_frames, end_frame)

        if total_frames <= 0:
            return np.zeros((0, out_channels), dtype=np.float32)

        track = np.zeros((total_frames, out_channels), dtype=np.float32)
        weights = np.zeros((total_frames, 1), dtype=np.float32)

        for start_frame, audio in rendered_segments:
            end_frame = start_frame + audio.shape[0]
            track[start_frame:end_frame] += audio
            weights[start_frame:end_frame] += 1.0

        nonzero = weights[:, 0] > 0.0
        if np.any(nonzero):
            track[nonzero] /= weights[nonzero]
        start_offset_ms = (segments[0].start_time - global_start_time) * 1000.0
        duration_seconds = total_frames / float(OUTPUT_RATE)
        print(
            f"[diag] render {track_name}: segments={len(segments)} "
            f"offset_ms={start_offset_ms:.1f} duration_s={duration_seconds:.3f} "
            f"channels={out_channels}"
        )
        return track

    def _build_mic_track(
        self,
        segments: list[Segment],
        global_start_time: float,
    ) -> np.ndarray:
        return self._render_track_from_segments(
            segments,
            out_channels=1,
            global_start_time=global_start_time,
            track_name="mic",
        )

    def _build_speaker_track(
        self,
        segments: list[Segment],
        global_start_time: float,
    ) -> np.ndarray:
        return self._render_track_from_segments(
            segments,
            out_channels=2,
            global_start_time=global_start_time,
            track_name="speaker",
        )

    def _build_final_mix(self, mic_track: np.ndarray, speaker_track: np.ndarray) -> np.ndarray:
        mic_track = ensure_2d(mic_track.astype(np.float32, copy=False))
        speaker_track = ensure_2d(speaker_track.astype(np.float32, copy=False))
        mic_track, speaker_track = pad_to_same_length(mic_track, speaker_track)

        mic_mono = mic_track[:, 0]
        mic_mono = highpass_filter_mono(mic_mono, MIC_HIGH_PASS_HZ, OUTPUT_RATE)
        mic_mono = apply_agc(mic_mono, sample_rate=OUTPUT_RATE)
        mic_stereo = mono_to_stereo(mic_mono[:, None])

        duck = build_ducking_envelope(mic_mono, sample_rate=OUTPUT_RATE)
        speaker_proc = speaker_track * (SPEAKER_GAIN * duck[:, None])

        mix = speaker_proc + mic_stereo
        mix = peak_limit_by_scaling(mix, PEAK_CEILING)
        return mix.astype(np.float32, copy=False)

    def render_and_write(self) -> Path:
        if not self.mic_chunks and not self.speaker_chunks:
            raise RuntimeError("No audio was captured from either the microphone or the speaker loopback")

        print(
            f"[diag] captured chunks: mic={len(self.mic_chunks)} "
            f"speaker={len(self.speaker_chunks)}"
        )
        mic_segments = self._chunks_to_segments(self.mic_chunks, track_name="mic")
        speaker_segments = self._chunks_to_segments(self.speaker_chunks, track_name="speaker")
        all_segments = mic_segments + speaker_segments
        global_start_time = min(segment.start_time for segment in all_segments)
        print(
            f"[diag] global timeline: start_time={global_start_time:.6f} "
            f"segment_count={len(all_segments)}"
        )

        mic_track = self._build_mic_track(mic_segments, global_start_time)
        speaker_track = self._build_speaker_track(speaker_segments, global_start_time)
        mic_track, speaker_track = pad_to_same_length(mic_track, speaker_track)
        print(
            f"[diag] aligned tracks: mic_frames={mic_track.shape[0]} "
            f"speaker_frames={speaker_track.shape[0]} duration_s={mic_track.shape[0] / float(OUTPUT_RATE):.3f}"
        )

        raw_dir = self.output_path.parent
        base = self.output_path.stem
        mic_raw_path = raw_dir / f"{base}_mic_raw.wav"
        speaker_raw_path = raw_dir / f"{base}_speaker_raw.wav"
        write_wav_file(mic_raw_path, mic_track, OUTPUT_RATE)
        write_wav_file(speaker_raw_path, speaker_track, OUTPUT_RATE)

        final_mix = self._build_final_mix(mic_track, speaker_track)
        write_wav_file(self.output_path, final_mix, OUTPUT_RATE)

        counters = self.status.snapshot()
        mic_peak = float(np.max(np.abs(mic_track))) if mic_track.size else 0.0
        speaker_peak = float(np.max(np.abs(speaker_track))) if speaker_track.size else 0.0
        mix_peak = float(np.max(np.abs(final_mix))) if final_mix.size else 0.0
        print(
            f"[diag] peaks: mic={mic_peak:.4f} speaker={speaker_peak:.4f} mix={mix_peak:.4f}"
        )
        print(f"[done] wrote mixed audio to {self.output_path.resolve()}")
        print(f"[done] wrote mic raw audio to {mic_raw_path.resolve()}")
        print(f"[done] wrote speaker raw audio to {speaker_raw_path.resolve()}")
        print(f"[diag] callback status counters: {counters}")
        return self.output_path


class ContinuousDualAudioCapture(DualAudioRecorder):
    def __init__(
        self,
        *,
        poll_interval_seconds: float,
        retention_seconds: float,
    ) -> None:
        super().__init__(
            duration_seconds=max(0.1, retention_seconds),
            output_path=Path("recordings") / "_live_capture.wav",
            poll_interval_seconds=poll_interval_seconds,
        )
        self.retention_seconds = max(retention_seconds, 1.0)
        self.capture_started_at: Optional[float] = None
        self.maintenance_thread: Optional[threading.Thread] = None
        self.watcher_thread: Optional[threading.Thread] = None

    @staticmethod
    def _chunk_end_time(chunk: ChunkRecord) -> float:
        return chunk.start_time + (chunk_frame_count(chunk) / float(max(1, chunk.sample_rate)))

    @staticmethod
    def _fit_track_length(
        track: np.ndarray,
        *,
        frames: int,
        channels: int,
    ) -> np.ndarray:
        if frames <= 0:
            return np.zeros((0, channels), dtype=np.float32)
        track = ensure_2d(np.asarray(track, dtype=np.float32))
        if track.shape[0] > frames:
            return track[:frames].astype(np.float32, copy=False)
        if track.shape[0] < frames:
            padding = np.zeros((frames - track.shape[0], channels), dtype=np.float32)
            return np.vstack([track, padding])
        return track.astype(np.float32, copy=False)

    @staticmethod
    def _trim_segment_to_window(
        segment: Segment,
        *,
        window_start_time: float,
        window_end_time: float,
    ) -> Segment | None:
        segment_audio = ensure_2d(np.asarray(segment.audio, dtype=np.float32))
        segment_duration_s = segment_audio.shape[0] / float(max(1, segment.sample_rate))
        segment_end_time = segment.start_time + segment_duration_s
        overlap_start_time = max(segment.start_time, window_start_time)
        overlap_end_time = min(segment_end_time, window_end_time)
        if overlap_end_time <= overlap_start_time:
            return None

        start_frame = int(round((overlap_start_time - segment.start_time) * segment.sample_rate))
        end_frame = int(round((overlap_end_time - segment.start_time) * segment.sample_rate))
        start_frame = min(max(start_frame, 0), segment_audio.shape[0])
        end_frame = min(max(end_frame, start_frame), segment_audio.shape[0])
        if end_frame <= start_frame:
            return None

        return Segment(
            generation=segment.generation,
            channels=segment.channels,
            sample_rate=segment.sample_rate,
            start_time=overlap_start_time,
            audio=segment_audio[start_frame:end_frame].copy(),
        )

    def _trim_segments_to_window(
        self,
        segments: list[Segment],
        *,
        window_start_time: float,
        window_end_time: float,
    ) -> list[Segment]:
        trimmed: list[Segment] = []
        for segment in segments:
            trimmed_segment = self._trim_segment_to_window(
                segment,
                window_start_time=window_start_time,
                window_end_time=window_end_time,
            )
            if trimmed_segment is not None:
                trimmed.append(trimmed_segment)
        return trimmed

    def _maintenance_loop(self) -> None:
        while not self.stop_event.wait(0.02):
            self._flush_queues_to_storage()
            self._restart_streams_if_needed()
            self._prune_old_chunks()

    def _prune_old_chunks(self) -> None:
        cutoff_time = time.perf_counter() - self.retention_seconds
        with self.storage_lock:
            self.mic_chunks = [
                chunk for chunk in self.mic_chunks if self._chunk_end_time(chunk) >= cutoff_time
            ]
            self.speaker_chunks = [
                chunk
                for chunk in self.speaker_chunks
                if self._chunk_end_time(chunk) >= cutoff_time
            ]

    def start(self) -> None:
        if self.capture_started_at is not None:
            raise RuntimeError("Continuous capture has already been started")
        self.capture_started_at = time.perf_counter()
        initial_pair = self.get_default_pair()
        self.open_streams(initial_pair)
        self.watcher_thread = threading.Thread(target=self.watch_default_devices, daemon=True)
        self.watcher_thread.start()
        self.maintenance_thread = threading.Thread(target=self._maintenance_loop, daemon=True)
        self.maintenance_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.close_streams()
        self._flush_queues_to_storage()
        for thread in (self.maintenance_thread, self.watcher_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        try:
            self.p.terminate()
        except Exception:
            pass

    def snapshot_mixed_window(
        self,
        *,
        window_seconds: float,
        end_time: float | None = None,
    ) -> MixedAudioWindow:
        if self.capture_started_at is None:
            raise RuntimeError("Continuous capture has not been started")

        self._flush_queues_to_storage()
        snapshot_end_time = end_time if end_time is not None else time.perf_counter()
        window_start_time = max(self.capture_started_at, snapshot_end_time - window_seconds)
        expected_frames = max(
            0,
            int(round((snapshot_end_time - window_start_time) * OUTPUT_RATE)),
        )

        with self.storage_lock:
            mic_chunks = [
                chunk
                for chunk in self.mic_chunks
                if self._chunk_end_time(chunk) > window_start_time
                and chunk.start_time < snapshot_end_time
            ]
            speaker_chunks = [
                chunk
                for chunk in self.speaker_chunks
                if self._chunk_end_time(chunk) > window_start_time
                and chunk.start_time < snapshot_end_time
            ]

        mic_segments = self._trim_segments_to_window(
            self._chunks_to_segments(mic_chunks, track_name="mic"),
            window_start_time=window_start_time,
            window_end_time=snapshot_end_time,
        )
        speaker_segments = self._trim_segments_to_window(
            self._chunks_to_segments(speaker_chunks, track_name="speaker"),
            window_start_time=window_start_time,
            window_end_time=snapshot_end_time,
        )

        mic_track = self._build_mic_track(mic_segments, window_start_time)
        speaker_track = self._build_speaker_track(speaker_segments, window_start_time)
        mic_track = self._fit_track_length(mic_track, frames=expected_frames, channels=1)
        speaker_track = self._fit_track_length(
            speaker_track,
            frames=expected_frames,
            channels=2,
        )
        final_mix = self._build_final_mix(mic_track, speaker_track)
        final_mix = self._fit_track_length(final_mix, frames=expected_frames, channels=2)

        return MixedAudioWindow(
            audio=final_mix,
            sample_rate=OUTPUT_RATE,
            start_time=window_start_time,
            end_time=snapshot_end_time,
        )


def default_output_path() -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return Path("recordings") / f"final_mix_{timestamp}.wav"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record the default microphone and default speaker loopback on Windows, "
            "reopen streams if the defaults change, and write raw+mixed WAV files."
        )
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help=f"Recording duration in seconds. Default: {DEFAULT_DURATION_SECONDS}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_path(),
        help="Final mixed WAV output path.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Default-device polling interval in seconds. Default: {DEFAULT_POLL_INTERVAL_SECONDS}.",
    )
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    recorder = DualAudioRecorder(
        duration_seconds=max(0.1, float(args.seconds)),
        output_path=Path(args.output),
        poll_interval_seconds=max(0.05, float(args.poll_interval)),
    )

    def handle_signal(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    recorder.run()


if __name__ == "__main__":
    main()
