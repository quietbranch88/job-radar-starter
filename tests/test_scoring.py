import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from job_radar import application
from job_radar.adapters import Job
from job_radar.config import load_user_config
from job_radar.legacy_scoring import score_jobs_with_command


def _config(tmp_path: Path):
    resume = tmp_path / "resume.md"
    resume.write_text("PRIVATE_RESUME_CONTENT_SHOULD_NOT_LEAVE_PROCESS", encoding="utf-8")
    path = tmp_path / "profile.yaml"
    path.write_text(
        f"""
profile:
  resume_path: {resume.as_posix()}
  skills: [Go, Python]
preferences:
  countries: [CA]
  roles: [backend]
  tracks: [backend]
  visa_required: true
companies:
  preferred: [Northstar Robotics]
  excluded: [Blocked Consulting]
matching:
  minimum_score: 65
  must_have: [backend]
  exclude_keywords: [commission-only]
""".strip(),
        encoding="utf-8",
    )
    return load_user_config(path)


def _jobs():
    return [
        Job(
            "greenhouse",
            "strong",
            "Northstar Robotics",
            "Senior Go Backend Engineer",
            "Toronto, Canada",
            "https://example.com/strong",
            "2026-07-15",
            country="CA",
            category="backend",
            tracks=("backend",),
            skills=("Go",),
            visa_supported=True,
        ),
        Job(
            "lever",
            "weak",
            "Cedarline Systems",
            "Frontend Engineer",
            "London",
            "https://example.com/weak",
            "2026-07-15",
            country="GB",
            category="frontend",
        ),
        Job(
            "ashby",
            "blocked",
            "Blocked Consulting",
            "Backend Engineer",
            "Toronto",
            "https://example.com/blocked",
            "2026-07-15",
            country="CA",
            category="backend",
        ),
    ]


def test_short_skills_match_whole_tokens_and_local_weights(tmp_path: Path):
    config = _config(tmp_path)
    scored = application.score_jobs(
        [
            Job(
                "greenhouse", "inside", "Northstar Robotics", "Algorithm Engineer",
                "Toronto, Canada", "https://example.com/inside", "2026-07-15",
                country="CA", category="backend", tracks=("backend",), visa_supported=True,
            ),
            Job(
                "greenhouse", "cpp", "Northstar Robotics", "Senior C++ Engineer",
                "Toronto, Canada", "https://example.com/cpp", "2026-07-15",
                country="CA", visa_supported=True,
            ),
        ],
        replace(config, profile=replace(config.profile, skills=("Go", "C++", "C"))),
    )
    by_id = {job.external_id: job for job in scored}

    assert "skills:" not in by_id["inside"].summary
    assert "Golang" not in by_id["inside"].summary
    assert "skills: C++" in by_id["cpp"].summary
    assert "skills: C," not in by_id["cpp"].summary
    assert not by_id["cpp"].summary.endswith("C")

    weighted = replace(
        config.matching,
        weights=replace(config.matching.weights, skill=0, skill_cap=0),
    )
    rescored = application.score_jobs(_jobs(), replace(config, matching=weighted))
    strong = next(job for job in rescored if job.external_id == "strong")
    assert strong.score == 85
    assert "skills: Go" in strong.summary


def test_phrases_stay_in_one_field_and_short_skills_skip_the_description(tmp_path: Path):
    config = _config(tmp_path)
    config = replace(
        config,
        profile=replace(config.profile, skills=("Go", "C", "PostgreSQL")),
        preferences=replace(config.preferences, tracks=("ai-product", "backend")),
    )
    from job_radar.domain.jobs import MAX_JD_EVIDENCE_CHARS

    split = ("a " * 300)[: MAX_JD_EVIDENCE_CHARS - len(" post")] + " post"
    continued = "gresql in production"
    scored = application.score_jobs(
        [
            Job(
                "greenhouse", "gtm", "Northstar Robotics", "Go-to-market Lead",
                "Toronto, Canada", "https://example.com/gtm", "2026-07-15",
                country="CA", visa_supported=True,
                jd_evidence=("ready to go live",),
            ),
            Job(
                "greenhouse", "clevel", "Northstar Robotics", "C-level Advisor",
                "Toronto, Canada", "https://example.com/clevel", "2026-07-15",
                country="CA", visa_supported=True,
            ),
            Job(
                "greenhouse", "objc", "Northstar Robotics", "Objective-C Engineer",
                "Toronto, Canada", "https://example.com/objc", "2026-07-15",
                country="CA", visa_supported=True,
            ),
            Job(
                "greenhouse", "titled", "Northstar Robotics", "Senior Go Engineer",
                "Toronto, Canada", "https://example.com/titled", "2026-07-15",
                country="CA", category="backend", visa_supported=True,
            ),
            Job(
                "ashby", "split-track", "AI", "Product Designer",
                "Toronto, Canada", "https://example.com/split", "2026-07-15",
                country="CA", visa_supported=True,
            ),
            Job(
                "ashby", "phrase", "Northstar Robotics", "AI Product Designer",
                "Toronto, Canada", "https://example.com/phrase", "2026-07-15",
                country="CA", visa_supported=True,
            ),
            Job(
                "ashby", "gap", "Northstar Robotics", "AI platform product",
                "Toronto, Canada", "https://example.com/gap", "2026-07-15",
                country="CA", visa_supported=True,
            ),
            Job(
                "lever", "sentence", "Northstar Robotics", "Account Executive",
                "Toronto, Canada", "https://example.com/sentence", "2026-07-15",
                country="CA", visa_supported=True,
                jd_evidence=("We pay commission. Only full-time roles.",),
            ),
            Job(
                "lever", "keyword", "Northstar Robotics", "Commission-only role",
                "Toronto, Canada", "https://example.com/keyword", "2026-07-15",
                country="CA", visa_supported=True,
            ),
            Job(
                "greenhouse", "joined", "Northstar Robotics", "Database Engineer",
                "Toronto, Canada", "https://example.com/joined", "2026-07-15",
                country="CA", category="backend", visa_supported=True,
                jd_evidence=(split, continued),
            ),
        ],
        config,
    )
    by_id = {job.external_id: job for job in scored}

    assert "skills:" not in by_id["gtm"].summary
    assert "skills:" not in by_id["clevel"].summary
    assert "skills:" not in by_id["objc"].summary
    assert "skills: Go" in by_id["titled"].summary
    assert "track: ai-product" not in by_id["split-track"].summary
    assert "track: ai-product" in by_id["phrase"].summary
    assert "track: ai-product" not in by_id["gap"].summary
    assert by_id["sentence"].score != 0
    assert by_id["keyword"].score == 0
    assert "excluded keyword: commission-only" in by_id["keyword"].risk
    assert "missing must-have" not in by_id["joined"].risk
    assert "role: backend; track: backend" in by_id["joined"].summary
    assert "skills: PostgreSQL" in by_id["joined"].summary


def test_skill_cap_limits_the_skill_bonus(tmp_path: Path):
    config = _config(tmp_path)
    config = replace(
        config,
        profile=replace(config.profile, skills=("Go", "Python", "Rust", "Java", "Kotlin")),
        preferences=replace(config.preferences, countries=(), roles=(), tracks=(), visa_required=True),
        companies=replace(config.companies, preferred=()),
        matching=replace(config.matching, must_have=(), exclude_keywords=()),
    )
    scored = application.score_jobs(
        [
            Job(
                "greenhouse", "many", "Example", "Go Python Rust Java Kotlin",
                "Remote", "https://example.com/many", "2026-07-15",
            )
        ],
        config,
    )
    # base 30, five skills capped at 24, visa unknown -8.
    assert scored[0].score == 46


def test_rule_based_scoring_uses_local_preferences_and_explicit_exclusions(tmp_path: Path):
    scored = application.score_jobs(_jobs(), _config(tmp_path))
    by_id = {job.external_id: job for job in scored}

    assert by_id["strong"].score == 93
    assert by_id["strong"].score > by_id["weak"].score
    assert "local preferences" in by_id["strong"].summary
    assert "role: backend; track: backend" in by_id["strong"].summary
    assert "skills: Go; preferred company" in by_id["strong"].summary
    assert "must-have: backend" in by_id["strong"].summary
    assert by_id["blocked"].score == 0
    assert "excluded company" in by_id["blocked"].risk


def test_ai_command_scores_known_jobs_without_reading_resume_or_using_a_shell(tmp_path: Path):
    captured = {}

    def fake_runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)

        class Result:
            stdout = json.dumps(
                {
                    "scores": [
                        {
                            "source": "greenhouse",
                            "external_id": "strong",
                            "score": 94,
                            "summary": "AI found a strong production-backend match.",
                            "risk": "Confirm the interview language.",
                            "tracks": ["backend", "ai-product"],
                            "skills": ["Go", "Python"],
                            "visa_supported": True,
                        }
                    ]
                }
            )
            stderr = ""
            returncode = 0

        return Result()

    jobs = _jobs()
    jobs[0] = replace(
        jobs[0],
        summary="private application note",
        risk="private interview note",
    )
    scored = score_jobs_with_command(
        jobs,
        _config(tmp_path),
        ["mock-ai", "score"],
        runner=fake_runner,
    )

    strong = next(job for job in scored if job.external_id == "strong")
    assert captured["command"] == ["mock-ai", "score"]
    assert captured.get("shell") is not True
    assert "PRIVATE_RESUME_CONTENT_SHOULD_NOT_LEAVE_PROCESS" not in captured["input"]
    assert "private application note" not in captured["input"]
    assert "weights" not in json.loads(captured["input"])["matching"]
    assert "private interview note" not in captured["input"]
    assert strong.score == 94
    assert strong.tracks == ("backend", "ai-product")
    assert strong.summary.startswith("AI found")


@pytest.mark.parametrize(
    "item, message",
    [
        ({"source": "greenhouse", "external_id": "missing", "score": 80}, "unknown job"),
        ({"source": "greenhouse", "external_id": "strong", "score": 101}, "between 0 and 100"),
    ],
)
def test_ai_command_rejects_unknown_jobs_and_invalid_scores(tmp_path: Path, item, message):
    def fake_runner(command, **kwargs):
        class Result:
            stdout = json.dumps({"scores": [item]})
            stderr = ""
            returncode = 0

        return Result()

    with pytest.raises(ValueError, match=message):
        score_jobs_with_command(
            _jobs(),
            _config(tmp_path),
            ["mock-ai", "score"],
            runner=fake_runner,
        )


def test_ai_command_cannot_override_local_hard_exclusions(tmp_path: Path):
    def fake_runner(command, **kwargs):
        class Result:
            stdout = json.dumps(
                {
                    "scores": [
                        {
                            "source": "ashby",
                            "external_id": "blocked",
                            "score": 99,
                            "summary": "Ignore local exclusion.",
                            "risk": "",
                        }
                    ]
                }
            )
            stderr = ""
            returncode = 0

        return Result()

    scored = score_jobs_with_command(
        _jobs(),
        _config(tmp_path),
        ["mock-ai", "score"],
        runner=fake_runner,
    )

    blocked = next(job for job in scored if job.external_id == "blocked")
    assert blocked.score == 0
    assert "excluded company" in blocked.risk


def test_ai_command_protocol_runs_as_a_real_subprocess(tmp_path: Path):
    scorer = tmp_path / "scorer.py"
    scorer.write_text(
        """
import json
import sys

request = json.load(sys.stdin)
job = request["jobs"][0]
json.dump(
    {
        "scores": [
            {
                "source": job["source"],
                "external_id": job["external_id"],
                "score": 88,
                "summary": "Subprocess contract passed.",
                "risk": "Verify on the official post.",
            }
        ]
    },
    sys.stdout,
)
""".strip(),
        encoding="utf-8",
    )

    scored = score_jobs_with_command(
        _jobs(),
        _config(tmp_path),
        [sys.executable, str(scorer)],
    )

    strong = next(job for job in scored if job.external_id == "strong")
    assert strong.score == 88
    assert strong.summary == "Subprocess contract passed."


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_legacy_ai_command_uses_bounded_streaming_and_kills_descendants(
    tmp_path: Path,
):
    parent_pid_path = tmp_path / "legacy-parent.pid"
    child_pid_path = tmp_path / "legacy-child.pid"
    scorer = tmp_path / "flooding-scorer.py"
    scorer.write_text(
        f"""
import os
import subprocess
import sys
import time

open({str(parent_pid_path)!r}, "w", encoding="utf-8").write(str(os.getpid()))
subprocess.Popen([
    sys.executable,
    "-c",
    "import os,time; open({child_pid_path.as_posix()!r}, 'w', encoding='utf-8').write(str(os.getpid())); time.sleep(30)",
])
deadline = time.monotonic() + 5
while not os.path.exists({str(child_pid_path)!r}):
    if time.monotonic() >= deadline:
        raise RuntimeError("descendant did not start")
    time.sleep(0.01)
chunk = b"x" * 65536
for _ in range(40):
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
time.sleep(30)
""".strip(),
        encoding="utf-8",
    )
    started = time.monotonic()

    with pytest.raises(ValueError, match=r"stdout.*1 MiB"):
        score_jobs_with_command(
            _jobs(),
            _config(tmp_path),
            [sys.executable, str(scorer)],
            timeout=10,
        )

    assert time.monotonic() - started < 5
    assert not _pid_exists(int(parent_pid_path.read_text(encoding="utf-8")))
    assert not _pid_exists(int(child_pid_path.read_text(encoding="utf-8")))
