"""
OAuth hook interface for FLUX.

Server operators implement their own OAuth flow and call into this interface
to complete account creation or login. FLUX does not handle redirects,
token exchange, or provider communication — that is entirely up to you.

USAGE EXAMPLE (in your own OAuth callback handler):

    from flux.oauth import complete_oauth_login

    # After you've verified the OAuth token with your provider and have the user info:
    token, username = await complete_oauth_login(
        accounts=app["accounts"],
        provider="google",
        provider_uid="1234567890",          # stable unique ID from the provider
        suggested_username="alice",          # derived from provider profile
        display_name="Alice Smith",          # optional, shown in messages
    )
    # `token` is a FLUX session token the client can use going forward
    # `username` is the account username (may differ if there was a collision)
"""

from typing import Optional
from .accounts import AccountStore


async def complete_oauth_login(
    accounts: AccountStore,
    provider: str,
    provider_uid: str,
    suggested_username: str,
    display_name: Optional[str] = None,
) -> tuple[str, str]:
    """
    Called after your OAuth flow has verified the user with the provider.
    Creates the account if it does not exist, or logs into the existing one.

    Returns (session_token, username).

    Parameters:
        accounts          — the AccountStore instance from app["accounts"]
        provider          — arbitrary string identifying the provider, e.g. "google", "github"
        provider_uid      — the stable unique user ID from the provider (not the email)
        suggested_username — what to use as the username if creating a new account
        display_name       — optional human-readable name shown in message headers
    """
    return await accounts.register_or_login_oauth(
        provider=provider,
        provider_uid=provider_uid,
        suggested_username=suggested_username,
        display_name=display_name,
    )
