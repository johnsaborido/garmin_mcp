# Running this server on Vercel

This fork can run as a cloud MCP server on Vercel, so Claude can read Garmin
data from any chat (including phone) without a computer switched on.

## How it works

- `app.py` serves `garmin_mcp.cloud.app`, which is the normal server
  with every request handled on a stateless HTTP transport.
- The MCP endpoint is `https://<project>.vercel.app/mcp/<GARMIN_MCP_SECRET>`.
  Every other path returns 404, so the secret in the URL acts as the password.
  Treat the full URL like a password.
- `https://<project>.vercel.app/healthz` reports whether the Garmin login
  worked (`"login": "ready"`).
- Read-only by default: only `get_*` and `count_*` tools are exposed.

## 1. Garmin login token (once, on your computer)

```bash
uvx --python 3.12 --from git+https://github.com/johnsaborido/garmin_mcp garmin-mcp-auth
```

Enter your email, password and MFA code. This writes
`~/.garminconnect/garmin_tokens.json`. Turn it into one line of text:

macOS / Linux:
```bash
base64 < ~/.garminconnect/garmin_tokens.json | tr -d '\n'
```

Windows (PowerShell):
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.garminconnect\garmin_tokens.json"))
```

Copy the output. That is `GARMIN_TOKENS_B64`.

## 2. Secret for the URL

Any long random string, at least 24 characters. For example:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. Vercel project

1. Vercel → Add New → Project → import `johnsaborido/garmin_mcp`
   (branch `vercel-cloud` until it is merged to `main`). No framework preset.
2. Environment variables:

| Name | Value |
| --- | --- |
| `GARMIN_TOKENS_B64` | output of step 1 |
| `GARMIN_MCP_SECRET` | output of step 2 |
| `GARMIN_READ_ONLY` | `true` (default; `false` exposes write tools) |

3. Optional but recommended: Storage → add **Upstash Redis** (Marketplace) to
   the project. It sets `KV_REST_API_URL` / `KV_REST_API_TOKEN`; the server then
   saves refreshed Garmin tokens there so they survive cold starts.
4. Deploy, then open `/healthz`. `"login": "ready"` means it worked.
   `"failed"` usually means a bad token or Garmin blocking the cloud login.

## 4. Add to Claude

claude.ai → Settings → Connectors → Add custom connector → paste the full
`/mcp/<secret>` URL.

## Renewing

Garmin tokens last roughly 6 months. When `/healthz` shows `failed`, repeat
step 1, update `GARMIN_TOKENS_B64` and redeploy. A new token always replaces
any saved in Redis.
