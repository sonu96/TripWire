"""TripWire API layer."""

from fastapi import Request

from tripwire.api.auth import WalletAuthContext


def get_supabase(request: Request):
    """FastAPI dependency that returns the Supabase client from app state."""
    return request.app.state.supabase


def get_supabase_scoped(request: Request, wallet: WalletAuthContext):
    """FastAPI dependency that returns a Supabase client with the RLS session
    variable ``app.current_wallet`` set to the authenticated wallet address.

    Use this when you want Supabase RLS policies to enforce ownership at the
    database level in addition to application-level checks.

    Usage in a route::

        sb = Depends(get_supabase_scoped)
    """
    sb = request.app.state.supabase
    # SET LOCAL scopes the variable to the current transaction.
    # The Supabase Python client exposes `.rpc()` which can execute raw SQL,
    # but for SET we use postgrest's built-in `.rpc` or a direct call.
    sb.postgrest.auth(
        token=None,
        headers={"x-wallet-address": wallet.wallet_address},
    )
    # Execute SET LOCAL to set the session variable for RLS policies.
    sb.rpc(
        "set_wallet_context",
        {"wallet_address": wallet.wallet_address},
    ).execute()
    return sb


def set_wallet_context_raw(sb, wallet_address: str) -> None:
    """Set the ``app.current_wallet`` session variable directly on a Supabase
    client using a raw SQL call via RPC.

    This requires a Postgres function ``set_wallet_context(wallet_address text)``
    to exist in the database (see migration 011).

    Alternatively, if you have direct DB access you can run:
        SET LOCAL "app.current_wallet" = '<address>';
    """
    sb.rpc(
        "set_wallet_context",
        {"wallet_address": wallet_address},
    ).execute()
