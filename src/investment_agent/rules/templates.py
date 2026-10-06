"""Template files: ``config/rule_templates/*.yaml`` (P1-C2).

Files are the authoring source; the rule store (:mod:`investment_agent.rules.store`)
holds the immutable versions. A file's ``version`` is informational - the
store assigns versions by content hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from investment_agent.rules.schema import RuleTemplate
from investment_agent.rules.validation import (
    RuleContext,
    RuleIssue,
    RuleValidationError,
    parse_template,
)

DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "config" / "rule_templates"


def read_rule_file(path: Path) -> dict[str, Any]:
    """A YAML (``.yaml`` / ``.yml``) or JSON rule file as a mapping."""

    text = path.read_text(encoding="utf-8")
    raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise RuleValidationError(
            [RuleIssue("schema", "$", "a rule file must contain a mapping")], source=str(path)
        )
    return raw


def load_template_file(path: Path, *, context: RuleContext | None = None) -> RuleTemplate:
    return parse_template(read_rule_file(path), context=context, source=str(path))


def template_files(directory: Path | None = None) -> list[Path]:
    root = directory or DEFAULT_TEMPLATES_DIR
    return sorted(
        path for path in root.iterdir() if path.suffix in {".yaml", ".yml"} and path.is_file()
    )


def load_templates(
    directory: Path | None = None, *, context: RuleContext | None = None
) -> list[tuple[Path, RuleTemplate]]:
    """Every template file, validated; all problems raised together.

    Duplicate template ids across files are a ``schema`` issue.
    """

    loaded: list[tuple[Path, RuleTemplate]] = []
    issues: list[RuleIssue] = []
    for path in template_files(directory):
        try:
            loaded.append((path, load_template_file(path, context=context)))
        except RuleValidationError as exc:
            issues.extend(
                RuleIssue(issue.code, f"{path.name}:{issue.path}", issue.message)
                for issue in exc.issues
            )
    seen: dict[str, Path] = {}
    for path, template in loaded:
        if template.id in seen:
            issues.append(
                RuleIssue(
                    "schema",
                    f"{path.name}:id",
                    f"template id {template.id!r} also defined in {seen[template.id].name}",
                )
            )
        seen[template.id] = path
    if issues:
        raise RuleValidationError(issues, source=str(directory or DEFAULT_TEMPLATES_DIR))
    return loaded
