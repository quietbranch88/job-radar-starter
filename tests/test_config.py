from pathlib import Path

import pytest

from job_radar.config import ScoreWeights, load_user_config


def test_load_user_config_keeps_developer_preferences_separate(tmp_path: Path):
    config = tmp_path / "profile.yaml"
    config.write_text(
        """
profile:
  resume_path: user-data/resume.md
  skills: [Go, Python]
preferences:
  countries: [CA, SG]
  roles: [backend, platform]
  tracks: [ai-product, backend]
  visa_required: true
companies:
  preferred: [Shopify, Cloudflare]
  excluded: [Example Outsourcing]
matching:
  minimum_score: 70
  must_have: [backend]
  exclude_keywords: [unpaid, commission-only]
""".strip(),
        encoding="utf-8",
    )

    loaded = load_user_config(config)

    assert loaded.profile.resume_path == "user-data/resume.md"
    assert loaded.profile.skills == ("Go", "Python")
    assert loaded.preferences.countries == ("CA", "SG")
    assert loaded.preferences.roles == ("backend", "platform")
    assert loaded.preferences.tracks == ("ai-product", "backend")
    assert loaded.preferences.visa_required is True
    assert loaded.companies.preferred == ("Shopify", "Cloudflare")
    assert loaded.companies.excluded == ("Example Outsourcing",)
    assert loaded.matching.minimum_score == 70
    assert loaded.matching.must_have == ("backend",)
    assert loaded.matching.exclude_keywords == ("unpaid", "commission-only")
    assert loaded.matching.weights == ScoreWeights()


def test_matching_weights_override_defaults_and_reject_unknown_values(tmp_path: Path):
    config = tmp_path / "profile.yaml"
    config.write_text(
        "matching:\n  weights: {skill: 3, skill_cap: 9}\n",
        encoding="utf-8",
    )

    loaded = load_user_config(config)

    assert loaded.matching.weights.skill == 3
    assert loaded.matching.weights.skill_cap == 9
    assert loaded.matching.weights.base == 30
    config.write_text("matching:\n  weights: {skill: true}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="matching.weights.skill"):
        load_user_config(config)
    config.write_text("matching:\n  weights: {favorite: 4}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported fields"):
        load_user_config(config)


def test_matching_minimum_score_must_be_a_percentage(tmp_path: Path):
    config = tmp_path / "profile.yaml"
    config.write_text("matching:\n  minimum_score: 120\n", encoding="utf-8")

    with pytest.raises(ValueError, match="between 0 and 100"):
        load_user_config(config)
