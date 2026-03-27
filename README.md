<div align="center">
  <b>x-mcp-server</b>

  <p align="center">
    <i>Your AI Assistant's Gateway to X (Twitter)!</i>
  </p>

[![PyPI - Version](https://img.shields.io/pypi/v/x-mcp-server)](https://pypi.org/project/x-mcp-server/)
[![PyPI Downloads](https://static.pepy.tech/badge/x-mcp-server)](https://pepy.tech/projects/x-mcp-server)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
</div>

---

## What is this?

`x-mcp-server` is a Python-based MCP server that connects any MCP-compatible client (Claude Desktop, Cursor, Windsurf) to the X (Twitter) API v2. It lets you post tweets, search, like, retweet, follow users, send DMs, manage bookmarks, and more — all driven by AI through natural language.

---

## Quick Start

```bash
uvx x-mcp-server@latest
```

1. **Get X API credentials** from the [X Developer Portal](https://developer.x.com/)
2. **Install `uv`** if you haven't:
   ```bash
   # macOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   # Windows
   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. **Set up your `.env`** or environment variables (see [Authentication](#authentication))
4. **Check auth:**
   ```bash
   uvx x-mcp-server@latest auth
   ```
5. **Run the server:**
   ```bash
   uvx x-mcp-server@latest
   ```

---

## Key Features

- **27 Tools** covering tweets, search, users, DMs, bookmarks, lists, and threads
- **Dual Authentication:** Bearer token (read-only) + OAuth 1.0a (full read-write)
- **Pagination:** All list operations support cursor-based pagination
- **Media Upload:** Attach images/videos to tweets via v1.1 media upload
- **Thread Posting:** Post multi-tweet threads in a single call
- **Polls:** Create tweets with polls
- **Stdio & SSE Transports:** Works with Claude Desktop, Cursor, and remote deployments

---

## Authentication

### Environment Variables

Set these in a `.env` file or as environment variables:

**Read-only** (app-only / Bearer token):
```
BEARER_TOKEN=your-bearer-token
```

**Full read-write** (OAuth 1.0a user context):
```
CONSUMER_KEY=your-consumer-key
SECRET_KEY=your-secret-key
ACCESS_TOKEN=your-access-token
ACCESS_TOKEN_SECRET=your-access-token-secret
```

If you have all 5, the Bearer token is used for reads and OAuth 1.0a for writes. If you only have `BEARER_TOKEN`, read operations work but posting/liking/etc. will be unavailable.

### Getting Your Credentials

1. Go to the [X Developer Portal](https://developer.x.com/)
2. Create a project and app
3. Under **Keys and tokens**:
   - Copy **API Key** → `CONSUMER_KEY`
   - Copy **API Secret** → `SECRET_KEY`
   - Copy **Bearer Token** → `BEARER_TOKEN`
   - Under **Authentication Tokens**, generate **Access Token and Secret** → `ACCESS_TOKEN` + `ACCESS_TOKEN_SECRET`
4. Make sure your app has **Read and Write** permissions

### The `auth` Command

Verify your credentials are working:

```bash
uvx x-mcp-server@latest auth
```

Output:
```
X (Twitter) MCP — Auth check
  BEARER_TOKEN:       set
  CONSUMER_KEY:       set
  SECRET_KEY:         set
  ACCESS_TOKEN:       set
  ACCESS_TOKEN_SECRET: set

  Authenticated as: @yourusername (Your Name)
  Followers: 1234  Following: 567  Tweets: 890
  Bearer token: valid (read operations available)
```

---

## Available Tools (27 Total)

### Read Operations

- **`x_get_me`** — Get the authenticated user's profile
- **`x_get_user`** — Look up a user by username
  - `username`: Handle without @ (e.g. `"elonmusk"`)
- **`x_get_user_by_id`** — Look up a user by numeric ID
  - `user_id`: Numeric user ID
- **`x_get_tweet`** — Get a single tweet by ID with full content and metrics
  - `tweet_id`: Numeric tweet ID
- **`x_get_user_tweets`** — Get recent tweets by a user (5-100 per page)
  - `username`, `max_results` (default 10), `pagination_token`
- **`x_get_user_mentions`** — Get tweets mentioning a user (5-100 per page)
  - `username`, `max_results` (default 10), `pagination_token`
- **`x_search_tweets`** — Search recent tweets (last 7 days)
  - `query` (supports operators: `from:`, `#hashtag`, `is:reply`, `has:media`)
  - `max_results` (10-100), `sort_order` (`"recency"` or `"relevancy"`), `next_token`
- **`x_get_home_timeline`** — Get home timeline (requires OAuth 1.0a)
  - `max_results` (1-100, default 20), `pagination_token`
- **`x_get_followers`** — Get a user's followers (1-1000 per page)
  - `username`, `max_results` (default 20), `pagination_token`
- **`x_get_following`** — Get who a user follows (1-1000 per page)
  - `username`, `max_results` (default 20), `pagination_token`
- **`x_get_liked_tweets`** — Get tweets liked by a user (5-100 per page)
  - `username` (optional, defaults to authenticated user), `max_results`, `pagination_token`
- **`x_get_bookmarks`** — Get bookmarked tweets (requires OAuth 1.0a)
  - `max_results` (1-100, default 20), `pagination_token`
- **`x_get_owned_lists`** — Get lists owned by a user
  - `username` (optional, defaults to authenticated user)
- **`x_get_list_tweets`** — Get tweets from an X List (1-100 per page)
  - `list_id`, `max_results` (default 20), `pagination_token`

### Write Operations (require OAuth 1.0a)

- **`x_post_tweet`** — Post a new tweet
  - `text` (required), `reply_to_tweet_id`, `quote_tweet_id`, `media_paths`, `poll_options`, `poll_duration_minutes`
- **`x_delete_tweet`** — Delete your own tweet
  - `tweet_id`
- **`x_post_thread`** — Post a thread (series of connected tweets)
  - `tweets`: List of tweet texts in order
- **`x_like_tweet`** / **`x_unlike_tweet`** — Like or unlike a tweet
  - `tweet_id`
- **`x_retweet`** / **`x_unretweet`** — Retweet or undo retweet
  - `tweet_id`
- **`x_follow_user`** / **`x_unfollow_user`** — Follow or unfollow by username
  - `username`
- **`x_bookmark_tweet`** / **`x_remove_bookmark`** — Bookmark or unbookmark a tweet
  - `tweet_id`

### DM Operations (require OAuth 1.0a)

- **`x_send_dm`** — Send a direct message
  - `username`, `text`
- **`x_get_dm_events`** — Get recent DM events (1-100 per page)
  - `max_results` (default 20), `pagination_token`

---

## Usage with Claude Desktop

Add to your `claude_desktop_config.json`:

<details>
<summary>Config: uvx (Recommended)</summary>

```json
{
  "mcpServers": {
    "x": {
      "command": "uvx",
      "args": ["x-mcp-server@latest"],
      "env": {
        "BEARER_TOKEN": "your-bearer-token",
        "CONSUMER_KEY": "your-consumer-key",
        "SECRET_KEY": "your-secret-key",
        "ACCESS_TOKEN": "your-access-token",
        "ACCESS_TOKEN_SECRET": "your-access-token-secret"
      }
    }
  }
}
```

macOS note: If you get `spawn uvx ENOENT`, use the full path:
```json
"command": "/Users/yourusername/.local/bin/uvx"
```
</details>

<details>
<summary>Config: Development (from cloned repo)</summary>

```json
{
  "mcpServers": {
    "x": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/x-mcp", "x-mcp"]
    }
  }
}
```
</details>

---

## Usage with Cursor / Windsurf

```json
{
  "mcpServers": {
    "x": {
      "command": "uvx",
      "args": ["x-mcp-server@latest"],
      "env": {
        "BEARER_TOKEN": "your-bearer-token",
        "CONSUMER_KEY": "your-consumer-key",
        "SECRET_KEY": "your-secret-key",
        "ACCESS_TOKEN": "your-access-token",
        "ACCESS_TOKEN_SECRET": "your-access-token-secret"
      }
    }
  }
}
```

---

## SSE Transport (Remote / Container)

```bash
uv run x-mcp --transport sse
```

| Variable | Default | Description |
|:---------|:--------|:------------|
| `HOST` / `FASTMCP_HOST` | `0.0.0.0` | Bind address |
| `PORT` / `FASTMCP_PORT` | `8000` | Listen port |

---

## Environment Variables Reference

| Variable | Required | Description |
|:---------|:---------|:------------|
| `BEARER_TOKEN` | For reads | X API v2 Bearer Token |
| `CONSUMER_KEY` | For writes | Consumer Key (API Key) |
| `SECRET_KEY` | For writes | Secret Key (API Secret) |
| `ACCESS_TOKEN` | For writes | User Access Token |
| `ACCESS_TOKEN_SECRET` | For writes | User Access Token Secret |
| `HOST` / `FASTMCP_HOST` | No | SSE transport bind address (default `0.0.0.0`) |
| `PORT` / `FASTMCP_PORT` | No | SSE transport port (default `8000`) |

---

## Example Prompts for Claude

- "What are the latest tweets about #AI?"
- "Show me @elonmusk's recent tweets"
- "Post a tweet saying 'Hello from my AI assistant!'"
- "Like the latest tweet from @openai"
- "Search for tweets about machine learning from the past week"
- "Post a thread about the benefits of open source"
- "Who are my most recent followers?"
- "Send a DM to @friend saying 'Hey, let's catch up!'"
- "Bookmark this interesting tweet for later"
- "Show me my home timeline"

---

## Contributing

Contributions are welcome! Please open an issue to discuss bugs or feature requests. Pull requests are appreciated.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## Credits

- Built with [FastMCP](https://github.com/jlowin/fastmcp)
- Uses [Tweepy](https://github.com/tweepy/tweepy) for X API v2 access
- By [MindMade](https://mindmade.ai)
