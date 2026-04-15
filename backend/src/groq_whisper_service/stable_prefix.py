from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any
import string
import unicodedata


def _clip(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _field(item: object, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _as_float(value: object, default: float) -> float:
    if value is None:
        return default
    return float(value)


@dataclass(frozen=True)
class RuntimeGeometry:
    edge_buffer_s: float
    match_delta_s: float


def _derive_runtime_geometry(
    config: AggregatorConfig,
    *,
    reference_word_duration_s: float | None,
) -> RuntimeGeometry:
    if reference_word_duration_s is None or reference_word_duration_s <= 0.0:
        return RuntimeGeometry(
            edge_buffer_s=config.center_margin_seconds,
            match_delta_s=config.max_match_delta_seconds,
        )
    edge_buffer_s = _clip(
        config.edge_buffer_word_duration_factor * reference_word_duration_s,
        config.edge_buffer_min_seconds,
        config.edge_buffer_max_seconds,
    )
    match_delta_s = _clip(
        config.match_delta_word_duration_factor * reference_word_duration_s,
        config.match_delta_min_seconds,
        config.match_delta_max_seconds,
    )
    return RuntimeGeometry(
        edge_buffer_s=edge_buffer_s,
        match_delta_s=match_delta_s,
    )


def _observation_geometry(
    *,
    center_s: float,
    window_start_s: float,
    window_end_s: float,
    geometry: RuntimeGeometry,
) -> tuple[float, float, bool]:
    actual_window_len_s = max(window_end_s - window_start_s, 1e-6)
    local_mid_s = center_s - window_start_s
    edge_scale_s = max(geometry.edge_buffer_s, 1e-6)

    # The absolute audio start is not a rolling crop boundary, so only a shifted
    # window should penalize missing left-side context.
    left_context_s = local_mid_s if window_start_s > 1e-6 else edge_scale_s
    right_context_s = actual_window_len_s - local_mid_s
    safe_distance_s = min(left_context_s, right_context_s)
    edge_score = _clip(safe_distance_s / edge_scale_s, 0.0, 1.0)
    center_seen = safe_distance_s >= (geometry.edge_buffer_s - 1e-6)
    return local_mid_s, edge_score, center_seen


def _low_confidence_from_metadata(
    *,
    avg_logprob: float,
    no_speech_prob: float,
    config: AggregatorConfig,
) -> bool:
    return (
        avg_logprob < config.low_logprob_threshold
        or no_speech_prob > config.high_no_speech_threshold
    )


def _is_edge_punctuation(char: str) -> bool:
    if char in string.whitespace:
        return True
    return unicodedata.category(char).startswith("P")


def _strip_edge_punctuation(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and _is_edge_punctuation(text[start]):
        start += 1
    while end > start and _is_edge_punctuation(text[end - 1]):
        end -= 1
    return text[start:end]


@dataclass(frozen=True)
class AggregatorConfig:
    window_seconds: float = 30.0
    hop_seconds: float = 5.0
    commit_lag_seconds: float = 7.0
    carry_grace_ticks: int = 1
    require_full_window_before_commit: bool = True
    center_margin_seconds: float = 2.0
    max_match_delta_seconds: float = 0.4
    edge_buffer_word_duration_factor: float = 4.0
    edge_buffer_min_seconds: float = 0.8
    edge_buffer_max_seconds: float = 2.5
    match_delta_word_duration_factor: float = 1.5
    match_delta_min_seconds: float = 0.15
    match_delta_max_seconds: float = 0.6
    recent_duration_window_count: int = 5
    min_support: int = 2
    min_commit_score: float = 0.2
    low_confidence_support: int = 3
    low_logprob_threshold: float = -0.5
    high_no_speech_threshold: float = 0.6
    stale_commit_age_seconds: float = 12.0
    anchor_seconds: float = 2.0
    anchor_reentry_min_overlap_ratio: float = 0.75


DEFAULT_CONFIG = AggregatorConfig()


@dataclass(frozen=True)
class WordObservation:
    surface: str
    norm: str
    start_s: float
    end_s: float
    center_s: float
    window_end_s: float
    local_mid_s: float
    score: float
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float
    center_seen: bool
    low_confidence: bool
    seen_in_full_window: bool


@dataclass
class MutableToken:
    norm: str
    surface: str
    start_s: float
    end_s: float
    total_score: float
    best_observation_score: float
    support: int
    center_seen: bool
    low_confidence: bool
    full_window_seen: bool
    last_seen_tick: int
    miss_count: int


@dataclass(frozen=True)
class PatchEvent:
    window_end_s: float
    replace_from_char: int
    replacement_text: str
    display_text: str
    committed_text: str
    tail_text: str


@dataclass(frozen=True)
class _RenderableToken:
    surface: str
    norm: str
    start_s: float
    end_s: float

    @property
    def center_s(self) -> float:
        return (self.start_s + self.end_s) / 2.0


@dataclass(frozen=True)
class _AlignmentNode:
    kind: str
    index: int
    norm: str
    center_s: float


def normalize_token(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    normalized = _strip_edge_punctuation(normalized)
    return normalized.strip()


def _anchor_overlap_absorbs_observation(
    *,
    anchor_token: _RenderableToken,
    observation: WordObservation,
    committed_end_s: float,
) -> bool:
    if observation.norm != anchor_token.norm:
        return False
    if observation.start_s >= committed_end_s:
        return False
    overlap_s = min(anchor_token.end_s, observation.end_s) - max(
        anchor_token.start_s,
        observation.start_s,
    )
    if overlap_s <= 0.0:
        return False
    anchor_duration_s = max(anchor_token.end_s - anchor_token.start_s, 1e-6)
    observation_duration_s = max(observation.end_s - observation.start_s, 1e-6)
    min_duration_s = min(anchor_duration_s, observation_duration_s)
    required_overlap_s = max(0.12, 0.5 * min_duration_s)
    return overlap_s >= required_overlap_s


def _pick_segment_for_word(
    segments: list[object],
    *,
    start_s: float,
    end_s: float,
    center_s: float,
) -> object | None:
    if not segments:
        return None

    nearest = None
    nearest_distance = float("inf")
    overlapping_segments: list[tuple[float, float, float, object]] = []
    for segment in segments:
        segment_start_s = _as_float(_field(segment, "start"), 0.0)
        segment_end_s = _as_float(_field(segment, "end"), segment_start_s)
        overlap_s = min(end_s, segment_end_s) - max(start_s, segment_start_s)
        if overlap_s > 0.0:
            overlapping_segments.append(
                (
                    -overlap_s,
                    segment_end_s - segment_start_s,
                    -segment_start_s,
                    segment,
                )
            )
        distance = min(abs(center_s - segment_start_s), abs(center_s - segment_end_s))
        if distance < nearest_distance:
            nearest = segment
            nearest_distance = distance
    if overlapping_segments:
        overlapping_segments.sort(key=lambda item: (item[0], item[1], item[2]))
        return overlapping_segments[0][3]
    return nearest


def build_observations(
    transcription: dict[str, Any] | Any,
    *,
    window_start_s: float,
    window_end_s: float,
    config: AggregatorConfig | None = None,
    geometry: RuntimeGeometry | None = None,
) -> list[WordObservation]:
    active_config = config or DEFAULT_CONFIG
    active_geometry = geometry or RuntimeGeometry(
        edge_buffer_s=active_config.center_margin_seconds,
        match_delta_s=active_config.max_match_delta_seconds,
    )
    words = list(_field(transcription, "words") or [])
    raw_segments = list(_field(transcription, "segments") or [])
    actual_window_len_s = max(window_end_s - window_start_s, 1e-6)
    is_full_window = actual_window_len_s >= active_config.window_seconds - 1e-6
    observations: list[WordObservation] = []
    segments = [
        {
            "start": window_start_s + _as_float(_field(segment, "start"), 0.0),
            "end": window_start_s
            + _as_float(
                _field(segment, "end"),
                _as_float(_field(segment, "start"), 0.0),
            ),
            "avg_logprob": _as_float(_field(segment, "avg_logprob"), 0.0),
            "no_speech_prob": _as_float(_field(segment, "no_speech_prob"), 0.0),
            "compression_ratio": _as_float(
                _field(segment, "compression_ratio"),
                1.0,
            ),
            "_source": _field(segment, "_source"),
        }
        for segment in raw_segments
    ]

    for word in words:
        surface = str(_field(word, "word") or "")
        if not surface:
            continue
        word_source = _field(word, "_source")
        raw_start_s = _as_float(_field(word, "start"), 0.0)
        raw_end_s = _as_float(_field(word, "end"), raw_start_s)
        start_s = window_start_s + raw_start_s
        end_s = window_start_s + raw_end_s
        center_s = (start_s + end_s) / 2.0
        candidate_segments = segments
        if word_source is not None:
            sourced_segments = [
                segment
                for segment in segments
                if _field(segment, "_source") == word_source
            ]
            if sourced_segments:
                candidate_segments = sourced_segments
        segment = _pick_segment_for_word(
            candidate_segments,
            start_s=start_s,
            end_s=end_s,
            center_s=center_s,
        )
        avg_logprob = _as_float(_field(segment, "avg_logprob"), 0.0)
        no_speech_prob = _as_float(_field(segment, "no_speech_prob"), 0.0)
        compression_ratio = _as_float(_field(segment, "compression_ratio"), 1.0)

        local_mid_s, edge, center_seen = _observation_geometry(
            center_s=center_s,
            window_start_s=window_start_s,
            window_end_s=window_end_s,
            geometry=active_geometry,
        )
        logp = _clip((avg_logprob + 0.5) / 0.5, 0.0, 1.0)
        speech = _clip(1.0 - no_speech_prob, 0.0, 1.0)
        comp = 0.85 if compression_ratio < 1.0 or compression_ratio > 2.4 else 1.0
        score = edge * logp * speech * comp
        low_confidence = _low_confidence_from_metadata(
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            config=active_config,
        )

        observations.append(
            WordObservation(
                surface=surface,
                norm=normalize_token(surface),
                start_s=start_s,
                end_s=end_s,
                center_s=center_s,
                window_end_s=window_end_s,
                local_mid_s=local_mid_s,
                score=score,
                avg_logprob=avg_logprob,
                no_speech_prob=no_speech_prob,
                compression_ratio=compression_ratio,
                center_seen=center_seen,
                low_confidence=low_confidence,
                seen_in_full_window=is_full_window,
            )
        )

    return observations


class StablePrefixAggregator:
    def __init__(self, config: AggregatorConfig | None = None) -> None:
        self.config = config or AggregatorConfig()
        self.committed_tokens: list[_RenderableToken] = []
        self.mutable_tail: list[MutableToken] = []
        self.tick_index = 0
        self.last_window_end_s = 0.0
        self.has_seen_full_window = False
        self._last_committed_char_count = 0
        self._recent_window_word_durations_s: list[float] = []
        self._runtime_geometry = RuntimeGeometry(
            edge_buffer_s=self.config.center_margin_seconds,
            match_delta_s=self.config.max_match_delta_seconds,
        )

    def ingest(
        self,
        transcription: dict[str, Any] | Any,
        *,
        window_end_s: float,
    ) -> PatchEvent:
        window_start_s = max(0.0, window_end_s - self.config.window_seconds)
        window_duration_s = max(window_end_s - window_start_s, 1e-6)
        is_full_window = window_duration_s >= self.config.window_seconds - 1e-6
        self.has_seen_full_window = self.has_seen_full_window or (
            is_full_window
        )
        current_window_median_duration_s = self._median_word_duration_from_transcription(
            transcription
        )
        self._runtime_geometry = self._runtime_geometry_for_tick(
            current_window_median_duration_s
        )
        committed_end_s = self._committed_end_s()
        raw_observations = build_observations(
            transcription,
            window_start_s=window_start_s,
            window_end_s=window_end_s,
            config=self.config,
            geometry=self._runtime_geometry,
        )
        observations = [
            self._apply_config_to_observation(
                observation,
                window_start_s=window_start_s,
                window_end_s=window_end_s,
                geometry=self._runtime_geometry,
            )
            for observation in raw_observations
        ]
        structural_match_delta_s = self._structural_match_delta_s()
        latest_observation_end_s = max(
            (observation.end_s for observation in observations),
            default=window_start_s,
        )
        anchor_tokens = self._anchor_tokens()
        anchor_boundary_s = (
            anchor_tokens[0].start_s - structural_match_delta_s
            if anchor_tokens
            else float("-inf")
        )
        anchor_candidate_observations = [
            observation
            for observation in observations
            if observation.end_s > anchor_boundary_s
            and observation.start_s <= committed_end_s
        ]
        anchor_assignments = self._align_exact(
            self._build_anchor_nodes(anchor_tokens),
            anchor_candidate_observations,
        )
        forced_anchor_ids = {
            id(observation)
            for observation, assignment in zip(
                anchor_candidate_observations,
                anchor_assignments,
            )
            if assignment is None
            and any(
                _anchor_overlap_absorbs_observation(
                    anchor_token=anchor_token,
                    observation=observation,
                    committed_end_s=committed_end_s,
                )
                for anchor_token in anchor_tokens
            )
        }
        tail_observations: list[WordObservation] = []
        consumed_anchor_ids = {
            id(observation)
            for observation, assignment in zip(
                anchor_candidate_observations,
                anchor_assignments,
            )
            if assignment is not None and assignment.kind == "anchor"
            and observation.end_s <= committed_end_s
        }
        anchor_matched_ids = {
            id(observation)
            for observation, assignment in zip(
                anchor_candidate_observations,
                anchor_assignments,
            )
            if assignment is not None and assignment.kind == "anchor"
        }
        anchor_matched_ids |= forced_anchor_ids
        for observation in observations:
            if id(observation) in consumed_anchor_ids:
                continue
            if (
                id(observation) in anchor_matched_ids
                and observation.start_s <= committed_end_s
            ):
                overlap_after_boundary_s = max(0.0, observation.end_s - committed_end_s)
                token_duration_s = max(observation.end_s - observation.start_s, 1e-6)
                overlap_ratio = overlap_after_boundary_s / token_duration_s
                starts_at_boundary = abs(observation.start_s - committed_end_s) <= 1e-6
                materially_post_boundary = (
                    (
                        overlap_after_boundary_s
                        >= (self.config.max_match_delta_seconds - 1e-6)
                        and overlap_ratio >= self.config.anchor_reentry_min_overlap_ratio
                    )
                    or (
                        starts_at_boundary
                        and overlap_ratio >= self.config.anchor_reentry_min_overlap_ratio
                    )
                )
                if not materially_post_boundary:
                    continue
            if observation.end_s <= committed_end_s:
                continue
            tail_observations.append(observation)
        previous_tail = self.mutable_tail
        matchable_previous_tail: list[MutableToken] = []
        carry_forward_tail: list[MutableToken] = []
        for token in previous_tail:
            renderable = self._renderable_from_mutable(token)
            if renderable is None:
                continue
            if renderable.end_s <= window_start_s:
                carried = self._clone_mutable_token(token)
                carried.miss_count += 1
                if not self._can_commit_without_future_observation(carried):
                    if carried.miss_count > self.config.carry_grace_ticks:
                        continue
                carry_forward_tail.append(carried)
            else:
                matchable_previous_tail.append(token)
        tail_assignments = self._align_exact(
            self._tail_nodes(matchable_previous_tail),
            tail_observations,
        )
        matched_tail_indices = {
            assignment.index
            for assignment in tail_assignments
            if assignment is not None and assignment.kind == "tail"
        }
        new_tail: list[MutableToken] = list(carry_forward_tail)
        for observation, assignment in zip(tail_observations, tail_assignments):
            if assignment is None:
                token = self._new_mutable_token(observation)
            else:
                token = self._clone_mutable_token(matchable_previous_tail[assignment.index])
                self._merge_observation(token, observation)
            new_tail.append(token)
        preserve_from_s = latest_observation_end_s - structural_match_delta_s
        for index, token in enumerate(matchable_previous_tail):
            if index in matched_tail_indices:
                continue
            renderable = self._renderable_from_mutable(token)
            if renderable is None:
                continue
            contradicted = any(
                observation.norm != renderable.norm
                and abs(renderable.center_s - observation.center_s)
                <= structural_match_delta_s
                and (
                    min(renderable.end_s, observation.end_s)
                    - max(renderable.start_s, observation.start_s)
                )
                >= (
                    0.5
                    * min(
                        renderable.end_s - renderable.start_s,
                        observation.end_s - observation.start_s,
                    )
                )
                for observation in tail_observations
            )
            if contradicted:
                continue
            if renderable.start_s < preserve_from_s:
                continue
            carried = self._clone_mutable_token(token)
            carried.miss_count += 1
            if carried.miss_count > self.config.carry_grace_ticks:
                continue
            new_tail.append(carried)

        self.mutable_tail = new_tail
        self._commit_ready(window_end_s)
        self._prune_tail_before_committed()
        self.last_window_end_s = window_end_s
        self.tick_index += 1
        self._record_window_duration(current_window_median_duration_s)
        return self._build_patch_event(window_end_s)

    def flush(self) -> PatchEvent:
        final_window_start_s = max(
            0.0,
            self.last_window_end_s - self.config.window_seconds,
        )
        non_overlapping_windows = (
            self.config.hop_seconds >= self.config.window_seconds - 1e-6
        )
        while self.mutable_tail:
            token = self.mutable_tail.pop(0)
            renderable = self._renderable_from_mutable(token)
            if renderable is None:
                continue
            seen_in_latest_tick = token.last_seen_tick == (self.tick_index - 1)
            slid_out_of_window = (
                non_overlapping_windows and renderable.end_s <= final_window_start_s
            )
            if (
                not self.has_seen_full_window
                or seen_in_latest_tick
                or slid_out_of_window
                or self._can_commit_without_future_observation(token)
            ):
                self.committed_tokens.append(renderable)
        return self._build_patch_event(self.last_window_end_s)

    def _apply_config_to_observation(
        self,
        observation: WordObservation,
        *,
        window_start_s: float,
        window_end_s: float,
        geometry: RuntimeGeometry,
    ) -> WordObservation:
        local_mid_s, edge_score, center_seen = _observation_geometry(
            center_s=observation.center_s,
            window_start_s=window_start_s,
            window_end_s=window_end_s,
            geometry=geometry,
        )
        low_confidence = _low_confidence_from_metadata(
            avg_logprob=observation.avg_logprob,
            no_speech_prob=observation.no_speech_prob,
            config=self.config,
        )
        logp = _clip((observation.avg_logprob + 0.5) / 0.5, 0.0, 1.0)
        speech = _clip(1.0 - observation.no_speech_prob, 0.0, 1.0)
        comp = (
            0.85
            if observation.compression_ratio < 1.0
            or observation.compression_ratio > 2.4
            else 1.0
        )
        score = edge_score * logp * speech * comp
        return WordObservation(
            surface=observation.surface,
            norm=observation.norm,
            start_s=observation.start_s,
            end_s=observation.end_s,
            center_s=observation.center_s,
            window_end_s=observation.window_end_s,
            local_mid_s=local_mid_s,
            score=score,
            avg_logprob=observation.avg_logprob,
            no_speech_prob=observation.no_speech_prob,
            compression_ratio=observation.compression_ratio,
            center_seen=center_seen,
            low_confidence=low_confidence,
            seen_in_full_window=observation.seen_in_full_window,
        )

    def _new_mutable_token(self, observation: WordObservation) -> MutableToken:
        return MutableToken(
            norm=observation.norm,
            surface=observation.surface,
            start_s=observation.start_s,
            end_s=observation.end_s,
            total_score=observation.score,
            best_observation_score=observation.score,
            support=1,
            center_seen=observation.center_seen,
            low_confidence=observation.low_confidence,
            full_window_seen=observation.seen_in_full_window,
            last_seen_tick=self.tick_index,
            miss_count=0,
        )

    def _clone_mutable_token(self, token: MutableToken) -> MutableToken:
        return MutableToken(
            norm=token.norm,
            surface=token.surface,
            start_s=token.start_s,
            end_s=token.end_s,
            total_score=token.total_score,
            best_observation_score=token.best_observation_score,
            support=token.support,
            center_seen=token.center_seen,
            low_confidence=token.low_confidence,
            full_window_seen=token.full_window_seen,
            last_seen_tick=token.last_seen_tick,
            miss_count=token.miss_count,
        )

    def _merge_observation(self, token: MutableToken, observation: WordObservation) -> None:
        token.total_score += observation.score
        if observation.score >= token.best_observation_score:
            token.surface = observation.surface
            token.start_s = observation.start_s
            token.end_s = observation.end_s
            token.best_observation_score = observation.score
        if token.last_seen_tick != self.tick_index:
            token.support += 1
        token.center_seen = token.center_seen or observation.center_seen
        token.low_confidence = token.low_confidence or observation.low_confidence
        token.full_window_seen = token.full_window_seen or observation.seen_in_full_window
        token.last_seen_tick = self.tick_index
        token.miss_count = 0

    def _committed_end_s(self) -> float:
        if not self.committed_tokens:
            return float("-inf")
        return self.committed_tokens[-1].end_s

    def _anchor_tokens(self) -> list[_RenderableToken]:
        if not self.committed_tokens:
            return []
        committed_end_s = self.committed_tokens[-1].end_s
        anchor_start_s = committed_end_s - self.config.anchor_seconds
        anchor_tokens = [
            token
            for token in self.committed_tokens
            if token.end_s > anchor_start_s
        ]
        return anchor_tokens or [self.committed_tokens[-1]]

    def _build_anchor_nodes(
        self,
        anchor_tokens: list[_RenderableToken] | None = None,
    ) -> list[_AlignmentNode]:
        anchor_tokens = anchor_tokens if anchor_tokens is not None else self._anchor_tokens()
        return [
            _AlignmentNode(
                kind="anchor",
                index=index,
                norm=token.norm,
                center_s=token.center_s,
            )
            for index, token in enumerate(anchor_tokens)
        ]

    def _tail_nodes(self, tail: list[MutableToken]) -> list[_AlignmentNode]:
        nodes: list[_AlignmentNode] = []
        for index, token in enumerate(tail):
            renderable = self._renderable_from_mutable(token)
            if renderable is None:
                continue
            nodes.append(
                _AlignmentNode(
                    kind="tail",
                    index=index,
                    norm=token.norm,
                    center_s=renderable.center_s,
                )
            )
        return nodes

    def _align_exact(
        self,
        nodes: list[_AlignmentNode],
        observations: list[WordObservation],
    ) -> list[_AlignmentNode | None]:
        node_count = len(nodes)
        observation_count = len(observations)
        assignments: list[_AlignmentNode | None] = [None] * observation_count
        if not nodes or not observations:
            return assignments

        gap_penalty = -0.8
        dp = [[0.0] * (observation_count + 1) for _ in range(node_count + 1)]
        move = [[""] * (observation_count + 1) for _ in range(node_count + 1)]

        for index in range(1, node_count + 1):
            dp[index][0] = dp[index - 1][0] + gap_penalty
            move[index][0] = "up"
        for index in range(1, observation_count + 1):
            dp[0][index] = dp[0][index - 1] + gap_penalty
            move[0][index] = "left"

        for node_index in range(1, node_count + 1):
            node = nodes[node_index - 1]
            for observation_index in range(1, observation_count + 1):
                observation = observations[observation_index - 1]
                best_score = dp[node_index - 1][observation_index] + gap_penalty
                best_move = "up"

                left_score = dp[node_index][observation_index - 1] + gap_penalty
                if left_score > best_score:
                    best_score = left_score
                    best_move = "left"

                center_diff = abs(node.center_s - observation.center_s)
                if (
                    node.norm == observation.norm
                    and center_diff <= self._runtime_geometry.match_delta_s
                ):
                    time_bonus = 1.0 - (
                        center_diff / self._runtime_geometry.match_delta_s
                    )
                    match_score = (
                        dp[node_index - 1][observation_index - 1]
                        + 2.0
                        + time_bonus
                        + (0.5 * observation.score)
                    )
                    if match_score > best_score:
                        best_score = match_score
                        best_move = "match"

                dp[node_index][observation_index] = best_score
                move[node_index][observation_index] = best_move

        node_index = node_count
        observation_index = observation_count
        while node_index > 0 or observation_index > 0:
            current_move = move[node_index][observation_index]
            if current_move == "match":
                assignments[observation_index - 1] = nodes[node_index - 1]
                node_index -= 1
                observation_index -= 1
            elif current_move == "left":
                observation_index -= 1
            else:
                node_index -= 1

        return assignments

    def _renderable_from_mutable(self, token: MutableToken) -> _RenderableToken | None:
        return _RenderableToken(
            surface=token.surface,
            norm=token.norm,
            start_s=token.start_s,
            end_s=token.end_s,
        )

    def _commit_ready(self, window_end_s: float) -> None:
        if self.config.require_full_window_before_commit and not self.has_seen_full_window:
            return
        while self.mutable_tail:
            token = self.mutable_tail[0]
            renderable = self._renderable_from_mutable(token)
            if renderable is None:
                self.mutable_tail.pop(0)
                continue
            full_window_ready = self._full_window_ready(token)
            evidence_ready = self._evidence_ready(token)
            stale = (
                token.last_seen_tick < self.tick_index
                and token.center_seen
                and full_window_ready
                and evidence_ready
                and (window_end_s - renderable.end_s)
                >= self.config.stale_commit_age_seconds
            )
            ready = (
                renderable.end_s <= window_end_s - self.config.commit_lag_seconds
                and token.center_seen
                and full_window_ready
                and evidence_ready
            )

            if not ready and not stale:
                break

            self.mutable_tail.pop(0)
            self.committed_tokens.append(renderable)

    def _full_window_ready(self, token: MutableToken) -> bool:
        return (
            not self.config.require_full_window_before_commit
            or token.full_window_seen
        )

    def _evidence_ready(self, token: MutableToken) -> bool:
        evidence_ready = token.support >= self.config.min_support
        if token.low_confidence:
            return evidence_ready and token.support >= self.config.low_confidence_support
        return evidence_ready and token.total_score >= self.config.min_commit_score

    def _can_commit_without_future_observation(self, token: MutableToken) -> bool:
        return (
            token.center_seen
            and self._full_window_ready(token)
            and self._evidence_ready(token)
        )

    def _structural_match_delta_s(self) -> float:
        return max(
            self.config.max_match_delta_seconds,
            self._runtime_geometry.match_delta_s,
        )

    def _median_word_duration_from_transcription(
        self,
        transcription: dict[str, Any] | Any,
    ) -> float | None:
        durations_s: list[float] = []
        for word in list(_field(transcription, "words") or []):
            raw_start_s = _as_float(_field(word, "start"), 0.0)
            raw_end_s = _as_float(_field(word, "end"), raw_start_s)
            duration_s = raw_end_s - raw_start_s
            if duration_s > 0.0:
                durations_s.append(duration_s)
        if not durations_s:
            return None
        return median(durations_s)

    def _runtime_geometry_for_tick(
        self,
        current_window_median_duration_s: float | None,
    ) -> RuntimeGeometry:
        reference_samples = list(self._recent_window_word_durations_s)
        if current_window_median_duration_s is not None:
            reference_samples.append(current_window_median_duration_s)
        reference_duration_s = median(reference_samples) if reference_samples else None
        return _derive_runtime_geometry(
            self.config,
            reference_word_duration_s=reference_duration_s,
        )

    def _record_window_duration(self, duration_s: float | None) -> None:
        if duration_s is None:
            return
        self._recent_window_word_durations_s.append(duration_s)
        if (
            self.config.recent_duration_window_count > 0
            and len(self._recent_window_word_durations_s)
            > self.config.recent_duration_window_count
        ):
            self._recent_window_word_durations_s = self._recent_window_word_durations_s[
                -self.config.recent_duration_window_count :
            ]

    def _prune_tail_before_committed(self) -> None:
        if not self.committed_tokens:
            return
        committed_end_s = self.committed_tokens[-1].end_s
        pruned_tail: list[MutableToken] = []
        for token in self.mutable_tail:
            renderable = self._renderable_from_mutable(token)
            if renderable is None:
                continue
            if renderable.end_s <= committed_end_s:
                continue
            pruned_tail.append(token)
        self.mutable_tail = pruned_tail

    def _join_text(self, tokens: list[_RenderableToken]) -> str:
        return " ".join(token.surface for token in tokens if token.surface)

    def _build_patch_event(self, window_end_s: float) -> PatchEvent:
        tail_tokens = [
            renderable
            for renderable in (
                self._renderable_from_mutable(token) for token in self.mutable_tail
            )
            if renderable is not None
        ]
        committed_text = self._join_text(self.committed_tokens)
        tail_text = self._join_text(tail_tokens)
        if not tail_text:
            replacement_text = ""
        elif committed_text:
            replacement_text = f" {tail_text}"
        else:
            replacement_text = tail_text
        display_text = committed_text + replacement_text
        replace_from_char = self._last_committed_char_count
        replacement_text = display_text[replace_from_char:]
        event = PatchEvent(
            window_end_s=window_end_s,
            replace_from_char=replace_from_char,
            replacement_text=replacement_text,
            display_text=display_text,
            committed_text=committed_text,
            tail_text=tail_text,
        )
        self._last_committed_char_count = len(committed_text)
        return event
