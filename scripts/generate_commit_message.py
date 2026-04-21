#!/usr/bin/env python3
"""Generate a conventional commit message from staged changes."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str
    old_path: str | None
    added: int
    deleted: int


DOC_EXTENSIONS = {".md", ".rst", ".txt"}
CONFIG_FILES = {
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    ".gitignore",
    ".dockerignore",
}


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def staged_name_status() -> list[tuple[str, str | None, str]]:
    output = run_git(["diff", "--cached", "--name-status", "--find-renames"])
    changes: list[tuple[str, str | None, str]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        status = parts[0]
        code = status[0]
        if code == "R" and len(parts) >= 3:
            changes.append((code, parts[1], parts[2]))
        elif len(parts) >= 2:
            changes.append((code, None, parts[1]))
    return changes


def staged_numstat() -> dict[str, tuple[int, int]]:
    output = run_git(["diff", "--cached", "--numstat", "--find-renames"])
    stats: dict[str, tuple[int, int]] = {}
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, deleted_raw = parts[0], parts[1]
        path = parts[-1]
        added = 0 if added_raw == "-" else int(added_raw)
        deleted = 0 if deleted_raw == "-" else int(deleted_raw)
        stats[path] = (added, deleted)
    return stats


def get_staged_changes() -> list[FileChange]:
    name_status = staged_name_status()
    numstat = staged_numstat()
    changes: list[FileChange] = []
    for status, old_path, path in name_status:
        added, deleted = numstat.get(path, (0, 0))
        changes.append(
            FileChange(
                path=path,
                status=status,
                old_path=old_path,
                added=added,
                deleted=deleted,
            )
        )
    return changes


def build_commit_message(raw_changes: list[dict[str, object]] | list[FileChange]) -> str:
    changes = [
        change if isinstance(change, FileChange) else FileChange(**change)
        for change in raw_changes
    ]
    if not changes:
        return "chore: update project files"

    commit_type = detect_commit_type(changes)
    subject = build_subject(changes, commit_type)
    body = build_body(changes)
    return f"{commit_type}: {subject}\n\n{body}"


def detect_commit_type(changes: list[FileChange]) -> str:
    paths = [change.path for change in changes]

    if all(is_docs_path(path) for path in paths):
        return "docs"
    if all(path.startswith(".github/workflows/") for path in paths):
        return "ci"
    if all(is_test_path(path) for path in paths):
        return "test"
    if all(is_config_path(path) for path in paths):
        return "chore"
    if all(change.status == "R" for change in changes):
        return "refactor"
    if any(change.status == "A" and is_code_path(change.path) for change in changes):
        return "feat"
    return "fix"


def build_subject(changes: list[FileChange], commit_type: str) -> str:
    if commit_type == "docs":
        return f"update {describe_targets(changes)}"
    if commit_type == "ci":
        return f"update {describe_workflow_targets(changes)}"
    if commit_type == "test":
        return f"update {describe_targets(changes)} coverage"
    if commit_type == "chore":
        return f"update {describe_targets(changes)}"
    if all(change.status == "R" for change in changes):
        return f"rename {describe_targets(changes)}"
    if any(change.status == "A" for change in changes):
        return f"add {describe_targets(changes)}"
    return f"update {describe_targets(changes)}"


def build_body(changes: list[FileChange]) -> str:
    lines = []
    for change in changes[:5]:
        action = action_word(change.status)
        stat = format_stat(change.added, change.deleted)
        if change.old_path:
            lines.append(f"- {action} {change.old_path} -> {change.path}{stat}")
        else:
            lines.append(f"- {action} {change.path}{stat}")
    remaining = len(changes) - 5
    if remaining > 0:
        lines.append(f"- update {remaining} more file(s)")
    return "\n".join(lines)


def describe_targets(changes: list[FileChange]) -> str:
    if len(changes) == 1:
        return describe_path(changes[0].path)

    areas = [top_level_area(change.path) for change in changes]
    counts = Counter(areas)
    top_area, top_count = counts.most_common(1)[0]
    if top_count == len(changes):
        return f"{top_area} files"
    return "project files"


def describe_workflow_targets(changes: list[FileChange]) -> str:
    if len(changes) == 1:
        name = Path(changes[0].path).stem.replace("-", " ")
        return f"{name} workflow"
    return "CI workflows"


def describe_path(path: str) -> str:
    normalized = path.rstrip("/")
    name = Path(normalized).stem or Path(normalized).name
    name = re.sub(r"[_-]+", " ", name).strip()
    if normalized.startswith(".github/workflows/"):
        return f"{name} workflow"
    if normalized.startswith("docs/") or normalized.endswith(".md"):
        return name
    return name


def top_level_area(path: str) -> str:
    parts = Path(path).parts
    if not parts:
        return "project"
    if parts[0] == ".github" and len(parts) > 1:
        return parts[1]
    return parts[0]


def is_docs_path(path: str) -> bool:
    file_path = Path(path)
    return file_path.suffix in DOC_EXTENSIONS or path.startswith("docs/")


def is_test_path(path: str) -> bool:
    file_path = Path(path)
    return (
        path.startswith("tests/")
        or file_path.name.startswith("test_")
        or file_path.name.endswith("_test.py")
    )


def is_config_path(path: str) -> bool:
    file_path = Path(path)
    return (
        path.startswith("config/")
        or path in CONFIG_FILES
        or file_path.suffix in {".yaml", ".yml", ".toml", ".ini"}
    )


def is_code_path(path: str) -> bool:
    return Path(path).suffix in {".py", ".js", ".ts", ".tsx", ".css", ".html"}


def action_word(status: str) -> str:
    return {
        "A": "add",
        "M": "modify",
        "D": "remove",
        "R": "rename",
    }.get(status, "update")


def format_stat(added: int, deleted: int) -> str:
    parts = []
    if added:
        parts.append(f"+{added}")
    if deleted:
        parts.append(f"-{deleted}")
    if not parts:
        return ""
    return f" ({', '.join(parts)})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="Generate message from staged diff")
    args = parser.parse_args()

    if not args.staged:
        parser.error("use --staged")

    try:
        changes = get_staged_changes()
    except subprocess.CalledProcessError as error:
        print(error.stderr.strip(), file=sys.stderr)
        return error.returncode

    if not changes:
        return 0

    if os.environ.get("AUTO_COMMIT_MESSAGE_SKIP") == "1":
        return 0

    print(build_commit_message(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
