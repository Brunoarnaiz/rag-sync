"""Embeddings over any OpenAI-compatible /v1/embeddings endpoint (Vercel AI Gateway by default)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx


class EmbeddingError(Exception):
    """The embeddings endpoint failed or answered something unusable."""


@dataclass
class Embeddings:
    vectors: list[list[float]]
    tokens: int
    cost_usd: float


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> Embeddings: ...


class HttpEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        url: str,
        price_per_million: float = 0.02,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        backoff: float = 2.0,
    ):
        self.api_key = api_key
        self.model = model
        self.url = url
        self.price_per_million = price_per_million
        self.client = client or httpx.Client(timeout=120)
        self.max_retries = max_retries
        self.backoff = backoff

    def embed(self, texts: list[str]) -> Embeddings:
        if not texts:
            return Embeddings(vectors=[], tokens=0, cost_usd=0.0)
        body = self._post({"model": self.model, "input": texts})
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(f"expected {len(texts)} embeddings, got {len(data) if isinstance(data, list) else 'none'}")
        vectors = [item.get("embedding") for item in sorted(data, key=lambda item: item.get("index", 0))]
        if not all(isinstance(v, list) and v for v in vectors):
            raise EmbeddingError("the endpoint returned an empty embedding")
        usage = body.get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        cost = usage.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
            cost = tokens * self.price_per_million / 1_000_000
        return Embeddings(vectors=vectors, tokens=tokens, cost_usd=float(cost))

    def _post(self, payload: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                response = self.client.post(self.url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    raise EmbeddingError(f"could not reach {self.url}: {exc}") from exc
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise EmbeddingError("the endpoint did not return JSON") from exc
                if response.status_code in (401, 403):
                    raise EmbeddingError(f"HTTP {response.status_code}: check your API key")
                if response.status_code == 402:
                    raise EmbeddingError("HTTP 402: the account has no credit left")
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or last:
                    raise EmbeddingError(f"HTTP {response.status_code}: {response.text[:300]}")
            time.sleep(self.backoff * 2**attempt)
        raise EmbeddingError("unreachable")
