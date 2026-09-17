"""Reset a LOCAL deployment's agent identity (and optionally its trust graph).

**This is destructive and it is not for production.** It exists because
`NEXUS_IDENTITY_KEY` is an encryption secret for private keys held at rest: if
the secret changes, every stored key becomes undecryptable, and M4's integrity
check correctly refuses to start rather than silently generating a new identity.

Refusing to start is the right default. The wrong way to get moving again is to
weaken that check - an identity that can be silently replaced is an identity
that can be silently *taken over*. So the escape hatch is explicit, offline, and
asks for confirmation.

What it does:

1. Deletes every agent key and every agent row for the owner. The next startup
   generates a fresh Ed25519 keypair and a new ``agent_id`` (the fingerprint of
   the new public key).
2. Optionally deletes trusted peers and cached agent cards, because after a new
   ``agent_id`` every existing trust record is stale: peers pinned the OLD
   fingerprint, and this deployment can no longer sign as the agent they trusted.

What it cannot fix: peers that already pinned the old fingerprint will reject the
new identity. That is trust working as intended - they must re-verify the new
card out of band. Messages, tasks, approvals, policies and memories are left
untouched unless ``--purge`` is given.

Usage::

    python -m scripts.reset_local_identity --database-url <url> --yes
    python -m scripts.reset_local_identity --database-url <url> --yes --purge

Refuses to run against a non-local host unless ``--i-know-what-i-am-doing`` is
also passed, so a copy-pasted command cannot wipe a hosted deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "db", "postgres"}

#: Deleted in dependency order. `agent_identities` references `agents`, so it
#: goes first; `trusted_agent_cards` references nothing but is meaningless once
#: the local identity changes.
IDENTITY_TABLES = ("agent_identities", "agents")
TRUST_TABLES = ("trusted_agents", "trusted_agent_cards")
#: Only with --purge: everything that belongs to a previous identity's activity.
PURGE_TABLES = (
    "a2a_tasks",
    "workflow_steps",
    "workflows",
    "autonomy_approvals",
    "autonomy_decisions",
    "autonomy_runs",
    "jobs",
)


def _host_of(database_url: str) -> str:
    """Hostname from a SQLAlchemy async URL like ``postgresql+asyncpg://u:p@host/db``."""
    # Strip the driver suffix so urlsplit sees a normal scheme.
    normalised = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return (urlsplit(normalised).hostname or "").lower()


async def _reset(database_url: str, *, purge: bool) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    tables = IDENTITY_TABLES + TRUST_TABLES + (PURGE_TABLES if purge else ())
    try:
        async with engine.begin() as connection:
            existing = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = current_schema()"
                        )
                    )
                ).all()
            }
            for table in tables:
                if table not in existing:
                    print(f"  skip {table} (not present)")
                    continue
                result = await connection.execute(text(f"DELETE FROM {table}"))
                print(f"  cleared {table}: {result.rowcount} row(s)")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        required=True,
        help="SQLAlchemy async URL of the database to reset.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required. Acknowledges that this destroys the local identity.",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Also delete tasks, workflows, autonomy runs and jobs.",
    )
    parser.add_argument(
        "--i-know-what-i-am-doing",
        dest="allow_remote",
        action="store_true",
        help="Permit running against a non-local host. Do not use on production.",
    )
    args = parser.parse_args(argv)

    if not args.yes:
        print(
            "Refusing to run without --yes. This deletes the deployment's agent "
            "identity; every trusted peer will need to re-verify the new card.",
            file=sys.stderr,
        )
        return 2

    host = _host_of(args.database_url)
    if host not in LOCAL_HOSTS and not args.allow_remote:
        print(
            f"Refusing to run against host {host!r}: not a local address.\n"
            "This command is for a development database. If you truly mean it, "
            "pass --i-know-what-i-am-doing.",
            file=sys.stderr,
        )
        return 3

    print(f"Resetting agent identity in {host} ...")
    return asyncio.run(_reset(args.database_url, purge=args.purge))


if __name__ == "__main__":
    raise SystemExit(main())
