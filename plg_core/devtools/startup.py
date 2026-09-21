from __future__ import annotations

import re
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DOCS = (
    PROJECT_ROOT / "docs" / "START-HERE.md",
    PROJECT_ROOT / "docs" / "Current-Sprint.md",
    PROJECT_ROOT / "docs" / "Session-Log.md",
    PROJECT_ROOT / "docs" / "Constitution.md",
    PROJECT_ROOT / "docs" / "Development-Standards.md",
)

GENERATED_DOCUMENT_PREFIX = "documents/Customers/"


def run_git(*arguments: str) -> tuple[bool, str]:
    """Run a read-only Git command and return success plus stripped output."""

    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return False, str(error)

    output = result.stdout.strip()

    if result.returncode != 0:
        error_text = result.stderr.strip()
        return False, error_text or output or "Git command failed."

    return True, output


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def extract_markdown_value(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n+(.+?)"
        rf"(?=\n##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    match = pattern.search(text)

    if not match:
        return "Not documented"

    block = match.group(1).strip()

    for line in block.splitlines():
        candidate = line.strip()

        if not candidate:
            continue

        if candidate.startswith(("-", "*", "`")):
            candidate = candidate.lstrip("-* ").strip("`")

        return candidate

    return "Not documented"


def classify_changes(status_output: str) -> tuple[list[str], list[str]]:
    source_changes: list[str] = []
    generated_documents: list[str] = []

    for raw_line in status_output.splitlines():
        line = raw_line.rstrip()

        if not line:
            continue

        path_text = line[3:].strip().strip('"')

        if path_text.startswith(GENERATED_DOCUMENT_PREFIX):
            generated_documents.append(line)
        else:
            source_changes.append(line)

    return source_changes, generated_documents


def print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> int:
    print("=" * 48)
    print("PLG DEVELOPMENT STARTUP")
    print("=" * 48)

    missing_docs = [
        path.relative_to(PROJECT_ROOT)
        for path in REQUIRED_DOCS
        if not path.exists()
    ]

    print_section("Documentation")

    if missing_docs:
        for path in missing_docs:
            print(f"WARNING Missing: {path}")
    else:
        print("OK Required documentation is present.")

    sprint_text = read_text(PROJECT_ROOT / "docs" / "Current-Sprint.md")
    session_text = read_text(PROJECT_ROOT / "docs" / "Session-Log.md")

    print_section("Current Release")
    print(extract_markdown_value(sprint_text, "Release"))

    print_section("Current Feature")
    print(extract_markdown_value(sprint_text, "Current Feature"))

    print_section("Current Task")
    print(extract_markdown_value(sprint_text, "Current Task"))

    print_section("Latest Session Log")
    latest_session_lines = session_text.strip().splitlines()[-20:]

    if latest_session_lines:
        print("\n".join(latest_session_lines))
    else:
        print("No session log content found.")

    branch_ok, branch = run_git("branch", "--show-current")
    local_ok, local_head = run_git("rev-parse", "HEAD")
    upstream_ok, upstream = run_git(
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )

    remote_head_ok = False
    remote_head = ""

    if upstream_ok and upstream:
        remote_head_ok, remote_head = run_git("rev-parse", upstream)

    status_ok, status_output = run_git("status", "--short")

    print_section("Git")

    print(f"Branch: {branch if branch_ok else 'Unavailable'}")
    print(f"Local:  {local_head if local_ok else 'Unavailable'}")
    print(f"Remote: {remote_head if remote_head_ok else 'Unavailable'}")

    if local_ok and remote_head_ok:
        if local_head == remote_head:
            print("Sync:    OK Local and remote commits match.")
        else:
            print("Sync:    WARNING Local and remote commits differ.")
    else:
        print("Sync:    WARNING Remote comparison unavailable.")

    source_changes: list[str] = []
    generated_documents: list[str] = []

    if status_ok:
        source_changes, generated_documents = classify_changes(status_output)

    print_section("Source Changes")

    if source_changes:
        for line in source_changes:
            print(line)
    else:
        print("None.")

    print_section("Generated Customer Documents")

    if generated_documents:
        for line in generated_documents:
            print(line)
    else:
        print("None.")

    print_section("Status")

    checkpoint_required = bool(
        missing_docs
        or source_changes
        or not branch_ok
        or not local_ok
        or not remote_head_ok
        or (local_ok and remote_head_ok and local_head != remote_head)
    )

    if checkpoint_required:
        print("CHECKPOINT REQUIRED")
        print(
            "Protect or resolve the current project state before "
            "starting another milestone."
        )
        return 1

    if generated_documents:
        print("READY WITH GENERATED DOCUMENT CHANGES")
        print(
            "Source code is protected. Generated customer documents "
            "remain outside the source checkpoint."
        )
        return 0

    print("READY")
    print("The repository and required documentation are synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())