from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .stable_prefix import AggregatorConfig, StablePrefixAggregator


DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_GRANULARITIES = ("word", "segment")
ROLLING_MANIFEST_NAME = "rolling-manifest.json"
ROLLING_MANIFEST_VERSION = 1
WINDOW_FILENAME_RE = re.compile(
    r"^tick-(?P<tick_index>\d+)-end-(?P<tick_end_s>\d+(?:\.\d+)?)\.json$"
)
ROLLING_CONFIG_FLAGS = {
    "window_seconds": "--window-seconds",
    "hop_seconds": "--hop-seconds",
    "commit_lag_seconds": "--commit-lag-seconds",
}


def load_api_key(default_key_path: Path | None) -> str:
    env_key = os.environ.get("GROQ_API_KEY", "").strip()
    if env_key:
        return env_key

    if default_key_path is None:
        raise RuntimeError(
            "Missing API key. Set GROQ_API_KEY or pass --key-file explicitly."
        )

    try:
        file_key = default_key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"API key file not found: {default_key_path}") from exc
    if not file_key:
        raise RuntimeError(f"API key file is empty: {default_key_path}")
    return file_key


def field(item: object, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def to_jsonable(value: object) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value


def parse_args() -> argparse.Namespace:
    repo_dir = Path(__file__).resolve().parents[2]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "audio",
        nargs="?",
        default=str(repo_dir / "tests" / "fixtures" / "30s.flac"),
        help="Path to the audio file.",
    )
    parser.add_argument(
        "--key-file",
        default=None,
        help="Path to the API key file used when GROQ_API_KEY is not set.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Optional prompt to guide style or spelling.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional ISO-639-1 language code.",
    )
    parser.add_argument(
        "--granularity",
        action="append",
        choices=["word", "segment"],
        dest="granularities",
        help="Timestamp granularity. Repeat to request both word and segment.",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional path to save the full verbose_json response.",
    )
    parser.add_argument(
        "--rolling",
        action="store_true",
        help="Request rolling overlapping windows instead of a single transcription.",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=30.0,
        help="Size of each rolling audio window in seconds.",
    )
    parser.add_argument(
        "--hop-seconds",
        type=float,
        default=5.0,
        help="Seconds advanced between rolling requests.",
    )
    parser.add_argument(
        "--commit-lag-seconds",
        type=float,
        default=7.0,
        help="How long to wait before freezing text into the stable prefix.",
    )
    parser.add_argument(
        "--save-windows-dir",
        default=None,
        help="Optional directory to save each rolling verbose_json response.",
    )
    parser.add_argument(
        "--rolling-from-dir",
        default=None,
        help="Replay rolling transcription from a directory of saved window responses.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Groq transcription model.",
    )
    args = parser.parse_args()

    granularities = tuple(dict.fromkeys(args.granularities or DEFAULT_GRANULARITIES))
    args.granularities = granularities
    raw_argv = sys.argv[1:]
    args.explicit_rolling_config = {
        key: any(
            token == flag or token.startswith(f"{flag}=")
            for token in raw_argv
        )
        for key, flag in ROLLING_CONFIG_FLAGS.items()
    }
    if args.rolling_from_dir and not args.rolling:
        parser.error("--rolling-from-dir requires --rolling.")
    if args.rolling_from_dir and args.save_windows_dir:
        parser.error("--rolling-from-dir cannot be combined with --save-windows-dir.")
    if args.rolling and set(granularities) != set(DEFAULT_GRANULARITIES):
        parser.error("--rolling requires both word and segment granularities.")
    if args.window_seconds <= 0.0:
        parser.error("--window-seconds must be positive.")
    if args.hop_seconds <= 0.0:
        parser.error("--hop-seconds must be positive.")
    if args.commit_lag_seconds < 0.0:
        parser.error("--commit-lag-seconds must be non-negative.")
    return args


def build_transcription_request(
    *,
    audio_name: str,
    audio_bytes: bytes,
    model: str,
    prompt: str | None,
    language: str | None,
    granularities: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "file": (audio_name, audio_bytes),
        "model": model,
        "temperature": 0.0,
        "response_format": "verbose_json",
        "timestamp_granularities": list(granularities),
    }
    if prompt:
        request["prompt"] = prompt
    if language:
        request["language"] = language
    return request


def create_client(api_key: str):
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "The 'groq' package is not installed. Use 'uv run python ...' for online commands."
        ) from exc

    return Groq(api_key=api_key)


def transcribe_bytes(
    client: Any,
    audio_name: str,
    audio_bytes: bytes,
    *,
    model: str,
    prompt: str | None,
    language: str | None,
    granularities: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    response = client.audio.transcriptions.create(
        **build_transcription_request(
            audio_name=audio_name,
            audio_bytes=audio_bytes,
            model=model,
            prompt=prompt,
            language=language,
            granularities=granularities,
        )
    )
    return to_jsonable(response)


def probe_audio_duration(audio_path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {audio_path}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return float(result.stdout.strip())


def slice_audio_window(audio_path: Path, start_s: float, duration_s: float) -> bytes:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(audio_path),
        "-t",
        f"{duration_s:.3f}",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-map",
        "0:a",
        "-c:a",
        "flac",
        "-f",
        "flac",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed for {audio_path}: {stderr}")
    if not result.stdout:
        raise RuntimeError(f"ffmpeg produced no audio bytes for {audio_path}.")
    return result.stdout


def generate_tick_ends(duration_s: float, hop_seconds: float) -> list[float]:
    if duration_s <= 0.0:
        return [0.0]
    if duration_s <= hop_seconds:
        return [duration_s]

    tick_ends: list[float] = []
    current = min(hop_seconds, duration_s)
    epsilon = 1e-9
    while current < duration_s - epsilon:
        tick_ends.append(round(current, 6))
        current += hop_seconds
    if not tick_ends or abs(tick_ends[-1] - duration_s) > epsilon:
        tick_ends.append(duration_s)
    return tick_ends


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_window_response_path(
    save_windows_dir: Path,
    *,
    tick_index: int,
    tick_end_s: float,
) -> Path:
    return save_windows_dir / f"tick-{tick_index:04d}-end-{tick_end_s:.2f}.json"


def build_rolling_manifest_path(save_windows_dir: Path) -> Path:
    return save_windows_dir / ROLLING_MANIFEST_NAME


def parse_window_response_path(path: Path) -> tuple[int, float]:
    match = WINDOW_FILENAME_RE.match(path.name)
    if match is None:
        raise RuntimeError(f"Invalid rolling window filename: {path.name}")
    return int(match.group("tick_index")), float(match.group("tick_end_s"))


def build_rolling_manifest(
    *,
    audio_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    aggregator_config = AggregatorConfig(
        window_seconds=args.window_seconds,
        hop_seconds=args.hop_seconds,
        commit_lag_seconds=args.commit_lag_seconds,
    )
    return {
        "version": ROLLING_MANIFEST_VERSION,
        "audio_path": str(audio_path),
        "model": args.model,
        "language": args.language,
        "prompt": args.prompt,
        "granularities": list(args.granularities),
        "window_seconds": args.window_seconds,
        "hop_seconds": args.hop_seconds,
        "commit_lag_seconds": args.commit_lag_seconds,
        "aggregator_config": asdict(aggregator_config),
    }


def _aggregator_config_from_manifest(manifest: dict[str, Any]) -> AggregatorConfig:
    config_payload = manifest.get("aggregator_config")
    if isinstance(config_payload, dict):
        return AggregatorConfig(**config_payload)
    return AggregatorConfig(
        window_seconds=float(manifest["window_seconds"]),
        hop_seconds=float(manifest["hop_seconds"]),
        commit_lag_seconds=float(manifest["commit_lag_seconds"]),
    )


def load_rolling_manifest(saved_windows_dir: Path) -> dict[str, Any]:
    manifest_path = build_rolling_manifest_path(saved_windows_dir)
    if not manifest_path.exists():
        raise RuntimeError(
            f"Rolling manifest not found: {manifest_path}. "
            "Replay requires the saved rolling manifest."
        )
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Rolling manifest must be a JSON object: {manifest_path}")
    if manifest.get("version") != ROLLING_MANIFEST_VERSION:
        raise RuntimeError(
            f"Unsupported rolling manifest version in {manifest_path}: "
            f"{manifest.get('version')!r}"
        )
    return manifest


def _saved_window_paths(saved_windows_dir: Path) -> list[Path]:
    return [
        path
        for path in saved_windows_dir.iterdir()
        if path.is_file() and WINDOW_FILENAME_RE.match(path.name)
    ]


def _same_float(left: Any, right: Any) -> bool:
    return abs(float(left) - float(right)) <= 1e-9


def _validate_manifest_matches_args(
    manifest: dict[str, Any],
    *,
    args: argparse.Namespace,
) -> None:
    expected_values = {
        "window_seconds": args.window_seconds,
        "hop_seconds": args.hop_seconds,
        "commit_lag_seconds": args.commit_lag_seconds,
        "model": args.model,
        "language": args.language,
        "prompt": args.prompt,
        "granularities": list(args.granularities),
    }
    for key, cli_value in expected_values.items():
        if key not in manifest:
            raise RuntimeError(f"Rolling manifest is missing required field: {key}")
        manifest_value = manifest.get(key)
        if key in ROLLING_CONFIG_FLAGS:
            if _same_float(manifest_value, cli_value):
                continue
            flag = ROLLING_CONFIG_FLAGS[key]
            raise RuntimeError(
                f"Rolling manifest conflict for {flag}: "
                f"manifest={manifest_value}, cli={cli_value}"
            )
        if manifest_value != cli_value:
            raise RuntimeError(
                f"Rolling manifest conflict for {key}: "
                f"manifest={manifest_value!r}, cli={cli_value!r}"
            )


def ensure_rolling_manifest(
    save_windows_dir: Path,
    *,
    audio_path: Path,
    args: argparse.Namespace,
) -> None:
    save_windows_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = build_rolling_manifest_path(save_windows_dir)
    manifest = build_rolling_manifest(audio_path=audio_path, args=args)
    if manifest_path.exists():
        existing_manifest = load_rolling_manifest(save_windows_dir)
        _validate_manifest_matches_args(existing_manifest, args=args)
        if existing_manifest != manifest:
            raise RuntimeError(
                f"Rolling manifest mismatch in {manifest_path}. "
                "Use a fresh directory or match the original rolling input and settings."
            )
        return
    if _saved_window_paths(save_windows_dir):
        raise RuntimeError(
            f"Rolling windows directory has saved windows but no manifest: {save_windows_dir}"
        )
    write_json(manifest_path, manifest)


def load_saved_rolling_responses(
    saved_windows_dir: Path,
) -> list[tuple[int, float, dict[str, Any]]]:
    if not saved_windows_dir.exists():
        raise RuntimeError(f"Rolling windows directory not found: {saved_windows_dir}")
    if not saved_windows_dir.is_dir():
        raise RuntimeError(f"Rolling windows path is not a directory: {saved_windows_dir}")

    responses: list[tuple[int, float, dict[str, Any]]] = []
    for path in _saved_window_paths(saved_windows_dir):
        tick_index, tick_end_s = parse_window_response_path(path)
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Rolling window payload must be a JSON object: {path}")
        responses.append((tick_index, tick_end_s, payload))

    if not responses:
        raise RuntimeError(f"No rolling window responses found in: {saved_windows_dir}")
    responses.sort(key=lambda item: (item[0], item[1]))
    return responses


def print_summary(transcription: dict[str, Any]) -> None:
    print(transcription.get("text", ""))
    print()

    duration = transcription.get("duration")
    if duration is not None:
        print(f"duration={duration}")

    segments = transcription.get("segments") or []
    if segments:
        print(f"segments={len(segments)}")
        for index, segment in enumerate(segments[:3], start=1):
            start_s = float(field(segment, "start") or 0.0)
            end_s = float(field(segment, "end") or 0.0)
            text = field(segment, "text") or ""
            print(f"segment[{index}] {start_s:.2f}-{end_s:.2f}: {text}")

    words = transcription.get("words") or []
    if words:
        print(f"words={len(words)}")
        for index, word in enumerate(words[:10], start=1):
            start_s = float(field(word, "start") or 0.0)
            end_s = float(field(word, "end") or 0.0)
            text = field(word, "word") or ""
            print(f"word[{index}] {start_s:.2f}-{end_s:.2f}: {text}")


def run_once(args: argparse.Namespace, client: Any) -> None:
    audio_path = Path(args.audio).resolve()
    transcription = transcribe_bytes(
        client,
        audio_path.name,
        audio_path.read_bytes(),
        model=args.model,
        prompt=args.prompt,
        language=args.language,
        granularities=args.granularities,
    )

    if args.output_json:
        output_path = Path(args.output_json).resolve()
        write_json(output_path, transcription)
        print(f"json_output={output_path}")
        print()

    print_summary(transcription)


def run_rolling(args: argparse.Namespace, client: Any) -> None:
    if args.rolling_from_dir:
        saved_windows_dir = Path(args.rolling_from_dir).resolve()
        manifest_path = build_rolling_manifest_path(saved_windows_dir)
        if manifest_path.exists():
            manifest = load_rolling_manifest(saved_windows_dir)
            for key, was_explicit in args.explicit_rolling_config.items():
                if was_explicit:
                    _validate_manifest_matches_args(manifest, args=args)
                    break
            aggregator_config = _aggregator_config_from_manifest(manifest)
        else:
            if not all(args.explicit_rolling_config.values()):
                raise RuntimeError(
                    "Legacy rolling directories without a manifest require explicit "
                    "--window-seconds, --hop-seconds, and --commit-lag-seconds."
                )
            aggregator_config = AggregatorConfig(
                window_seconds=args.window_seconds,
                hop_seconds=args.hop_seconds,
                commit_lag_seconds=args.commit_lag_seconds,
            )
        aggregator = StablePrefixAggregator(aggregator_config)
        for _, tick_end_s, transcription in load_saved_rolling_responses(saved_windows_dir):
            event = aggregator.ingest(transcription, window_end_s=tick_end_s)
            print(json.dumps(asdict(event), ensure_ascii=False))

        final_event = aggregator.flush()
        print(json.dumps(asdict(final_event), ensure_ascii=False))
        return

    audio_path = Path(args.audio).resolve()
    duration_s = probe_audio_duration(audio_path)
    save_windows_dir = (
        Path(args.save_windows_dir).resolve() if args.save_windows_dir else None
    )
    aggregator = StablePrefixAggregator(
        AggregatorConfig(
            window_seconds=args.window_seconds,
            hop_seconds=args.hop_seconds,
            commit_lag_seconds=args.commit_lag_seconds,
        )
    )
    if save_windows_dir is not None:
        ensure_rolling_manifest(
            save_windows_dir,
            audio_path=audio_path,
            args=args,
        )

    for tick_index, tick_end_s in enumerate(
        generate_tick_ends(duration_s, args.hop_seconds),
        start=1,
    ):
        window_start_s = max(0.0, tick_end_s - args.window_seconds)
        window_duration_s = max(tick_end_s - window_start_s, 0.0)
        saved_window_path = (
            build_window_response_path(
                save_windows_dir,
                tick_index=tick_index,
                tick_end_s=tick_end_s,
            )
            if save_windows_dir is not None
            else None
        )
        if saved_window_path is not None and saved_window_path.exists():
            transcription = read_json(saved_window_path)
        else:
            audio_bytes = slice_audio_window(audio_path, window_start_s, window_duration_s)
            transcription = transcribe_bytes(
                client,
                f"{audio_path.stem}-tick-{tick_index:04d}.flac",
                audio_bytes,
                model=args.model,
                prompt=args.prompt,
                language=args.language,
                granularities=args.granularities,
            )
            if saved_window_path is not None:
                write_json(saved_window_path, transcription)
        event = aggregator.ingest(transcription, window_end_s=tick_end_s)
        print(json.dumps(asdict(event), ensure_ascii=False))

    final_event = aggregator.flush()
    print(json.dumps(asdict(final_event), ensure_ascii=False))


def main() -> None:
    args = parse_args()
    if args.rolling:
        if args.rolling_from_dir:
            run_rolling(args, client=None)
            return
    audio_path = Path(args.audio).resolve()
    if not audio_path.exists():
        raise RuntimeError(f"Audio file not found: {audio_path}")
    key_path = Path(args.key_file).resolve() if args.key_file else None
    client = create_client(load_api_key(key_path))
    if args.rolling:
        run_rolling(args, client)
        return
    run_once(args, client)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
