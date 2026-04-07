# pyfunda

Local MCP server and Python client for Funda.

Forked from [0xMH/pyfunda](https://github.com/0xMH/pyfunda) and extended with a local MCP server plus Kadaster WOZ tools.

## MCP

This repo is mainly set up to run as a local stdio MCP server for Codex, Claude Code, or any other MCP client that can launch a local command.

Install from the repo root:

```bash
.venv/bin/pip install -e ".[mcp]"
```

Run the server:

```bash
./funda-mcp/run
```

Codex:

```bash
./funda-mcp/codex-on
codex mcp list
```

Claude Code:

```bash
claude mcp add --transport stdio funda -- /absolute/path/to/repo/funda-mcp/run
claude mcp list
```

## Tools

- `get_listing`: fetch one Funda listing by ID or full URL
- `search_listings`: search listings with filters; resolves common city aliases, postcodes, and some neighbourhood inputs
- `get_latest_id`: fetch the highest listing ID currently visible in search
- `poll_new_listings`: poll for new listings above a known global ID
- `get_price_history`: fetch historical price changes, with WOZ enrichment when available
- `get_woz_history`: fetch Kadaster WOZ history for one exact address
- `calculate_growth_roi`: calculate WOZ-based growth metrics
- `calculate_gross_yield`: calculate gross rental yield with WOZ context

## Python

If you only want the Python client:

```bash
pip install pyfunda
```

```python
from funda import Funda

f = Funda()

listing = f.get_listing(43117443)
results = f.search_listing("amsterdam", price_max=500000)
history = f.get_price_history(listing)
```

## About

This project uses Funda's unofficial API and Kadaster's public WOZ Waardeloket for single-address lookups.
