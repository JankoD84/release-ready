import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ready as rr  # noqa: E402

CFG = rr.load_config("/nonexistent.json")


def ch(*paths, lines=10):
    return [rr.Change(p, lines, 0) for p in paths]


def rules(signals):
    return {s.rule for s in signals}


def test_docs_only_is_go():
    d, s = rr.evaluate(ch("README.md", "docs/guide.md"), {}, CFG)
    assert d == rr.GO and not s


def test_code_with_tests_is_go():
    d, _ = rr.evaluate(ch("src/app/orders.py", "tests/test_orders.py"), {}, CFG)
    assert d == rr.GO


def test_code_without_tests_is_conditional():
    d, s = rr.evaluate(ch("src/app/orders.py"), {}, CFG)
    assert d == rr.COND and "code-without-tests" in rules(s)


def test_risky_without_tests_is_nogo():
    d, s = rr.evaluate(ch("src/auth/login.py"), {}, CFG)
    assert d == rr.NOGO and "risky-without-tests" in rules(s)


def test_risky_with_tests_is_conditional():
    d, s = rr.evaluate(ch("src/billing/invoice.ts", "src/billing/invoice.test.ts"), {}, CFG)
    assert d == rr.COND and "risky-paths" in rules(s)


def test_ci_workflow_is_risky():
    _, s = rr.evaluate(ch(".github/workflows/deploy.yml", "tests/test_x.py"), {}, CFG)
    assert "risky-paths" in rules(s)


def test_secret_in_added_lines_is_nogo():
    d, s = rr.evaluate(ch("src/cfg.py"), {"src/cfg.py": ["TOKEN = 'ghp_" + "A" * 36 + "'"]}, CFG)
    assert d == rr.NOGO and "secrets-added" in rules(s)


def test_secret_in_example_file_ignored():
    _, s = rr.evaluate(ch(".env.example"), {".env.example": ["API_KEY='sk-" + "a" * 40 + "'"]}, CFG)
    assert "secrets-added" not in rules(s)


def test_destructive_migration_is_nogo():
    d, s = rr.evaluate(ch("alembic/versions/0042_drop.py"), {"alembic/versions/0042_drop.py": ["    op.drop_column('users', 'phone')"]}, CFG)
    assert d == rr.NOGO and "destructive-sql" in rules(s)


def test_additive_migration_is_conditional():
    d, s = rr.evaluate(ch("db/migrations/002.sql"), {"db/migrations/002.sql": ["ALTER TABLE users ADD COLUMN phone text;"]}, CFG)
    assert d == rr.COND and "migrations" in rules(s)


def test_delete_without_where():
    _, s = rr.evaluate(ch("db/migrations/003.sql"), {"db/migrations/003.sql": ["DELETE FROM sessions;"]}, CFG)
    assert "destructive-sql" in rules(s)


def test_dependencies_and_large_change():
    d, s = rr.evaluate([rr.Change("package-lock.json", 900, 50)], {}, CFG)
    assert d == rr.COND and {"dependencies", "large-change"} <= rules(s)


def test_checks_failed_and_pending():
    d, s = rr.evaluate(ch("README.md"), {}, CFG, {"failed": ["unit"], "pending": ["e2e"]})
    assert d == rr.NOGO and {"failed-checks", "pending-checks"} <= rules(s)


def test_empty_description_only_when_body_known():
    assert "empty-description" not in rules(rr.evaluate(ch("README.md"), {}, CFG, None, None)[1])
    assert "empty-description" in rules(rr.evaluate(ch("README.md"), {}, CFG, None, "")[1])


def test_config_disable_and_override():
    cfg = dict(CFG, disabled_rules=["code-without-tests"], severity_overrides={"risky-without-tests": "conditional"})
    assert rr.evaluate(ch("src/app/x.py"), {}, cfg)[0] == rr.GO
    assert rr.evaluate(ch("src/auth/x.py"), {}, cfg)[0] == rr.COND


def test_deterministic():
    args = (ch("src/auth/a.py", "pkg/x.go"), {"pkg/x.go": ["x := 1"]}, CFG)
    assert rr.evaluate(*args)[0] == rr.evaluate(*args)[0]


def test_markdown_has_marker_and_decision():
    d, s = rr.evaluate(ch("src/auth/login.py"), {}, CFG)
    md = rr.render_markdown(d, s, ch("src/auth/login.py"))
    assert md.startswith(rr.MARKER) and "NO-GO" in md and "`src/auth/login.py`" in md


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_end_to_end_git(tmp_path, monkeypatch, capsys):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "src" / "auth" / "login.py").write_text("def login():\n    return True\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "auth")
    monkeypatch.chdir(tmp_path)
    for k in ("GITHUB_EVENT_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert rr.main(["--base", "main", "--head", "feature", "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == rr.NOGO
    assert rr.main(["--base", "main", "--head", "feature", "--fail-on", "never"]) == 0


def test_bad_ref_returns_2(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert rr.main(["--base", "nope", "--head", "HEAD"]) == 2


def test_release_notes_grouping():
    import release_ready as rr
    commits = [
        ("a" * 40, "feat(api): add export endpoint (#12)", ""),
        ("b" * 40, "fix: handle empty body", ""),
        ("c" * 40, "chore: bump deps", ""),
        ("d" * 40, "refactor!: drop v1 routes", "BREAKING CHANGE: /v1 is gone, use /v2"),
        ("e" * 40, "Merge pull request #15 from x/y", "Improve onboarding copy"),
        ("f" * 40, "Update README", ""),
    ]
    out = rr.build_notes(commits, "v1.4.0")
    assert out.startswith("## v1.4.0")
    assert "### ✨ Features\n- **api:** Add export endpoint (#12) (aaaaaaa)" in out
    assert "### 🐛 Fixes\n- Handle empty body (bbbbbbb)" in out
    assert "Breaking changes\n- Drop v1 routes (ddddddd) — /v1 is gone, use /v2" in out
    assert "Improve onboarding copy (#15)" in out and "Update README" in out
    assert "ump deps" not in out
    assert "Bump deps" in rr.build_notes(commits, None, True)
    assert rr.build_notes([], None) .endswith("No user-facing changes.\n")
