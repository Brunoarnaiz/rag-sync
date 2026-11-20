"""Load and validate rag-sync.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_MODEL = "openai/text-embedding-3-small"
DEFAULT_URL = "https://ai-gateway.vercel.sh/v1/embeddings"
SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ConfigError(Exception):
    """The config file is missing or invalid."""


@dataclass(frozen=True)
class Source:
    name: str
    query: str


@dataclass(frozen=True)
class Config:
    sources: tuple[Source, ...]
    model: str = DEFAULT_MODEL
    embeddings_url: str = DEFAULT_URL
    api_key_env: str = "AI_GATEWAY_API_KEY"
    # USD per million tokens, used only when the API does not report the cost.
    price_per_million: float = 0.02
    chunk_chars: int = 1500
    batch_size: int = 64
    # Refuse a sync that would delete more than this share of a source's index.
    max_delete_ratio: float = 0.5

    def pick(self, names: list[str] | None) -> list[Source]:
        if not names:
            return list(self.sources)
        known = {s.name: s for s in self.sources}
        missing = [n for n in names if n not in known]
        if missing:
            raise ConfigError(f"unknown source(s): {', '.join(missing)} (known: {', '.join(known)})")
        return [known[n] for n in names]


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found (copy rag-sync.example.yaml to start)")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, dict) or not sources_raw:
        raise ConfigError(f"{path}: 'sources' must map each source name to its SQL query")
    sources = []
    for name, query in sources_raw.items():
        if not isinstance(name, str) or not SOURCE_NAME.match(name):
            raise ConfigError(f"{path}: bad source name {name!r} (lowercase letters, digits, '-' and '_')")
        if not isinstance(query, str) or not query.strip():
            raise ConfigError(f"{path}: source {name!r} needs a SQL query")
        sources.append(Source(name=name, query=query.strip().rstrip(";")))

    config = Config(
        sources=tuple(sources),
        model=_str(raw, "model", DEFAULT_MODEL, path),
        embeddings_url=_str(raw, "embeddings_url", DEFAULT_URL, path),
        api_key_env=_str(raw, "api_key_env", "AI_GATEWAY_API_KEY", path),
        price_per_million=_num(raw, "price_per_million", 0.02, path, low=0),
        chunk_chars=int(_num(raw, "chunk_chars", 1500, path, low=200, high=20000)),
        batch_size=int(_num(raw, "batch_size", 64, path, low=1, high=2048)),
        max_delete_ratio=_num(raw, "max_delete_ratio", 0.5, path, low=0, high=1),
    )
    return config


def _str(raw: dict, key: str, default: str, path: Path) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: '{key}' must be a non-empty string")
    return value.strip()


def _num(raw: dict, key: str, default: float, path: Path, low: float | None = None, high: float | None = None) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: '{key}' must be a number")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ConfigError(f"{path}: '{key}' must be between {low} and {high}")
    return float(value)
