"""Command line: rag-sync sync | search | status."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .documents import DuplicateIdError
from .embedder import EmbeddingError, HttpEmbedder
from .store import PostgresStore, SourceError
from .sync import SyncRefused, SyncResult, sync_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-sync", description="Keep a pgvector RAG index in sync with your tables.")
    parser.add_argument("--version", action="version", version=f"rag-sync {__version__}")
    parser.add_argument("-c", "--config", default="rag-sync.yaml", help="config file (default: rag-sync.yaml)")
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="re-embed what changed and drop what's gone")
    sync.add_argument("sources", nargs="*", help="sources to sync (default: all)")
    sync.add_argument("--dry-run", action="store_true", help="show what would change and the estimated cost; embed nothing")
    sync.add_argument("--force", action="store_true", help="allow a sync that deletes most of a source's index")

    search = commands.add_parser("search", help="find the closest chunks to a question")
    search.add_argument("query")
    search.add_argument("-s", "--source", help="search only this source")
    search.add_argument("-o", "--owner", help="see only this owner's documents plus shared ones, like that user would")
    search.add_argument("-k", type=int, default=5, help="number of results (default: 5)")

    commands.add_parser("status", help="indexed documents per source and the last runs")

    args = parser.parse_args(argv)
    _load_dotenv(Path(".env"))

    try:
        config = load_config(args.config)
        database_url = _env("DATABASE_URL")
        store = PostgresStore.connect(database_url)
    except ConfigError as exc:
        return _fail(str(exc), 2)
    except Exception as exc:  # connection errors come in many shapes
        return _fail(f"could not connect to the database: {exc}", 2)

    try:
        if args.command == "sync":
            return _sync(store, config, args)
        if args.command == "search":
            return _search(store, config, args)
        return _status(store)
    except (ConfigError, SourceError, DuplicateIdError, EmbeddingError, SyncRefused) as exc:
        return _fail(str(exc), 1)
    finally:
        store.close()


def _sync(store: PostgresStore, config: Config, args: argparse.Namespace) -> int:
    sources = config.pick(args.sources)
    embedder = None if args.dry_run else _embedder(config)
    for source in sources:
        result = sync_source(store, embedder, config, source, dry_run=args.dry_run, force=args.force, log=print)
        print(_summary(result))
    return 0


def _summary(result: SyncResult) -> str:
    plan = result.plan
    counts = f"+{len(plan.added)} new  ~{len(plan.modified)} modified  -{len(plan.deleted)} deleted  ={plan.unchanged} unchanged"
    if result.dry_run:
        if not plan.has_changes:
            return f"{result.source} (dry run): up to date  {counts}"
        return (f"{result.source} (dry run): {counts}\n"
                f"  would embed {result.chunks} chunks, ~{result.tokens:,} tokens, ~${result.cost_usd:.4f}")
    if not plan.has_changes:
        return f"{result.source}: up to date  {counts}"
    return f"{result.source}: {counts}\n  embedded {result.chunks} chunks, {result.tokens:,} tokens, ${result.cost_usd:.4f}"


def _search(store: PostgresStore, config: Config, args: argparse.Namespace) -> int:
    if args.source:
        config.pick([args.source])
    embedding = _embedder(config).embed([args.query]).vectors[0]
    hits = store.search(embedding, args.source, args.owner, max(1, min(args.k, 50)))
    if not hits:
        print("no results")
        return 0
    for rank, hit in enumerate(hits, 1):
        preview = " ".join(hit.content.split())[:160]
        print(f"{rank}. {hit.similarity:.3f}  [{hit.source}] {hit.title or hit.doc_id}  ({hit.doc_id})")
        print(f"   {preview}")
    return 0


def _status(store: PostgresStore) -> int:
    data = store.status()
    if not data["sources"]:
        print("the index is empty: run `rag-sync sync`")
        return 0
    print("source        docs   chunks  last synced")
    for source, docs, chunks, synced in data["sources"]:
        print(f"{source:<12} {docs:>5}  {chunks:>7}  {synced:%Y-%m-%d %H:%M}")
    print("\nlast runs")
    for source, finished, added, modified, deleted, unchanged, chunks, tokens, cost in data["runs"]:
        print(f"  {finished:%Y-%m-%d %H:%M}  {source:<12} +{added} ~{modified} -{deleted} ={unchanged}"
              f"  {chunks} chunks  {tokens:,} tokens  ${float(cost):.4f}")
    return 0


def _embedder(config: Config) -> HttpEmbedder:
    return HttpEmbedder(
        api_key=_env(config.api_key_env), model=config.model, url=config.embeddings_url,
        price_per_million=config.price_per_million,
    )


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (put it in .env or export it)")
    return value


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=value lines; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _fail(message: str, code: int) -> int:
    print(f"rag-sync: {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
