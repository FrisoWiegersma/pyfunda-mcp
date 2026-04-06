# Funda MCP

Local stdio MCP server for `pyfunda`.

This folder exposes the stable `pyfunda` API as MCP tools for clients such as Codex and Claude Code.

For the underlying Python package and listing field details, see the main [pyfunda README](../README.md).

## Install

From the repository root:

```bash
.venv/bin/pip install -e ".[mcp]"
```

## Codex

Enable:

```bash
./funda-mcp/codex-on
```

Disable:

```bash
./funda-mcp/codex-off
```

Check:

```bash
codex mcp list
```

## Claude Code

Add this server:

```bash
claude mcp add --transport stdio funda -- /absolute/path/to/repo/funda-mcp/run
```

Remove it:

```bash
claude mcp remove funda
```

Check:

```bash
claude mcp list
```

## Other MCP Clients

Any MCP client that can launch a local stdio server can use:

```bash
./funda-mcp/run
```

Example config:

```json
{
  "mcpServers": {
    "funda": {
      "command": "/absolute/path/to/repo/funda-mcp/run",
      "cwd": "/absolute/path/to/repo"
    }
  }
}
```

## Tools

`get_listing`

- Input: `listing_id_or_url`
- Accepts a 7 to 9 digit listing ID or a full `funda.nl` detail URL
- Returns `{ "listing": { ... } }`

`search_listings`

- Input: location and optional filters such as `offering_type`, `price_max`, `area_min`, `sort`
- Default filters:
  - `offering_type = "buy"`
  - `availability = ["available", "negotiations"]`
  - `object_type = ["house", "apartment"]`
- Returns:

```json
{
  "total_count": 504,
  "returned_count": 15,
  "applied_filters": {
    "location": ["leiden"],
    "offering_type": "buy",
    "availability": ["available", "negotiations"],
    "object_type": ["house", "apartment"],
    "page": 0
  },
  "results": []
}
```

`get_latest_id`

- Returns `{ "latest_id": 7852306 }`

`poll_new_listings`

- Input: `since_id` and optional bounds
- Returns a bounded list plus `last_seen_id`

`get_price_history`

- Input: `listing_id_or_url`
- Returns `{ "count": n, "changes": [ ... ] }`

## Future Ideas

- Saved search profiles for specific places and filter sets
- Periodic polling in Docker
- Notifications when matching listings appear
- A small interface for managing profiles and viewing matches

## Notes

- This is a local stdio MCP server, not an interactive terminal app.
- Codex and Claude Code can launch it directly.
- Anthropic's Messages API MCP connector requires a public HTTP or SSE server, so this local stdio server is not a direct fit for that path.
- The upstream Funda API is unofficial and can change without notice.
