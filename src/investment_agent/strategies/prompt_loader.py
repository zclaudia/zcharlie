"""Jinja2-based prompt template loader."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


class PromptLoader:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)
        self._env = Environment(
            loader=FileSystemLoader(str(self._base_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )

    def render(self, template_name: str, **vars: Any) -> str:
        path = self._base_dir / f"{template_name}.j2"
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt template not found: {template_name}.j2 in {self._base_dir}"
            )
        return self._env.get_template(f"{template_name}.j2").render(**vars)
