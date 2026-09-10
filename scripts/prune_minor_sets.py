"""One-time cleanup: remove cached cards and sets that aren't in the curated
"major" set list (see utils.sets.MAJOR_SET_TYPES) — promos, tokens,
Commander precons, memorabilia, art series, box toppers, etc. Safe to
re-run any time; also useful if sync_all_cards.py or sync_sets.py was ever
run with an older, unfiltered version.

Usage:
    python scripts/prune_minor_sets.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import database  # noqa: E402
from utils.sets import MAJOR_SET_TYPES  # noqa: E402


async def prune() -> dict:
    """Delegate to database.prune_non_major_sets with the current major-set
    type list, returning its {cards, sets, collection_rows, trade_rows}
    deletion counts."""
    await database.init_db()
    try:
        return await database.prune_non_major_sets(list(MAJOR_SET_TYPES))
    finally:
        await database.close_db()


def main() -> None:
    result = asyncio.run(prune())
    print(f"Removed {result['cards']} cards and {result['sets']} sets outside the major set list.")
    if result["collection_rows"] or result["trade_rows"]:
        print(
            f"Also cleaned up {result['collection_rows']} orphaned collection rows "
            f"and {result['trade_rows']} orphaned trade rows."
        )


if __name__ == "__main__":
    main()
