"""DF-HLM-2 Brand-Voice-Auditor fuer HeyLou-Marketing-Wave-2.

Phase 1 ist mock-first und deterministisch: Regex-Detection statt LLM, lokale
Sample-Texte statt API-Calls. Live-Quellen sind ueber ENV-Gates plus
PHRONESIS_TICKET bewusst default-disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

COMMON_ROOT = str(Path(__file__).resolve().parents[2])
if COMMON_ROOT not in sys.path:
    sys.path.insert(0, COMMON_ROOT)

from _df_common.pii_scrubber import PIIScrubber, scrub_audit_payload
from _df_common.welle_b2_patches import (
    K13PreActionVerifier,
    K16MutexGuard,
    MOCK_PREFIX,
    make_mock_url,
    make_provenance_envelope,
)

try:  # pragma: no cover - fallback only when structlog is unavailable.
    import structlog
except Exception:  # pragma: no cover
    import logging

    class _KeywordLogger:
        def __init__(self) -> None:
            self._logger = logging.getLogger("df_hlm_2_brand_voice")

        def warning(self, event: str, **kwargs: Any) -> None:
            self._logger.warning("%s %s", event, kwargs)

        def info(self, event: str, **kwargs: Any) -> None:
            self._logger.info("%s %s", event, kwargs)

    class _StructlogFallback:
        @staticmethod
        def get_logger() -> _KeywordLogger:
            logging.basicConfig(level=logging.INFO)
            return _KeywordLogger()

    structlog = _StructlogFallback()  # type: ignore[assignment]


LOGGER = structlog.get_logger()
ENGINE_MARKER = str(Path(__file__).resolve())

REQUIRED_VOCABULARY = [
    "Hey Lou",
    "Direct-Booking",
    "Self-Check-in",
    "60 Sekunden",
    "Selbstbestimmt",
    "Heritage",
    "Authentisch",
    "Lokal-Anker",
]
FORBIDDEN_VOCABULARY = [
    "Buchen Sie jetzt",
    "Special Offer",
    "Booking.com",
    "Expedia",
    "HRS",
    "Luxus",
    "garantiert guenstig",
    "Schnaeppchen",
    "Nur heute",
    "Discount",
    "Last Minute Deal",
]
PATCH_REPLACEMENTS = {
    "Buchen Sie jetzt": "Entdecke deinen Aufenthalt",
    "Special Offer": "Direct-Booking Vorteil",
    "Booking.com": "Direct-Booking auf hey-lou.com",
    "Expedia": "Direct-Booking auf hey-lou.com",
    "HRS": "Direct-Booking auf hey-lou.com",
    "Luxus": "charaktervoller Komfort",
    "garantiert guenstig": "transparent direkt",
    "Schnaeppchen": "fairer Direktvorteil",
    "Nur heute": "direkt verfuegbar",
    "Discount": "Direktvorteil",
    "Last Minute Deal": "spontaner Direktaufenthalt",
}
ALLOWED_DOMAINS = {"instagram.com", "linkedin.com", "hey-lou.com", "mail.local", "local.test"}
SOCIAL_SOURCES = {"instagram", "linkedin"}


def utc_now() -> str:
    """Return UTC timestamp in stable ISO format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, content: str) -> None:
    """Atomic UTF-8 write via temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON line; create parent directories on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile phrase regex with conservative non-word boundaries."""
    escaped = re.escape(phrase)
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def normalize_hashtag(value: str) -> str:
    """Normalize a hotel name into a simple hashtag token."""
    clean = re.sub(r"[^A-Za-z0-9]+", "", value)
    return f"#{clean}"


@dataclass(frozen=True)
class TextSample:
    """Input sample from a marketing source."""

    source: str
    text: str
    hotel_name: str
    source_url: str
    timestamp: str

    @property
    def idempotency_key(self) -> str:
        raw = f"{self.text}|{self.timestamp}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ScoreComponents:
    """Deterministic score components per DF-HLM-2 formula."""

    required_count: int
    forbidden_count: int
    hashtag_count: int
    required_score: float
    forbidden_score: float
    hashtag_score: float
    total_score: float


@dataclass(frozen=True)
class AuditResult:
    """Per-sample audit result with K12 provenance."""

    source: str
    source_url: str
    timestamp: str
    hotel_name: str
    idempotency_key: str
    found_required: list[str]
    found_forbidden: list[str]
    found_hashtags: list[str]
    missing_hashtags: list[str]
    score_components: ScoreComponents
    patched_text: str
    provenance: dict[str, Any]


@dataclass
class CircuitBreaker:
    """Small per-source circuit breaker for LC3."""

    timeout_s: int = 30
    open_threshold: int = 3
    failures: dict[str, int] = field(default_factory=dict)
    opened_at: dict[str, float] = field(default_factory=dict)

    def is_open(self, source: str) -> bool:
        opened = self.opened_at.get(source)
        if opened is None:
            return False
        return (time.time() - opened) < self.timeout_s

    def record_success(self, source: str) -> None:
        self.failures[source] = 0
        self.opened_at.pop(source, None)

    def record_failure(self, source: str) -> None:
        count = self.failures.get(source, 0) + 1
        self.failures[source] = count
        if count >= self.open_threshold:
            self.opened_at[source] = time.time()


class RunMutex:
    """Backward-compatible wrapper around K16MutexGuard."""

    def __init__(self, lock_dir: Path, stale_age_s: int = 21_600):
        self.lock_dir = Path(lock_dir)
        self.stale_age_s = stale_age_s
        self._guard = K16MutexGuard(
            lock_dir=self.lock_dir,
            df_engine_marker=ENGINE_MARKER,
            stale_age_hours=stale_age_s / 3600,
        )
        self.acquired = False

    def acquire(self) -> bool:
        result = self._guard.acquire()
        self.acquired = result.acquired
        return result.acquired

    def release(self) -> None:
        self._guard.release()
        self.acquired = False


class BrandVoiceAuditor:
    """Main engine for deterministic HeyLou brand-voice audits."""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        fetchers: dict[str, Callable[[], list[TextSample]]] | None = None,
    ) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.reports_dir = self.base_dir / "branch-hub" / "reports"
        self.aggregates_dir = self.base_dir / "branch-hub" / "aggregates"
        self.audit_log_path = self.base_dir / "branch-hub" / "audit" / "df-hlm-2-brand-voice.jsonl"
        self.dlq_dir = self.base_dir / "branch-hub" / "dlq"
        self.lock_dir = Path(os.environ.get("DF_HLM_2_LOCK_DIR", "/tmp/df-hlm-2.lock"))
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.fetchers = fetchers or {}
        self.pii_scrubber = PIIScrubber(enabled=True, kemmer_names_enabled=True)

    def real_api_requested(self, source: str) -> bool:
        env_by_source = {
            "instagram": "DF_HLM_2_REAL_INSTAGRAM_ENABLED",
            "linkedin": "DF_HLM_2_REAL_LINKEDIN_ENABLED",
        }
        env_name = env_by_source.get(source)
        if env_name is None:
            return False
        return os.environ.get(env_name, "").lower() == "true"

    def verify_real_mode_dispatch(self) -> None:
        verifier = K13PreActionVerifier(
            expected_env_tag="dev",
            expected_mount_pattern="/Users/make",
            blast_radius_class="state-only",
        )
        result = verifier.verify()
        if not result.ok:
            raise RuntimeError(f"K13-VETO: {result.failed_check}")

    def live_api_enabled(self, source: str) -> bool:
        if not self.real_api_requested(source):
            return False
        ticket = os.environ.get("PHRONESIS_TICKET", "")
        return re.fullmatch(r"PT-2026-[A-Z0-9]{2}-[A-Z0-9]{3}", ticket) is not None

    def default_mock_samples(self) -> list[TextSample]:
        ts = utc_now()
        mock_blog_id = f"hildesheim-{hashlib.sha256(ts.encode('utf-8')).hexdigest()[:8]}"
        text = (
            "Hey Lou macht Direct-Booking mit Self-Check-in in 60 Sekunden. "
            "Selbstbestimmt, Heritage, Authentisch und Lokal-Anker. "
            "#HeyLou #DirectBooking #HotelHildesheim"
        )
        return [
            TextSample("email", text, "Hotel Hildesheim", f"mailto:{MOCK_PREFIX}sent@mail.local", ts),
            TextSample("blog", text, "Hotel Hildesheim", make_mock_url("https://hey-lou.com/blog", mock_blog_id), ts),
        ]

    def fetch_source(self, source: str) -> list[TextSample]:
        if self.circuit_breaker.is_open(source):
            raise RuntimeError(f"circuit breaker open for {source}")
        if source in {"instagram", "linkedin"} and self.real_api_requested(source):
            self.verify_real_mode_dispatch()
        if source in {"instagram", "linkedin"} and not self.live_api_enabled(source):
            return []
        fetcher = self.fetchers.get(source)
        if fetcher is None:
            return []
        return fetcher()

    def collect_samples(self, sources: Iterable[str] | None = None) -> tuple[list[TextSample], list[str]]:
        selected = list(sources or ["instagram", "linkedin", "blog", "email"])
        samples: list[TextSample] = []
        failures: list[str] = []
        for source in selected:
            try:
                fetched = self.fetch_source(source)
                samples.extend(fetched)
                self.circuit_breaker.record_success(source)
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc).startswith("K13-VETO"):
                    raise
                failures.append(source)
                self.circuit_breaker.record_failure(source)
                dlq_payload = self.pii_scrubber.scrub_dict_recursive(
                    {"source": source, "error": str(exc), "timestamp": utc_now()}
                )
                append_jsonl(self.dlq_dir / f"{source}.jsonl", dlq_payload)
                LOGGER.warning("source_fetch_failed", source=source, error=str(exc))
        if not samples:
            samples = self.default_mock_samples()
        return samples, failures

    def detect_required(self, text: str) -> list[str]:
        return [phrase for phrase in REQUIRED_VOCABULARY if phrase_pattern(phrase).search(text)]

    def detect_forbidden(self, text: str) -> list[str]:
        return [phrase for phrase in FORBIDDEN_VOCABULARY if phrase_pattern(phrase).search(text)]

    def required_hashtags(self, hotel_name: str) -> list[str]:
        return ["#HeyLou", "#DirectBooking", normalize_hashtag(hotel_name)]

    def detect_hashtags(self, text: str, hotel_name: str) -> tuple[list[str], list[str]]:
        required = self.required_hashtags(hotel_name)
        found = [tag for tag in required if re.search(rf"(?<!\w){re.escape(tag)}(?!\w)", text, re.IGNORECASE)]
        missing = [tag for tag in required if tag not in found]
        return found, missing

    def calculate_score(self, required_count: int, forbidden_count: int, hashtag_count: int) -> ScoreComponents:
        required_score = min(required_count, 8) / 8
        forbidden_score = max(0, 11 - min(forbidden_count, 11)) / 11
        hashtag_score = min(hashtag_count, 3) / 3
        total = min(3.0, required_score + forbidden_score + hashtag_score)
        return ScoreComponents(required_count, forbidden_count, hashtag_count, required_score, forbidden_score, hashtag_score, total)

    def auto_patch(self, text: str) -> str:
        patched = text
        for bad, replacement in PATCH_REPLACEMENTS.items():
            patched = phrase_pattern(bad).sub(replacement, patched)
        return patched

    def pre_action_domain_check(self, sample: TextSample) -> bool:
        parsed = urlparse(sample.source_url)
        host = parsed.hostname or ("mail.local" if parsed.scheme == "mailto" else "")
        return any(host == domain or host.endswith(f".{domain}") for domain in ALLOWED_DOMAINS)

    def audit_sample(self, sample: TextSample) -> AuditResult:
        if not self.pre_action_domain_check(sample):
            raise ValueError(f"source domain not allowed: {sample.source_url}")
        required = self.detect_required(sample.text)
        forbidden = self.detect_forbidden(sample.text)
        hashtags, missing = self.detect_hashtags(sample.text, sample.hotel_name)
        components = self.calculate_score(len(required), len(forbidden), len(hashtags))
        provenance = {
            "source_url": sample.source_url,
            "timestamp": sample.timestamp,
            "score_components": asdict(components),
            "detector": "regex-deterministic",
        }
        return AuditResult(
            source=sample.source,
            source_url=sample.source_url,
            timestamp=sample.timestamp,
            hotel_name=sample.hotel_name,
            idempotency_key=sample.idempotency_key,
            found_required=required,
            found_forbidden=forbidden,
            found_hashtags=hashtags,
            missing_hashtags=missing,
            score_components=components,
            patched_text=self.auto_patch(sample.text),
            provenance=provenance,
        )

    def external_anchor_validation(self, samples: list[TextSample]) -> dict[str, Any]:
        available = {sample.source for sample in samples if sample.source in SOCIAL_SOURCES}
        return {
            "external_anchor_type": "social_media_api",
            "required": sorted(SOCIAL_SOURCES),
            "available": sorted(available),
            "valid": SOCIAL_SOURCES.issubset(available),
        }

    def determine_mode(self, failures: list[str], samples: list[TextSample]) -> str:
        social_failures = [source for source in failures if source in SOCIAL_SOURCES]
        sample_sources = {sample.source for sample in samples}
        if sample_sources == {"email"}:
            return "standalone_email_only"
        if len(social_failures) >= 2:
            return "standalone_email_only"
        if "instagram" in failures:
            return "degraded_instagram_api"
        if "linkedin" in failures:
            return "degraded_linkedin_api"
        if "blog" in failures:
            return "degraded_blog_rss"
        return "full" if SOCIAL_SOURCES.issubset(sample_sources) else "standalone_email_only"

    def aggregate_results(self, results: list[AuditResult], mode: str, failures: list[str]) -> dict[str, Any]:
        scores = [result.score_components.total_score for result in results]
        return {
            "df_id": "DF-HLM-2",
            "generated_at": utc_now(),
            "mode": mode,
            "failures": failures,
            "sample_count": len(results),
            "average_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "max_score": max(scores) if scores else 0.0,
            "min_score": min(scores) if scores else 0.0,
            "results": [asdict(result) for result in results],
        }

    def is_mock_run(self, samples: list[TextSample]) -> bool:
        if not samples:
            return True
        for sample in samples:
            if sample.source_url.startswith(f"mailto:{MOCK_PREFIX}"):
                continue
            if f"/{MOCK_PREFIX}" in sample.source_url:
                continue
            return False
        return True

    def render_markdown_report(self, aggregate: dict[str, Any]) -> str:
        provenance = aggregate["output_provenance"]
        frontmatter = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
        lines = [
            "---",
            frontmatter,
            "---",
            "",
            "# DF-HLM-2 Daily Brand-Voice Audit",
            "",
            f"- Generated: {aggregate['generated_at']}",
            f"- Mode: {aggregate['mode']}",
            f"- Samples: {aggregate['sample_count']}",
            f"- Average score: {aggregate['average_score']} / 3.0",
            "",
            "## Samples",
        ]
        for item in aggregate["results"]:
            score = item["score_components"]["total_score"]
            lines.extend(
                [
                    "",
                    f"### {item['source']} - {item['hotel_name']}",
                    f"- Source URL: {item['source_url']}",
                    f"- Timestamp: {item['timestamp']}",
                    f"- Score: {score} / 3.0",
                    f"- Required vocabulary: {', '.join(item['found_required']) or 'none'}",
                    f"- Forbidden vocabulary: {', '.join(item['found_forbidden']) or 'none'}",
                    f"- Hashtags: {', '.join(item['found_hashtags']) or 'none'}",
                    f"- Missing hashtags: {', '.join(item['missing_hashtags']) or 'none'}",
                    "",
                    "#### Patched Text",
                    item["patched_text"],
                ]
            )
        return "\n".join(lines) + "\n"

    def persist_outputs(self, aggregate: dict[str, Any]) -> tuple[Path, Path]:
        run_hash = hashlib.sha256(json.dumps(aggregate["results"], sort_keys=True).encode("utf-8")).hexdigest()[:16]
        day = aggregate["generated_at"][:10]
        report_path = self.reports_dir / f"DF-HLM-2-audit-{day}-{run_hash}.md"
        json_path = self.aggregates_dir / f"DF-HLM-2-aggregate-{day}-{run_hash}.json"
        report_content = self.pii_scrubber.scrub(self.render_markdown_report(aggregate))
        aggregate_scrubbed = self.pii_scrubber.scrub_dict_recursive(aggregate)
        atomic_write(report_path, report_content)
        atomic_write(json_path, json.dumps(aggregate_scrubbed, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        event_name = "mock_run_complete" if aggregate["output_provenance"]["mode"] == "mock" else "run_complete"
        audit_entry = scrub_audit_payload(
            {
                "event": event_name,
                "report": str(report_path),
                "aggregate": str(json_path),
                "timestamp": utc_now(),
            }
        )
        append_jsonl(self.audit_log_path, audit_entry)
        return report_path, json_path

    def health_check(self) -> dict[str, Any]:
        return {"df_id": "DF-HLM-2", "status": "ok", "dependencies": [], "timestamp": utc_now()}

    def run(self, samples: list[TextSample] | None = None, sources: Iterable[str] | None = None) -> dict[str, Any]:
        with K16MutexGuard(lock_dir=self.lock_dir, df_engine_marker=ENGINE_MARKER):
            if samples is None:
                samples, failures = self.collect_samples(sources)
            else:
                failures = []
            results = [self.audit_sample(sample) for sample in samples]
            mode = self.determine_mode(failures, samples)
            aggregate = self.aggregate_results(results, mode, failures)
            aggregate["external_anchor_validation"] = self.external_anchor_validation(samples)
            aggregate["output_provenance"] = make_provenance_envelope(
                df_id="DF-HLM-2",
                timestamp_iso=aggregate["generated_at"],
                is_mock=self.is_mock_run(samples),
                activation_gate_id=os.environ.get("PHRONESIS_TICKET") or None,
                source_hash=hashlib.sha256(
                    json.dumps(aggregate["results"], ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            )
            report_path, json_path = self.persist_outputs(aggregate)
            aggregate["report_path"] = str(report_path)
            aggregate["aggregate_path"] = str(json_path)
            return aggregate


def main() -> int:
    parser = argparse.ArgumentParser(description="DF-HLM-2 Brand-Voice-Auditor")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--lock-dir", default=os.environ.get("DF_HLM_2_LOCK_DIR", "/tmp/df-hlm-2.lock"))
    args = parser.parse_args()
    previous_lock_dir = os.environ.get("DF_HLM_2_LOCK_DIR")
    os.environ["DF_HLM_2_LOCK_DIR"] = args.lock_dir
    try:
        auditor = BrandVoiceAuditor(Path(args.base_dir) if args.base_dir else None)
        result = auditor.run()
        print(json.dumps({"report_path": result["report_path"], "aggregate_path": result["aggregate_path"], "average_score": result["average_score"]}, ensure_ascii=False))
        return 0
    except RuntimeError as exc:
        if "K16-VETO" in str(exc):
            print(f"[K16-VETO] Concurrent DF-HLM-2 instance detected at {args.lock_dir}")
            return 3
        raise
    finally:
        if previous_lock_dir is None:
            os.environ.pop("DF_HLM_2_LOCK_DIR", None)
        else:
            os.environ["DF_HLM_2_LOCK_DIR"] = previous_lock_dir


if __name__ == "__main__":
    raise SystemExit(main())
