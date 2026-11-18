"""Skill library: reads .md files that contain expert instructions for the model.

A skill is a Markdown file with a title, an optional short description, and instructions.
The model finds one with `find_skill`, then loads it with `use_skill` and follows it.

Layout (Luhen compatible):
    skills/
        debugging/
            run-tests.md        # -> skill name "run-tests"
        frontend/
            react-ui.md         # -> skill name "react-ui"
        gmail-send.md           # -> skill name "gmail-send"

The top-level folder name is not used in the skill name; skills from different
sub-folders are merged, and duplicate names collide (the first one wins).
"""
from __future__ import annotations

import re
from pathlib import Path

from forgecli.nova.core.config import ROOT


class SkillStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else ROOT / "skills"
        self._cache: dict[str, dict] | None = None
        self._ensure_scaffold()

    def _ensure_scaffold(self) -> None:
        """Create the skills folder with starter templates if it doesn't exist."""
        if self.root.exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        
        starter_skills = {
            "debugging": {
                "debug-error.md": "# Debug Error\n\n> Systematically find and fix the root cause of an error.\n\n1. Read the full error message and stack trace.\n2. Identify the file and line number.\n3. Read the surrounding code to understand the context.\n4. Form a hypothesis about the cause.\n5. Make a small edit to test the hypothesis.\n6. Run the code again. Repeat until fixed."
            },
            "frontend": {
                "react-ui.md": "# React UI\n\n> Build clean, accessible, and responsive React components.\n\n1. Use functional components and hooks.\n2. Keep components small and focused.\n3. Use semantic HTML.\n4. Ensure WCAG AA contrast ratios.\n5. Use CSS modules or Tailwind."
            },
            "planning": {
                "task-breakdown.md": "# Task Breakdown\n\n> Break a large request into actionable steps.\n\n1. Understand the final goal.\n2. List every necessary step.\n3. Order them by dependency.\n4. Estimate the effort for each.\n5. Start with the first step."
            },
            "testing": {
                "run-tests.md": "# Run Tests\n\n> Verify code correctness.\n\n1. Identify the test command (e.g., `pytest`, `npm test`, `cargo test`).\n2. Run the test suite.\n3. If tests fail, read the failure output carefully.\n4. Fix the code, not the test (unless the test is wrong).\n5. Run tests again until green."
            },
            "gmail-send.md": "# Gmail Send\n\n> Send an email safely.\n\n1. Confirm the recipient, subject, and body.\n2. Ask the user for approval before sending.\n3. Do not send without explicit confirmation."
        }

        for folder, files in starter_skills.items():
            if folder.endswith(".md"):
                (self.root / folder).write_text(files, encoding="utf-8")
                continue
            folder_path = self.root / folder
            folder_path.mkdir(exist_ok=True)
            for name, content in files.items():
                (folder_path / name).write_text(content, encoding="utf-8")

    def _scan(self) -> dict[str, dict]:
        skills: dict[str, dict] = {}
        if not self.root.is_dir():
            return skills
        for path in self.root.rglob("*.md"):
            if any(p.startswith(".") for p in path.relative_to(self.root).parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            name = path.stem.lower()
            title, description = self._parse_heading(text, name)
            skills.setdefault(name, {
                "name": name,
                "title": title,
                "description": description,
                "path": path,
                "full": text,
            })
        return skills

    def _parse_heading(self, text: str, name: str) -> tuple[str, str]:
        title, description = name, ""
        for line in text.splitlines()[:8]:
            m = re.match(r"^#\s+(.+?)\s*$", line)
            if m and title == name:
                title = m.group(1).strip()
                continue
            m = re.match(r"^>\s*(.+?)\s*$", line)
            if m and not description:
                description = m.group(1).strip()
                continue
            if line.strip() and not line.startswith("#"):
                if not description:
                    description = line.strip()
                break
        return title, description

    def list(self) -> dict[str, dict]:
        if self._cache is None:
            self._cache = self._scan()
        return self._cache

    def get(self, name: str) -> dict | None:
        return self.list().get(name)

    def find(self, query: str, limit: int = 5) -> list[dict]:
        q = query.lower().strip()
        if not q:
            return []
        words = re.findall(r"\w+", q)
        scored: list[tuple[int, dict]] = []
        for skill in self.list().values():
            haystack = (skill["name"] + " " + skill["title"] + " " + skill["description"]).lower()
            hits = sum(1 for w in words if w in haystack)
            if hits:
                scored.append((hits, skill))
        scored.sort(key=lambda t: -t[0])
        return [s for _h, s in scored[:limit]]

    def reload(self) -> None:
        self._cache = None


def discover_skills(root: Path | None = None) -> SkillStore:
    """Called once at startup and again by the skill tool after a change."""
    return SkillStore(root)