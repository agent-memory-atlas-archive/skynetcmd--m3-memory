# Remote MCP clients: Grok, OpenAI and others over `m3 serve`

`m3 serve` exposes your m3 memory as a remote MCP endpoint (streamable HTTP at
`/mcp`), protected by a bearer token. Any client that can reach a URL and send that
token can then read and write the same memory your local agents use.

**Set it up first:** [Claude.ai connector guide](claude_ai_connector.md) covers the
token (`m3 serve --generate-token`), a tunnel (Cloudflare, Tailscale Funnel, ngrok or
a reverse proxy), `--public-host`, and running `m3 serve` as a service. The recipes
below assume a public HTTPS URL such as `https://m3.example.com/mcp`.

> **The token is access to your whole memory.** Anyone holding it can read and write
> it. Keep `m3 serve` behind HTTPS, rotate the token if it leaks
> (`m3 serve --generate-token --force`), and remember that tools which delete or
> overwrite stay off unless `mcp.allow_destructive` is set.

## Which clients work

| Client | Status | How |
|---|---|---|
| **Claude.ai** (web, desktop) and the **Anthropic API** | Works | [Claude.ai connector guide](claude_ai_connector.md) |
| **xAI API** (Grok models) | **Verified 2026-10-18** | [below](#xai-api-grok) |
| **OpenAI API** (Responses API) | **Verified 2026-10-18** | [below](#openai-api-responses) |
| **ChatGPT** app custom connectors | Not supported yet | ChatGPT's connectors authenticate with OAuth only and cannot send a bearer token; `m3 serve` has no OAuth flow |
| **Grok** app connectors, **Grok Bot** | Not verified | they reach a public MCP URL; whether they can send a bearer token is unconfirmed |

Codex CLI, Claude Code, Gemini CLI and the other local agents do not need `m3 serve`:
`m3 setup` connects them directly ([client setup](MCP_CLIENT_INSTALL.md)).

## xAI API (Grok)

xAI's Responses API calls remote MCP servers itself. Pass the token as the full
`Authorization` value, with the `Bearer ` prefix written out:

```python
import httpx

r = httpx.post(
    "https://api.x.ai/v1/responses",
    headers={"Authorization": f"Bearer {XAI_API_KEY}"},
    json={
        "model": "grok-4.3",
        "input": "Search my memory for the database we chose.",
        "tools": [{
            "type": "mcp",
            "server_url": "https://m3.example.com/mcp",
            "server_label": "m3",
            "authorization": f"Bearer {M3_SERVE_TOKEN}",
        }],
    },
    timeout=300,
)
```

Measured: Grok called `memory_write` and `memory_search` against a test store, and the
written memory was confirmed in the store afterwards.

## OpenAI API (Responses)

OpenAI's Responses API takes the token as a request header:

```python
import httpx

r = httpx.post(
    "https://api.openai.com/v1/responses",
    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
    json={
        "model": "gpt-5-mini",
        "input": "Search my memory for the database we chose.",
        "tools": [{
            "type": "mcp",
            "server_label": "m3",
            "server_url": "https://m3.example.com/mcp",
            "headers": {"Authorization": f"Bearer {M3_SERVE_TOKEN}"},
            "require_approval": "never",
        }],
    },
    timeout=300,
)
```

Measured: the model listed m3's tools, wrote a memory and found it, along with one a
Grok session had written into the same store.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` | no token, or the wrong one |
| `421` | `--public-host` does not match the tunnel's hostname |
| A brand-new quick-tunnel URL does not resolve from your machine | a local DNS resolver that has not seen the fresh `*.trycloudflare.com` name yet; the client's servers (xAI, OpenAI) resolve it through public DNS |
| The client says the tool call failed | check `m3 serve`'s log; the request reached it only if a `POST /mcp` line appears |
