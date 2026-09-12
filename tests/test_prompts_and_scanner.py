from __future__ import annotations

from sentinel.models import Task
from sentinel.prompts import STRUCTURED_OUTPUT_SCHEMA, build_prompt, slugify
from sentinel.triggers.scanner import Finding, npm_findings, pip_findings, render_issue


def test_prompt_contains_guardrails():
    t = Task(id="o/r#5", repo="o/r", issue_number=5, issue_title="Bump python-multipart to 0.0.31", issue_body="details",
             issue_url="https://github.com/o/r/issues/5", labels=["security"], category="security")
    p = build_prompt(t, repo="o/r", default_branch="master", max_acu=10)
    assert "Never open a pull request against apache/superset" in p
    assert "devin/issue-5-bump-python-multipart-to-0-0-31" in p
    assert "Fixes #5" in p and "10 ACUs" in p and "security finding" in p
    assert STRUCTURED_OUTPUT_SCHEMA["required"] == ["outcome", "summary", "verification", "risk"]


def test_slugify():
    assert slugify("Hello, World!! (v2)") == "hello-world-v2"


def test_pip_findings_group_per_package_and_pick_highest_fix():
    results = {
        "requirements/development.txt": [
            {"name": "python-multipart", "version": "0.0.29", "vulns": [
                {"id": "PYSEC-1", "aliases": ["CVE-A"], "fix_versions": ["0.0.30"], "description": "bug one"},
                {"id": "PYSEC-2", "aliases": ["CVE-B"], "fix_versions": ["0.0.31"], "description": "bug two"},
            ]},
            {"name": "paramiko", "version": "3.5.1", "vulns": [{"id": "PYSEC-3", "aliases": [], "fix_versions": [], "description": "no fix"}]},
        ]
    }
    fs = {f.package: f for f in pip_findings(results)}
    assert fs["python-multipart"].fix_version == "0.0.31"
    assert set(fs["python-multipart"].advisories) == {"PYSEC-1", "CVE-A", "PYSEC-2", "CVE-B"}
    assert fs["paramiko"].fix_version is None
    title, body, labels = render_issue(fs["python-multipart"], "o/r", "master")
    assert "upgrade to 0.0.31" in title and fs["python-multipart"].marker in body and "security" in labels


def test_npm_findings_skip_semver_major_and_summary_nodes():
    audit = {"vulnerabilities": {
        "smol-toml": {"severity": "high", "range": "<=1.7.0", "isDirect": False, "fixAvailable": True,
                      "via": [{"url": "https://github.com/advisories/GHSA-1", "title": "DoS"}]},
        "nx": {"severity": "high", "range": ">=22", "isDirect": False, "fixAvailable": True, "via": ["smol-toml"]},
        "@deck.gl/geo-layers": {"severity": "high", "range": ">=8", "isDirect": True,
                                "fixAvailable": {"name": "@deck.gl/geo-layers", "version": "9.0.6", "isSemVerMajor": True},
                                "via": [{"url": "https://github.com/advisories/GHSA-2", "title": "x"}]},
    }}
    fs = npm_findings(audit)
    assert [f.package for f in fs] == ["smol-toml"]
    assert fs[0].advisories == ["GHSA-1"]


def test_finding_marker_is_stable():
    a = Finding("pip", "x", "1", "2", advisories=["B", "A"])
    b = Finding("pip", "x", "1", "2", advisories=["A", "B"])
    assert a.marker == b.marker
