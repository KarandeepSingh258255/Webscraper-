from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "Tavily API key": re.compile(r"\btvly-[A-Za-z0-9_-]{16,}\b"),
    "Firecrawl API key": re.compile(r"\bfc-[A-Za-z0-9]{20,}\b"),
    "Assigned env secret": re.compile(
        r"(?im)^[A-Z0-9_]*(?:API_KEY|CLIENT_SECRET|PASSWORD|PASSWORD_HASH|SECRET_KEY|JWT_SECRET)[ \t]*=[ \t]*[^.\s][^\r\n]*"
    ),
}

ALLOWLIST_VALUES = {"...", ""}
ALLOWLIST_FILES = {".env.example"}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def is_allowed(path: Path, match: str) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    if relative in ALLOWLIST_FILES:
        _, _, value = match.partition("=")
        return value.strip() in ALLOWLIST_VALUES
    return False


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        relative = path.relative_to(ROOT).as_posix()
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if is_allowed(path, value):
                    continue
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line_no}: possible {label}")

    if findings:
        print("Potential secrets found:")
        for finding in findings:
            print(f"  {finding}")
        return 1

    print("No obvious secrets found in tracked or untracked commit-candidate files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
