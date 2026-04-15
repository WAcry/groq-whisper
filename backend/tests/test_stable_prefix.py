from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import groq_whisper_service.stable_prefix as stable_prefix
from groq_whisper_service.stable_prefix import (
    AggregatorConfig,
    StablePrefixAggregator,
    build_observations,
)
from groq_whisper_service.stable_prefix import RuntimeGeometry, _derive_runtime_geometry
from groq_whisper_service.rolling_transcriber import (
    _aggregator_config_from_manifest,
    build_rolling_manifest,
    build_rolling_manifest_path,
    build_transcription_request,
    build_window_response_path,
    generate_tick_ends,
    load_saved_rolling_responses,
    run_rolling,
)


FIXTURE_PATH = ROOT_DIR / "tests" / "fixtures" / "30s.word-segment.json"
FIXTURE_AUDIO_PATH = ROOT_DIR / "tests" / "fixtures" / "30s.flac"
TRANSCRIBE_PATH = ROOT_DIR / "transcribe.py"
LONG_REPLAY_WINDOWS_DIR = (
    ROOT_DIR / "artifacts" / "long-audio-1mp3" / "rerun-20260413" / "rolling" / "windows"
)


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def transcription_text(transcription: dict) -> str:
    return " ".join(word["word"] for word in transcription.get("words", []))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_rolling_args(**overrides: object) -> argparse.Namespace:
    values = {
        "audio": str(FIXTURE_AUDIO_PATH),
        "prompt": None,
        "language": None,
        "granularities": ("word", "segment"),
        "save_windows_dir": None,
        "rolling_from_dir": None,
        "window_seconds": 30.0,
        "hop_seconds": 5.0,
        "commit_lag_seconds": 7.0,
        "model": "whisper-large-v3-turbo",
        "explicit_rolling_config": {
            "window_seconds": False,
            "hop_seconds": False,
            "commit_lag_seconds": False,
        },
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def localize_window(
    transcription: dict,
    *,
    window_start_s: float,
    window_end_s: float,
) -> dict:
    window_length_s = window_end_s - window_start_s
    words = []
    for word in transcription.get("words", []):
        start_s = float(word["start"])
        end_s = float(word["end"])
        if end_s <= window_start_s or start_s >= window_end_s:
            continue
        words.append(
            {
                "word": word["word"],
                "start": max(0.0, start_s - window_start_s),
                "end": min(window_length_s, end_s - window_start_s),
            }
        )

    segments = []
    for segment in transcription.get("segments", []):
        start_s = float(segment["start"])
        end_s = float(segment["end"])
        if end_s <= window_start_s or start_s >= window_end_s:
            continue
        localized = dict(segment)
        localized["start"] = max(0.0, start_s - window_start_s)
        localized["end"] = min(window_length_s, end_s - window_start_s)
        segments.append(localized)

    return {
        "text": " ".join(word["word"] for word in words),
        "duration": window_length_s,
        "words": words,
        "segments": segments,
    }


def scale_transcription_times(transcription: dict, scale: float) -> dict:
    scaled = {
        "text": transcription_text(transcription),
        "duration": float(transcription.get("duration", 0.0)) * scale,
        "words": [],
        "segments": [],
    }
    for word in transcription.get("words", []):
        scaled["words"].append(
            {
                **word,
                "start": float(word["start"]) * scale,
                "end": float(word["end"]) * scale,
            }
        )
    for segment in transcription.get("segments", []):
        scaled["segments"].append(
            {
                **segment,
                "start": float(segment["start"]) * scale,
                "end": float(segment["end"]) * scale,
            }
        )
    return scaled


def run_perfect_replay(
    transcription: dict,
    *,
    window_seconds: float = 30.0,
    hop_seconds: float = 5.0,
) -> str:
    aggregator = StablePrefixAggregator(
        AggregatorConfig(
            window_seconds=window_seconds,
            hop_seconds=hop_seconds,
        )
    )
    duration_s = float(transcription["duration"])
    for tick_end_s in generate_tick_ends(duration_s, hop_seconds):
        aggregator.ingest(
            localize_window(
                transcription,
                window_start_s=max(0.0, tick_end_s - window_seconds),
                window_end_s=tick_end_s,
            ),
            window_end_s=tick_end_s,
        )
    return aggregator.flush().display_text


def make_transcription(
    words: list[tuple[str, float, float]],
    *,
    avg_logprob: float = -0.1,
    no_speech_prob: float = 0.01,
    compression_ratio: float = 1.2,
) -> dict:
    segment_start = words[0][1] if words else 0.0
    segment_end = words[-1][2] if words else 0.0
    return {
        "text": " ".join(word for word, _, _ in words),
        "words": [
            {
                "word": word,
                "start": start_s,
                "end": end_s,
            }
            for word, start_s, end_s in words
        ],
        "segments": [
            {
                "id": 0,
                "seek": 0,
                "start": segment_start,
                "end": segment_end,
                "text": " ".join(word for word, _, _ in words),
                "avg_logprob": avg_logprob,
                "compression_ratio": compression_ratio,
                "no_speech_prob": no_speech_prob,
            }
        ],
    }


def localize_words(
    words: list[tuple[str, float, float]],
    *,
    window_start_s: float,
) -> list[tuple[str, float, float]]:
    return [
        (word, start_s - window_start_s, end_s - window_start_s)
        for word, start_s, end_s in words
    ]


class StablePrefixTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("GROQ_API_KEY", None)
        return subprocess.run(
            [sys.executable, str(TRANSCRIBE_PATH), *args],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_build_observations_preserves_word_order(self) -> None:
        transcription = load_fixture()
        observations = build_observations(
            transcription,
            window_start_s=0.0,
            window_end_s=float(transcription["duration"]),
        )
        words = [observation.surface for observation in observations]
        together_index = words.index("together,")
        article_index = words.index("a", together_index)
        self.assertLess(together_index, article_index)
        self.assertLessEqual(
            observations[together_index].avg_logprob,
            0.0,
        )
        self.assertGreaterEqual(
            observations[together_index].compression_ratio,
            1.0,
        )

    def test_build_observations_translates_local_time_to_global_time(self) -> None:
        transcription = make_transcription([("tail", 27.5, 28.0)])
        observations = build_observations(
            transcription,
            window_start_s=2.5,
            window_end_s=32.5,
        )
        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].start_s, 30.0)
        self.assertAlmostEqual(observations[0].end_s, 30.5)
        self.assertAlmostEqual(observations[0].center_s, 30.25)
        self.assertAlmostEqual(observations[0].local_mid_s, 27.75)

    def test_build_observations_respects_custom_config(self) -> None:
        transcription = make_transcription(
            [("edge", 0.8, 1.2)],
            avg_logprob=-0.3,
            no_speech_prob=0.3,
        )
        observations = build_observations(
            transcription,
            window_start_s=0.0,
            window_end_s=4.0,
            config=AggregatorConfig(
                center_margin_seconds=0.5,
                low_logprob_threshold=-0.2,
                high_no_speech_threshold=0.2,
            ),
        )
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0].center_seen)
        self.assertTrue(observations[0].low_confidence)

    def test_build_observations_treats_absolute_start_as_safe_left_boundary(self) -> None:
        transcription = make_transcription([("alpha", 0.2, 0.6)])
        observations = build_observations(
            transcription,
            window_start_s=0.0,
            window_end_s=30.0,
        )
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0].center_seen)
        self.assertGreaterEqual(observations[0].score, 0.5)

    def test_runtime_geometry_scales_with_word_duration(self) -> None:
        config = AggregatorConfig()
        fast = _derive_runtime_geometry(
            config,
            reference_word_duration_s=0.10,
        )
        typical = _derive_runtime_geometry(
            config,
            reference_word_duration_s=0.30,
        )
        slow = _derive_runtime_geometry(
            config,
            reference_word_duration_s=0.50,
        )
        self.assertAlmostEqual(fast.edge_buffer_s, 0.8)
        self.assertAlmostEqual(fast.match_delta_s, 0.15)
        self.assertAlmostEqual(typical.edge_buffer_s, 1.2)
        self.assertAlmostEqual(typical.match_delta_s, 0.45)
        self.assertAlmostEqual(slow.edge_buffer_s, 2.0)
        self.assertAlmostEqual(slow.match_delta_s, 0.6)

    def test_runtime_geometry_drives_observation_center_seen_in_ingest(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        transcription = make_transcription([("edge", 0.3, 0.7)])
        with mock.patch.object(
            StablePrefixAggregator,
            "_runtime_geometry_for_tick",
            return_value=RuntimeGeometry(edge_buffer_s=0.2, match_delta_s=0.4),
        ):
            first = aggregator.ingest(transcription, window_end_s=35.0)
            second = aggregator.ingest(transcription, window_end_s=35.0)
        self.assertEqual(first.committed_text, "")
        self.assertEqual(second.committed_text, "edge")

    def test_runtime_geometry_drives_exact_match_alignment_in_ingest(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        with mock.patch.object(
            StablePrefixAggregator,
            "_runtime_geometry_for_tick",
            return_value=RuntimeGeometry(edge_buffer_s=2.0, match_delta_s=0.6),
        ):
            aggregator.ingest(
                make_transcription([("alpha", 4.0, 4.4)]),
                window_end_s=10.0,
            )
            second = aggregator.ingest(
                make_transcription([("alpha", 4.5, 4.9)]),
                window_end_s=15.0,
            )
        self.assertEqual(second.committed_text, "alpha")

    def test_perfect_replay_keeps_full_startup_prefix(self) -> None:
        transcription = load_fixture()
        final_text = run_perfect_replay(transcription)
        self.assertEqual(final_text, transcription_text(transcription))

    def test_startup_prefix_is_not_rate_dependent(self) -> None:
        fixture = load_fixture()
        expected = transcription_text(fixture)
        for scale in (1.0, 2.0, 3.0):
            with self.subTest(scale=scale):
                scaled = scale_transcription_times(fixture, scale)
                final_text = run_perfect_replay(scaled)
                self.assertEqual(final_text, expected)

    def test_high_confidence_token_commits_after_second_support(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        first = aggregator.ingest(
            make_transcription([("alpha", 4.0, 4.4)]),
            window_end_s=10.0,
        )
        second = aggregator.ingest(
            make_transcription([("alpha", 4.0, 4.4)]),
            window_end_s=15.0,
        )
        self.assertEqual(first.committed_text, "")
        self.assertEqual(second.committed_text, "alpha")
        self.assertEqual(second.tail_text, "")

    def test_startup_windows_do_not_commit_before_first_full_window(self) -> None:
        aggregator = StablePrefixAggregator()
        first = aggregator.ingest(
            make_transcription([("thank", 7.0, 7.4)]),
            window_end_s=10.0,
        )
        second = aggregator.ingest(
            make_transcription([("thank", 7.0, 7.4)]),
            window_end_s=15.0,
        )
        third = aggregator.ingest(
            make_transcription(
                [
                    ("alpha", 7.0, 7.4),
                    ("beta", 20.0, 20.4),
                ]
            ),
            window_end_s=30.0,
        )
        final_event = aggregator.flush()
        self.assertEqual(first.committed_text, "")
        self.assertEqual(second.committed_text, "")
        self.assertEqual(second.tail_text, "thank")
        self.assertEqual(third.committed_text, "")
        self.assertEqual(third.tail_text, "alpha beta")
        self.assertEqual(final_event.display_text, "alpha beta")
        self.assertNotIn("thank", final_event.display_text)

    def test_first_full_window_unlocks_normal_commit_behavior(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(
                require_full_window_before_commit=True,
            )
        )
        aggregator.ingest(
            make_transcription([("alpha", 4.0, 4.4)]),
            window_end_s=10.0,
        )
        pre_full = aggregator.ingest(
            make_transcription([("alpha", 4.0, 4.4)]),
            window_end_s=15.0,
        )
        at_full = aggregator.ingest(
            make_transcription([("alpha", 4.0, 4.4)]),
            window_end_s=30.0,
        )
        self.assertEqual(pre_full.committed_text, "")
        self.assertEqual(pre_full.tail_text, "alpha")
        self.assertEqual(at_full.committed_text, "alpha")
        self.assertEqual(at_full.tail_text, "")

    def test_low_confidence_token_waits_for_third_support(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        low_conf_transcription = make_transcription(
            [("uncertain", 4.0, 4.4)],
            avg_logprob=-0.9,
            no_speech_prob=0.7,
        )
        second = None
        for tick_end_s in (10.0, 15.0, 20.0):
            event = aggregator.ingest(low_conf_transcription, window_end_s=tick_end_s)
            if tick_end_s == 15.0:
                second = event
        final = aggregator.ingest(low_conf_transcription, window_end_s=25.0)
        self.assertIsNotNone(second)
        self.assertEqual(second.committed_text, "")
        self.assertEqual(final.committed_text, "uncertain")

    def test_weak_but_not_low_confidence_token_needs_more_evidence(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        weak_transcription = make_transcription(
            [("fragile", 4.0, 4.4)],
            avg_logprob=-0.45,
            no_speech_prob=0.55,
        )
        second = None
        final = None
        for tick_end_s in (10.0, 15.0, 20.0, 25.0, 30.0):
            event = aggregator.ingest(weak_transcription, window_end_s=tick_end_s)
            if tick_end_s == 15.0:
                second = event
            if tick_end_s == 30.0:
                final = event
        self.assertIsNotNone(second)
        self.assertIsNotNone(final)
        self.assertEqual(second.committed_text, "")
        self.assertEqual(second.tail_text, "fragile")
        self.assertEqual(final.committed_text, "fragile")

    def test_suffix_patch_only_rewrites_tail(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        first = aggregator.ingest(
            make_transcription(
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("gamma", 9.0, 9.4),
                ]
            ),
            window_end_s=10.0,
        )
        second = aggregator.ingest(
            make_transcription(
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("delta", 9.0, 9.4),
                ]
            ),
            window_end_s=15.0,
        )
        third = aggregator.ingest(
            make_transcription(
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("epsilon", 9.0, 9.4),
                ]
            ),
            window_end_s=20.0,
        )
        fourth = aggregator.ingest(
            make_transcription(
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("epsilon", 9.0, 9.4),
                ]
            ),
            window_end_s=25.0,
        )
        self.assertLessEqual(first.replace_from_char, second.replace_from_char)
        self.assertLessEqual(second.replace_from_char, third.replace_from_char)
        self.assertLessEqual(third.replace_from_char, fourth.replace_from_char)
        self.assertEqual(
            second.display_text[: second.replace_from_char],
            fourth.display_text[: second.replace_from_char],
        )
        self.assertEqual(second.committed_text, "alpha beta")
        self.assertNotEqual(second.tail_text, fourth.tail_text)
        self.assertEqual(fourth.display_text, "alpha beta epsilon")

    def test_patch_event_respects_previous_committed_boundary(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        sequence = [
            (
                10.0,
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("gamma", 9.0, 9.4),
                ],
            ),
            (
                15.0,
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("delta", 9.0, 9.4),
                ],
            ),
            (
                20.0,
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("epsilon", 9.0, 9.4),
                ],
            ),
            (
                25.0,
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 5.0, 5.4),
                    ("epsilon", 9.0, 9.4),
                ],
            ),
        ]
        previous_display = ""
        for tick_end_s, words in sequence:
            event = aggregator.ingest(make_transcription(words), window_end_s=tick_end_s)
            self.assertLessEqual(event.replace_from_char, len(previous_display))
            self.assertEqual(
                previous_display[: event.replace_from_char],
                event.display_text[: event.replace_from_char],
            )
            previous_display = event.display_text

    def test_new_observations_replace_old_tail_hallucination(self) -> None:
        aggregator = StablePrefixAggregator()
        global_words = [
            ("by", 26.0, 26.1),
            ("miscarriage", 26.1, 26.7),
        ]
        first = aggregator.ingest(
            make_transcription(localize_words(global_words, window_start_s=0.0)),
            window_end_s=30.0,
        )
        corrected_global_words = [
            ("by", 26.0, 26.1),
            ("means", 26.1, 26.4),
            ("of", 26.4, 26.6),
        ]
        second = aggregator.ingest(
            make_transcription(localize_words(corrected_global_words, window_start_s=0.5)),
            window_end_s=30.5,
        )
        final_event = aggregator.flush()
        self.assertEqual(first.tail_text, "by miscarriage")
        self.assertEqual(second.tail_text, "by means of")
        self.assertEqual(final_event.display_text, "by means of")

    def test_expanding_windows_do_not_reappend_committed_prefix(self) -> None:
        aggregator = StablePrefixAggregator()
        sequence = [
            (
                5.0,
                [
                    ("alpha", 0.0, 0.4),
                    ("beta", 0.5, 0.9),
                    ("gamma", 4.2, 4.6),
                ],
            ),
            (
                10.0,
                [
                    ("alpha", 0.0, 0.4),
                    ("beta", 0.5, 0.9),
                    ("gamma", 4.2, 4.6),
                    ("delta", 8.8, 9.2),
                ],
            ),
            (
                15.0,
                [
                    ("alpha", 0.0, 0.4),
                    ("beta", 0.5, 0.9),
                    ("gamma", 4.2, 4.6),
                    ("delta", 8.8, 9.2),
                    ("epsilon", 13.8, 14.2),
                ],
            ),
            (
                20.0,
                [
                    ("alpha", 0.0, 0.4),
                    ("beta", 0.5, 0.9),
                    ("gamma", 4.2, 4.6),
                    ("delta", 8.8, 9.2),
                    ("epsilon", 13.8, 14.2),
                    ("zeta", 18.8, 19.2),
                ],
            ),
        ]
        events = [
            aggregator.ingest(make_transcription(words), window_end_s=window_end_s)
            for window_end_s, words in sequence
        ]
        final_event = aggregator.flush()
        self.assertEqual(final_event.display_text, "alpha beta gamma delta epsilon zeta")
        self.assertNotIn("alpha beta alpha beta", final_event.display_text)
        self.assertTrue(
            all(
                events[index].replace_from_char <= events[index + 1].replace_from_char
                for index in range(len(events) - 1)
            )
        )

    def test_identical_windows_do_not_drop_adjacent_short_word(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig()
        )
        global_words = [
            ("which", 22.82, 22.96),
            ("strive", 22.96, 23.36),
            ("with", 23.36, 23.60),
            ("one", 23.60, 23.78),
        ]
        first = aggregator.ingest(
            make_transcription(localize_words(global_words, window_start_s=0.0)),
            window_end_s=30.0,
        )
        second = aggregator.ingest(
            make_transcription(localize_words(global_words, window_start_s=0.5)),
            window_end_s=30.5,
        )
        final_event = aggregator.flush()
        self.assertEqual(first.committed_text, "")
        self.assertEqual(first.tail_text, "which strive with one")
        self.assertEqual(second.committed_text, "which strive")
        self.assertEqual(second.tail_text, "with one")
        self.assertEqual(final_event.display_text, "which strive with one")

    def test_observations_before_committed_boundary_do_not_reenter_tail(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        transcription = make_transcription(
            [
                ("alpha", 4.0, 4.4),
                ("beta", 4.5, 4.9),
            ]
        )
        aggregator.ingest(transcription, window_end_s=10.0)
        aggregator.ingest(transcription, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("alpha", 4.0, 4.4),
                    ("beta", 4.5, 4.9),
                    ("gamma", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "alpha beta")
        self.assertEqual(event.tail_text, "gamma")

    def test_cross_boundary_overlap_is_consumed_by_anchor(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        seed = make_transcription(
            [
                ("alpha", 4.0, 4.4),
                ("beta", 4.5, 4.9),
            ]
        )
        aggregator.ingest(seed, window_end_s=10.0)
        aggregator.ingest(seed, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("beta", 4.6, 4.85),
                    ("gamma", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "alpha beta")
        self.assertEqual(event.tail_text, "gamma")
        self.assertNotIn("beta beta", event.display_text)

    def test_true_repeated_word_can_enter_tail(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        seed = make_transcription(
            [
                ("alpha", 4.0, 4.4),
                ("beta", 4.5, 4.9),
            ]
        )
        aggregator.ingest(seed, window_end_s=10.0)
        aggregator.ingest(seed, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("beta", 5.2, 5.6),
                    ("gamma", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "alpha beta")
        self.assertEqual(event.tail_text, "beta gamma")
        self.assertIn("alpha beta beta gamma", event.display_text)

    def test_cross_boundary_repeated_word_can_reenter_tail(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        seed = make_transcription([("go", 4.0, 4.4)])
        aggregator.ingest(seed, window_end_s=10.0)
        aggregator.ingest(seed, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("go", 4.3, 4.8),
                    ("next", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "go")
        self.assertEqual(event.tail_text, "go next")
        self.assertEqual(event.display_text, "go go next")

    def test_high_overlap_boundary_word_is_absorbed_by_anchor(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        first = aggregator.ingest(
            make_transcription(
                [
                    ("because", 4.0, 4.58),
                    ("one", 4.58, 5.74),
                ]
            ),
            window_end_s=10.0,
        )
        second = aggregator.ingest(
            make_transcription(
                [
                    ("because", 4.0, 4.58),
                    ("one", 4.58, 5.74),
                    ("of", 5.74, 6.0),
                ]
            ),
            window_end_s=15.0,
        )
        third = aggregator.ingest(
            make_transcription(
                [
                    ("because", 4.0, 4.58),
                    ("one", 4.44, 6.86),
                    ("of", 6.86, 7.0),
                    ("the", 7.0, 7.1),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(first.committed_text, "")
        self.assertEqual(second.committed_text, "because one")
        self.assertEqual(second.tail_text, "of")
        self.assertEqual(third.committed_text, "because one")
        self.assertEqual(third.tail_text, "of the")
        self.assertNotIn("one one", third.display_text)

    def test_real_artifact_boundary_duplicate_requires_anchor_absorb_helper(self) -> None:
        if not LONG_REPLAY_WINDOWS_DIR.exists():
            self.skipTest("long rolling artifact fixture is not available in this repository")
        responses = load_saved_rolling_responses(LONG_REPLAY_WINDOWS_DIR)

        def replay_until_59() -> tuple[str, str]:
            aggregator = StablePrefixAggregator(
                AggregatorConfig(window_seconds=30.0, hop_seconds=5.0, commit_lag_seconds=7.0)
            )
            tick_59_display = ""
            final_display = ""
            for tick_index, tick_end_s, transcription in responses:
                event = aggregator.ingest(transcription, window_end_s=tick_end_s)
                if tick_index == 59:
                    tick_59_display = event.display_text
                    break
            final_display = tick_59_display
            return tick_59_display, final_display

        tick_59_display, _ = replay_until_59()
        self.assertNotIn("one one of the wire services", tick_59_display)

        with mock.patch.object(
            stable_prefix,
            "_anchor_overlap_absorbs_observation",
            return_value=False,
        ):
            duplicated_display, _ = replay_until_59()
        self.assertIn("one one of the wire services", duplicated_display)

    def test_exact_boundary_repeated_word_can_reenter_tail(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        seed = make_transcription([("bridge", 4.0, 4.4)])
        aggregator.ingest(seed, window_end_s=10.0)
        aggregator.ingest(seed, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("bridge", 4.4, 4.8),
                    ("next", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "bridge")
        self.assertEqual(event.tail_text, "bridge next")
        self.assertEqual(event.display_text, "bridge bridge next")


    def test_small_boundary_jitter_does_not_repeat_committed_word(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        seed = make_transcription(
            [
                ("alpha", 4.0, 4.4),
                ("beta", 4.5, 4.9),
            ]
        )
        aggregator.ingest(seed, window_end_s=10.0)
        aggregator.ingest(seed, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("beta", 4.6, 4.95),
                    ("gamma", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "alpha beta")
        self.assertEqual(event.tail_text, "gamma")
        self.assertNotIn("beta beta", event.display_text)

    def test_anchor_overlap_duplicate_does_not_commit_life_twice(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(
                require_full_window_before_commit=False,
            )
        )
        global_words = [
            ("successes", 22.8, 23.2),
            ("in", 23.2, 23.52),
            ("life.", 23.52, 23.78),
        ]
        aggregator.ingest(
            make_transcription(localize_words(global_words, window_start_s=0.0)),
            window_end_s=30.0,
        )
        aggregator.ingest(
            make_transcription(localize_words(global_words, window_start_s=5.0)),
            window_end_s=35.0,
        )
        event = aggregator.ingest(
            make_transcription(
                localize_words(
                    [
                        ("life.", 23.52, 24.08),
                        ("I", 24.1, 24.2),
                        ("want", 24.2, 24.5),
                    ],
                    window_start_s=15.0,
                )
            ),
            window_end_s=45.0,
        )
        final_event = aggregator.flush()
        self.assertIn("successes in life. I want", event.display_text)
        self.assertNotIn("life. life.", event.display_text)
        self.assertNotIn("life. life.", final_event.display_text)

    def test_small_boundary_jitter_of_different_word_is_retained(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        seed = make_transcription(
            [
                ("alpha", 4.0, 4.4),
                ("beta", 4.5, 4.9),
            ]
        )
        aggregator.ingest(seed, window_end_s=10.0)
        aggregator.ingest(seed, window_end_s=15.0)
        event = aggregator.ingest(
            make_transcription(
                [
                    ("gamma", 4.75, 5.0),
                    ("delta", 10.0, 10.4),
                ]
            ),
            window_end_s=15.5,
        )
        self.assertEqual(event.committed_text, "alpha beta")
        self.assertEqual(event.tail_text, "gamma delta")

    def test_missing_tail_observation_drops_after_second_missing_window(self) -> None:
        aggregator = StablePrefixAggregator()
        first = aggregator.ingest(
            make_transcription([("ghost", 29.7, 29.9)]),
            window_end_s=30.0,
        )
        second = aggregator.ingest(
            make_transcription([]),
            window_end_s=30.2,
        )
        third = aggregator.ingest(
            make_transcription([]),
            window_end_s=30.4,
        )
        final_event = aggregator.flush()
        self.assertEqual(first.tail_text, "ghost")
        self.assertEqual(second.tail_text, "ghost")
        self.assertEqual(third.tail_text, "")
        self.assertEqual(final_event.display_text, "")

    def test_contradicted_tail_token_drops_immediately(self) -> None:
        aggregator = StablePrefixAggregator()
        global_first = [("ghost", 29.7, 29.9)]
        global_second = [("real", 29.7, 29.9)]
        first = aggregator.ingest(
            make_transcription(localize_words(global_first, window_start_s=0.0)),
            window_end_s=30.0,
        )
        second = aggregator.ingest(
            make_transcription(localize_words(global_second, window_start_s=0.5)),
            window_end_s=30.5,
        )
        final_event = aggregator.flush()
        self.assertEqual(first.tail_text, "ghost")
        self.assertEqual(second.tail_text, "real")
        self.assertNotIn("ghost", second.display_text)
        self.assertEqual(final_event.display_text, "real")

    def test_lightly_overlapping_different_word_is_retained_for_one_tick(self) -> None:
        aggregator = StablePrefixAggregator()
        global_first = [("alpha", 29.70, 29.90)]
        global_second = [("beta", 29.88, 29.95)]
        first = aggregator.ingest(
            make_transcription(localize_words(global_first, window_start_s=0.0)),
            window_end_s=30.0,
        )
        second = aggregator.ingest(
            make_transcription(localize_words(global_second, window_start_s=0.5)),
            window_end_s=30.5,
        )
        self.assertEqual(first.tail_text, "alpha")
        self.assertIn("alpha", second.display_text)
        self.assertIn("beta", second.display_text)

    def test_short_window_carry_forward_token_is_not_reused_for_new_match(self) -> None:
        aggregator = StablePrefixAggregator(
            config=AggregatorConfig(window_seconds=5.0, hop_seconds=5.0)
        )
        first = aggregator.ingest(
            make_transcription(localize_words([("a", 4.8, 5.0)], window_start_s=0.0)),
            window_end_s=5.0,
        )
        second = aggregator.ingest(
            make_transcription(localize_words([("a", 5.6, 5.8)], window_start_s=5.6)),
            window_end_s=10.6,
        )
        third = aggregator.ingest(
            make_transcription([]),
            window_end_s=15.6,
        )
        final_event = aggregator.flush()
        self.assertEqual(first.tail_text, "a")
        self.assertEqual(second.tail_text, "a a")
        self.assertEqual(third.tail_text, "a")
        self.assertEqual(final_event.display_text, "a")

    def test_shifted_window_fixture_does_not_reintroduce_old_tail_text(self) -> None:
        transcription = load_fixture()
        first_window = localize_window(
            transcription,
            window_start_s=0.0,
            window_end_s=30.0,
        )
        second_window = localize_window(
            transcription,
            window_start_s=2.645,
            window_end_s=32.645,
        )
        aggregator = StablePrefixAggregator()
        aggregator.ingest(first_window, window_end_s=30.0)
        aggregator.ingest(second_window, window_end_s=32.645)
        final_event = aggregator.flush()
        self.assertIn(
            "by means of the hitherto existing morality",
            final_event.display_text,
        )
        self.assertNotIn("miscarriage", final_event.display_text)

    def test_truncated_full_window_preserves_existing_right_tail(self) -> None:
        aggregator = StablePrefixAggregator()
        first_words = [
            ("alpha", 20.0, 20.4),
            ("omega", 27.5, 27.9),
        ]
        second_words = [
            ("alpha", 20.0, 20.4),
        ]
        first = aggregator.ingest(
            make_transcription(localize_words(first_words, window_start_s=0.0)),
            window_end_s=30.0,
        )
        second = aggregator.ingest(
            make_transcription(localize_words(second_words, window_start_s=5.0)),
            window_end_s=35.0,
        )
        self.assertEqual(first.display_text, "alpha omega")
        self.assertEqual(second.display_text, "alpha omega")
        self.assertEqual(second.committed_text, "alpha")
        self.assertEqual(second.tail_text, "omega")

    def test_unmatched_tail_token_is_retained_for_one_tick_then_dropped(self) -> None:
        aggregator = StablePrefixAggregator()
        first = aggregator.ingest(
            make_transcription([("tail", 29.2, 29.6)]),
            window_end_s=30.0,
        )
        second = aggregator.ingest(
            make_transcription([]),
            window_end_s=30.5,
        )
        third = aggregator.ingest(
            make_transcription([]),
            window_end_s=31.0,
        )
        self.assertEqual(first.tail_text, "tail")
        self.assertEqual(second.tail_text, "tail")
        self.assertEqual(third.tail_text, "")

    def test_flush_commits_remaining_tail(self) -> None:
        aggregator = StablePrefixAggregator()
        aggregator.ingest(
            make_transcription([("tail", 8.5, 8.9)]),
            window_end_s=10.0,
        )
        final = aggregator.flush()
        self.assertEqual(final.display_text, "tail")
        self.assertEqual(final.tail_text, "")

    def test_aging_fallback_commits_stalled_token(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(
                require_full_window_before_commit=False,
                commit_lag_seconds=100.0,
            )
        )
        aggregator.ingest(
            make_transcription([("edge", 4.0, 4.4)]),
            window_end_s=10.0,
        )
        aggregator.ingest(
            make_transcription([("edge", 4.0, 4.4)]),
            window_end_s=15.0,
        )
        aged = aggregator.ingest(
            make_transcription([]),
            window_end_s=40.0,
        )
        self.assertEqual(aged.committed_text, "edge")

    def test_single_weak_token_does_not_stale_commit_on_next_empty_tick(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        weak_transcription = make_transcription(
            [("fragile", 4.0, 4.4)],
            avg_logprob=-0.45,
            no_speech_prob=0.55,
        )
        aggregator.ingest(weak_transcription, window_end_s=10.0)
        stale_tick = aggregator.ingest(
            make_transcription([]),
            window_end_s=30.0,
        )
        self.assertEqual(stale_tick.committed_text, "")
        self.assertIn("fragile", stale_tick.display_text)

    def test_single_low_confidence_token_does_not_stale_commit_on_next_empty_tick(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        low_conf_transcription = make_transcription(
            [("uncertain", 4.0, 4.4)],
            avg_logprob=-0.9,
            no_speech_prob=0.7,
        )
        aggregator.ingest(low_conf_transcription, window_end_s=10.0)
        stale_tick = aggregator.ingest(
            make_transcription([]),
            window_end_s=30.0,
        )
        self.assertEqual(stale_tick.committed_text, "")
        self.assertIn("uncertain", stale_tick.display_text)

    def test_startup_partial_window_token_does_not_stale_commit_on_first_full_empty_window(
        self,
    ) -> None:
        aggregator = StablePrefixAggregator()
        aggregator.ingest(
            make_transcription([("thank", 7.0, 7.4)]),
            window_end_s=10.0,
        )
        aggregator.ingest(
            make_transcription([("thank", 7.0, 7.4)]),
            window_end_s=15.0,
        )
        full_window = aggregator.ingest(
            make_transcription([]),
            window_end_s=30.0,
        )
        final_event = aggregator.flush()
        self.assertEqual(full_window.committed_text, "")
        self.assertIn("thank", full_window.display_text)
        self.assertEqual(final_event.display_text, "")

    def test_left_edge_startup_token_does_not_block_later_commits(self) -> None:
        aggregator = StablePrefixAggregator()
        aggregator.ingest(
            make_transcription([("thank", 4.0, 4.4)]),
            window_end_s=10.0,
        )
        aggregator.ingest(
            make_transcription([("thank", 4.0, 4.4)]),
            window_end_s=15.0,
        )
        aggregator.ingest(
            make_transcription([]),
            window_end_s=30.0,
        )
        fourth = aggregator.ingest(
            make_transcription(
                localize_words(
                    [
                        ("alpha", 20.0, 20.4),
                        ("beta", 27.0, 27.4),
                    ],
                    window_start_s=5.0,
                )
            ),
            window_end_s=35.0,
        )
        fifth = aggregator.ingest(
            make_transcription(
                localize_words(
                    [
                        ("alpha", 20.0, 20.4),
                        ("beta", 27.0, 27.4),
                    ],
                    window_start_s=10.0,
                )
            ),
            window_end_s=40.0,
        )
        final_event = aggregator.flush()
        self.assertNotIn("thank", fourth.display_text)
        self.assertEqual(fifth.committed_text, "alpha beta")
        self.assertEqual(final_event.display_text, "alpha beta")

    def test_flush_drops_weak_token_after_empty_followup_window(self) -> None:
        aggregator = StablePrefixAggregator(
            AggregatorConfig(require_full_window_before_commit=False)
        )
        weak_transcription = make_transcription(
            [("fragile", 4.0, 4.4)],
            avg_logprob=-0.45,
            no_speech_prob=0.55,
        )
        aggregator.ingest(weak_transcription, window_end_s=10.0)
        aggregator.ingest(make_transcription([]), window_end_s=30.0)
        final_event = aggregator.flush()
        self.assertEqual(final_event.display_text, "")

    def test_generate_tick_ends_covers_non_multiple_duration(self) -> None:
        self.assertEqual(generate_tick_ends(12.0, 5.0), [5.0, 10.0, 12.0])
        self.assertEqual(generate_tick_ends(4.0, 5.0), [4.0])

    def test_build_transcription_request_requests_verbose_json(self) -> None:
        request = build_transcription_request(
            audio_name="sample.flac",
            audio_bytes=b"123",
            model="whisper-large-v3-turbo",
            prompt="hello",
            language="en",
            granularities=("word", "segment"),
        )
        self.assertEqual(request["response_format"], "verbose_json")
        self.assertEqual(request["timestamp_granularities"], ["word", "segment"])
        self.assertEqual(request["language"], "en")

    def test_build_rolling_manifest_includes_full_aggregator_config(self) -> None:
        args = make_rolling_args()
        manifest = build_rolling_manifest(
            audio_path=FIXTURE_AUDIO_PATH,
            args=args,
        )
        self.assertIn("aggregator_config", manifest)
        self.assertEqual(
            manifest["aggregator_config"],
            {
                "window_seconds": 30.0,
                "hop_seconds": 5.0,
                "commit_lag_seconds": 7.0,
                "carry_grace_ticks": 1,
                "require_full_window_before_commit": True,
                "center_margin_seconds": 2.0,
                "max_match_delta_seconds": 0.4,
                "edge_buffer_word_duration_factor": 4.0,
                "edge_buffer_min_seconds": 0.8,
                "edge_buffer_max_seconds": 2.5,
                "match_delta_word_duration_factor": 1.5,
                "match_delta_min_seconds": 0.15,
                "match_delta_max_seconds": 0.6,
                "recent_duration_window_count": 5,
                "min_support": 2,
                "min_commit_score": 0.2,
                "low_confidence_support": 3,
                "low_logprob_threshold": -0.5,
                "high_no_speech_threshold": 0.6,
                "stale_commit_age_seconds": 12.0,
                "anchor_seconds": 2.0,
                "anchor_reentry_min_overlap_ratio": 0.75,
            },
        )

    def test_aggregator_config_from_manifest_prefers_full_snapshot(self) -> None:
        manifest = build_rolling_manifest(
            audio_path=FIXTURE_AUDIO_PATH,
            args=make_rolling_args(),
        )
        manifest["aggregator_config"]["require_full_window_before_commit"] = False
        manifest["aggregator_config"]["center_margin_seconds"] = 1.25
        config = _aggregator_config_from_manifest(manifest)
        self.assertFalse(config.require_full_window_before_commit)
        self.assertAlmostEqual(config.center_margin_seconds, 1.25)

    def test_load_saved_rolling_responses_orders_by_tick(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            write_json(
                build_window_response_path(
                    saved_dir,
                    tick_index=2,
                    tick_end_s=10.0,
                ),
                make_transcription([("beta", 0.0, 0.4)]),
            )
            write_json(
                build_window_response_path(
                    saved_dir,
                    tick_index=1,
                    tick_end_s=5.0,
                ),
                make_transcription([("alpha", 0.0, 0.4)]),
            )

            responses = load_saved_rolling_responses(saved_dir)

        self.assertEqual([tick_index for tick_index, _, _ in responses], [1, 2])
        self.assertEqual([tick_end_s for _, tick_end_s, _ in responses], [5.0, 10.0])

    def test_load_saved_rolling_responses_orders_ticks_past_9999(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            for tick_index in (10000, 9999, 10001, 9998):
                write_json(
                    build_window_response_path(
                        saved_dir,
                        tick_index=tick_index,
                        tick_end_s=float(tick_index),
                    ),
                    make_transcription([(str(tick_index), 0.0, 0.4)]),
                )

            responses = load_saved_rolling_responses(saved_dir)

        self.assertEqual(
            [tick_index for tick_index, _, _ in responses],
            [9998, 9999, 10000, 10001],
        )

    def test_run_rolling_reuses_existing_saved_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            args = make_rolling_args(save_windows_dir=str(saved_dir))
            write_json(
                build_rolling_manifest_path(saved_dir),
                build_rolling_manifest(
                    audio_path=FIXTURE_AUDIO_PATH,
                    args=args,
                ),
            )
            write_json(
                build_window_response_path(
                    saved_dir,
                    tick_index=1,
                    tick_end_s=4.0,
                ),
                make_transcription([("alpha", 0.0, 0.4)]),
            )

            stdout = io.StringIO()
            with (
                mock.patch(
                    "groq_whisper_service.rolling_transcriber.probe_audio_duration",
                    return_value=4.0,
                ),
                mock.patch(
                    "groq_whisper_service.rolling_transcriber.slice_audio_window",
                    side_effect=AssertionError("saved rolling window should be reused"),
                ),
                mock.patch(
                    "groq_whisper_service.rolling_transcriber.transcribe_bytes",
                    side_effect=AssertionError("saved rolling window should be reused"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                run_rolling(args, client=object())

        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["display_text"], "alpha")
        self.assertEqual(lines[-1]["tail_text"], "")

    def test_run_rolling_uses_single_request_for_truncated_full_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            args = make_rolling_args(save_windows_dir=str(saved_dir), hop_seconds=30.0)
            primary = make_transcription([("Thank", 11.32, 13.80), ("you.", 13.80, 15.48)])
            stdout = io.StringIO()
            mocked_transcribe = mock.Mock(return_value=primary)
            with (
                mock.patch(
                    "groq_whisper_service.rolling_transcriber.probe_audio_duration",
                    return_value=30.0,
                ),
                mock.patch(
                    "groq_whisper_service.rolling_transcriber.slice_audio_window",
                    return_value=b"primary",
                ),
                mock.patch(
                    "groq_whisper_service.rolling_transcriber.transcribe_bytes",
                    mocked_transcribe,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                run_rolling(args, client=object())

            saved_tick = json.loads(
                build_window_response_path(saved_dir, tick_index=1, tick_end_s=30.0).read_text(
                    encoding="utf-8"
                )
            )

        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(lines[-1]["display_text"], "Thank you.")
        self.assertEqual(saved_tick["words"], primary["words"])
        self.assertEqual(saved_tick["segments"], primary["segments"])
        self.assertEqual(mocked_transcribe.call_count, 1)
        self.assertFalse(any(saved_dir.glob("*.primary.json")))
        self.assertFalse(any(saved_dir.glob("*.rescue-*.json")))

    def test_cli_rolling_from_dir_uses_manifest_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            manifest_args = make_rolling_args(
                rolling_from_dir=str(saved_dir),
                window_seconds=5.0,
                hop_seconds=5.0,
                commit_lag_seconds=7.0,
            )
            write_json(
                build_rolling_manifest_path(saved_dir),
                build_rolling_manifest(
                    audio_path=FIXTURE_AUDIO_PATH,
                    args=manifest_args,
                ),
            )
            write_json(
                build_window_response_path(saved_dir, tick_index=1, tick_end_s=5.0),
                make_transcription([("alpha", 4.6, 4.9)]),
            )
            write_json(
                build_window_response_path(saved_dir, tick_index=2, tick_end_s=10.0),
                make_transcription([("beta", 4.6, 4.9)]),
            )

            result = self.run_cli(
                "--rolling",
                "--rolling-from-dir",
                str(saved_dir),
            )

        self.assertEqual(result.returncode, 0)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line]
        self.assertEqual(lines[-1]["display_text"], "alpha beta")
        self.assertEqual(lines[-1]["tail_text"], "")

    def test_cli_rolling_from_dir_uses_manifest_aggregator_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            manifest = build_rolling_manifest(
                audio_path=FIXTURE_AUDIO_PATH,
                args=make_rolling_args(
                    rolling_from_dir=str(saved_dir),
                    window_seconds=30.0,
                    hop_seconds=5.0,
                    commit_lag_seconds=7.0,
                ),
            )
            manifest["aggregator_config"]["require_full_window_before_commit"] = False
            write_json(build_rolling_manifest_path(saved_dir), manifest)
            write_json(
                build_window_response_path(saved_dir, tick_index=1, tick_end_s=10.0),
                make_transcription([("alpha", 4.0, 4.4)]),
            )
            write_json(
                build_window_response_path(saved_dir, tick_index=2, tick_end_s=15.0),
                make_transcription([("alpha", 4.0, 4.4)]),
            )

            result = self.run_cli(
                "--rolling",
                "--rolling-from-dir",
                str(saved_dir),
            )

        self.assertEqual(result.returncode, 0)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line]
        self.assertEqual(lines[1]["committed_text"], "alpha")

    def test_cli_rolling_from_dir_rejects_conflicting_window_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            manifest_args = make_rolling_args(
                rolling_from_dir=str(saved_dir),
                window_seconds=5.0,
                hop_seconds=5.0,
                commit_lag_seconds=7.0,
            )
            write_json(
                build_rolling_manifest_path(saved_dir),
                build_rolling_manifest(
                    audio_path=FIXTURE_AUDIO_PATH,
                    args=manifest_args,
                ),
            )
            write_json(
                build_window_response_path(saved_dir, tick_index=1, tick_end_s=5.0),
                make_transcription([("alpha", 4.6, 4.9)]),
            )

            result = self.run_cli(
                "--rolling",
                "--rolling-from-dir",
                str(saved_dir),
                "--window-seconds",
                "30",
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Rolling manifest conflict for --window-seconds: manifest=5.0, cli=30.0",
            result.stderr,
        )

    def test_cli_rolling_from_dir_does_not_require_key(self) -> None:
        transcription = load_fixture()
        tick_ends = generate_tick_ends(float(transcription["duration"]), 5.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            manifest_args = make_rolling_args(rolling_from_dir=str(saved_dir))
            write_json(
                build_rolling_manifest_path(saved_dir),
                build_rolling_manifest(
                    audio_path=FIXTURE_AUDIO_PATH,
                    args=manifest_args,
                ),
            )
            for tick_index, tick_end_s in enumerate(tick_ends, start=1):
                write_json(
                    build_window_response_path(
                        saved_dir,
                        tick_index=tick_index,
                        tick_end_s=tick_end_s,
                    ),
                    localize_window(
                        transcription,
                        window_start_s=max(0.0, tick_end_s - 30.0),
                        window_end_s=tick_end_s,
                    ),
                )

            result = self.run_cli(
                "--rolling",
                "--rolling-from-dir",
                str(saved_dir),
            )

        self.assertEqual(result.returncode, 0)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line]
        self.assertEqual(len(lines), len(tick_ends) + 1)
        self.assertEqual(lines[-1]["tail_text"], "")
        self.assertEqual(result.stderr, "")

    def test_cli_rolling_from_dir_legacy_directory_without_manifest(self) -> None:
        transcription = load_fixture()
        tick_ends = generate_tick_ends(float(transcription["duration"]), 5.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            for tick_index, tick_end_s in enumerate(tick_ends, start=1):
                write_json(
                    build_window_response_path(
                        saved_dir,
                        tick_index=tick_index,
                        tick_end_s=tick_end_s,
                    ),
                    localize_window(
                        transcription,
                        window_start_s=max(0.0, tick_end_s - 30.0),
                        window_end_s=tick_end_s,
                    ),
                )

            result = self.run_cli(
                "--rolling",
                "--rolling-from-dir",
                str(saved_dir),
                "--window-seconds",
                "30",
                "--hop-seconds",
                "5",
                "--commit-lag-seconds",
                "7",
            )

        self.assertEqual(result.returncode, 0)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line]
        self.assertEqual(len(lines), len(tick_ends) + 1)
        self.assertEqual(lines[-1]["tail_text"], "")
        self.assertEqual(result.stderr, "")

    def test_cli_rolling_from_dir_legacy_directory_requires_explicit_config(self) -> None:
        transcription = load_fixture()
        tick_ends = generate_tick_ends(float(transcription["duration"]), 5.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_dir = Path(temp_dir)
            for tick_index, tick_end_s in enumerate(tick_ends, start=1):
                write_json(
                    build_window_response_path(
                        saved_dir,
                        tick_index=tick_index,
                        tick_end_s=tick_end_s,
                    ),
                    localize_window(
                        transcription,
                        window_start_s=max(0.0, tick_end_s - 30.0),
                        window_end_s=tick_end_s,
                    ),
                )

            result = self.run_cli(
                "--rolling",
                "--rolling-from-dir",
                str(saved_dir),
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Legacy rolling directories without a manifest require explicit",
            result.stderr,
        )

    def test_cli_help_does_not_require_groq(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: transcribe.py", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_cli_missing_key_reports_clear_error(self) -> None:
        result = self.run_cli(str(FIXTURE_AUDIO_PATH))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing API key", result.stderr)
        self.assertNotIn("playground/groq_api_key", result.stderr)

    def test_cli_missing_explicit_key_file_reports_given_path(self) -> None:
        result = self.run_cli(str(FIXTURE_AUDIO_PATH), "--key-file", "/tmp/test-groq-key")
        self.assertEqual(result.returncode, 1)
        missing_key_path = Path("/tmp/test-groq-key").resolve()
        self.assertIn(f"API key file not found: {missing_key_path}", result.stderr)
        self.assertNotIn("playground/groq_api_key", result.stderr)

    def test_cli_missing_audio_reports_clear_error(self) -> None:
        result = self.run_cli("/tmp/does-not-exist.flac", "--key-file", "/tmp/test-groq-key")
        self.assertEqual(result.returncode, 1)
        missing_audio_path = Path("/tmp/does-not-exist.flac").resolve()
        self.assertIn(f"Audio file not found: {missing_audio_path}", result.stderr)


if __name__ == "__main__":
    unittest.main()
