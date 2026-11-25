# release-ready

**GO / CONDITIONAL GO / NO-GO for every pull request.** Deterministic rules, no AI, no external service, no data leaves your runner.

```
🔴 Release readiness: NO-GO
| Level          | Signal                | What to do                                         |
| NO-GO          | `risky-without-tests` | Sensitive code changed and no test changed.        |
| CONDITIONAL GO | `migrations`          | Confirm it is backward compatible and how to roll back. |
```

## Quick start

```yaml
# .github/workflows/release-ready.yml
name: release-ready
on:
  pull_request:
    types: [opened, synchronize, reopened, edited, ready_for_review]
permissions:
  contents: read
  pull-requests: write
  checks: read
jobs:
  release-ready:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: JankoD84/release-ready@v1
```

Locally or in any CI: `python3 release_ready.py --base origin/main --head HEAD`

## Rules

| Rule | Level | Fires when |
|---|---|---|
| `secrets-added` | NO-GO | added lines look like a token, key or hardcoded password |
| `failed-checks` | NO-GO | another check run on the head commit failed |
| `destructive-sql` | NO-GO | a migration drops a table/column, truncates, or deletes without `WHERE` |
| `risky-without-tests` | NO-GO | auth, payments, security, infra or CI files changed and no test changed |
| `risky-paths` | CONDITIONAL | the same sensitive areas changed, with tests |
| `code-without-tests` | CONDITIONAL | source changed, no test changed |
| `migrations` | CONDITIONAL | a database migration changed |
| `dependencies` | CONDITIONAL | lockfiles or manifests changed |
| `large-change` | CONDITIONAL | more than 600 changed lines (configurable) |
| `pending-checks` | CONDITIONAL | other checks are still running |
| `empty-description` | CONDITIONAL | the PR description is empty |

The decision is the most severe signal. Same diff, same decision.

## Configuration (`.release-ready.json`)

```json
{
  "large_change_lines": 400,
  "extra_risky_paths": ["src/app/invoices/**"],
  "extra_ignore_paths": ["fixtures/**"],
  "disabled_rules": ["empty-description"],
  "severity_overrides": { "dependencies": "go" }
}
```

`extra_*` appends to the defaults; a key without `extra_` replaces them. Inputs: `fail-on` (`no-go` | `conditional` | `never`), `comment`, `checks`, `config`. Output: `decision`.

## Limits

It reads the diff and check runs, not your code's behaviour. It will not find a logic bug. It tells you where a human should look before the merge.

When you need decisions across many repositories, with evidence, policies and an audit trail, see [Dulvarn](https://tools.dulvarn.com).

MIT License.

## Release notes (1.1)

```bash
python3 release_ready.py --notes --base v1.3.0 --head HEAD --notes-version v1.4.0 > NOTES.md
gh release create v1.4.0 --notes-file NOTES.md
```

Groups conventional commits into Breaking changes (from `!` or `BREAKING CHANGE:` with its note), Features, Fixes, Performance, Security, Reverts, Docs and Other; keeps PR references; hides chore/ci/test/refactor unless `--notes-all`. Squash-merge PR titles work best.

## About

Built by **Ján Ďuriš**, QA Automation Engineer (Playwright, Python, CI/CD). Part of a small set of open tools for release quality: [release-ready](https://github.com/JankoD84/release-ready) · [gates-doctor](https://github.com/JankoD84/gates-doctor) · [flaky-playwright-lab](https://github.com/JankoD84/flaky-playwright-lab) · [oncall-rota](https://github.com/JankoD84/oncall-rota) · [expiry-radar](https://github.com/JankoD84/expiry-radar) · [dependabot-tamer](https://github.com/JankoD84/dependabot-tamer)

Flaky tests, slow CI or releases nobody trusts? Find me on [Upwork](https://www.upwork.com/freelancers/~0198692795e824559b) or [LinkedIn](https://www.linkedin.com/in/jduris).
