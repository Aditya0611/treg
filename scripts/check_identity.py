"""Free identity check: where each domain redirects and who Crustdata says owns it.

    python scripts/check_identity.py windows.com ciscom.com statuspage.io
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from app.identity import crustdata_identity, redirect_identity
from app.pipeline import _final_host
from app.treg_client import TregClient


async def main(domains: list[str]) -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    async with TregClient(
        os.environ.get("TREG_TOKEN", ""),
        base_url=os.environ.get("TREG_BASE_URL") or "https://treg.to",
        org=os.environ.get("TREG_ORG", ""),
        max_cost_usd=0.0,
        waterfall=False,
    ) as client:
        for domain in domains:
            final = await _final_host(domain)
            owner, parked = redirect_identity(domain, final)
            if parked:
                print(f"{domain}: {parked}")
                continue
            result = await client.identify_company(owner)
            found = crustdata_identity(result.raw if result.raw is not None else result.output, owner)
            print(
                f"{domain} -> site {final} -> owner {owner} | "
                f"{found.get('name') or 'no confident match'} | {found.get('linkedin', '')} | "
                f"{found.get('employees', '')} | cost ${result.cost_micro / 1_000_000:.4f}"
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["windows.com", "ciscom.com"]))
