from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx

from .base import MusicResult


SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")

SPEAKER_LINE_RE = re.compile(
    r"^(?P<speaker>[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’ -]{0,30}):"
    r"\s*[\"“](?P<text>.+?)[\"”]\s*$"
)
INLINE_DIRECTION_RE = re.compile(r"\{[^{}]*\}")
WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
GROUP_SPEAKERS = frozenset(
    {"all", "bambini", "children", "chorus", "coro", "tutti"}
)
ITALIAN_DIRECTION_MARKERS = (
    "andamento",
    "bambini",
    "basso",
    "cori",
    "evitare",
    "femminile",
    "filastrocca",
    "introduzione",
    "maschile",
    "percussioni",
    "ritornello",
    "strofe",
    "voce",
)
DIRECTION_STYLE_MAPPINGS = (
    (("electro-pop", "electropop"), "electro-pop"),
    (("dance-pop", "dance pop"), "dance-pop"),
    (
        ("mediterranean", "mediterranea", "mediterraneo", "aegean", "egeo"),
        "Mediterranean and Aegean summer-pop character",
    ),
    (("summer", "estiva", "estivo"), "bright summer atmosphere"),
    (("energetic", "energica", "energico"), "high-energy performance"),
    (
        ("beat immediato", "dal primo secondo", "first second", "immediate beat"),
        "rhythm starts in the first second",
    ),
    (("basso elastico", "elastic bass"), "elastic modern bass groove"),
    (("clean dance kick", "kick", "cassa"), "clean dance kick"),
    (("handclap", "clap"), "strong handclaps"),
    (
        ("mediterranean percussion", "percussioni mediterranee"),
        "light Mediterranean percussion",
    ),
    (("corde pizzicate", "plucked strings"), "original Aegean-style plucked strings"),
    (("synth", "sintetizz"), "bright synthesizers"),
    (("brass", "ottoni"), "small summer brass accents"),
    (
        ("adult female lead", "voce femminile adulta", "voce principale femminile adulta"),
        "bright adult female lead vocalist",
    ),
    (("male character voice", "voce maschile"), "warm male character voice for brief responses"),
    (("vocoder",), "short vocoder character responses"),
    (
        ("children's voices", "children voices", "cori infantili"),
        "children's voices used only for brief call-and-response",
    ),
    (
        ("almost-rapped verses", "quasi rappate", "strofe quasi rappate"),
        "rhythmic lightly syncopated almost-rapped verses",
    ),
    (("chorus", "ritornello"), "wide melodic and memorable chorus"),
    (("explosive", "esplosivo"), "explosive chorus lift"),
    (("musical stop", "stop musicale"), "brief musical stop before the final chorus"),
    (
        ("higher key", "key change", "trasposto più in alto", "finale trasposto"),
        "final chorus lifted to a higher key",
    ),
)
DIRECTION_NEGATIVE_MAPPINGS = (
    (("filastrocca", "nursery-rhyme"), "traditional nursery-rhyme melody"),
    (("introduzioni lente", "introduzione lenta", "slow introduction"), "slow introduction"),
    (
        ("coro di bambini continuo", "continuous children's choir"),
        "continuous children's choir",
    ),
    (("ukulele generico", "generic ukulele"), "generic ukulele nursery arrangement"),
    (("andamento dolce", "overly gentle"), "overly gentle pacing"),
)


def _music_v2_lyric_line(line: str) -> str:
    """Convert screenplay speaker labels without touching literal lyric lines."""

    stripped = line.strip()
    match = SPEAKER_LINE_RE.match(stripped)
    if not match:
        return stripped
    speaker = match.group("speaker").strip().casefold()
    text = match.group("text").strip()
    cue = "{group response}" if speaker in GROUP_SPEAKERS else "{brief character response}"
    return f"{cue} {text}"


def _looks_like_italian_direction(direction: str) -> bool:
    lowered = direction.casefold()
    return sum(marker in lowered for marker in ITALIAN_DIRECTION_MARKERS) >= 2


def _dedupe_styles(styles: list[str]) -> list[str]:
    return list(dict.fromkeys(style for style in styles if style))


def _provider_direction_styles(direction: str) -> list[str]:
    """Keep provider styles in English while retaining common Italian briefs."""

    normalized = " ".join(direction.split())
    if not normalized:
        return []
    if not _looks_like_italian_direction(normalized):
        return [f"Authoritative musical and vocal direction: {normalized}"]
    lowered = normalized.casefold()
    styles = ["custom musical and vocal direction normalized into English style tags"]
    for markers, style in DIRECTION_STYLE_MAPPINGS:
        if any(marker in lowered for marker in markers):
            styles.append(style)
    if len(styles) == 1:
        styles.extend(
            [
                "preserve the requested genre and energy",
                "preserve the requested lead and character-response casting",
            ]
        )
    return _dedupe_styles(styles)


def _provider_direction_negative_styles(direction: str) -> list[str]:
    lowered = direction.casefold()
    styles: list[str] = []
    for markers, style in DIRECTION_NEGATIVE_MAPPINGS:
        if any(marker in lowered for marker in markers):
            styles.append(style)
    return _dedupe_styles(styles)


def _parse_lyric_sections(lyrics: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    name = "Song"
    lines: list[str] = []
    for raw_line in lyrics.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = SECTION_RE.match(line)
        if heading:
            if lines:
                sections.append((name, lines))
            name = heading.group(1).strip()
            lines = []
            continue
        lines.append(line)
    if lines:
        sections.append((name, lines))
    if not sections:
        raise ValueError("Lyrics must contain at least one sung line")
    return sections


def build_music_v2_composition_plan(
    *,
    lyrics: str,
    duration_seconds: int,
    bpm: int,
    production_prompt: str = "",
    music_direction: str = "",
) -> dict:
    """Bind approved lyrics and exact section timing for Music v2."""

    sections = _parse_lyric_sections(lyrics)
    total_ms = duration_seconds * 1000
    if len(sections) > 30:
        raise ValueError("Music v2 supports at most 30 composition chunks")
    minimum_chunk_ms = 3000
    maximum_chunk_ms = 120000
    if len(sections) * minimum_chunk_ms > total_ms:
        raise ValueError(
            "The lyric has too many sections for the requested song duration"
        )
    # Give every section a minimum phrase while distributing the remainder by
    # sung-line count. The final correction makes the total exact.
    remaining_ms = total_ms - minimum_chunk_ms * len(sections)
    total_lines = sum(len(lines) for _name, lines in sections)
    durations: list[int] = []
    assigned = 0
    for index, (_name, lines) in enumerate(sections):
        if index == len(sections) - 1:
            duration_ms = total_ms - assigned
        else:
            share = remaining_ms * len(lines) // total_lines
            duration_ms = minimum_chunk_ms + share
        if duration_ms > maximum_chunk_ms:
            raise ValueError(
                "A Music v2 composition chunk cannot exceed 120 seconds"
            )
        durations.append(duration_ms)
        assigned += duration_ms

    # ElevenLabs documents the first chunk as the strongest influence on the
    # whole track. Repeat an explicit *band* arrangement in every chunk so the
    # lyrics constraint cannot collapse the result into spoken or a-cappella
    # vocals.
    direction = music_direction.strip()
    direction_styles = _provider_direction_styles(direction)
    direction_negative_styles = _provider_direction_negative_styles(direction)
    if direction:
        # Music v2 has no free-form top-level prompt when a composition plan is
        # supplied. Provider style fields must stay in English; the original
        # direction remains in the immutable receipt for editorial traceability.
        global_styles = [
            f"{bpm} BPM",
            production_prompt.strip(),
            *direction_styles,
            "follow the normalized musical and vocal direction exactly",
            "full instrumental backing under every sung line",
            "polished full-frequency mix with clearly audible rhythm and bass",
            "keep every approved lyric highly intelligible",
        ]
    else:
        # Compatibility fallback for episodes created before the dedicated
        # music-direction field existed.
        global_styles = [
            f"{bpm} BPM",
            "original modern preschool pop",
            "bright major key",
            "full instrumental backing under every sung line",
            "steady acoustic drum groove with audible kick and snare throughout",
            "audible warm bass groove throughout",
            "bright ukulele chord strumming throughout",
            "glockenspiel and toy piano melodic hook",
            "polished wide stereo mix with the lead vocal balanced over the band",
        ]
    global_styles.extend(
        [
            "the designated lead singer performs every supplied lyric line",
            "clearly audible sung lead vocal begins within the first two seconds",
            "lead vocal remains present in every lyric-bearing section",
            "clear pronunciation in the language of the supplied lyrics",
            "lyrics are louder and more prominent than the instrumental backing",
            "no supplied lyric line is omitted, rewritten or replaced",
        ]
    )
    global_styles = _dedupe_styles(global_styles)
    global_negative_styles = [
        "dark",
        "aggressive",
        "frightening",
        "rapid tempo changes",
        "imitation of an existing song or artist",
        "a cappella",
        "voice only",
        "unaccompanied choir",
        "sparse backing",
        "ambient drone",
        "long unaccompanied vocal passages",
        "sound effects without music",
        "instrumental-only track",
        "karaoke backing track",
        "no vocals",
        "wordless melody",
        "omitted lyrics",
        "humming instead of lyrics",
    ]
    global_negative_styles.extend(direction_negative_styles)
    global_negative_styles = _dedupe_styles(global_negative_styles)
    if direction:
        global_negative_styles.append(
            "entire song as spoken word or recitation"
        )
    else:
        global_negative_styles.extend(["spoken word", "recitation"])
    chunks = []
    for index, ((name, lines), duration_ms) in enumerate(
        zip(sections, durations, strict=True)
    ):
        if direction:
            positive_styles = global_styles + [
                "vocal identities and delivery follow the supplied direction",
                "highly intelligible approved lyrics",
            ]
        else:
            positive_styles = global_styles + [
                "clear warm Italian lead vocal",
                "highly intelligible Italian lyrics",
                "simple melody for very young children",
            ]
        normalized_name = name.casefold()
        if index == 0 or "intro" in normalized_name:
            if direction:
                positive_styles.append(
                    "opening follows the supplied direction with no generic slow nursery intro"
                )
            else:
                positive_styles.extend(
                    [
                        "instrumental hook starts in the first second",
                        "rhythm section enters immediately and never drops out",
                    ]
                )
        if "ritornello" in normalized_name or "chorus" in normalized_name:
            if direction:
                positive_styles.extend(
                    [
                        "memorable chorus hook",
                        "full-arrangement chorus lift",
                        "stronger rhythm and bass",
                        "chorus vocals and backing responses follow the supplied direction exactly",
                    ]
                )
            else:
                positive_styles.extend(
                    [
                        "memorable singalong hook",
                        "full-band chorus lift",
                        "stronger kick and bass",
                        "gentle hand claps",
                        "light child backing vocals behind the lead",
                    ]
                )
        elif "strofa" in normalized_name or "verse" in normalized_name:
            positive_styles.append(
                (
                    "verse arrangement and vocal delivery follow the supplied direction exactly"
                    if direction
                    else "steady ukulele, kick, bass and glockenspiel accompaniment"
                )
            )
        if "final" in normalized_name or "outro" in normalized_name:
            positive_styles.extend(
                [
                    "largest full-band arrangement",
                    "bright celebratory musical ending",
                ]
            )
        provider_lines = [_music_v2_lyric_line(line) for line in lines]
        chunks.append(
            {
                "text": f"[{name}]\n" + "\n".join(provider_lines),
                "duration_ms": duration_ms,
                "positive_styles": positive_styles,
                "negative_styles": global_negative_styles
                + [
                    "spoken narration",
                    "additional or improvised lyrics",
                    "dense vocal runs",
                ],
                "context_adherence": "high",
            }
        )
    return {"chunks": chunks}


def _elevenlabs_error_detail(response: httpx.Response) -> str:
    """Expose actionable provider diagnostics without request headers or secrets."""

    try:
        body = response.json()
        detail = json.dumps(body, ensure_ascii=False, sort_keys=True)
    except (ValueError, json.JSONDecodeError):
        detail = response.text.strip()
    return (detail or "no response body")[:2000]



def _parse_detailed_music_response(
    response: httpx.Response,
) -> tuple[bytes, dict[str, Any]]:
    """Extract JSON metadata and audio from ElevenLabs multipart/mixed."""

    content_type = response.headers.get("content-type", "")
    if not content_type.casefold().startswith("multipart/mixed"):
        raise RuntimeError(
            "ElevenLabs detailed music response was not multipart/mixed: "
            f"content-type={content_type!r}, bytes={len(response.content)}"
        )
    raw_message = (
        b"Content-Type: "
        + content_type.encode("latin-1")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + response.content
    )
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if not message.is_multipart():
        raise RuntimeError("ElevenLabs detailed music response has no MIME parts")

    metadata: dict[str, Any] = {}
    audio_candidates: list[bytes] = []
    for part in message.iter_parts():
        part_type = part.get_content_type().casefold()
        payload = part.get_payload(decode=True) or b""
        filename = (part.get_filename() or "").casefold()
        if part_type == "application/json" or part_type.endswith("+json"):
            try:
                parsed = json.loads(payload.decode(part.get_content_charset() or "utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "ElevenLabs returned invalid JSON music metadata"
                ) from exc
            if isinstance(parsed, dict):
                metadata.update(parsed)
            continue
        is_audio = part_type.startswith("audio/") or filename.endswith(
            (".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav")
        )
        if is_audio and payload:
            audio_candidates.append(payload)
            continue
        if payload.lstrip().startswith(b"{"):
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                metadata.update(parsed)

    audio = max(audio_candidates, key=len, default=b"")
    if len(audio) < 1024:
        raise RuntimeError(
            "ElevenLabs returned a detailed response without a usable audio part: "
            f"parts={len(list(message.iter_parts()))}, audio_bytes={len(audio)}"
        )
    return audio, metadata


def _find_word_timestamp_payload(value: Any) -> Any | None:
    if isinstance(value, dict):
        for key in ("words_timestamps", "word_timestamps"):
            if key in value:
                return value[key]
        for nested in value.values():
            found = _find_word_timestamp_payload(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_word_timestamp_payload(nested)
            if found is not None:
                return found
    return None


def _extract_word_timestamps(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _find_word_timestamp_payload(metadata)
    if isinstance(payload, dict):
        for key in ("words", "items", "timestamps"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
    if not isinstance(payload, list):
        return []
    timestamps: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        word = item.get("word") or item.get("text")
        if isinstance(word, str) and word.strip():
            timestamps.append(dict(item))
    return timestamps


def _normalized_words(text: str) -> list[str]:
    without_cues = INLINE_DIRECTION_RE.sub(" ", text)
    decomposed = unicodedata.normalize("NFKD", without_cues.casefold())
    ascii_like = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return [word.replace("’", "'") for word in WORD_RE.findall(ascii_like)]


def _approved_lyric_words(lyrics: str) -> list[str]:
    return [
        word
        for _name, lines in _parse_lyric_sections(lyrics)
        for line in lines
        for word in _normalized_words(_music_v2_lyric_line(line))
    ]


def _music_vocal_quality(
    lyrics: str,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    timestamps = _extract_word_timestamps(metadata)
    expected_words = _approved_lyric_words(lyrics)
    detected_words = [
        word
        for item in timestamps
        for word in _normalized_words(str(item.get("word") or item.get("text") or ""))
    ]
    overlap = Counter(expected_words) & Counter(detected_words)
    matched_count = sum(overlap.values())
    expected_count = len(expected_words)
    detected_count = len(detected_words)
    coverage = matched_count / expected_count if expected_count else 0.0
    minimum_coverage = 0.30
    minimum_matches = min(expected_count, 3)

    if not expected_words:
        passed = False
        reason = "no_approved_lyric_words"
    elif not detected_words:
        passed = False
        reason = "no_sung_words_detected"
    elif matched_count < minimum_matches or coverage < minimum_coverage:
        passed = False
        reason = "approved_lyrics_not_detected"
    else:
        passed = True
        reason = "sung_lyrics_detected"

    return (
        {
            "passed": passed,
            "reason": reason,
            "expected_word_count": expected_count,
            "detected_word_count": detected_count,
            "matched_word_count": matched_count,
            "coverage_ratio": round(coverage, 4),
            "minimum_coverage_ratio": minimum_coverage,
            "timestamp_count": len(timestamps),
        },
        timestamps,
    )


def music_request_fingerprint(
    *,
    lyrics: str,
    prompt: str,
    music_direction: str = "",
    duration_seconds: int,
    bpm: int,
    variant: int,
    model_id: str,
    output_format: str,
) -> str:
    digest = hashlib.sha256()
    values = [lyrics, prompt]
    # Preserve fingerprints created before this field existed when an older
    # episode has no direction. A real direction still becomes part of the
    # idempotency key and cannot be reconciled with a different brief.
    if music_direction:
        values.append(music_direction)
    values.extend(
        [
            str(duration_seconds),
            str(bpm),
            str(variant),
            model_id,
            output_format,
        ]
    )
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def music_receipt_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.name}.receipt.json")


class ElevenLabsMusicProvider:
    def __init__(self, *, api_key: str, model_id: str, output_format: str):
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required in live mode")
        self.api_key = api_key
        self.model_id = model_id
        self.output_format = output_format

    def generate(
        self,
        *,
        lyrics: str,
        prompt: str,
        music_direction: str = "",
        duration_seconds: int,
        bpm: int,
        output_path: Path,
        variant: int,
    ) -> MusicResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.model_id == "music_v2":
            payload = {
                "composition_plan": build_music_v2_composition_plan(
                    lyrics=lyrics,
                    duration_seconds=duration_seconds,
                    bpm=bpm,
                    production_prompt=prompt,
                    music_direction=music_direction,
                ),
                "model_id": self.model_id,
                "sign_with_c2pa": True,
                "with_timestamps": True,
            }
        else:
            direction_block = (
                "\nAuthoritative musical and vocal direction, separate from "
                f"the lyrics:\n{music_direction.strip()}\n"
                if music_direction.strip()
                else "\n"
            )
            provider_constraints = (
                "Original preschool song with a full instrumental arrangement. "
                "The supplied musical and vocal direction is authoritative. "
                "Keep the approved lyrics highly intelligible and do not "
                "imitate existing artists or songs."
                if music_direction.strip()
                else (
                    "Original nursery song, gentle dynamics, clean lead vocal, "
                    "highly intelligible lyrics, no imitation of existing "
                    "artists or songs."
                )
            )
            full_prompt = (
                f"{prompt}{direction_block}Tempo: {bpm} BPM. Exact target "
                f"duration: {duration_seconds} seconds. "
                f"{provider_constraints} Lyrics:\n{lyrics}"
            )
            payload = {
                "prompt": full_prompt,
                "music_length_ms": duration_seconds * 1000,
                "model_id": self.model_id,
            }
        fingerprint = music_request_fingerprint(
            lyrics=lyrics,
            prompt=prompt,
            music_direction=music_direction,
            duration_seconds=duration_seconds,
            bpm=bpm,
            variant=variant,
            model_id=self.model_id,
            output_format=self.output_format,
        )
        receipt_path = music_receipt_path(output_path)
        if receipt_path.exists():
            try:
                prior_state = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Cannot safely reconcile ElevenLabs submission: {receipt_path}"
                ) from exc
            if prior_state.get("request_fingerprint") != fingerprint:
                raise RuntimeError(
                    f"ElevenLabs receipt belongs to another request: {receipt_path}"
                )
            if prior_state.get("state") != "complete":
                raise RuntimeError(
                    "A previous ElevenLabs submission has an ambiguous outcome; "
                    f"refusing to buy a duplicate song: {receipt_path}"
                )
            raise RuntimeError(
                f"Completed ElevenLabs receipt exists without a reusable ledger asset: {receipt_path}"
            )
        receipt_path.write_text(
            json.dumps(
                {
                    "state": "submitting",
                    "request_fingerprint": fingerprint,
                    "model": self.model_id,
                    "output_format": self.output_format,
                    "duration_seconds": duration_seconds,
                    "music_direction": music_direction,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        endpoint = (
            "https://api.elevenlabs.io/v1/music/detailed"
            if self.model_id == "music_v2"
            else "https://api.elevenlabs.io/v1/music"
        )
        accept = "multipart/mixed" if self.model_id == "music_v2" else "audio/mpeg"
        response: httpx.Response | None = None
        for attempt in range(3):
            response = httpx.post(
                endpoint,
                headers={"xi-api-key": self.api_key, "Accept": accept, "Content-Type": "application/json"},
                params={"output_format": self.output_format},
                json=payload,
                timeout=600,
            )
            # Only retry an explicit rate-limit rejection. Retrying an
            # ambiguous 5xx could buy the same song twice.
            if response.status_code != 429:
                break
            if attempt < 2:
                retry_after = response.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt)
        assert response is not None
        if 400 <= response.status_code < 500:
            receipt_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"ElevenLabs rejected the music request ({response.status_code}): "
                f"{_elevenlabs_error_detail(response)}"
            )
        response.raise_for_status()
        provider_metadata: dict[str, Any] = {}
        vocal_qc: dict[str, Any] | None = None
        words_timestamps: list[dict[str, Any]] = []
        if self.model_id == "music_v2":
            audio_bytes, provider_metadata = _parse_detailed_music_response(response)
            vocal_qc, words_timestamps = _music_vocal_quality(
                lyrics, provider_metadata
            )
        else:
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("audio/") or len(response.content) < 1024:
                raise RuntimeError(
                    f"ElevenLabs returned an invalid music payload: content-type={content_type!r}, "
                    f"bytes={len(response.content)}"
                )
            audio_bytes = response.content
        output_path.write_bytes(audio_bytes)
        # The exact account charge depends on the active ElevenLabs plan; store a transparent estimate only.
        estimated_cost = duration_seconds / 60 * 0.15
        song_id = response.headers.get("song-id") or response.headers.get("x-song-id")
        receipt_path.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "request_fingerprint": fingerprint,
                    "song_id": song_id,
                    "model": self.model_id,
                    "output_format": self.output_format,
                    "duration_seconds": duration_seconds,
                    "music_direction": music_direction,
                    "estimated_cost_usd": round(estimated_cost, 4),
                    "bytes": len(audio_bytes),
                    "vocal_qc": vocal_qc,
                    "words_timestamps": words_timestamps,
                    "song_metadata": provider_metadata.get("song_metadata"),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return MusicResult(
            path=output_path,
            provider="elevenlabs-music",
            variant=variant,
            duration_seconds=float(duration_seconds),
            cost_usd=round(estimated_cost, 4),
            metadata={
                "model": self.model_id,
                "output_format": self.output_format,
                "song_id": song_id,
                "request_fingerprint": fingerprint,
                "music_direction": music_direction,
                "vocal_qc": vocal_qc,
                "words_timestamps": words_timestamps,
                "song_metadata": provider_metadata.get("song_metadata"),
            },
        )
