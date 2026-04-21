---
name: commit-message-from-diff
description: Generate a commit message automatically from staged git changes and install a prepare-commit-msg hook for VS Code or terminal git commits.
---

# Commit Message From Diff

Use this skill when the user wants commit messages to be generated automatically from staged changes before `git commit`.

## What This Skill Does

1. Installs or updates a repo-local `prepare-commit-msg` hook.
2. Uses `scripts/generate_commit_message.py --staged` to build a message from the staged diff.
3. Writes a conventional commit style title and a short body with changed files.

## Workflow

1. Confirm whether the user wants auto-generation only for empty messages or forced overwrite.
2. Prefer a versioned hook in `.githooks/prepare-commit-msg`.
3. Point `git config core.hooksPath .githooks` at the versioned hook.
4. Keep merge and squash commits untouched unless the user explicitly wants those overwritten.
5. Verify by staging a change and running the generator or a test command.

## Files

- Hook: `.githooks/prepare-commit-msg`
- Generator: `scripts/generate_commit_message.py`
- Tests: `tests/test_generate_commit_message.py`

## Notes

- Set `AUTO_COMMIT_MESSAGE_SKIP=1` to bypass generation temporarily.
- This implementation is heuristic and local; it does not call an external AI API.
