"""MCP server for X (Twitter) API v2.

Exposes tools for posting, searching, reading, liking, retweeting,
following/unfollowing, and managing tweets via the Model Context Protocol.

Authentication
--------------
Set the following environment variables (or put them in a ``.env`` file next
to the server):

Read-only (app-only / Bearer token):
    BEARER_TOKEN          – X API v2 Bearer Token

Read-write (OAuth 1.0a user context) — required for posting, liking, etc.:
    CONSUMER_KEY          – Consumer Key (API Key)
    SECRET_KEY            – Secret Key (API Secret / Consumer Secret)
    ACCESS_TOKEN          – User Access Token
    ACCESS_TOKEN_SECRET   – User Access Token Secret
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import tweepy
from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load .env from CWD or the directory containing this file
_env_candidates = [
    Path.cwd() / ".env",
    Path(__file__).resolve().parent.parent.parent / ".env",
]
for _p in _env_candidates:
    if _p.exists():
        load_dotenv(_p)
        break

BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "")
CONSUMER_KEY = os.environ.get("CONSUMER_KEY", "") or os.environ.get("API_KEY", "")
CONSUMER_SECRET = (
    os.environ.get("CONSUMER_SECRET", "")
    or os.environ.get("SECRET_KEY", "")
    or os.environ.get("API_SECRET", "")
)
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.environ.get("ACCESS_TOKEN_SECRET", "")

_resolved_host = os.environ.get("HOST", os.environ.get("FASTMCP_HOST", "0.0.0.0"))
_resolved_port = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8000")))

# Tweet fields requested on every lookup / search
_TWEET_FIELDS = [
    "id",
    "text",
    "author_id",
    "created_at",
    "public_metrics",
    "referenced_tweets",
    "conversation_id",
    "in_reply_to_user_id",
    "attachments",
    "lang",
    "source",
]

_USER_FIELDS = [
    "id",
    "name",
    "username",
    "description",
    "public_metrics",
    "profile_image_url",
    "verified",
    "created_at",
    "location",
    "url",
]

_EXPANSIONS = [
    "author_id",
    "referenced_tweets.id",
    "in_reply_to_user_id",
]


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


def _build_app_client() -> tweepy.Client | None:
    """Build an app-only (Bearer token) client for read operations."""
    if not BEARER_TOKEN:
        return None
    return tweepy.Client(bearer_token=BEARER_TOKEN, wait_on_rate_limit=True)


def _build_user_client() -> tweepy.Client | None:
    """Build an OAuth 1.0a user-context client for write operations."""
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        return None
    return tweepy.Client(
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
        wait_on_rate_limit=True,
    )


def _build_auth_v1() -> tweepy.OAuth1UserHandler | None:
    """Build OAuth 1.0a handler for v1.1 media upload."""
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        return None
    auth = tweepy.OAuth1UserHandler(CONSUMER_KEY, CONSUMER_SECRET)
    auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
    return auth


# ---------------------------------------------------------------------------
# Lifespan context
# ---------------------------------------------------------------------------


@dataclass
class XContext:
    """Holds authenticated X API clients."""

    app_client: tweepy.Client | None  # Bearer token — reads
    user_client: tweepy.Client | None  # OAuth 1.0a — reads + writes
    api_v1: tweepy.API | None  # v1.1 — media upload
    authenticated_user_id: str | None


@asynccontextmanager
async def x_lifespan(server: FastMCP) -> AsyncIterator[XContext]:
    """Create X API clients once at startup."""
    app_client = _build_app_client()
    user_client = _build_user_client()

    auth_v1 = _build_auth_v1()
    api_v1 = tweepy.API(auth_v1) if auth_v1 else None

    # Resolve the authenticated user's ID for endpoints that require it
    authenticated_user_id: str | None = None
    if user_client:
        try:
            me = user_client.get_me()
            if me and me.data:
                authenticated_user_id = str(me.data.id)
        except Exception:
            pass

    if not app_client and not user_client:
        raise RuntimeError(
            "No X API credentials found. Set at least BEARER_TOKEN (read-only) "
            "or CONSUMER_KEY + CONSUMER_SECRET + ACCESS_TOKEN + ACCESS_TOKEN_SECRET "
            "(read-write) in your environment or .env file."
        )

    try:
        yield XContext(
            app_client=app_client,
            user_client=user_client,
            api_v1=api_v1,
            authenticated_user_id=authenticated_user_id,
        )
    finally:
        pass


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "X (Twitter)",
    dependencies=["tweepy", "python-dotenv"],
    lifespan=x_lifespan,
    host=_resolved_host,
    port=_resolved_port,
)


def _get_ctx(ctx: Context) -> XContext:
    return ctx.request_context.lifespan_context


def _read_client(xctx: XContext) -> tweepy.Client:
    """Return the best available client for read operations.

    Prefers the app client (Bearer token) for reads because some
    endpoints like search require it on the Free tier.
    Falls back to user client if Bearer token is not available.
    """
    client = xctx.app_client or xctx.user_client
    if not client:
        raise RuntimeError("No X API client available for reading.")
    return client


def _read_with_fallback(xctx: XContext, operation):
    """Run a read operation, falling back between app and user clients.

    On the Free tier some endpoints only work with Bearer (app) and
    others only with OAuth 1.0a (user).  This helper tries the primary
    client first and retries with the other on 401/403.

    Args:
        xctx: The X context with both clients.
        operation: A callable that takes a ``tweepy.Client`` and returns
                   the API response.
    """
    clients: list[tweepy.Client] = []
    if xctx.app_client:
        clients.append(xctx.app_client)
    if xctx.user_client and xctx.user_client is not xctx.app_client:
        clients.append(xctx.user_client)
    if not clients:
        raise RuntimeError("No X API client available for reading.")

    last_err: Exception | None = None
    for client in clients:
        try:
            return operation(client)
        except (
            tweepy.errors.Unauthorized,
            tweepy.errors.Forbidden,
            TypeError,
            ValueError,
        ) as e:
            last_err = e
            continue
    raise last_err  # type: ignore[misc]


def _write_client(xctx: XContext) -> tweepy.Client:
    """Return the user-context client required for write operations."""
    if not xctx.user_client:
        raise RuntimeError(
            "Write operations require OAuth 1.0a credentials. "
            "Set CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, and ACCESS_TOKEN_SECRET."
        )
    return xctx.user_client


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _tweet_to_dict(tweet: tweepy.Tweet) -> dict:
    """Convert a tweepy Tweet object to a plain dict."""
    d: dict = {"id": str(tweet.id), "text": tweet.text}
    if tweet.author_id:
        d["author_id"] = str(tweet.author_id)
    if tweet.created_at:
        d["created_at"] = tweet.created_at.isoformat()
    if hasattr(tweet, "public_metrics") and tweet.public_metrics:
        d["public_metrics"] = tweet.public_metrics
    if tweet.referenced_tweets:
        d["referenced_tweets"] = [
            {"type": rt["type"], "id": str(rt["id"])} for rt in tweet.referenced_tweets
        ]
    if tweet.conversation_id:
        d["conversation_id"] = str(tweet.conversation_id)
    if tweet.in_reply_to_user_id:
        d["in_reply_to_user_id"] = str(tweet.in_reply_to_user_id)
    if tweet.lang:
        d["lang"] = tweet.lang
    if hasattr(tweet, "source") and tweet.source:
        d["source"] = tweet.source
    return d


def _user_to_dict(user: tweepy.User) -> dict:
    """Convert a tweepy User object to a plain dict."""
    d: dict = {
        "id": str(user.id),
        "name": user.name,
        "username": user.username,
    }
    if user.description:
        d["description"] = user.description
    if hasattr(user, "public_metrics") and user.public_metrics:
        d["public_metrics"] = user.public_metrics
    if user.profile_image_url:
        d["profile_image_url"] = user.profile_image_url
    if hasattr(user, "verified"):
        d["verified"] = user.verified
    if user.created_at:
        d["created_at"] = user.created_at.isoformat()
    if user.location:
        d["location"] = user.location
    if user.url:
        d["url"] = user.url
    return d


def _includes_users(response) -> dict[str, dict]:
    """Build a lookup of user data from response includes."""
    users: dict[str, dict] = {}
    if (
        hasattr(response, "includes")
        and response.includes
        and "users" in response.includes
    ):
        for u in response.includes["users"]:
            users[str(u.id)] = _user_to_dict(u)
    return users


# ---------------------------------------------------------------------------
# Tools — Read operations
# ---------------------------------------------------------------------------


@mcp.tool()
def x_get_me(ctx: Context) -> dict:
    """Get the authenticated user's profile information.

    Returns the profile of the currently authenticated user including
    name, username, bio, follower/following counts, and more.
    """
    try:
        xctx = _get_ctx(ctx)
        resp = _read_with_fallback(xctx, lambda c: c.get_me(user_fields=_USER_FIELDS))
        if not resp or not resp.data:
            return {"error": "Could not retrieve authenticated user."}
        return _user_to_dict(resp.data)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_user(ctx: Context, username: str) -> dict:
    """Look up an X user by their username (handle).

    Args:
        username: The X handle without the @ sign (e.g. "elonmusk").

    Returns user profile including name, bio, follower counts, etc.
    """
    try:
        xctx = _get_ctx(ctx)
        resp = _read_with_fallback(
            xctx, lambda c: c.get_user(username=username, user_fields=_USER_FIELDS)
        )
        if not resp or not resp.data:
            return {"error": f"User @{username} not found."}
        return _user_to_dict(resp.data)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_user_by_id(ctx: Context, user_id: str) -> dict:
    """Look up an X user by their numeric user ID.

    Args:
        user_id: The numeric user ID.

    Returns user profile including name, bio, follower counts, etc.
    """
    try:
        xctx = _get_ctx(ctx)
        resp = _read_with_fallback(
            xctx, lambda c: c.get_user(id=user_id, user_fields=_USER_FIELDS)
        )
        if not resp or not resp.data:
            return {"error": f"User ID {user_id} not found."}
        return _user_to_dict(resp.data)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_tweet(ctx: Context, tweet_id: str) -> dict:
    """Get a single tweet by its ID.

    Args:
        tweet_id: The numeric tweet ID.

    Returns full tweet content, metrics, referenced tweets, and author info.
    """
    try:
        xctx = _get_ctx(ctx)
        resp = _read_with_fallback(
            xctx,
            lambda c: c.get_tweet(
                tweet_id,
                tweet_fields=_TWEET_FIELDS,
                user_fields=_USER_FIELDS,
                expansions=_EXPANSIONS,
            ),
        )
        if not resp or not resp.data:
            return {"error": f"Tweet {tweet_id} not found."}
        result = _tweet_to_dict(resp.data)
        users = _includes_users(resp)
        if result.get("author_id") and result["author_id"] in users:
            result["author"] = users[result["author_id"]]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_user_tweets(
    ctx: Context,
    username: str,
    max_results: int = 10,
    pagination_token: str | None = None,
) -> dict:
    """Get recent tweets posted by a specific user.

    Args:
        username: The X handle without the @ sign.
        max_results: Number of tweets to return (5-100, default 10).
        pagination_token: Token for paginating through results.

    Returns a list of tweets and an optional next_token for pagination.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(5, min(100, max_results))

        def _op(client):
            user_resp = client.get_user(username=username, user_fields=["id"])
            if not user_resp or not user_resp.data:
                return None
            kwargs: dict = {
                "id": user_resp.data.id,
                "tweet_fields": _TWEET_FIELDS,
                "user_fields": _USER_FIELDS,
                "expansions": _EXPANSIONS,
                "max_results": max_results,
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token
            return client.get_users_tweets(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        if resp is None:
            return {"error": f"User @{username} not found."}
        tweets = [_tweet_to_dict(t) for t in (resp.data or [])]
        users = _includes_users(resp)
        for t in tweets:
            if t.get("author_id") and t["author_id"] in users:
                t["author"] = users[t["author_id"]]

        result: dict = {"tweets": tweets, "count": len(tweets)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_user_mentions(
    ctx: Context,
    username: str,
    max_results: int = 10,
    pagination_token: str | None = None,
) -> dict:
    """Get recent tweets mentioning a specific user.

    Args:
        username: The X handle without the @ sign.
        max_results: Number of tweets to return (5-100, default 10).
        pagination_token: Token for paginating through results.

    Returns a list of tweets mentioning the user.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(5, min(100, max_results))

        def _op(client):
            user_resp = client.get_user(username=username, user_fields=["id"])
            if not user_resp or not user_resp.data:
                return None
            kwargs: dict = {
                "id": user_resp.data.id,
                "tweet_fields": _TWEET_FIELDS,
                "user_fields": _USER_FIELDS,
                "expansions": _EXPANSIONS,
                "max_results": max_results,
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token
            return client.get_users_mentions(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        if resp is None:
            return {"error": f"User @{username} not found."}
        tweets = [_tweet_to_dict(t) for t in (resp.data or [])]
        users = _includes_users(resp)
        for t in tweets:
            if t.get("author_id") and t["author_id"] in users:
                t["author"] = users[t["author_id"]]

        result: dict = {"tweets": tweets, "count": len(tweets)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_search_tweets(
    ctx: Context,
    query: str,
    max_results: int = 10,
    sort_order: str | None = None,
    next_token: str | None = None,
) -> dict:
    """Search for recent tweets matching a query.

    Uses the X API v2 recent search endpoint (last 7 days).

    Args:
        query: Search query — supports X search operators like
               "from:user", "#hashtag", "is:reply", "has:media", etc.
        max_results: Number of results (10-100, default 10).
        sort_order: "recency" or "relevancy" (default: relevancy).
        next_token: Pagination token from a previous search.

    Returns matching tweets with author info and pagination token.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(10, min(100, max_results))

        def _op(client):
            kwargs: dict = {
                "query": query,
                "tweet_fields": _TWEET_FIELDS,
                "user_fields": _USER_FIELDS,
                "expansions": _EXPANSIONS,
                "max_results": max_results,
            }
            if sort_order and sort_order in ("recency", "relevancy"):
                kwargs["sort_order"] = sort_order
            if next_token:
                kwargs["next_token"] = next_token
            return client.search_recent_tweets(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        tweets = [_tweet_to_dict(t) for t in (resp.data or [])]
        users = _includes_users(resp)
        for t in tweets:
            if t.get("author_id") and t["author_id"] in users:
                t["author"] = users[t["author_id"]]

        result: dict = {
            "tweets": tweets,
            "count": len(tweets),
        }
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        if resp.meta and "result_count" in resp.meta:
            result["total_results"] = resp.meta["result_count"]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_home_timeline(
    ctx: Context,
    max_results: int = 20,
    pagination_token: str | None = None,
) -> dict:
    """Get the authenticated user's home timeline (reverse-chronological).

    Requires OAuth 1.0a user-context authentication.

    Args:
        max_results: Number of tweets to return (1-100, default 20).
        pagination_token: Token for paginating through results.

    Returns tweets from the home timeline.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)  # requires user context
        max_results = max(1, min(100, max_results))

        kwargs: dict = {
            "tweet_fields": _TWEET_FIELDS,
            "user_fields": _USER_FIELDS,
            "expansions": _EXPANSIONS,
            "max_results": max_results,
        }
        if pagination_token:
            kwargs["pagination_token"] = pagination_token

        resp = client.get_home_timeline(**kwargs)
        tweets = [_tweet_to_dict(t) for t in (resp.data or [])]
        users = _includes_users(resp)
        for t in tweets:
            if t.get("author_id") and t["author_id"] in users:
                t["author"] = users[t["author_id"]]

        result: dict = {"tweets": tweets, "count": len(tweets)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools — Write operations
# ---------------------------------------------------------------------------


@mcp.tool()
def x_post_tweet(
    ctx: Context,
    text: str,
    reply_to_tweet_id: str | None = None,
    quote_tweet_id: str | None = None,
    media_paths: list[str] | None = None,
    poll_options: list[str] | None = None,
    poll_duration_minutes: int | None = None,
) -> dict:
    """Post a new tweet.

    Requires OAuth 1.0a user-context authentication.

    Args:
        text: The tweet text (up to 280 characters, or 25,000 for X Premium).
        reply_to_tweet_id: Tweet ID to reply to (creates a threaded reply).
        quote_tweet_id: Tweet ID to quote-tweet.
        media_paths: List of absolute file paths for images/videos to attach
                     (max 4 images or 1 video). Requires v1.1 media upload.
        poll_options: List of poll option strings (2-4 options).
        poll_duration_minutes: Poll duration in minutes (5-10080).

    Returns the created tweet's ID and text.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)

        kwargs: dict = {"text": text}

        # Reply
        if reply_to_tweet_id:
            kwargs["in_reply_to_tweet_id"] = reply_to_tweet_id

        # Quote tweet
        if quote_tweet_id:
            kwargs["quote_tweet_id"] = quote_tweet_id

        # Media upload via v1.1 API
        if media_paths:
            if not xctx.api_v1:
                return {
                    "error": "Media upload requires v1.1 API credentials (CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)."
                }
            media_ids = []
            for path in media_paths:
                media = xctx.api_v1.media_upload(filename=path)
                media_ids.append(media.media_id)
            kwargs["media_ids"] = media_ids

        # Poll
        if poll_options and poll_duration_minutes:
            kwargs["poll_options"] = poll_options
            kwargs["poll_duration_minutes"] = poll_duration_minutes

        resp = client.create_tweet(**kwargs)
        if resp and resp.data:
            return {"id": str(resp.data["id"]), "text": resp.data["text"]}
        return {"error": "Failed to create tweet."}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_delete_tweet(ctx: Context, tweet_id: str) -> dict:
    """Delete a tweet by its ID.

    Requires OAuth 1.0a user-context authentication.
    You can only delete your own tweets.

    Args:
        tweet_id: The numeric tweet ID to delete.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        resp = client.delete_tweet(tweet_id)
        if resp and resp.data and resp.data.get("deleted"):
            return {"success": True, "tweet_id": tweet_id}
        return {"success": False, "tweet_id": tweet_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_like_tweet(ctx: Context, tweet_id: str) -> dict:
    """Like a tweet.

    Requires OAuth 1.0a user-context authentication.

    Args:
        tweet_id: The numeric tweet ID to like.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        resp = client.like(tweet_id)
        if resp and resp.data and resp.data.get("liked"):
            return {"success": True, "tweet_id": tweet_id}
        return {"success": False, "tweet_id": tweet_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_unlike_tweet(ctx: Context, tweet_id: str) -> dict:
    """Unlike a previously liked tweet.

    Requires OAuth 1.0a user-context authentication.

    Args:
        tweet_id: The numeric tweet ID to unlike.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        resp = client.unlike(tweet_id)
        if resp and resp.data and not resp.data.get("liked"):
            return {"success": True, "tweet_id": tweet_id}
        return {"success": False, "tweet_id": tweet_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_retweet(ctx: Context, tweet_id: str) -> dict:
    """Retweet a tweet.

    Requires OAuth 1.0a user-context authentication.

    Args:
        tweet_id: The numeric tweet ID to retweet.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        resp = client.retweet(tweet_id)
        if resp and resp.data and resp.data.get("retweeted"):
            return {"success": True, "tweet_id": tweet_id}
        return {"success": False, "tweet_id": tweet_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_unretweet(ctx: Context, tweet_id: str) -> dict:
    """Remove a retweet (undo retweet).

    Requires OAuth 1.0a user-context authentication.

    Args:
        tweet_id: The numeric tweet ID to un-retweet.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        if not xctx.authenticated_user_id:
            return {"error": "Could not determine authenticated user ID."}
        resp = client.unretweet(tweet_id)
        if resp and resp.data and not resp.data.get("retweeted"):
            return {"success": True, "tweet_id": tweet_id}
        return {"success": False, "tweet_id": tweet_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_follow_user(ctx: Context, username: str) -> dict:
    """Follow a user by their username.

    Requires OAuth 1.0a user-context authentication.

    Args:
        username: The X handle without the @ sign.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        if not xctx.authenticated_user_id:
            return {"error": "Could not determine authenticated user ID."}

        # Resolve username to ID
        user_resp = client.get_user(username=username, user_fields=["id"])
        if not user_resp or not user_resp.data:
            return {"error": f"User @{username} not found."}

        resp = client.follow_user(user_resp.data.id)
        if resp and resp.data and resp.data.get("following"):
            return {"success": True, "username": username}
        return {"success": False, "username": username}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_unfollow_user(ctx: Context, username: str) -> dict:
    """Unfollow a user by their username.

    Requires OAuth 1.0a user-context authentication.

    Args:
        username: The X handle without the @ sign.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        if not xctx.authenticated_user_id:
            return {"error": "Could not determine authenticated user ID."}

        user_resp = client.get_user(username=username, user_fields=["id"])
        if not user_resp or not user_resp.data:
            return {"error": f"User @{username} not found."}

        resp = client.unfollow_user(user_resp.data.id)
        if resp and resp.data and not resp.data.get("following"):
            return {"success": True, "username": username}
        return {"success": False, "username": username}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_followers(
    ctx: Context,
    username: str,
    max_results: int = 20,
    pagination_token: str | None = None,
) -> dict:
    """Get a user's followers.

    Args:
        username: The X handle without the @ sign.
        max_results: Number of followers to return (1-1000, default 20).
        pagination_token: Token for paginating through results.

    Returns a list of user profiles who follow the specified user.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(1, min(1000, max_results))

        def _op(client):
            user_resp = client.get_user(username=username, user_fields=["id"])
            if not user_resp or not user_resp.data:
                return None
            kwargs: dict = {
                "id": user_resp.data.id,
                "user_fields": _USER_FIELDS,
                "max_results": max_results,
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token
            return client.get_users_followers(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        if resp is None:
            return {"error": f"User @{username} not found."}
        followers = [_user_to_dict(u) for u in (resp.data or [])]

        result: dict = {"followers": followers, "count": len(followers)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_following(
    ctx: Context,
    username: str,
    max_results: int = 20,
    pagination_token: str | None = None,
) -> dict:
    """Get users that a specific user is following.

    Args:
        username: The X handle without the @ sign.
        max_results: Number of users to return (1-1000, default 20).
        pagination_token: Token for paginating through results.

    Returns a list of user profiles that the specified user follows.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(1, min(1000, max_results))

        def _op(client):
            user_resp = client.get_user(username=username, user_fields=["id"])
            if not user_resp or not user_resp.data:
                return None
            kwargs: dict = {
                "id": user_resp.data.id,
                "user_fields": _USER_FIELDS,
                "max_results": max_results,
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token
            return client.get_users_following(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        if resp is None:
            return {"error": f"User @{username} not found."}
        following = [_user_to_dict(u) for u in (resp.data or [])]

        result: dict = {"following": following, "count": len(following)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_post_thread(ctx: Context, tweets: list[str]) -> dict:
    """Post a thread (series of connected tweets).

    Requires OAuth 1.0a user-context authentication.

    Args:
        tweets: List of tweet texts to post as a thread, in order.
                Each tweet can be up to 280 characters (or 25,000 for X Premium).

    Returns a list of created tweet IDs.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)

        posted: list[dict] = []
        reply_to: str | None = None

        for text in tweets:
            kwargs: dict = {"text": text}
            if reply_to:
                kwargs["in_reply_to_tweet_id"] = reply_to

            resp = client.create_tweet(**kwargs)
            if resp and resp.data:
                tweet_id = str(resp.data["id"])
                posted.append({"id": tweet_id, "text": resp.data["text"]})
                reply_to = tweet_id
            else:
                return {
                    "error": f"Failed to post tweet #{len(posted) + 1}.",
                    "posted_so_far": posted,
                }

        return {"tweets": posted, "count": len(posted)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_liked_tweets(
    ctx: Context,
    username: str | None = None,
    max_results: int = 20,
    pagination_token: str | None = None,
) -> dict:
    """Get tweets liked by a user.

    Args:
        username: The X handle without the @ sign. Defaults to the
                  authenticated user if not specified.
        max_results: Number of tweets to return (5-100, default 20).
        pagination_token: Token for paginating through results.

    Returns a list of liked tweets.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(5, min(100, max_results))

        if not username and not xctx.authenticated_user_id:
            return {"error": "Specify a username or authenticate with OAuth 1.0a."}

        def _op(client):
            if username:
                user_resp = client.get_user(username=username, user_fields=["id"])
                if not user_resp or not user_resp.data:
                    return None
                uid = user_resp.data.id
            else:
                uid = xctx.authenticated_user_id
            kwargs: dict = {
                "id": uid,
                "tweet_fields": _TWEET_FIELDS,
                "user_fields": _USER_FIELDS,
                "expansions": _EXPANSIONS,
                "max_results": max_results,
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token
            return client.get_liked_tweets(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        if resp is None:
            return {"error": f"User @{username} not found."}
        tweets = [_tweet_to_dict(t) for t in (resp.data or [])]
        users = _includes_users(resp)
        for t in tweets:
            if t.get("author_id") and t["author_id"] in users:
                t["author"] = users[t["author_id"]]

        result: dict = {"tweets": tweets, "count": len(tweets)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools — DM operations
# ---------------------------------------------------------------------------


@mcp.tool()
def x_send_dm(ctx: Context, username: str, text: str) -> dict:
    """Send a direct message to a user.

    Requires OAuth 1.0a user-context authentication.

    Args:
        username: The X handle without the @ sign.
        text: The message text.

    Returns the DM event details.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)

        user_resp = client.get_user(username=username, user_fields=["id"])
        if not user_resp or not user_resp.data:
            return {"error": f"User @{username} not found."}

        resp = client.create_direct_message(
            participant_id=user_resp.data.id,
            text=text,
        )
        if resp and resp.data:
            return {
                "success": True,
                "dm_event_id": str(resp.data["dm_event_id"]),
                "dm_conversation_id": str(resp.data.get("dm_conversation_id", "")),
            }
        return {"error": "Failed to send DM."}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_dm_events(
    ctx: Context,
    max_results: int = 20,
    pagination_token: str | None = None,
) -> dict:
    """Get recent DM events (messages received/sent).

    Requires OAuth 1.0a user-context authentication.

    Args:
        max_results: Number of DM events to return (1-100, default 20).
        pagination_token: Token for paginating through results.

    Returns a list of DM events.
    """
    try:
        xctx = _get_ctx(ctx)
        client = _write_client(xctx)
        max_results = max(1, min(100, max_results))

        kwargs: dict = {
            "max_results": max_results,
            "dm_event_fields": [
                "id",
                "text",
                "event_type",
                "created_at",
                "sender_id",
                "dm_conversation_id",
            ],
        }
        if pagination_token:
            kwargs["pagination_token"] = pagination_token

        resp = client.get_direct_message_events(**kwargs)
        events = []
        for ev in resp.data or []:
            events.append(
                {
                    "id": str(ev.id),
                    "text": ev.text if hasattr(ev, "text") else None,
                    "event_type": ev.event_type if hasattr(ev, "event_type") else None,
                    "created_at": ev.created_at.isoformat()
                    if hasattr(ev, "created_at") and ev.created_at
                    else None,
                    "sender_id": str(ev.sender_id)
                    if hasattr(ev, "sender_id") and ev.sender_id
                    else None,
                    "dm_conversation_id": str(ev.dm_conversation_id)
                    if hasattr(ev, "dm_conversation_id") and ev.dm_conversation_id
                    else None,
                }
            )

        result: dict = {"events": events, "count": len(events)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools — List operations
# ---------------------------------------------------------------------------


@mcp.tool()
def x_get_list_tweets(
    ctx: Context,
    list_id: str,
    max_results: int = 20,
    pagination_token: str | None = None,
) -> dict:
    """Get tweets from an X List.

    Args:
        list_id: The numeric list ID.
        max_results: Number of tweets to return (1-100, default 20).
        pagination_token: Token for paginating through results.

    Returns tweets from the specified list.
    """
    try:
        xctx = _get_ctx(ctx)
        max_results = max(1, min(100, max_results))

        def _op(client):
            kwargs: dict = {
                "id": list_id,
                "tweet_fields": _TWEET_FIELDS,
                "user_fields": _USER_FIELDS,
                "expansions": _EXPANSIONS,
                "max_results": max_results,
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token
            return client.get_list_tweets(**kwargs)

        resp = _read_with_fallback(xctx, _op)
        tweets = [_tweet_to_dict(t) for t in (resp.data or [])]
        users = _includes_users(resp)
        for t in tweets:
            if t.get("author_id") and t["author_id"] in users:
                t["author"] = users[t["author_id"]]

        result: dict = {"tweets": tweets, "count": len(tweets)}
        if resp.meta and "next_token" in resp.meta:
            result["next_token"] = resp.meta["next_token"]
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def x_get_owned_lists(ctx: Context, username: str | None = None) -> dict:
    """Get lists owned by a user.

    Args:
        username: The X handle without the @ sign. Defaults to the
                  authenticated user if not specified.

    Returns a list of X Lists owned by the user.
    """
    try:
        xctx = _get_ctx(ctx)

        if not username and not xctx.authenticated_user_id:
            return {"error": "Specify a username or authenticate with OAuth 1.0a."}

        def _op(client):
            if username:
                user_resp = client.get_user(username=username, user_fields=["id"])
                if not user_resp or not user_resp.data:
                    return None
                uid = user_resp.data.id
            else:
                uid = xctx.authenticated_user_id
            return client.get_owned_lists(
                id=uid,
                list_fields=[
                    "id",
                    "name",
                    "description",
                    "member_count",
                    "follower_count",
                    "created_at",
                    "owner_id",
                ],
            )

        resp = _read_with_fallback(xctx, _op)
        if resp is None:
            return {"error": f"User @{username} not found."}
        lists = []
        for lst in resp.data or []:
            lists.append(
                {
                    "id": str(lst.id),
                    "name": lst.name,
                    "description": getattr(lst, "description", None),
                    "member_count": getattr(lst, "member_count", None),
                    "follower_count": getattr(lst, "follower_count", None),
                    "created_at": lst.created_at.isoformat()
                    if hasattr(lst, "created_at") and lst.created_at
                    else None,
                }
            )

        return {"lists": lists, "count": len(lists)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def auth():
    """Verify authentication and print account info."""
    load_dotenv()
    print("X (Twitter) MCP — Auth check")
    print(f"  BEARER_TOKEN:       {'set' if BEARER_TOKEN else 'NOT SET'}")
    print(f"  CONSUMER_KEY:       {'set' if CONSUMER_KEY else 'NOT SET'}")
    print(f"  SECRET_KEY:         {'set' if CONSUMER_SECRET else 'NOT SET'}")
    print(f"  ACCESS_TOKEN:       {'set' if ACCESS_TOKEN else 'NOT SET'}")
    print(f"  ACCESS_TOKEN_SECRET: {'set' if ACCESS_TOKEN_SECRET else 'NOT SET'}")

    user_client = _build_user_client()
    if user_client:
        me = user_client.get_me(user_fields=_USER_FIELDS)
        if me and me.data:
            print(f"\n  Authenticated as: @{me.data.username} ({me.data.name})")
            if hasattr(me.data, "public_metrics") and me.data.public_metrics:
                pm = me.data.public_metrics
                print(
                    f"  Followers: {pm.get('followers_count', '?')}  Following: {pm.get('following_count', '?')}  Tweets: {pm.get('tweet_count', '?')}"
                )
        else:
            print("\n  OAuth 1.0a: could not retrieve user profile.")
    else:
        print("\n  OAuth 1.0a: credentials incomplete — write operations unavailable.")

    app_client = _build_app_client()
    if app_client:
        print("  Bearer token: valid (read operations available)")
    else:
        print(
            "  Bearer token: NOT SET — read-only operations unavailable via app-only auth"
        )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        auth()
        return

    transport = "stdio"
    for i, arg in enumerate(sys.argv):
        if arg == "--transport" and i + 1 < len(sys.argv):
            transport = sys.argv[i + 1]
            break

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
