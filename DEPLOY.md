# Deploying the Dispatch MCP Server to AWS Lambda

This is a HTTP-mode deployment of the dispatch MCP server. The stdio
entry point (`main.py`) is unchanged and still works for local desktop
clients; for Lambda we use `lambda_app.py`, which exposes the same MCP
tools over **MCP streamable HTTP** at `<function-url>/mcp`.

Architecture:

```
Client (Claude Desktop / VS Code / curl)
    │
    │  HTTPS POST /mcp
    │  Authorization: Bearer <MCP_AUTH_TOKEN>
    ▼
Lambda Function URL  (BUFFERED, AuthType=NONE)
    │
    ▼
Mangum (ASGI adapter)
    │
    ▼
Starlette app
    │   • BearerTokenAuthMiddleware
    │   • FastMCP.streamable_http_app()
    ▼
Existing DispatchManager / SheetsManager
    │
    ▼
Google Sheets API
```

Why a container image: `reportlab` + `google-api-python-client` +
`pydantic-core` together push past the 250 MB unzipped Lambda zip limit,
and Lambda container images go up to 10 GB.

---

## Prerequisites

Install once on your machine:

| Tool       | Why                                            | Install                                                   |
|------------|------------------------------------------------|-----------------------------------------------------------|
| AWS CLI v2 | Talk to AWS, configure your credentials        | <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html> |
| SAM CLI    | Build & deploy the container + Lambda + URL    | <https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html> |
| Docker     | Build the container image SAM ships to ECR     | Docker Desktop on Windows                                 |

Configure your AWS credentials:

```powershell
aws configure
# AWS Access Key ID:     ...
# AWS Secret Access Key: ...
# Default region name:   ap-south-1
# Default output format: json
```

Verify:

```powershell
aws sts get-caller-identity
```

---

## One-time setup: get the values you'll pass to SAM

You need three secrets:

### 1. `GoogleCredentialsJson`

Take the value already in `.env` (the `GOOGLE_CREDENTIALS_JSON=` line) and
copy everything **after the `=`** to a temp file or env var. It must be a
single line — newlines inside the JSON should already be escaped as `\n`.

Quick way to grab it from `.env` in PowerShell:

```powershell
$creds = (Select-String -Path .env -Pattern '^GOOGLE_CREDENTIALS_JSON=').Line `
         -replace '^GOOGLE_CREDENTIALS_JSON=',''
```

### 2. `SpreadsheetId`

The Sheet ID this server should default to. Same value as `SPREADSHEET_ID`
in `.env`. Leave blank to require it per-request:

```powershell
$sheetId = "1OTYdnrb29AOPkNVaEfkKdG93Tplj9YdwDSbP7-mYlwg"
```

### 3. `McpAuthToken` — generate a fresh long random secret

```powershell
$token = -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) })
Write-Host "MCP_AUTH_TOKEN = $token"
# SAVE THIS. Clients must send it on every request.
```

---

## Deploy

From the repo root (`e:\CANDOR\DISPATCH\server`):

```powershell
# Build the container image. Docker must be running.
sam build

# First deploy — walks you through stack creation.
sam deploy --guided `
  --parameter-overrides `
    "GoogleCredentialsJson=$creds" `
    "SpreadsheetId=$sheetId" `
    "McpAuthToken=$token"
```

During the guided prompt:

| Prompt                                   | Answer                              |
|------------------------------------------|-------------------------------------|
| Stack Name                               | `dispatch-mcp-server`               |
| AWS Region                               | `ap-south-1` (or your choice)       |
| Confirm changes before deploy            | `Y` (recommended for first deploy)  |
| Allow SAM CLI IAM role creation          | `Y`                                 |
| `DispatchMcpFunction` has no auth        | `Y` (auth is inside the function)   |
| Save arguments to samconfig.toml         | `Y`                                 |

Subsequent deploys can just use:

```powershell
sam build && sam deploy --parameter-overrides "..."
```

When the deploy finishes, the **Outputs** section will print:

```
Key                  FunctionUrl
Description          Public HTTPS URL — append /mcp to reach the JSON-RPC endpoint.
Value                https://<random>.lambda-url.ap-south-1.on.aws/
```

That URL is your MCP server.

---

## Test from the command line

```powershell
$base = "https://<random>.lambda-url.ap-south-1.on.aws"
$headers = @{
    "Authorization" = "Bearer $token"
    "Accept"        = "application/json, text/event-stream"
    "Content-Type"  = "application/json"
}

# 1. initialize — handshake
$init = @{
    jsonrpc = "2.0"
    id      = 1
    method  = "initialize"
    params  = @{
        protocolVersion = "2024-11-05"
        capabilities    = @{}
        clientInfo      = @{ name = "curl-test"; version = "0.1" }
    }
} | ConvertTo-Json -Depth 10 -Compress

Invoke-RestMethod -Method POST -Uri "$base/mcp" -Headers $headers -Body $init

# 2. tools/list — see what's exposed
$listTools = @{
    jsonrpc = "2.0"
    id      = 2
    method  = "tools/list"
    params  = @{}
} | ConvertTo-Json -Depth 10 -Compress

Invoke-RestMethod -Method POST -Uri "$base/mcp" -Headers $headers -Body $listTools
```

A 401 means the bearer token didn't match. A 200 with a JSON-RPC payload
means the server is alive and reachable.

---

## Connect Claude Desktop / VS Code

Add the deployed URL to your MCP client's config. Example for
**Claude Desktop** (`~/AppData/Roaming/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dispatch-remote": {
      "transport": "http",
      "url": "https://<random>.lambda-url.ap-south-1.on.aws/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

Restart the client. The dispatch tools should appear alongside any local
MCP servers.

---

## Iterating after the first deploy

You only need three commands after editing code:

```powershell
sam build
sam deploy
```

Parameters are saved in `samconfig.toml`, so no need to repeat them
unless you rotate the auth token or change the sheet ID.

---

## Observability

```powershell
# Tail recent logs (last 5 minutes)
sam logs -n DispatchMcpFunction --tail

# Or via AWS CLI
aws logs tail "/aws/lambda/dispatch-mcp-server-DispatchMcpFunction-<suffix>" --follow
```

Cold-start time on arm64 / 1024 MB is typically 3–5 seconds because the
Google SDK and reportlab load at module import. Warm invocations return
in well under 1 second for read-only tools.

---

## Teardown

```powershell
sam delete --stack-name dispatch-mcp-server
```

This removes the Lambda, its Function URL, the IAM role SAM created, the
ECR repo, and all CloudWatch log groups.

---

## Common failure modes

| Symptom                                              | Cause / fix                                                                                            |
|------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| `sam build` fails with "Docker daemon not running"   | Start Docker Desktop.                                                                                  |
| `sam deploy` hangs on ECR push                       | First push uploads ~250 MB. Subsequent pushes are layer-cached and fast.                              |
| `401 unauthorized` from the function URL             | Wrong/missing `Authorization: Bearer <token>` header. Check the token saved during `sam deploy`.       |
| `403 Forbidden` from Google in CloudWatch logs       | SA doesn't have Editor on the Sheet. Share the Sheet with `service_account_email` logged at startup.   |
| `Self-test skipped` warnings                         | Expected. Self-test only runs in stdio mode; first real tool call surfaces credential issues instead.  |
| Cold-start over 10 s                                 | Bump `MemorySize` to 2048 in `template.yaml`. CPU scales with memory on Lambda.                        |


## Postgres mirror (optional)

The server can mirror every sheet write to an AWS RDS Postgres instance
(`billed_details` + `billed_articles` tables). Set:

    DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<dbname>

in your environment. SSL is required (RDS forces it); the asyncpg pool sets
`ssl="require"` automatically.

If `DATABASE_URL` is empty the mirror is disabled and the server behaves
exactly as before — every dual-write path is a no-op.

### Failure semantics

Sheet-first, DB best-effort. The sheet write blocks the MCP response; if the
DB mirror raises (network blip, connection limit, schema drift), the failure
is logged as a `WARNING` and the request still returns success. A reconcile
job (out of scope) can later sweep up missing DB rows by reading the sheet.

### Soft-delete

`dispatch_delete_invoice` removes the rows from the sheet AND sets
`deleted_at = now()` on the corresponding DB rows. MCP clients cannot hard-delete
from the DB; that's intentional. Reads always go through the views
`v_billed_details` / `v_billed_articles` which filter out soft-deleted rows.

### Schema migrations

The DDL lives in `db/migrations/`. On a fresh database, run the migrations
once before starting the server:

    psql "$DATABASE_URL" -f db/migrations/001_initial.sql
    psql "$DATABASE_URL" -f db/migrations/002_soft_delete.sql

For an existing database that already has the tables (e.g. created manually,
as in the live AWS RDS instance), the `db/migrations/run.py` runner can be
invoked with `pretend_applied=["001_initial.sql", "002_soft_delete.sql"]` so
it records them as applied without re-running.

### Backfilling existing sheet data

After enabling the mirror, copy historical sheet rows into the DB:

1. Restart the server with `DATABASE_URL` set.
2. From the MCP client, call `dispatch_db_backfill_from_sheet` with
   `dry_run=true` to preview the per-tab counts.
3. Re-call with `dry_run=false` to commit the inserts.
4. The tool is idempotent — invoices already present (by route + invoice_no)
   are skipped on re-runs.

### Article-to-SKU fuzzy matching

Every article row written to `billed_articles` is matched against the
`all_sku` master table (`particulars` column) via rapidfuzz token_set_ratio
with a default threshold of 80. Matches populate seven `matched_*` columns
(`matched_sku_id`, `matched_item_type`, `matched_item_group`,
`matched_sub_group`, `matched_uom`, `matched_gst`, `matched_sale_group`).

The matcher loads all `all_sku` rows into memory once at server startup —
restart the server to pick up changes to the SKU master.

Unmatched articles still insert successfully, with the `matched_*` columns
NULL. There is no automatic alert for unmatched articles; query
`SELECT article, COUNT(*) FROM v_billed_articles WHERE matched_sku_id IS NULL
GROUP BY article ORDER BY 2 DESC` to find them.
