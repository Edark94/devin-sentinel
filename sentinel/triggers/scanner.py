"""Scan-results trigger: turn dependency vulnerability findings into remediation issues.

    python -m sentinel.triggers.scanner --ref master            # pip-audit + npm audit -> issues
    python -m sentinel.triggers.scanner --ref master --dry-run  # print what would be filed

Design notes
  * The scanner never talks to Devin. It files *issues*; the issue label is the event that starts
    remediation (webhook or sweep). That keeps one intake path and one audit trail.
  * Findings are grouped per package so one PR fixes all CVEs for that package.
  * Only findings with a known fixed version become issues; the rest are reported but not filed.
  * Idempotent: an HTML comment marker in the issue body identifies the finding set; existing
    open issues with the same marker are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import load_settings
from ..github_client import GitHubClient

log = logging.getLogger("sentinel.scanner")

PYTHON_MANIFESTS = ["requirements/base.txt", "requirements/development.txt"]
NPM_DIR = "superset-frontend"


@dataclass
class Finding:
    ecosystem: str          # pip | npm
    package: str
    installed: str
    fix_version: str | None
    advisories: list[str] = field(default_factory=list)   # GHSA/CVE ids
    severity: str = "unknown"
    manifests: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    direct: bool = True

    @property
    def marker(self) -> str:
        key = f"{self.ecosystem}:{self.package}:{'+'.join(sorted(self.advisories))}"
        return f"<!-- sentinel:finding:{hashlib.sha1(key.encode()).hexdigest()[:12]} -->"


# ---------------------------------------------------------------------- pip-audit
async def fetch_raw(client: httpx.AsyncClient, repo: str, ref: str, path: str) -> str | None:
    r = await client.get(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")
    return r.text if r.status_code == 200 else None


def run_pip_audit(requirements_file: Path) -> list[dict]:
    cmd = ["pip-audit", "-r", str(requirements_file), "--no-deps", "--disable-pip", "-f", "json", "--progress-spinner", "off"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):  # 1 = vulnerabilities found
        raise RuntimeError(f"pip-audit failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout).get("dependencies", [])


def pip_findings(results_by_manifest: dict[str, list[dict]]) -> list[Finding]:
    by_pkg: dict[str, Finding] = {}
    for manifest, deps in results_by_manifest.items():
        for dep in deps:
            for v in dep.get("vulns", []):
                key = dep["name"].lower()
                f = by_pkg.setdefault(key, Finding("pip", dep["name"], dep["version"], None))
                ids = [v["id"], *v.get("aliases", [])]
                for i in ids:
                    if i not in f.advisories:
                        f.advisories.append(i)
                if manifest not in f.manifests:
                    f.manifests.append(manifest)
                desc = (v.get("description") or "").strip().splitlines()
                if desc and desc[0] not in f.descriptions:
                    f.descriptions.append(desc[0][:200])
                fixes = v.get("fix_versions") or []
                if fixes:
                    best = max(fixes, key=_version_key)
                    if f.fix_version is None or _version_key(best) > _version_key(f.fix_version):
                        f.fix_version = best
    return list(by_pkg.values())


# ---------------------------------------------------------------------- npm audit
def run_npm_audit(frontend_dir: Path) -> dict:
    if not shutil.which("npm"):
        log.warning("npm not on PATH; skipping npm audit")
        return {}
    proc = subprocess.run(["npm", "audit", "--package-lock-only", "--json"], cwd=frontend_dir, capture_output=True, text=True, check=False)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"npm audit produced no JSON: {proc.stderr[-500:]}") from None


def npm_findings(audit: dict, min_severity: str = "high") -> list[Finding]:
    rank = {"low": 0, "moderate": 1, "high": 2, "critical": 3}
    out: list[Finding] = []
    for name, v in (audit.get("vulnerabilities") or {}).items():
        if rank.get(v.get("severity"), -1) < rank[min_severity]:
            continue
        fix = v.get("fixAvailable")
        # Only take findings npm can fix without a semver-major change of a direct dependency.
        if fix is False or (isinstance(fix, dict) and fix.get("isSemVerMajor")):
            continue
        advisories, descs = [], []
        for via in v.get("via", []):
            if isinstance(via, dict):
                advisories.append(via.get("url", "").rsplit("/", 1)[-1] or via.get("source", ""))
                descs.append(via.get("title", "")[:200])
        if not advisories:  # transitive summary node (e.g. "nx" via brace-expansion) -> skip, the leaf is filed
            continue
        out.append(
            Finding(
                "npm", name, v.get("range", "?"), "npm audit fix",
                advisories=[a for a in advisories if a], severity=v.get("severity", "unknown"),
                manifests=[f"{NPM_DIR}/package-lock.json"], descriptions=descs, direct=bool(v.get("isDirect")),
            )
        )
    return out


# ---------------------------------------------------------------------- issues
def render_issue(f: Finding, repo: str, ref: str) -> tuple[str, str, list[str]]:
    adv = ", ".join(f.advisories)
    if f.ecosystem == "pip":
        title = f"[security] {f.package} {f.installed} is vulnerable ({adv}) — upgrade to {f.fix_version}"
        steps = "\n".join(f"- [ ] bump `{f.package}` pin in `{m}` to `{f.fix_version}` (or later patch release)" for m in f.manifests)
        verify = f"`pip-audit -r {f.manifests[0]} --no-deps --disable-pip` reports no findings for `{f.package}`; `python -c 'import superset'` still works."
    else:
        title = f"[security][frontend] {f.package} {f.installed} has {f.severity} advisories ({adv}) — refresh lockfile"
        steps = f"- [ ] in `{NPM_DIR}`, run a targeted `npm audit fix` / `npm update {f.package}` so `package-lock.json` resolves a patched version\n- [ ] do not bump semver-major versions of direct dependencies"
        verify = f"`cd {NPM_DIR} && npm audit --package-lock-only` no longer lists `{f.package}`; `npm ci` succeeds."
    body = f"""{f.marker}
## Finding
Automated scan (`{'pip-audit' if f.ecosystem == 'pip' else 'npm audit'}`) of `{repo}@{ref}` found **{f.package} {f.installed}** affected by:

""" + "\n".join(f"- {d}" for d in f.descriptions if d) + f"""

Advisories: {', '.join(f'`{a}`' for a in f.advisories)}
Manifests: {', '.join(f'`{m}`' for m in f.manifests)}
Fixed in: `{f.fix_version}`

## Remediation
{steps}

## Verification
{verify}

## Scope
Minimal diff: only this package (and hard transitive requirements of the new version). No unrelated upgrades.
"""
    labels = ["security", "dependencies"] + (["frontend"] if f.ecosystem == "npm" else [])
    return title, body, labels


async def scan_and_file(ref: str, dry_run: bool, trigger_label: str, file_issues: bool = True) -> list[Finding]:
    cfg = load_settings()
    gh = GitHubClient(cfg.github_token, cfg.github_repo, cfg.github_api_base, dry_run or cfg.github_dry_run)
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        async with httpx.AsyncClient(timeout=60) as http:
            results: dict[str, list[dict]] = {}
            for manifest in PYTHON_MANIFESTS:
                text = await fetch_raw(http, cfg.github_repo, ref, manifest)
                if text is None:
                    log.warning("manifest %s not found at %s", manifest, ref)
                    continue
                p = tmpdir / manifest.replace("/", "_")
                p.write_text(text)
                results[manifest] = run_pip_audit(p)
                log.info("pip-audit %s: %d packages checked", manifest, len(results[manifest]))
            findings += pip_findings(results)

            fe = tmpdir / NPM_DIR
            fe.mkdir()
            ok = True
            for name in ("package.json", "package-lock.json"):
                text = await fetch_raw(http, cfg.github_repo, ref, f"{NPM_DIR}/{name}")
                if text is None:
                    ok = False
                    break
                (fe / name).write_text(text)
            if ok:
                findings += npm_findings(run_npm_audit(fe))

    fixable = [f for f in findings if f.fix_version]
    for f in findings:
        log.info("finding: %s %s %s fix=%s advisories=%s", f.ecosystem, f.package, f.installed, f.fix_version, f.advisories)
    if not file_issues:
        await gh.aclose()
        return findings

    existing = await gh.list_issues_with_label("security") if not dry_run else []
    existing_markers = {m for i in existing for m in [line for line in (i.get("body") or "").splitlines() if line.startswith("<!-- sentinel:finding:")]}
    filed = 0
    for f in fixable:
        if f.marker in existing_markers:
            log.info("issue already open for %s (%s), skipping", f.package, f.marker)
            continue
        title, body, labels = render_issue(f, cfg.github_repo, ref)
        issue = await gh.create_issue(title, body, labels + [trigger_label])
        filed += 1
        log.info("filed issue #%s: %s", issue.get("number"), title)
    log.info("scan complete: %d findings, %d fixable, %d issue(s) filed", len(findings), len(fixable), filed)
    await gh.aclose()
    return findings


def _version_key(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan the target repo and file remediation issues")
    ap.add_argument("--ref", default=None, help="git ref to scan (default: configured default branch)")
    ap.add_argument("--dry-run", action="store_true", help="print findings, do not file issues")
    ap.add_argument("--no-file", action="store_true", help="only print findings")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    cfg = load_settings()
    asyncio.run(scan_and_file(args.ref or cfg.github_default_branch, args.dry_run, cfg.trigger_label, file_issues=not args.no_file))


if __name__ == "__main__":
    main()
