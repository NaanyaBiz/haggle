"""Interactive PKCE setup CLI (alternative to the in-webapp wizard).

Run:  uv run python -m webapp.auth
"""

from __future__ import annotations

import asyncio
import secrets
import sys

from . import setup_flow, storage


async def run() -> int:
    storage.init_db()

    verifier, challenge = setup_flow.gen_pkce()
    state = secrets.token_urlsafe(16)
    url = setup_flow.build_authorize_url(challenge, state)

    print()
    print("=" * 78)
    print("Haggle webapp — first-run AGL setup")
    print("=" * 78)
    print()
    print("1. Open this URL in your real browser:")
    print()
    print(f"   {url}")
    print()
    print("2. Log in to AGL. You'll land on a Not-Found page — that's expected.")
    print("   Copy the FULL URL from your address bar.")
    print()
    callback_url = input("3. Paste the callback URL here: ").strip()
    if not callback_url:
        print("Aborted.", file=sys.stderr)
        return 2

    try:
        code = setup_flow.extract_code(callback_url, state)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    print("\nExchanging code for tokens…")
    access_token, refresh_token, auth_pin = await setup_flow.exchange_code(code, verifier)
    print(f"   refresh_token captured  ({len(refresh_token)} chars)")
    print(f"   secure.agl.com.au pin   {auth_pin or '(capture failed)'}")

    print("\nFetching /v3/overview…")
    contracts, bff_pin = await setup_flow.fetch_contracts(access_token)
    print(f"   api.platform.agl.com.au pin {bff_pin or '(capture failed)'}")
    print(f"   {len(contracts)} contract(s):")
    for i, c in enumerate(contracts, 1):
        print(f"     [{i}] {c.fuel_type:20s}  {c.contract_number}  {c.address}")

    if not contracts:
        print("ERROR: no contracts on this account.", file=sys.stderr)
        return 2

    if len(contracts) == 1:
        chosen = contracts[0]
    else:
        while True:
            raw = input(f"Select contract [1-{len(contracts)}]: ").strip()
            try:
                idx = int(raw)
                if 1 <= idx <= len(contracts):
                    chosen = contracts[idx - 1]
                    break
            except ValueError:
                pass
            print("  (invalid)", file=sys.stderr)

    storage.upsert_contracts(contracts)
    storage.set_config("refresh_token", refresh_token)
    storage.set_config("contract_number", chosen.contract_number)
    storage.set_config("account_number", chosen.account_number)
    storage.set_config("pin_auth", auth_pin)
    storage.set_config("pin_bff", bff_pin)

    print()
    print(f"Saved to {storage.db_path()}")
    print(f"Active contract: {chosen.contract_number}  ({chosen.address})")
    print()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
