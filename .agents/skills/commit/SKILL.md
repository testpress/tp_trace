---
name: commit
description: ALWAYS use this skill when committing code changes — never commit directly without it. Creates commits following conventions with proper conventional commit format and issue references. Trigger on any commit, git commit, save changes, or commit message task.
---

# Commit Messages

Follow these conventions when creating commits.

## Prerequisites

Before committing, always check the current branch:

```bash
git branch --show-current
```

**If you're on `main` or `master`, you MUST create a feature branch first** — unless the user explicitly asked to commit to main. Do not ask the user whether to create a branch; just proceed with branch creation. The `create-branch` skill will still propose a branch name for the user to confirm.

Use the `create-branch` skill to create the branch. After `create-branch` completes, verify the current branch has changed before proceeding:

```bash
git branch --show-current
```

If still on `main` or `master` (e.g., the user aborted branch creation), stop — do not commit.

## Version Tagging

Version numbers live in exactly two files:

- `pyproject.toml` — `version = "X.Y.Z"` (packaging metadata)
- `src/tp_trace/version.py` — `__version__ = "X.Y.Z"` (runtime value)

Do not hand-edit either file during normal feature work. Version bumps and tags
are release work, handled by the `release.yml` GitHub Actions workflow (see
`AGENTS.md`). `CHANGELOG.md` is also human-maintained and is not rewritten by
the workflow.

Pre-release tags mark in-progress work on a release track:

- Tag format: `vX.Y.Z-rc.N` (also `alpha`, `beta`).
- Tag the commit that introduces the change, e.g.
  `git tag -a "v0.1.7-rc.1" -m "Pre-release for v0.1.7"`.
- On release, the pre-release tag is superseded by the final `vX.Y.Z` tag that
  the release workflow creates and pushes.
- Push tags explicitly: `git push origin "vX.Y.Z-rc.N"`.

Do not create final `vX.Y.Z` tags by hand — the release workflow owns them.

## Format

```
<type>: <subject>

<body>

```

All lines must stay under 100 characters.

## Commit Types

| Type | Purpose |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `ref` | Refactoring (no behavior change) |
| `perf` | Performance improvement |
| `docs` | Documentation only |
| `test` | Test additions or corrections |
| `build` | Build system or dependencies |
| `ci` | CI configuration |
| `chore` | Maintenance tasks |
| `style` | Code formatting (no logic change) |
| `meta` | Repository metadata |
| `license` | License changes |

## Subject Line Rules

- Use imperative, present tense: "Add feature" not "Added feature"
- Capitalize the first letter
- No period at the end
- Maximum 70 characters

## Body Guidelines

- Explain **what** and **why**, not how (implementation details are visible in the diff)
- Use imperative mood and present tense
- Include motivation for the change
- Contrast with previous behavior when relevant
- Use real newlines in commit bodies; never include literal `\n` sequences
- **Behavior-focused**, not implementation-focused: describe the change in terms of user-visible behavior, not specific code or functions
- Use bullet points when they improve clarity

**For bug fixes (type `fix`):** Use the Issue → Cause → Fix format:
- **What was the issue?** – Describe the problem users faced
- **What caused the issue?** – Explain why it happened
- **How is it fixed?** – Describe the change that resolves it

## Commit Command Hygiene

When creating commits from the CLI, do not embed escaped newlines like `\n` inside `-m` strings. That produces literal backslash characters in the final commit message.

Prefer one of these patterns:

```bash
git commit -m "type: Subject" \
  -m "First paragraph with real line wrapping.

Second paragraph.

```

```bash
git commit
```

Use the editor flow when the message needs careful formatting.


## Examples

### Simple fix

```
fix: Handle null response in user endpoint

- The user API returned null for deleted accounts, causing a crash in the
  dashboard when accessing user properties
- This occurred because deleted accounts were not filtered out before
  processing the response
- Fixed by adding a null check before accessing user properties

```

### Feature

```
feat: Add Slack thread replies for alert updates

When an alert is updated or resolved, post a reply to the original
Slack thread instead of creating a new message. This keeps related
notifications grouped together.

```

### Refactor

```
ref: Extract common validation logic to shared module

Move duplicate validation code from three endpoints into a shared
validator class. No behavior change.
```

## Revert Format

```
revert: feat: Add new endpoint

This reverts commit abc123def456.

Reason: Caused performance regression in production.
```

## Principles

- Each commit should be a single, stable change
- Commits should be independently reviewable
- The repository should be in a working state after each commit

## References

- [Conventional Commits](https://www.conventionalcommits.org/)
