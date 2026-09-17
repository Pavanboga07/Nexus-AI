"""Build the pgvector HNSW index for memory retrieval (M8).

Why this is a script and not a migration
----------------------------------------
An HNSW index requires a PINNED dimension on the vector column. The dimension
depends on the configured embedder (256 for the local hash embedder, 1536 for
``text-embedding-3-small``), which a migration cannot know. Worse, pinning the
column would reject any existing row written with a different dimension, so the
decision needs the operator's intent and a verification step - not a silent
schema change on deploy.

Before this index exists, every retrieval is an EXACT cosine scan over the
owner's memories: correct, but linear. That is fine at thousands of memories
and not fine at millions.

Usage
-----
    python scripts/build_vector_index.py --dry-run          # inspect only
    python scripts/build_vector_index.py --dimensions 1536  # build the index
    python scripts/build_vector_index.py --drop             # remove it

Safety properties:
  * refuses to run if the column already holds vectors of another dimension
    (the index would be built on unusable data),
  * refuses to run if the deployment's configured dimension disagrees with the
    requested one, unless ``--force`` is passed,
  * uses ``CREATE INDEX CONCURRENTLY`` so it does not lock the table, and
    reports clearly when that cannot be used inside a transaction,
  * is idempotent: running it twice is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

INDEX_NAME = "ix_memories_embedding_hnsw"


async def _inspect(conn) -> tuple[int | None, int, int]:
    """Return (pinned_dimension, rows_with_embeddings, distinct_dimensions)."""
    typed = await conn.execute(
        text("""
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = 'memories'::regclass AND attname = 'embedding'
        """)
    )
    atttypmod = typed.scalar_one_or_none()

    rows = await conn.execute(
        text("SELECT count(*) FROM memories WHERE embedding IS NOT NULL")
    )
    total = int(rows.scalar_one())

    dims = await conn.execute(
        text("""
        SELECT count(DISTINCT vector_dims(embedding))
        FROM memories
        WHERE embedding IS NOT NULL
        """)
    )
    distinct = int(dims.scalar_one() or 0)
    return atttypmod, total, distinct


async def _column_dimensions(conn) -> tuple[bool, int | None]:
    """Whether the embedding column has a pinned dimension, and which.

    pgvector refuses to build an HNSW index on a dimensionless ``vector``
    column ("column does not have dimensions"), so this is the difference
    between a working index and a silently ignored one.
    """
    result = await conn.execute(
        text(
            """
            SELECT format_type(atttypid, atttypmod) AS coltype
            FROM pg_attribute
            WHERE attrelid = 'memories'::regclass AND attname = 'embedding'
            """
        )
    )
    coltype = result.scalar_one_or_none() or ""
    # "vector(1536)" when pinned, plain "vector" when not.
    if "(" in coltype and ")" in coltype:
        try:
            return True, int(coltype.split("(", 1)[1].rstrip(")"))
        except (ValueError, IndexError):
            return False, None
    return False, None


async def _embedding_dimension(conn) -> int | None:
    """The dimension actually present in the table, when unambiguous."""
    result = await conn.execute(
        text("""
        SELECT vector_dims(embedding) AS d
        FROM memories
        WHERE embedding IS NOT NULL
        GROUP BY d
        ORDER BY count(*) DESC
        LIMIT 2
        """)
    )
    rows = [int(r[0]) for r in result.fetchall()]
    if not rows:
        return None
    if len(rows) > 1:
        return -1  # mixed dimensions: unusable
    return rows[0]


async def _index_exists(conn) -> bool:
    """True only when a VALID index exists.

    A failed ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index behind that
    the planner silently ignores. Treating that as "exists" would report
    success while retrieval stayed a sequential scan, so validity is part of
    the check.
    """
    result = await conn.execute(
        text(
            "SELECT indisvalid FROM pg_index "
            "WHERE indexrelid = to_regclass(:name)"
        ),
        {"name": INDEX_NAME},
    )
    valid = result.scalar_one_or_none()
    return bool(valid)


async def _index_is_invalid(conn) -> bool:
    """An index exists but is not usable (failed concurrent build)."""
    result = await conn.execute(
        text(
            "SELECT indisvalid FROM pg_index "
            "WHERE indexrelid = to_regclass(:name)"
        ),
        {"name": INDEX_NAME},
    )
    row = result.scalar_one_or_none()
    return row is not None and not row


async def _run_statements(engine, statements: list[str], *, autocommit: bool) -> None:
    """Execute DDL, using a real autocommit connection when required.

    ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction block at all.
    Setting ``isolation_level='AUTOCOMMIT'`` on a pooled connection does not
    reliably produce one, which is how a previous version of this script left
    an INVALID index behind. A raw asyncpg connection via ``driver_connection``
    always autocommits, which is the only reliable way to do this.
    """
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        if autocommit and hasattr(driver, "execute"):
            for statement in statements:
                await driver.execute(statement)
            return
        # Non-concurrent DDL is fine inside a transaction.
        for statement in statements:
            await conn.execute(text(statement))
        await conn.commit()


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config.settings import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    configured = _configured_dimensions(settings)

    async with engine.connect() as conn:
        exists = await _index_exists(conn)
        is_invalid = await _index_is_invalid(conn)
        _, total_rows, distinct_dims = await _inspect(conn)
        present_dim = await _embedding_dimension(conn)
        column_is_pinned, pinned_dimensions = await _column_dimensions(conn)

    print(f"database              : {_safe_url(settings.database_url)}")
    print(f"configured embedder   : {settings.nexus_embedding_provider} "
          f"(dimension {configured})")
    print(f"column                : "
          + (f"vector({pinned_dimensions})" if column_is_pinned
             else "vector (dimensionless - cannot be indexed)"))
    print(f"memories with vectors : {total_rows}")
    print(f"dimensions present    : {distinct_dims}"
          + (f" (primary: {present_dim})" if present_dim else ""))
    print(
        f"index present         : {exists}"
        + (" (INVALID - ignoring)" if is_invalid else "")
    )

    index_dimensions = (
        args.dimensions
        or (present_dim if present_dim and present_dim > 0 else configured)
    )

    if args.drop:
        if not exists and not is_invalid:
            print("\nNothing to do: the index does not exist.")
            await engine.dispose()
            return 0
        print(f"\nDropping {INDEX_NAME} ...")
        await _run_statements(
            engine, [f"DROP INDEX IF EXISTS {INDEX_NAME}"], autocommit=True
        )
        print("Dropped. Retrieval falls back to an exact cosine scan.")
        await engine.dispose()
        return 0

    if present_dim == -1:
        print(
            "\nREFUSING to build: the table holds vectors of more than one "
            "dimension.\nAn index cannot serve a mix. Re-embed the memories so "
            "all rows share one dimension first.",
            file=sys.stderr,
        )
        await engine.dispose()
        return 2

    if present_dim and present_dim > 0 and present_dim != index_dimensions:
        print(
            f"\nREFUSING to build: existing vectors have dimension "
            f"{present_dim} but the index would be built for "
            f"{index_dimensions}. Re-run with --dimensions {present_dim}.",
            file=sys.stderr,
        )
        await engine.dispose()
        return 2

    if (
        present_dim is None
        and configured != index_dimensions
        and not args.force
    ):
        print(
            f"\nREFUSING to build: no vectors exist yet, the configured "
            f"embedder produces {configured} dimensions, but you asked for "
            f"{index_dimensions}. Pass --force if that is intended.",
            file=sys.stderr,
        )
        await engine.dispose()
        return 2

    if exists and not args.force:
        print(
            f"\nNothing to do: {INDEX_NAME} already exists and is valid. "
            "Pass --force to rebuild it."
        )
        await engine.dispose()
        return 0

    k = max(2, args.m)
    statements: list[str] = []
    # A stale INVALID index must be dropped first: the name is taken, and a
    # rebuild would otherwise fail with "relation already exists".
    if is_invalid or (args.force and exists):
        statements.append(f"DROP INDEX IF EXISTS {INDEX_NAME}")

    # pgvector CANNOT build an HNSW index on a dimensionless vector column -
    # it fails with "column does not have dimensions". Pinning the dimension is
    # therefore a prerequisite, not an optimisation. This is done here, after
    # every verification above, rather than in a migration: the correct value
    # depends on the deployment's embedder, and a migration that guessed wrong
    # would reject existing rows.
    if not column_is_pinned or pinned_dimensions != index_dimensions:
        statements.append(
            f"ALTER TABLE memories ALTER COLUMN embedding TYPE vector({index_dimensions})"
        )

    statements.append(
        f"CREATE INDEX {'CONCURRENTLY ' if args.concurrent else ''}"
        f"{INDEX_NAME} ON memories "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {k}, ef_construction = {max(2, args.ef_construction)})"
    )

    if args.dry_run:
        print("\n[dry run] would execute:")
        for statement in statements:
            print(f"  {statement}")
        await engine.dispose()
        return 0

    print(f"\nBuilding index (dimension {index_dimensions}) ...")
    if is_invalid:
        print("  (an invalid index from a previous failed build will be dropped)")
    if not column_is_pinned or pinned_dimensions != index_dimensions:
        print(
            f"  Pinning memories.embedding to vector({index_dimensions}) - "
            "pgvector cannot index a dimensionless column."
        )
    try:
        await _run_statements(
            engine, statements, autocommit=bool(args.concurrent)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nIndex build FAILED: {exc}", file=sys.stderr)
        print(
            "Retrieval continues to work (exact scan); the index is purely a "
            "performance optimisation.",
            file=sys.stderr,
        )
        await engine.dispose()
        return 1

    # Verify rather than assume: a concurrent build can leave an invalid index.
    async with engine.connect() as conn:
        valid = await _index_exists(conn)
    if not valid:
        print(
            f"\nIndex build did not produce a VALID index. It will be ignored "
            "by the planner.\nRe-run without --no-concurrent, or inspect the "
            "database for an INVALID index.",
            file=sys.stderr,
        )
        await engine.dispose()
        return 1

    print(f"Built {INDEX_NAME} (valid). Retrieval now uses the index.")
    await engine.dispose()
    return 0


def _configured_dimensions(settings) -> int:
    if settings.nexus_embedding_provider == "local":
        return 256
    return settings.nexus_embedding_dimensions


def _safe_url(url: str) -> str:
    """Redact credentials before printing a connection URL."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dimensions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--drop", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--concurrent",
        action="store_true",
        default=True,
        help="Use CREATE INDEX CONCURRENTLY (default; does not lock the table).",
    )
    parser.add_argument("--no-concurrent", dest="concurrent", action="store_false")
    parser.add_argument("--m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=64)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
