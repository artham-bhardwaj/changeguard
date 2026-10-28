from __future__ import annotations

import json
import subprocess
import sys

import pytest

from changeguard.cli import _parse_pull_request
from changeguard.cli import main as cli_main
from changeguard.report import render_markdown
from changeguard.settings import default_database_path, github_token, repository_key


def run_cli(monkeypatch, capsys, *argv: str) -> str:
    monkeypatch.setattr(sys, "argv", ["changeguard", *argv])
    cli_main()
    return capsys.readouterr().out


def test_impact_defaults_to_readable_report_with_symbolic_revisions(git_repo, tmp_path, monkeypatch, capsys):
    repo, _base, _head = git_repo
    report_file = tmp_path / "report.md"
    out = run_cli(
        monkeypatch, capsys, "impact", "--repo", str(repo), "--base", "HEAD~1", "--head", "HEAD",
        "--database", str(tmp_path / "db.sqlite3"), "--output", str(report_file),
    )
    assert out.startswith("# ChangeGuard change report")
    assert "## Risk:" in out
    assert "`app.txt`" in out and "`new.txt`" in out
    # Symbolic revisions are resolved, so the checkout is not reported as differing.
    assert "differs from the requested analysis head" not in out
    assert report_file.read_text(encoding="utf-8") == out


def test_impact_rejects_unknown_revision(git_repo, tmp_path, monkeypatch, capsys):
    repo, _base, _head = git_repo
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, capsys, "impact", "--repo", str(repo), "--base", "no-such-branch",
                "--database", str(tmp_path / "db.sqlite3"))
    assert "is not a commit" in capsys.readouterr().err


def test_evidence_add_list_search(tmp_path, monkeypatch, capsys):
    database = str(tmp_path / "evidence.sqlite3")
    incident = tmp_path / "checkout-outage.md"
    incident.write_text("# Checkout outage\n\nThe payment retry loop exhausted the database pool.\n", encoding="utf-8")

    out = run_cli(monkeypatch, capsys, "evidence", "add", str(incident), "--type", "incident",
                  "--repo", "Owner/Shop", "--database", database)
    assert "added: [incident] Checkout outage (owner/shop)" in out
    out = run_cli(monkeypatch, capsys, "evidence", "add", str(incident), "--type", "incident",
                  "--repo", "owner/shop", "--database", database)
    assert "already stored" in out

    out = run_cli(monkeypatch, capsys, "evidence", "list", "--database", database)
    assert "[incident] Checkout outage" in out

    out = run_cli(monkeypatch, capsys, "evidence", "search", "payment retry database",
                  "--repo", "owner/shop", "--database", database)
    assert "Checkout outage" in out
    out = run_cli(monkeypatch, capsys, "evidence", "search", "payment retry",
                  "--repo", "owner/other", "--database", database)
    assert "No matching evidence." in out


def test_impact_uses_evidence_for_the_repository(git_repo, tmp_path, monkeypatch, capsys):
    repo, _base, _head = git_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Owner/Shop.git"], check=True)
    assert repository_key(str(repo)) == "owner/shop"
    database = str(tmp_path / "db.sqlite3")
    note = tmp_path / "note.md"
    note.write_text("# App text incident\n\nChanging app.txt second line broke new.txt parsing.\n", encoding="utf-8")
    run_cli(monkeypatch, capsys, "evidence", "add", str(note), "--type", "incident",
            "--repo", "owner/shop", "--database", database)

    out = run_cli(monkeypatch, capsys, "impact", "--repo", str(repo), "--database", database, "--json")
    history = json.loads(out)["analysis"]["historical_evidence"]
    assert history["filters"]["repository"] == "owner/shop"
    assert [item["title"] for item in history["results"]] == ["App text incident"]
    report = run_cli(monkeypatch, capsys, "impact", "--repo", str(repo), "--database", database)
    assert "## Related history" in report and "**App text incident**" in report


@pytest.mark.parametrize("value", [
    "owner/repo#12",
    "https://github.com/owner/repo/pull/12",
    "https://github.com/owner/repo/pull/12/files",
])
def test_pull_request_reference_formats(value):
    reference = _parse_pull_request(value)
    assert (reference.full_name, reference.number) == ("owner/repo", 12)


def test_repository_key_without_github_remote(git_repo):
    repo, _base, _head = git_repo
    assert repository_key(str(repo)) == str(repo.resolve())


def test_default_database_path(monkeypatch, tmp_path):
    monkeypatch.delenv("CHANGEGUARD_DATABASE", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_database_path() == str(tmp_path / "changeguard" / "changeguard.sqlite3")
    monkeypatch.setenv("CHANGEGUARD_DATABASE", "/custom.sqlite3")
    assert default_database_path() == "/custom.sqlite3"


def test_github_token_prefers_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert github_token() == "from-env"
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("CHANGEGUARD_USE_GH_TOKEN", "0")
    assert github_token() is None


def test_report_renders_pr_api_impact_and_ai_sections():
    endpoint = "api:GET:/orders"
    analysis = {
        "change_summary": "1 file changed",
        "changed_files": ["src/Orders.java"],
        "changed_symbols": [{"kind": "method", "qualified_name": "shop.Orders.list()"}],
        "risk_assessment": {
            "overall_level": "MEDIUM", "score": 42.5,
            "metrics": {"score_weight_coverage": 0.5},
            "factors": [{"factor": "blast_radius", "state": "known", "contribution": 10.0, "weight": 25}],
        },
        "api_impact": {
            "endpoints": [{"endpoint_id": endpoint, "http_method": "GET", "route": "/orders",
                           "declaring_method": "shop.Orders.list()"}],
            "consumers": [{"consumer_id": "c1", "source_file": "web/app.js", "source_line": 7}],
            "edges": [{"endpoint_id": endpoint, "target_node": "c1"}],
            "changed_endpoint_ids": [endpoint],
            "impacted_consumer_ids": ["c1"],
            "metrics": {"endpoints_discovered": 1, "consumers_discovered": 1, "deterministic_matches": 1},
        },
        "investigation_report": {
            "summary": "Orders listing changed.",
            "findings": [{"statement": "List query changed.", "evidence_refs": ["tool-01"]}],
            "uncertainties": [],
        },
        "investigation_error": None,
        "investigation_state": {},
        "recommended_verification": ["Run tests.", "Run tests."],
        "uncertainties": ["NO_RELEVANT_EVIDENCE", "Runtime signal unknown."],
    }
    pr = {"repository": "owner/shop", "pull_number": 3, "status": "COMPLETED",
          "base_sha": "a" * 40, "head_sha": "b" * 40}
    text = render_markdown(analysis, pr=pr)
    assert text.startswith("# ChangeGuard: owner/shop#3")
    assert "## Risk: MEDIUM (42.5 / 100 · based on 50% of signals)" in text
    assert "**Changed endpoint `GET /orders`**" in text
    assert "called from `web/app.js:7`" in text
    assert "Orders listing changed." in text and "List query changed. _(evidence: tool-01)_" in text
    assert text.count("- [ ] Run tests.") == 1
    assert "NO_RELEVANT_EVIDENCE" not in text


def test_report_shows_ai_failure_without_hiding_analysis():
    text = render_markdown({
        "change_summary": "x", "changed_files": [], "investigation_report": None,
        "investigation_error": "Gemini request failed: HTTP 503", "investigation_state": None,
    })
    assert "## AI investigation" in text
    assert "Unavailable: Gemini request failed: HTTP 503" in text
