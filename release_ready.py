#!/usr/bin/env python3
"""release-ready: a GO / CONDITIONAL GO / NO-GO release readiness check for pull requests.

Reads the diff between a base and a head commit, applies transparent rules, and prints a
decision with the reasons. In GitHub Actions it can post (and keep updating) one PR comment.

Rules (all configurable in .release-ready.json):
  secrets-added       NO-GO        added lines look like credentials
  failed-checks       NO-GO        a check run on the head commit failed (needs a token)
  destructive-sql     NO-GO        DROP TABLE/COLUMN, TRUNCATE, DELETE without WHERE in a migration
  risky-without-tests NO-GO        risky paths changed and no test changed
  code-without-tests  CONDITIONAL  source changed and no test changed
  risky-paths         CONDITIONAL  auth, payments, security, infra, CI workflows ...
  migrations          CONDITIONAL  database migrations changed (plan the rollback)
  dependencies        CONDITIONAL  lockfiles / manifests changed
  large-change        CONDITIONAL  more changed lines than the threshold
  pending-checks      CONDITIONAL  check runs still running (needs a token)
  empty-description   CONDITIONAL  PR has no description (only when the PR body is known)

Standard library only. Exit code depends on --fail-on (default: no-go).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

__version__ = "1.1.0"
MARKER = "<!-- release-ready -->"
GO, COND, NOGO = "GO", "CONDITIONAL GO", "NO-GO"
RANK = {GO: 0, COND: 1, NOGO: 2}

DEFAULT_CONFIG: dict = {
    "large_change_lines": 600,
    "risky_paths": [
        "**/auth/**", "**/*auth*.*", "**/login*", "**/session*", "**/permission*", "**/rbac/**",
        "**/payment*/**", "**/*payment*.*", "**/billing/**", "**/*billing*.*", "**/checkout/**", "**/stripe*",
        "**/security/**", "**/crypto*", "**/*secret*", "**/middleware/**",
        ".github/workflows/**", "Dockerfile", "**/Dockerfile", "**/*.tf", "**/k8s/**", "**/helm/**",
        "**/nginx*.conf", "docker-compose*.yml",
    ],
    "migration_paths": ["**/migrations/**", "**/alembic/**", "**/migrate/**", "**/*.sql", "db/schema.rb", "**/prisma/migrations/**"],
    "dependency_files": ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements*.txt",
                         "poetry.lock", "pyproject.toml", "Pipfile.lock", "go.mod", "go.sum", "Cargo.toml",
                         "Cargo.lock", "Gemfile.lock", "composer.lock", "pom.xml", "build.gradle*"],
    "ignore_paths": ["docs/**", "**/*.md", "**/*.txt", "**/*.png", "**/*.svg", "**/*.jpg", "LICENSE*", "CHANGELOG*"],
    "source_extensions": [".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".java", ".kt", ".rb", ".php",
                          ".cs", ".rs", ".swift", ".scala", ".vue", ".svelte", ".sql"],
    "disabled_rules": [],
    "severity_overrides": {},
}

TEST_PATTERNS = [re.compile(p) for p in (
    r"(^|/)(tests?|__tests__|spec|specs|e2e|cypress|playwright)/", r"(^|/)test_[^/]+\.py$", r"_test\.(py|go)$",
    r"\.(test|spec)\.[jt]sx?$", r"(Test|Tests|IT)\.(java|kt|cs)$", r"_spec\.rb$", r"\.feature$")]
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "Stripe live key": re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    "API key (sk-)": re.compile(r"\bsk-(ant-|proj-)?[A-Za-z0-9_-]{32,}\b"),
    "Private key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Hardcoded password": re.compile(r"(?i)\b(password|passwd|secret|api_key|apikey)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
}
DESTRUCTIVE_SQL = re.compile(
    r"(?i)\b(drop\s+(table|column|schema|database)|truncate\s+table|truncate\s+\w+|alter\s+table\s+\w+\s+drop\b|"
    r"op\.drop_(table|column)|remove_column|drop_table|dropColumn|dropTable)")
DELETE_NO_WHERE = re.compile(r"(?i)\bdelete\s+from\s+[\w\".]+\s*;")


@dataclass
class Change:
    path: str
    added: int
    deleted: int


@dataclass
class Signal:
    rule: str
    level: str
    message: str
    files: list[str] = field(default_factory=list)


def match_any(path: str, globs: list[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", "")) or fnmatch.fnmatch(path.split("/")[-1], g):
            return True
    return False


def is_test(path: str) -> bool:
    return any(p.search(path) for p in TEST_PATTERNS)


def is_source(path: str, cfg: dict) -> bool:
    return any(path.endswith(ext) for ext in cfg["source_extensions"]) and not is_test(path) \
        and not match_any(path, cfg["migration_paths"])


def evaluate(changes: list[Change], added_lines: dict[str, list[str]], cfg: dict,
             checks: dict | None = None, pr_body: str | None = None) -> tuple[str, list[Signal]]:
    """Pure decision function: same inputs, same decision."""
    signals: list[Signal] = []
    relevant = [c for c in changes if not match_any(c.path, cfg["ignore_paths"])]
    tests = [c.path for c in changes if is_test(c.path)]
    source = [c.path for c in relevant if is_source(c.path, cfg)]
    risky = [c.path for c in relevant if match_any(c.path, cfg["risky_paths"]) and not is_test(c.path)]
    migrations = [c.path for c in changes if match_any(c.path, cfg["migration_paths"])]
    deps = [c.path for c in changes if match_any(c.path, cfg["dependency_files"])]
    total = sum(c.added + c.deleted for c in relevant)

    secret_hits = []
    for path, lines in added_lines.items():
        for line in lines:
            for label, pat in SECRET_PATTERNS.items():
                if pat.search(line) and "example" not in path.lower():
                    secret_hits.append(f"{path} ({label})")
                    break
    if secret_hits:
        signals.append(Signal("secrets-added", NOGO, "Added lines look like credentials. Remove them and rotate the key.",
                              sorted(set(secret_hits))))

    destructive = []
    for path in migrations:
        for line in added_lines.get(path, []):
            if DESTRUCTIVE_SQL.search(line) or DELETE_NO_WHERE.search(line):
                destructive.append(path)
                break
    if destructive:
        signals.append(Signal("destructive-sql", NOGO,
                              "Migration drops or wipes data. Use expand/contract and ship the drop in a later release.",
                              destructive))
    elif migrations:
        signals.append(Signal("migrations", COND, "Database migration changed. Confirm it is backward compatible and how to roll back.",
                              migrations))

    if risky and not tests:
        signals.append(Signal("risky-without-tests", NOGO, "Sensitive code changed and no test changed.", risky))
    elif risky:
        signals.append(Signal("risky-paths", COND, "Sensitive area changed. Ask for a review from its owner.", risky))
    if source and not tests and not risky:
        signals.append(Signal("code-without-tests", COND, "Source code changed without any test change.", source[:10]))
    if deps:
        signals.append(Signal("dependencies", COND, "Dependencies changed. Check the lockfile diff and release notes.", deps))
    if total > int(cfg["large_change_lines"]):
        signals.append(Signal("large-change", COND,
                              f"{total} changed lines (threshold {cfg['large_change_lines']}). Large changes hide defects; consider splitting."))
    if checks:
        if checks.get("failed"):
            signals.append(Signal("failed-checks", NOGO, "Checks failed on the head commit.", checks["failed"]))
        if checks.get("pending"):
            signals.append(Signal("pending-checks", COND, "Checks are still running.", checks["pending"]))
    if pr_body is not None and len(pr_body.strip()) < 20:
        signals.append(Signal("empty-description", COND, "The PR has no real description: say what changes and how it was tested."))

    for s in signals:
        if s.rule in cfg.get("severity_overrides", {}):
            s.level = {"go": GO, "conditional": COND, "no-go": NOGO}[cfg["severity_overrides"][s.rule].lower()]
    signals = [s for s in signals if s.rule not in cfg.get("disabled_rules", []) and s.level != GO]
    decision = max((s.level for s in signals), key=RANK.__getitem__, default=GO)
    return decision, signals



# ---------------------------------------------------------------- release notes

CC = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
SECTIONS = [("breaking", "⚠️ Breaking changes"), ("feat", "✨ Features"), ("fix", "🐛 Fixes"), ("perf", "⚡ Performance"),
            ("security", "🔒 Security"), ("revert", "↩️ Reverts"), ("docs", "📝 Docs"), ("other", "🔧 Other changes")]
HIDDEN = {"chore", "ci", "build", "style", "test", "refactor"}


def build_notes(commits: list[tuple[str, str, str]], version: str | None = None, include_all: bool = False) -> str:
    """commits: (sha, subject, body). Conventional commits are grouped; PR refs (#123) are kept."""
    groups: dict[str, list[str]] = {k: [] for k, _ in SECTIONS}
    for sha, subject, body in commits:
        if subject.startswith(("Merge branch", "Merge remote-tracking")):
            continue
        m = CC.match(subject)
        if m:
            t = m.group("type").lower()
            desc = m.group("desc").strip()
            scope = f"**{m.group('scope')}:** " if m.group("scope") else ""
        else:
            t, desc, scope = "other", subject.strip(), ""
            mm = re.match(r"^Merge pull request (#\d+) from \S+", subject)
            if mm:
                desc = (body.strip().splitlines() or [subject])[0] + f" ({mm.group(1)})"
        entry = f"- {scope}{desc[:1].upper() + desc[1:]} ({sha[:7]})"
        breaking = (m and m.group("bang")) or "BREAKING CHANGE" in body
        if breaking:
            note = re.search(r"BREAKING CHANGE:\s*(.+)", body)
            groups["breaking"].append(entry + (f" — {note.group(1).strip()}" if note else ""))
        if t in ("feat", "fix", "perf", "security", "revert", "docs"):
            groups[t].append(entry)
        elif t in HIDDEN:
            if include_all and not breaking:
                groups["other"].append(entry)
        elif not breaking:
            groups["other"].append(entry)
    head = f"## {version}" if version else "## Release notes"
    out = [head, ""]
    for key, title in SECTIONS:
        if groups[key]:
            out += [f"### {title}", *groups[key], ""]
    if len(out) == 2:
        out.append("No user-facing changes.")
    return "\n".join(out).rstrip() + "\n"


def read_commits(base: str, head: str) -> list[tuple[str, str, str]]:
    raw = git("log", "--format=%H%x1f%s%x1f%b%x1e", "--first-parent", f"{base}..{head}")
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if rec:
            sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
            out.append((sha, subject, body))
    return out


# ---------------------------------------------------------------- git + GitHub I/O

def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def read_diff(base: str, head: str) -> tuple[list[Change], dict[str, list[str]]]:
    changes = []
    for line in git("diff", "--numstat", "-M", f"{base}...{head}").splitlines():
        a, d, path = line.split("\t", 2)
        if " => " in path:  # rename: keep the new path
            path = re.sub(r"\{[^}]* => ([^}]*)\}", r"\1", path).split(" => ")[-1]
        changes.append(Change(path.replace("//", "/"), 0 if a == "-" else int(a), 0 if d == "-" else int(d)))
    added: dict[str, list[str]] = {}
    current = None
    for line in git("diff", "-U0", "-M", f"{base}...{head}").splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current, []).append(line[1:])
    return changes, added


def github_api(url: str, token: str, method: str = "GET", body: dict | None = None) -> object:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": f"release-ready/{__version__}"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed GitHub API host
        return json.loads(resp.read() or b"null")


def fetch_checks(api: str, repo: str, sha: str, token: str, self_name: str) -> dict:
    data = github_api(f"{api}/repos/{repo}/commits/{sha}/check-runs?per_page=100", token)
    failed, pending = [], []
    for run in data.get("check_runs", []):  # type: ignore[union-attr]
        if run["name"] == self_name:
            continue
        if run["status"] != "completed":
            pending.append(run["name"])
        elif run["conclusion"] in ("failure", "timed_out", "cancelled", "action_required"):
            failed.append(run["name"])
    return {"failed": sorted(set(failed)), "pending": sorted(set(pending))}


def upsert_comment(api: str, repo: str, pr: int, token: str, body: str) -> None:
    comments = github_api(f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100", token)
    for c in comments:  # type: ignore[union-attr]
        if MARKER in (c.get("body") or ""):
            github_api(c["url"], token, "PATCH", {"body": body})
            return
    github_api(f"{api}/repos/{repo}/issues/{pr}/comments", token, "POST", {"body": body})


def render_markdown(decision: str, signals: list[Signal], changes: list[Change]) -> str:
    icon = {GO: "🟢", COND: "🟡", NOGO: "🔴"}[decision]
    lines = [MARKER, f"## {icon} Release readiness: **{decision}**", ""]
    if not signals:
        lines.append("No readiness risks found in this diff.")
    else:
        lines += ["| Level | Signal | What to do |", "|---|---|---|"]
        for s in sorted(signals, key=lambda s: -RANK[s.level]):
            files = "<br>" + "<br>".join(f"`{f}`" for f in s.files[:6]) + ("<br>…" if len(s.files) > 6 else "") if s.files else ""
            lines.append(f"| {s.level} | `{s.rule}` | {s.message}{files} |")
    total = sum(c.added + c.deleted for c in changes)
    lines += ["", f"<sub>{len(changes)} files, {total} changed lines · release-ready {__version__} · "
                  "deterministic rules, no AI · want evidence-based release decisions across repos? "
                  "[Dulvarn](https://tools.dulvarn.com)</sub>"]
    return "\n".join(lines)


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = path or ".release-ready.json"
    if os.path.isfile(p):
        user = json.load(open(p, encoding="utf-8"))
        for k, v in user.items():
            if k.startswith("extra_") and k[6:] in cfg:
                cfg[k[6:]] = cfg[k[6:]] + v
            else:
                cfg[k] = v
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release-ready", description="GO / CONDITIONAL GO / NO-GO for a diff")
    ap.add_argument("--base", help="base ref/sha (default: PR base from GitHub event, else origin/main)")
    ap.add_argument("--head", help="head ref/sha (default: PR head from GitHub event, else HEAD)")
    ap.add_argument("--config", help="config JSON (default: .release-ready.json)")
    ap.add_argument("--fail-on", choices=["no-go", "conditional", "never"], default="no-go")
    ap.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    ap.add_argument("--comment", action="store_true", help="post/update the PR comment (needs GITHUB_TOKEN)")
    ap.add_argument("--checks", action="store_true", help="include check-run status (needs GITHUB_TOKEN)")
    ap.add_argument("--notes", action="store_true", help="print release notes for base..head (conventional commits) and exit")
    ap.add_argument("--notes-version", help="heading for --notes, e.g. v1.4.0")
    ap.add_argument("--notes-all", action="store_true", help="with --notes: also list chore/ci/test/refactor commits")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = ap.parse_args(argv)
    if a.notes:
        try:
            print(build_notes(read_commits(a.base or "origin/main", a.head or "HEAD"), a.notes_version, a.notes_all), end="")
        except subprocess.CalledProcessError as exc:
            print(f"release-ready: git log failed ({exc.stderr.strip()}). Pass --base <last tag>.", file=sys.stderr)
            return 2
        return 0

    event = {}
    if os.environ.get("GITHUB_EVENT_PATH") and os.path.isfile(os.environ["GITHUB_EVENT_PATH"]):
        event = json.load(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8"))
    pr = event.get("pull_request") or {}
    base = a.base or (pr.get("base") or {}).get("sha") or "origin/main"
    head = a.head or (pr.get("head") or {}).get("sha") or "HEAD"
    cfg = load_config(a.config)
    try:
        changes, added = read_diff(base, head)
    except subprocess.CalledProcessError as exc:
        print(f"release-ready: git diff failed ({exc.stderr.strip()}). Use actions/checkout with fetch-depth: 0.", file=sys.stderr)
        return 2

    token = os.environ.get("GITHUB_TOKEN", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    checks = None
    if a.checks and token and repo:
        try:
            checks = fetch_checks(api, repo, head, token, os.environ.get("RELEASE_READY_CHECK_NAME", "release-ready"))
        except urllib.error.URLError as exc:
            print(f"release-ready: could not read checks ({exc}); continuing without them", file=sys.stderr)
    decision, signals = evaluate(changes, added, cfg, checks, pr.get("body") if pr else None)

    md = render_markdown(decision, signals, changes)
    if a.format == "json":
        print(json.dumps({"decision": decision, "signals": [s.__dict__ for s in signals],
                          "files": len(changes)}, indent=2))
    elif a.format == "markdown":
        print(md)
    else:
        print(f"Release readiness: {decision}")
        for s in signals:
            print(f"  [{s.level}] {s.rule}: {s.message}")
            for f in s.files[:6]:
                print(f"      - {f}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"decision={decision}\n")
    if a.comment and token and repo and pr.get("number"):
        try:
            upsert_comment(api, repo, int(pr["number"]), token, md)
        except urllib.error.HTTPError as exc:
            print(f"release-ready: could not comment (HTTP {exc.code}); grant pull-requests: write", file=sys.stderr)

    threshold = {"no-go": RANK[NOGO], "conditional": RANK[COND], "never": 99}[a.fail_on]
    return 1 if RANK[decision] >= threshold else 0


if __name__ == "__main__":
    sys.exit(main())
