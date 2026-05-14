from __future__ import annotations

import json
import os
import sys
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
