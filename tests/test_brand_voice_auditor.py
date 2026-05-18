
# K12+K13+K16 Trinity-CONTRARIAN 2026-05-17 (Cross-LLM-validated)
def k12_provenance(payload: bytes, key: bytes = b"df-trinity-contrarian-v1") -> dict:
    import hashlib, hmac
    return {
        "payload_hash": hashlib.sha256(payload).hexdigest(),
        "hmac_sha256": hmac.new(key, payload, hashlib.sha256).hexdigest(),
    }

def k13_anchor(payload_hash: str) -> dict:
    from datetime import datetime, timezone
    return {
        "anchor_type": "rfc3161-mock",
        "iso_ts": datetime.now(timezone.utc).isoformat(),
        "payload_hash": payload_hash,
    }

def k16_lock_or_exit(df_name: str):
    import fcntl, os, sys
    lock_path = f"/tmp/df-trinity-{df_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        sys.exit(3)

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brand_voice_auditor import (  # noqa: E402
    BrandVoiceAuditor,
    CircuitBreaker,
    RunMutex,
    TextSample,
)


GOOD_TEXT = (
    "Hey Lou Direct-Booking Self-Check-in 60 Sekunden Selbstbestimmt "
    "Heritage Authentisch Lokal-Anker #HeyLou #DirectBooking #HotelHildesheim"
)
BAD_TEXT = (
    "Buchen Sie jetzt Special Offer Booking.com Expedia HRS Luxus "
    "garantiert guenstig Schnaeppchen Nur heute Discount Last Minute Deal"
)


@pytest.fixture
def auditor(tmp_path: Path) -> BrandVoiceAuditor:
    return BrandVoiceAuditor(base_dir=tmp_path)


@pytest.fixture
def engine(tmp_path: Path) -> BrandVoiceAuditor:
    auditor = BrandVoiceAuditor(base_dir=tmp_path)
    auditor.lock_dir = tmp_path / "df-hlm-2.lock"
    return auditor


def sample(text: str = GOOD_TEXT, source: str = "instagram", hotel: str = "Hotel Hildesheim") -> TextSample:
    url_by_source = {
        "instagram": "https://instagram.com/heylou/p/1",
        "linkedin": "https://linkedin.com/company/heylou/posts/1",
        "blog": "https://hey-lou.com/blog/1",
        "email": "mailto:sent@mail.local",
    }
    return TextSample(source, text, hotel, url_by_source[source], "2026-05-14T06:00:00Z")


def test_default_mock_mode_no_api_call(monkeypatch: pytest.MonkeyPatch, auditor: BrandVoiceAuditor) -> None:
    monkeypatch.delenv("DF_HLM_2_REAL_INSTAGRAM_ENABLED", raising=False)
    called = False

    def fetcher() -> list[TextSample]:
        nonlocal called
        called = True
        return [sample()]

    auditor.fetchers["instagram"] = fetcher
    samples, failures = auditor.collect_samples(["instagram"])
    assert called is False
    assert failures == []
    assert {item.source for item in samples} == {"email", "blog"}


def test_pflicht_vokabular_detection(auditor: BrandVoiceAuditor) -> None:
    assert auditor.detect_required(GOOD_TEXT) == [
        "Hey Lou",
        "Direct-Booking",
        "Self-Check-in",
        "60 Sekunden",
        "Selbstbestimmt",
        "Heritage",
        "Authentisch",
        "Lokal-Anker",
    ]


def test_verbots_vokabular_detection(auditor: BrandVoiceAuditor) -> None:
    assert auditor.detect_forbidden(BAD_TEXT) == [
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


def test_hashtag_pflicht_check(auditor: BrandVoiceAuditor) -> None:
    found, missing = auditor.detect_hashtags(GOOD_TEXT, "Hotel Hildesheim")
    assert found == ["#HeyLou", "#DirectBooking", "#HotelHildesheim"]
    assert missing == []


def test_score_calculation_max_3_0(auditor: BrandVoiceAuditor) -> None:
    score = auditor.calculate_score(12, 0, 5)
    assert score.total_score == 3.0


def test_auto_patch_booking_com_to_direct(auditor: BrandVoiceAuditor) -> None:
    patched = auditor.auto_patch("Mehr Reichweite via Booking.com.")
    assert "Booking.com" not in patched
    assert "Direct-Booking auf hey-lou.com" in patched


def test_concurrent_spawn_protection(tmp_path: Path) -> None:
    first = RunMutex(tmp_path / "df-hlm-2.lock")
    second = RunMutex(tmp_path / "df-hlm-2.lock")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()


def test_cascade_containment(monkeypatch: pytest.MonkeyPatch, auditor: BrandVoiceAuditor) -> None:
    monkeypatch.setenv("DF_HLM_2_REAL_INSTAGRAM_ENABLED", "true")
    monkeypatch.setenv("DF_HLM_2_REAL_LINKEDIN_ENABLED", "true")
    monkeypatch.setenv("PHRONESIS_TICKET", "PT-2026-XX-XXX")
    auditor.fetchers["instagram"] = lambda: (_ for _ in ()).throw(RuntimeError("api down"))
    auditor.fetchers["linkedin"] = lambda: [sample(source="linkedin")]
    samples, failures = auditor.collect_samples(["instagram", "linkedin"])
    assert failures == ["instagram"]
    assert [item.source for item in samples] == ["linkedin"]


def test_external_anchor_validation(auditor: BrandVoiceAuditor) -> None:
    validation = auditor.external_anchor_validation([sample(source="instagram"), sample(source="linkedin")])
    assert validation["external_anchor_type"] == "social_media_api"
    assert validation["valid"] is True


def test_circuit_breaker_open(auditor: BrandVoiceAuditor) -> None:
    breaker = CircuitBreaker(timeout_s=30, open_threshold=3)
    for _ in range(3):
        breaker.record_failure("instagram")
    assert breaker.is_open("instagram") is True


def test_direct_mode_email_only(auditor: BrandVoiceAuditor) -> None:
    mode = auditor.determine_mode(["instagram", "linkedin"], [sample(source="email")])
    assert mode == "standalone_email_only"


def test_idempotent_operations(auditor: BrandVoiceAuditor) -> None:
    s1 = sample()
    s2 = sample()
    assert s1.idempotency_key == s2.idempotency_key


def test_health_check_no_deps(auditor: BrandVoiceAuditor) -> None:
    assert auditor.health_check()["dependencies"] == []


def test_provenance_in_output(auditor: BrandVoiceAuditor) -> None:
    result = auditor.audit_sample(sample())
    assert result.provenance["source_url"] == "https://instagram.com/heylou/p/1"
    assert result.provenance["timestamp"] == "2026-05-14T06:00:00Z"
    assert "score_components" in result.provenance


def test_pre_action_domain_check(auditor: BrandVoiceAuditor) -> None:
    good = sample()
    bad = TextSample("blog", GOOD_TEXT, "Hotel Hildesheim", "https://evil.example/post", "2026-05-14T06:00:00Z")
    assert auditor.pre_action_domain_check(good) is True
    assert auditor.pre_action_domain_check(bad) is False
    with pytest.raises(ValueError):
        auditor.audit_sample(bad)


def test_audit_log_appended_per_run(auditor: BrandVoiceAuditor) -> None:
    auditor.run([sample(source="email")])
    auditor.run([sample(source="email", hotel="Hotel Hildesheim")])
    lines = auditor.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_hashtag_dynamic_hotel_name(auditor: BrandVoiceAuditor) -> None:
    text = GOOD_TEXT.replace("#HotelHildesheim", "#HotelStuttgart")
    found, missing = auditor.detect_hashtags(text, "Hotel Stuttgart")
    assert "#HotelStuttgart" in found
    assert missing == []


def test_score_aggregation_multi_source(auditor: BrandVoiceAuditor) -> None:
    result = auditor.run([sample(source="instagram"), sample(source="linkedin", text=GOOD_TEXT + " Booking.com")])
    assert result["sample_count"] == 2
    assert result["average_score"] < 3.0
    aggregate_path = Path(result["aggregate_path"])
    data = json.loads(aggregate_path.read_text(encoding="utf-8"))
    assert data["results"][0]["source"] == "instagram"
    assert data["results"][1]["source"] == "linkedin"


def test_pii_scrubbed_in_output_with_kemmer_name(engine: BrandVoiceAuditor) -> None:
    result = engine.run([sample(text=f"{GOOD_TEXT} Martin und Imke empfehlen den Aufenthalt.", source="email")])
    report_content = Path(result["report_path"]).read_text(encoding="utf-8")
    aggregate_content = Path(result["aggregate_path"]).read_text(encoding="utf-8")
    assert "Martin" not in report_content
    assert "Imke" not in report_content
    assert "Martin" not in aggregate_content
    assert "Imke" not in aggregate_content


def test_k13_pre_action_verification_env_tag_block(
    engine: BrandVoiceAuditor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DF_ENV_TAG", "prod")
    monkeypatch.setenv("DF_HLM_2_REAL_INSTAGRAM_ENABLED", "true")
    monkeypatch.setenv("PHRONESIS_TICKET", "PT-2026-AB-123")
    engine.fetchers["instagram"] = lambda: [sample(source="instagram")]
    with pytest.raises(RuntimeError) as exc_info:
        engine.run(sources=["instagram"])
    assert "K13" in str(exc_info.value)


def test_mock_provenance_explicit_in_output(engine: BrandVoiceAuditor) -> None:
    result = engine.run()
    report_content = Path(result["report_path"]).read_text(encoding="utf-8")
    aggregate_data = json.loads(Path(result["aggregate_path"]).read_text(encoding="utf-8"))
    assert '"mode": "mock"' in report_content
    assert aggregate_data["output_provenance"]["mode"] == "mock"


def test_k16_mutex_blocks_concurrent_spawn(tmp_path: Path) -> None:
    first = BrandVoiceAuditor(base_dir=tmp_path)
    second = BrandVoiceAuditor(base_dir=tmp_path)
    shared_lock = tmp_path / "df-hlm-2.lock"
    first.lock_dir = shared_lock
    second.lock_dir = shared_lock

    started = threading.Event()
    release = threading.Event()
    outcomes: dict[str, object] = {}

    def slow_persist(aggregate: dict[str, object]) -> tuple[Path, Path]:
        started.set()
        release.wait(timeout=2)
        path = tmp_path / "branch-hub" / "reports" / "held.md"
        aggregate_path = tmp_path / "branch-hub" / "aggregates" / "held.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        aggregate_path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("held", encoding="utf-8")
        aggregate_path.write_text("{}", encoding="utf-8")
        return path, aggregate_path

    first.persist_outputs = slow_persist  # type: ignore[method-assign]

    def run_first() -> None:
        outcomes["first"] = first.run([sample(source="email")])

    thread = threading.Thread(target=run_first)
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)

    with pytest.raises(RuntimeError) as exc_info:
        second.run([sample(source="email")])
    outcomes["second_error"] = str(exc_info.value)

    release.set()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert isinstance(outcomes["first"], dict)
    assert "K16-VETO" in str(outcomes["second_error"])
