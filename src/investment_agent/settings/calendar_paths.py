"""Deployment resource locations: the app.yaml default and the trading calendar.

Extracted from ``settings.loader``: where a deployment's files live is a
different concern from reading/writing runtime settings values. Resolution has
to work before (and without) a ``Settings`` instance, so ``runtime_data_dir()``
hand-parses the ``.env`` file pydantic-settings reads instead of constructing
one.
"""

import os
from pathlib import Path

from investment_agent.config import SchedulerConfig, Settings, load_app_yaml

DEFAULT_APP_YAML = Path("config/app.yaml")
RUNTIME_CALENDAR_DIR = "calendars"
"""Where an applied calendar lives under ``DATA_DIR`` (``DATA_DIR/calendars/<name>``)."""


def configured_calendar_path(app_yaml_path: Path = DEFAULT_APP_YAML) -> str:
    """``scheduler.a_share_calendar_path`` from app.yaml (its default when absent).

    This is the *baseline* the repository ships; the file in effect may be the
    runtime copy under ``DATA_DIR`` (see :func:`calendar_path`).
    """

    if app_yaml_path.exists():
        return load_app_yaml(app_yaml_path).scheduler.a_share_calendar_path
    return str(SchedulerConfig.model_fields["a_share_calendar_path"].default)


def runtime_data_dir() -> Path:
    """``DATA_DIR`` as the running app sees it (env value, else the Settings default).

    Mirrors pydantic-settings resolution for a pure ``.env`` deployment: env
    wins, then the ``.env`` file ``Settings()`` reads, then the code default.
    Without the ``.env`` step an applied calendar would be written to (and
    resolved from) a directory the app itself never uses.
    """

    raw = os.environ.get("DATA_DIR")
    if raw:
        return Path(raw)
    env_file = Settings.model_config.get("env_file")
    if env_file:
        try:
            from dotenv import dotenv_values

            # Runtime config sets a single ".env" string (the stub omits str,
            # but the setting also admits a sequence of candidates, which
            # pydantic merges last-wins).
            paths = (
                [Path(env_file)]
                if isinstance(env_file, (str, Path))
                else [Path(p) for p in env_file]
            )
            values: dict[str, str | None] = {}
            for path in paths:
                values.update(dotenv_values(path))
        except OSError:
            values = {}
        for name in ("DATA_DIR", "data_dir"):
            raw = values.get(name)
            if raw:
                return Path(raw)
    return Path(str(Settings.model_fields["data_dir"].default))


def runtime_calendar_path(configured: Path | str, *, data_dir: Path | None = None) -> Path:
    """The writable location an applied calendar goes to.

    Keeps the baseline's file name so a future second calendar would resolve
    independently. This path does not have to exist yet.
    """

    baseline = Path(configured)
    root = data_dir if data_dir is not None else runtime_data_dir()
    return root / RUNTIME_CALENDAR_DIR / baseline.name


def resolved_calendar_path(configured: Path | str, *, data_dir: Path | None = None) -> Path:
    """The calendar in effect: the DATA_DIR copy when it exists, else the baseline.

    A runtime copy that exists but does not parse is *not* silently ignored:
    the read fails loudly (fail-closed) so a broken file cannot silently
    shadow a good baseline.
    """

    baseline = Path(configured)
    runtime = runtime_calendar_path(baseline, data_dir=data_dir)
    return runtime if runtime.is_file() else baseline


def calendar_path(app_yaml_path: Path = DEFAULT_APP_YAML) -> str:
    """The calendar in effect: the DATA_DIR copy first, else ``a_share_calendar_path``.

    Every reader (scheduler jobs, the worker runtime, the API pages) resolves
    through this, so applying a candidate from the health page takes effect on
    the next scheduled fire without a restart or a commit.
    """

    return str(resolved_calendar_path(configured_calendar_path(app_yaml_path)))
