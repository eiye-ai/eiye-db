# eiye_db — Semantic Surface for AI-Ready Organizations

## Overview

eiye_db provides a **semantic surface** that helps organizations safely expose their data to AI agents. It solves the core problem: **data discovery → safe agent connection → governance & standard format**.

Organizations have data scattered across dozens of systems (databases, file stores, APIs, email, CRM, etc.). Agents need access to all of it without gaps (which cause hallucination) and without exposing PII or sensitive data. eiye_db bridges that gap.

## Core Value Proposition

- **Unified datasource registry** — discover what data exists across your org
- **Safe agent connection** — PII detection/detraction, access control, audit trails
- **Standard format layer** — map heterogeneous sources to a common semantic model
- **MCP server exposure** — agents query via standard MCP protocol
- **Natural language interface** — orchestration agent manages all datasources

## Architecture Layers

```
+-------------------------------------------------------+
|                    UI Layer                            |
|  React Dashboard (config, monitoring, deployment)      |
+--------------------------------------------------------+
|              Interface Layer                           |
|  REST API  |  MCP Servers  |  Natural Language          |
+--------------------------------------------------------+
|            Orchestration Layer                         |
|  Strands-Agents + OpenPlanter Engine                   |
|  Multi-turn context persistence across datasources     |
+--------------------------------------------------------+
|           Semantic Surface Layer                       |
|  Datasource Registry  |  Schema Discovery              |
|  PII Detection        |  Access Control (ABAC/RBAC)    |
|  Cache Proxy          |  Format Mapping                |
+--------------------------------------------------------+
|           Datasource Connectors                        |
|  SQL | NoSQL | CSV | PDF | Word | Email | CRM         |
|  | ERP | Repos | Logs | MCP | Web Search | Cloud       |
+--------------------------------------------------------+
```

## Quick Start (5 minutes)

**Fastest path — one command against your own data:**

```bash
scripts/quickstart.sh --name mydata --type filesystem --root /path/to/a/folder
#   or:  --type postgresql --dsn postgresql://user:pass@host/db
#   or:  --type rest_api    --url https://api.example.com
```

It sets up the venv, registers the source, discovers its schema, runs one governed
(PII-redacted, audited) query, and prints the command to connect an agent over MCP.
The manual steps below do the same thing by hand if you'd rather drive the API.

### 1. Run the API

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e .          # installs the package + deps; -e so `python -m eiye_db.mcp_server` resolves from any working directory (the MCP host spawns it from an arbitrary cwd)
uvicorn eiye_db.main:app --reload
```

> Use `pip install -e .` rather than `pip install -r requirements.txt`: the
> editable install puts the `eiye_db` package on the path so the MCP server
> launches from any directory, and it reads dependencies from `pyproject.toml`
> (the single source of truth).

With **no keys set**, the API runs in open dev mode and the curls below work as
written. To secure it, set **both** `EIYE_API_KEY=<key>` and
`EIYE_ADMIN_API_KEY=<admin-key>`, then add `-H "X-API-Key: $EIYE_API_KEY"` to
every request below. Setting only one is refused at boot: a half-configured
service looks secured and isn't, so it fails loudly instead of serving.

The two keys map onto two surfaces:

| surface | key | serves |
|---|---|---|
| `/api/v1/datasources` | admin only | raw registrations, `config` included (Postgres DSNs, REST auth headers); create, update, delete, test |
| `/api/v1/surface/sources` | any valid key | policy-filtered listing, `config` omitted |

The admin key also gates unredacted PII, the audit log, policy management, and
every curation step (approving relationships and metrics). So with keys set, the
registration curls in step 2 take `$EIYE_ADMIN_API_KEY`; querying takes either.

### 2. Register the demo datasource and query it

```bash
# Register the bundled demo CSVs as a filesystem datasource
curl -s -X POST localhost:8000/api/v1/datasources \
  -H 'Content-Type: application/json' \
  -d '{"name": "demo", "type": "filesystem", "config": {"root": "'$PWD'/../examples/demo_data"}}'

# Grab the id from the response, then:
curl -s -X POST localhost:8000/api/v1/datasources/<id>/test      # connection check
curl -s -X POST localhost:8000/api/v1/datasources/<id>/discover  # schema discovery
curl -s -X POST localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"datasource_id": "<id>", "request": {"path": "customers.csv"}}'
```

The query response comes back with emails and phone numbers already redacted
(`[REDACTED:email]`, `[REDACTED:phone]`) and the access recorded in
`GET /api/v1/audit`.

The filesystem connector reads **CSV, text, PDF, and XLSX** — query a document
the same way (`{"path": "receipt.pdf"}` or `{"path": "report.xlsx"}`); the
extracted text/rows flow through the identical redaction + audit path. Baseline
redaction is regex (email, phone, SSN, credit card, IPv4). To also redact
**names and locations**, enable the optional spaCy NER layer:

```bash
pip install -e ".[ner]"
python -m spacy download en_core_web_sm
export EIYE_PII_NER_ENABLED=true          # off by default; when on, the model must load (fails loud, never silently fail-open)
```

### 3. Connect an agent via MCP

```bash
# Claude Code
claude mcp add eiye-db --env EIYE_KEY_ID=support-agent \
  -- /path/to/backend/.venv/bin/python -m eiye_db.mcp_server
```

Or in Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "eiye-db": {
      "command": "/path/to/backend/.venv/bin/python",
      "args": ["-m", "eiye_db.mcp_server"],
      "env": { "EIYE_KEY_ID": "support-agent" }
    }
  }
}
```

`EIYE_KEY_ID` (default `mcp-stdio`) names the principal the server runs as: its
ABAC subject and its identity in the audit trail. Give each agent its own so
policies can target it and the trail can tell them apart. It is **not a
credential** — any local process can claim any id — which is acceptable only
because stdio MCP already trusts whoever spawned the process. Do not treat it
as authentication.

The agent gets nine tools — `list_datasources`, `get_schema`, `query_datasource`,
`list_metrics`, `query_metric`, `resolve_entities`, `ask`, and the draft-only
`propose_relationship` / `propose_metric` — all read-only, PII-redacted, and
audited.

`get_schema` also returns **relationships** (joins): `"approved"` ones are
governed ground truth (e.g. real foreign keys); `"candidate"` ones are detected
guesses awaiting human review (`POST /api/v1/semantic/detect` to detect,
`PUT /api/v1/semantic/relationships/{id}` with the admin key to approve/reject).
Datasources discovered before this feature show none until re-discovered
(`POST /api/v1/datasources/{id}/discover`).

**Note:** the API and the MCP server share the registry via
`EIYE_DATABASE_URL` (default `sqlite:///./eiye.db`, relative to the working
directory). Since MCP hosts spawn the server from an arbitrary cwd, set an
absolute path for both processes, e.g. add
`"env": {"EIYE_DATABASE_URL": "sqlite:////absolute/path/to/eiye.db"}` to the
MCP server config and export the same for uvicorn.

### 4. Manage datasources in a browser (web UI)

A React dashboard lets you add, edit, and delete datasources, test connections,
discover schemas, and run governed (PII-redacted) queries — no curl required.

```bash
# 1. Start the backend (from backend/, as in step 1) — it now allows the dev UI via CORS
uvicorn eiye_db.main:app --reload

# 2. In a second terminal, start the frontend
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The dev server proxies `/api` to the backend on
`localhost:8000`, so no extra configuration is needed. Use **+ New** to register a
datasource (filesystem / PostgreSQL / REST API), then **Test connection**,
**Discover schema**, and **Run query** to see redacted results.

- Backend on a different host/port? Set `VITE_PROXY_TARGET` (proxy) or
  `VITE_API_BASE` (direct, e.g. `http://host:8000/api/v1`) before `npm run dev`,
  and add that browser origin to `EIYE_CORS_ORIGINS` (comma-separated) for the backend.
- If the backend has keys set, paste the **admin** key into the field in the UI
  header: the dashboard drives the operator surface (registrations, curation).
  Since admins bypass ABAC, handing out UI access hands out full governance
  authority — treat it as an operator console, not an agent-facing app.

## Datasource Connectors

| Connector | Type | Status |
|-----------|------|--------|
| PostgreSQL | SQL DB | ✅ Available (read-only transactions) |
| File System | Files (CSV, text, PDF, XLSX) | ✅ Available (root-scoped, schema inference, PII-redacted) |
| REST API | HTTP API | ✅ Available (GET-only, OpenAPI discovery) |
| MySQL | SQL DB | Planned |
| MongoDB | NoSQL | Planned |
| Google Drive | Cloud Storage | Planned |
| Email (IMAP) | Email | Planned |
| GitHub/GitLab | Repositories | Planned |

## Pricing Tiers

| Tier | Datasources | Queries/mo | Price |
|------|-------------|------------|-------|
| Free | Up to 5 | 1,000 | $0 |
| Starter | Up to 15 | 25,000 | $99/mo |
| Pro | Up to 50 | 250,000 | $499/mo |
| Business | Up to 150 | 1M | $1,999/mo |
| Enterprise | Unlimited | Custom | Contact sales |

The Free tier is the Additional Use Grant in the [license](LICENSE), not a trial
that expires. See GOALS.md for the licensing model and tier detail.

## Competitive Landscape

| Competitor | Focus | Gap vs eiye_db |
|-----------|-------|----------------|
| Databricks Unity Catalog | Data governance at scale | No agent-native interface |
| Collibra | Enterprise data catalog | Heavy, no semantic surface |
| Airbyte | Data integration | No PII/governance built-in |
| LangChain/LlamaIndex | Agent frameworks | No datasource management |
| MCP (Anthropic) | Protocol standard | No orchestration layer |
| Weaviate/Pinecone | Vector DBs | Storage only, no discovery |
| **eiye_db** | **Semantic surface for agents** | **Full stack: discovery + governance + access** |

## License

**Source-available, not open source.** eiye_db is licensed under the
[Business Source License 1.1](LICENSE). The full source is public and auditable —
which matters for a governance control you are being asked to trust — but BSL is
not an OSI-approved open source license, and we don't describe it as one.

What the license grants you free of charge:

- Copy, modify, redistribute, and use non-productively (evaluation, development, testing)
- **Production use up to 5 registered datasources and 1,000 queries per calendar month**

What requires a commercial license:

- Production use beyond those limits (see the pricing tiers in `GOALS.md`)
- Offering eiye_db to third parties as a hosted or managed service
- Enterprise features: SSO, compliance reports, multi-tenant control plane, advanced analytics

Each released version converts to **Apache-2.0 four years after publication**
(Change Date `2030-08-30` for the current version), so nothing is permanently
enclosed. Commercial licensing: max.goff@gmail.com.
