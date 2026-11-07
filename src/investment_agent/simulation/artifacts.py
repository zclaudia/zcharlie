"""Content-addressed simulation artifacts; no caller-selected filesystem paths."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from investment_agent.simulation.data import canonical_json

_DIGEST = re.compile(r"[0-9a-f]{64}")
_PATH = re.compile(r"objects/([0-9a-f]{64})\.json")


def secret_key(value: str) -> bool:
    key = "".join(c for c in value.casefold() if c.isalnum())
    return any(
        word in key
        for word in (
            "apikey",
            "secret",
            "password",
            "credential",
            "authorization",
            "accesstoken",
            "refreshtoken",
            "privatekey",
            "bearertoken",
        )
    )


def safe_export(value: Any) -> Any:
    """Redact common credential fields and authenticated URL components recursively."""
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if secret_key(str(k)) else safe_export(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_export(item) for item in value]
    if isinstance(value, str):
        if value.lower().startswith("bearer "):
            return "[redacted]"
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            host = parsed.netloc.rsplit("@", 1)[-1]
            query = urlencode(
                [
                    (k, "[redacted]" if secret_key(k) or k.lower() in {"token", "key"} else v)
                    for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                ]
            )
            return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))
    return value


class ArtifactStore:
    def __init__(self, data_dir: Path) -> None:
        data_root = Path(data_dir).resolve()
        self.root = (data_root / "simulation-runs").resolve()
        if not self.root.is_relative_to(data_root):
            raise ValueError("artifact_path_outside_data_root")
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self._checked(self.objects)

    def _checked(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("artifact_path_outside_simulation_root")
        return resolved

    def archive(self, digest: str, serialized: str) -> str:
        if (
            not _DIGEST.fullmatch(digest)
            or hashlib.sha256(serialized.encode()).hexdigest() != digest
        ):
            raise ValueError("artifact_hash_mismatch")
        # Persist JSON only, with no NaN/Infinity escape hatch.
        json.loads(
            serialized, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json"))
        )
        path = self._checked(self.objects / f"{digest}.json")
        if path.exists():
            if path.read_text(encoding="utf-8") != serialized:
                raise ValueError("artifact_hash_mismatch")
            return f"objects/{digest}.json"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.objects, prefix=".pending-", delete=False
        ) as handle:
            pending = Path(handle.name)
            try:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                pending.unlink(missing_ok=True)
                raise
        try:
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
        return f"objects/{digest}.json"

    def put(self, value: object) -> tuple[str, str]:
        serialized = canonical_json(value)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        return self.archive(digest, serialized), digest

    def read(self, path: str, *, expected_hash: str) -> str:
        match = _PATH.fullmatch(path)
        if match is None or match.group(1) != expected_hash:
            raise ValueError("invalid_artifact_reference")
        serialized = self._checked(self.root / path).read_text(encoding="utf-8")
        if hashlib.sha256(serialized.encode()).hexdigest() != expected_hash:
            raise ValueError("artifact_hash_mismatch")
        return serialized
