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

With **no keys set at all**, the API runs in open dev mode and the curls below
work as written. To secure it, set **both** `EIYE_API_KEY=<key>` and
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

#### Named keys: one identity per agent

`EIYE_API_KEY` resolves **every** HTTP caller to the single subject `primary`.
That is fine for one agent and wrong for several: a policy naming `subjects`
cannot tell them apart, and neither can the audit trail. Give each agent its own
key instead:

```bash
python scripts/mint_key.py --id support-agent
python scripts/mint_key.py --id ops --admin --expires 2027-01-01
```

Each run prints the secret **once** and the map entry to keep. Collect the
entries into one JSON object:

```bash
export EIYE_API_KEYS='{
  "support-agent": {"sha256": "<hex>", "is_admin": false},
  "ops":           {"sha256": "<hex>", "is_admin": true, "expires_at": "2027-01-01"}
}'
```

The id becomes the ABAC subject and the audit principal, so
`"subjects": ["support-agent"]` in a policy now means that one agent. Only the
digest is stored, so the config leaks no working credential. `is_admin` grants
the admin surface described above, and `expires_at` is optional; an expired key
gets a 401 that says so rather than one that reads like a typo.

The map coexists with the two settings above, which keep their reserved ids
`primary` and `admin` — existing policies naming those subjects go on working.
Three rules are enforced at boot, not discovered in production: the map alone
counts as a configured deployment (it does **not** leave you in open dev mode),
it may not reuse a reserved id while that setting is live, and it must contain
an admin unless `EIYE_ADMIN_API_KEY` supplies one. Keys are read at startup, so
adding or revoking one takes a restart.

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
as authentication. This is the one real difference from the named keys above:
over HTTP an id is *proved* by presenting its secret, over stdio it is merely
*asserted*. Both are ABAC subjects and both land in the audit trail; only one is
evidence of who was calling.

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
datasource (filesystem / PostgreSQL / MySQL / SQL Server / SQLite / S3 / REST API), then **Test connection**,
**Discover schema**, and **Run query** to see redacted results.

- Backend on a different host/port? Set `VITE_PROXY_TARGET` (proxy) or
  `VITE_API_BASE` (direct, e.g. `http://host:8000/api/v1`) before `npm run dev`,
  and add that browser origin to `EIYE_CORS_ORIGINS` (comma-separated) for the backend.
- If the backend has keys set, paste the **admin** key into the field in the UI
  header: the dashboard drives the operator surface (registrations, curation,
  access). Since admins bypass ABAC, handing out UI access hands out full
  governance authority — treat it as an operator console, not an agent-facing app.
- The **Access** tab reviews what one subject can reach, grants it read and
  discover on a source or on everything, and lists and deletes policies. It is
  the console equivalent of `scripts/grant.py` and the access review below, and
  writes policies under the same names, so the two do not leave a deployment
  with two conventions.

## Access control (ABAC)

Policies say which subjects may reach which sources, and which columns are
masked out of the results. A subject is a key id: an `EIYE_API_KEYS` entry, the
reserved ids `primary` and `admin`, or whatever an MCP client puts in
`EIYE_KEY_ID`. Admins bypass policy entirely, because they are the ones who
write it.

Evaluation order is fixed and does not depend on which policy was created
first: **explicit deny beats explicit allow beats the default.** A deny with
`conditions.columns` masks those columns instead of blocking the source, and
masks from every matching policy accumulate. Masking is applied before PII
redaction and disclosed in result lineage.

### Two postures

**Allow-by-default (the default).** A fresh install works with no policies, and
you add denies to carve access down. Right for a single trusted agent and for
evaluating the product.

**Default-deny**, set with `EIYE_ABAC_DEFAULT_DENY=true`. Every non-admin
subject is denied on every source until an allow policy names it, so a new
agent starts with nothing and gets exactly what you grant. Right when several
agents share a deployment, or when a source should never be reachable by
accident.

### Running default-deny

Grant access with `scripts/grant.py`, which resolves sources by name and writes
the policy for you:

```bash
python scripts/grant.py --subject support-agent --source customers \
  --api-key "$EIYE_ADMIN_API_KEY"

# or every source, present and future
python scripts/grant.py --subject support-agent --all-sources \
  --api-key "$EIYE_ADMIN_API_KEY"
```

Re-running is safe: a grant whose policy name already exists is reported and
skipped rather than widened. To remove one, take the policy id it printed and
`DELETE /api/v1/policies/<id>`.

The web console's **Access** tab does the same thing with a form, and writes
the same policy names. Use whichever fits; a deployment can mix them.

Then check what a subject can actually reach. This is the part that makes the
posture operable: a denied caller gets a deliberately generic message, so from
the outside a missing allow and an explicit deny look identical.

```bash
curl -s localhost:8000/api/v1/access/support-agent -H "X-API-Key: $EIYE_ADMIN_API_KEY"
```

```json
{
  "key_id": "support-agent",
  "credential": "EIYE_API_KEYS",
  "is_admin": false,
  "default_deny": true,
  "dev_mode": false,
  "datasources": [
    {"datasource_id": "...", "name": "customers", "read": true, "discover": true, "masked_columns": ["ssn"]}
  ]
}
```

`credential` says which setting configures the subject, so a mistyped key id
reads as `"none"` rather than as a real agent that lost its access. The review
is computed by the same functions that enforce access, so it cannot drift from
what a caller will actually get.

Two things to know before turning it on:

- **Your MCP agents need grants too.** The MCP setup above gives each agent its
  own `EIYE_KEY_ID`, and under default-deny each of those is a subject with no
  access until you grant it. Granting `mcp-stdio` alone is not enough if your
  clients set their own ids.
- **Starting with no allow policy denies every non-admin caller.** The server
  logs a warning at boot when that is the state. It warns rather than refusing
  to start, because policies are created through the API and a server that
  refused to run without one could not be used to write the first one. Admin
  keys keep working throughout, so it is always recoverable.

Example policies, including column masking, per-source grants and the named-key
shape, are in `examples/policies/`; `scripts/seed_example_policies.py` loads
them. They are shape references rather than the recommended path — a policy
carrying an unsubstituted placeholder is skipped rather than seeded, because a
subject that matches nothing reads as configured and is not.

## Datasource Connectors

| Connector | Type | Read-only claim | Status |
|-----------|------|-----------------|--------|
| PostgreSQL | SQL DB | server-enforced | ✅ Available (requires a read-only login; read-only transactions — see below) |
| MySQL / MariaDB | SQL DB | server-enforced | ✅ Available (requires a login with no write privileges — see below) |
| SQL Server | SQL DB | server-enforced | ✅ Available (SQL auth; requires a login with no write permissions — see below) |
| SQLite | SQL DB (file) | server-enforced | ✅ Available (opened `mode=ro` + `PRAGMA query_only`; no driver to install) |
| Oracle | SQL DB | server-enforced | ✅ Available (requires a read-only login; no Instant Client — see below) |
| File System | Files (CSV, text, PDF, XLSX) | structural | ✅ Available (root-scoped, schema inference, PII-redacted) |
| S3 / MinIO | Object storage (CSV, text, PDF, XLSX) | structural | ✅ Available (prefix-scoped, list + get only — see below) |
| REST API | HTTP API | structural | ✅ Available (GET-only, OpenAPI discovery) |
| Confluence | Wiki (Cloud) | structural | ✅ Available (operator API token, space-scoped, GET-only — see below) |
| Jira | Issue tracker (Cloud) | structural | ✅ Available (operator API token, project-scoped, GET-only — see below) |
| ServiceNow | ITSM | structural | ✅ Available (instance credentials, explicit table allowlist, GET-only — see below) |
| SharePoint / OneDrive | Document library (CSV, text, PDF, XLSX) | structural | ✅ Available (customer Entra app, `*.Selected` scope required, library- and folder-scoped — **item-level ACLs are not applied**, see below) |

### The two read-only claims, and why they are not the same claim

Every connector above is read-only. They are not read-only in the same way, and
collapsing that into one badge would be the more comfortable lie.

**Server-enforced.** The database itself refuses to write, whatever eiye sends
it. The connector verifies at every connect that the login it was given cannot
write, and the mechanism differs per engine because the engines differ — a
Postgres read-only transaction covers DDL, MySQL's does not, SQL Server has none
at all, and Oracle's would block nothing the query wrapper does not already.
Each engine's mechanism is exercised against a real server in CI and
mutation-tested: the guard is removed, and the suite has to go red.

**Structural.** There is no server to refuse anything — a GET-only HTTP client,
an S3 credential, a directory of files. The guarantee is that the connector
never asks to write. That is a weaker claim than the one above and it is
labelled differently for that reason.

It is not, however, an unverified claim. Each structural connector's whole test
suite runs behind a guard sitting at the boundary it actually crosses:

| connector | boundary | guard |
|-----------|----------|-------|
| REST API | HTTP transport | any method other than GET or HEAD fails the test |
| S3 / MinIO | botocore `before-call` | any operation outside `ListObjectsV2` / `GetObject` fails the test, before the request leaves the process |
| File System | `open()` and `Path.open()` | any mode requesting write access fails the test |
| Confluence · Jira · ServiceNow | HTTP transport | any method other than GET or HEAD fails the test |
| SharePoint | HTTP transport | as above, except POST to `login.microsoftonline.com` — see the note below |

Add a `POST`, a `PutObject` or an `open(..., "w")` anywhere on an exercised path
and the build breaks.

SharePoint has the one exception, and it is bounded rather than waived. OAuth2
client credentials are fetched with a POST, so that connector cannot be
literally GET-only. The alternative — giving the token client its own unguarded
transport — would have made the claim untestable, because the guard would no
longer see every request the connector makes. Instead the guard allows POST to
exactly `login.microsoftonline.com`, records the URLs, and the suite asserts the
only POST that happened was that one token request. A POST to Graph fails the
build. The guards are themselves mutation-tested — each one has
a real write introduced into the connector to confirm it catches it — and they
raise outside the exception hierarchy the connectors catch, so the connectors'
own error handling cannot absorb them.

What the structural tier does **not** prove is a path the tests never take. A
branch with no coverage could still hide a write. It is a coverage-bounded
proof, which is a long way past "read the code and trust it" and still short of
a server refusing you. Where the honest answer is "we cannot check this", it is
said plainly — see the S3 section on why the credential itself is not probed.

Drivers for connectors beyond the original three ship as extras, so a deployment
installs only what it connects to:

```bash
pip install -e "backend[mysql]"      # also: [mssql], [s3]
```

SQLite needs no extra — `sqlite3` is in the standard library.

### PostgreSQL needs a read-only login too

This connector shipped first, and for a long time its read-only claim rested on
`BEGIN READ ONLY` alone. Testing it against a live server showed that is not
enough, so it now requires a login that cannot write, checked on every connect.

The read-only transaction is genuinely strong — it rejects every INSERT, UPDATE,
DELETE, CREATE, DROP, TRUNCATE and GRANT, along with `nextval`, `SELECT INTO`
and writes performed inside a function. Two things get past it, both verified
against PostgreSQL 16:

- **`COPY ... TO PROGRAM`** runs a shell command on the database server, inside
  a read-only transaction. Only the connector's query wrapper stops it.
- **`dblink`** opens a *second* session, which the transaction — scoped to the
  first — never covers. As a superuser, `SELECT dblink_exec(...)` performs a
  real INSERT through the ordinary query path, as a statement that is
  syntactically a plain SELECT.

Both need privileges an ordinary reader does not have, which is the point: the
login is the boundary here exactly as it is on the other engines.

```sql
CREATE ROLE eiye LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE mydb TO eiye;
GRANT USAGE ON SCHEMA public TO eiye;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO eiye;
```

Register it with `postgresql://eiye:...@host:5432/mydb`. A **superuser DSN is
refused outright**, so if you have been pointing eiye at the `postgres` account,
this is a breaking change and the grants above are the fix. The check reads
effective privileges, so a write held through a role, through `PUBLIC`, through
`pg_write_all_data`, or on a view over the table is caught the same way.

### MySQL / MariaDB needs a read-only login

Postgres gives us a server-enforced read-only transaction that also covers DDL
(though not everything — see below). **MySQL does not.**
`START TRANSACTION READ ONLY` rejects INSERT/UPDATE/DELETE, but
CREATE, DROP, TRUNCATE and GRANT all execute inside one — verified against MySQL 8.4
and MariaDB 11.8. So the connector refuses to connect at all unless the login it is
given provably cannot write, and it re-checks on every connection:

```sql
CREATE USER 'eiye'@'%' IDENTIFIED BY '...';
GRANT SELECT ON mydb.* TO 'eiye'@'%';
```

Register it with `mysql://eiye:...@host:3306/mydb` (`mariadb://` also accepted). Point
it at a write-capable account and `test` fails with an explanation rather than
connecting and hoping. One type covers both engines; both are exercised in CI.

### SQL Server needs a read-only login too, and relies on it more

SQL Server has **no read-only transaction at all** — not even the partial one MySQL
offers. On top of that, TDS transmits multi-statement batches and no driver setting
disables it, so the query-wrapping trick that helps on the other engines cannot be
relied on here either. The login is therefore close to the whole guarantee, and the
connector refuses anything that can write: `sysadmin`, `db_owner`, or any database
permission outside a small read-only allowlist.

```sql
CREATE LOGIN eiye WITH PASSWORD = '...';
CREATE USER eiye FOR LOGIN eiye;
ALTER ROLE db_datareader ADD MEMBER eiye;
```

Register it with `sqlserver://eiye:...@host:1433/mydb` (`mssql://` also accepted).
A database in `READ_ONLY` state is accepted regardless of grants. SQL auth only —
not SSPI, not Azure AD. `pip install -e "backend[mssql]"`; pymssql bundles FreeTDS,
so there is no `msodbcsql18` system package to install.

### Oracle checks the login's effective privileges, not just its grants

`pip install 'eiye-db[oracle]'`, then register with
`oracle://user:pass@host:1521/SERVICE`. python-oracledb runs in Thin mode, which
speaks the wire protocol directly, so there is **no Oracle Instant Client to
install** on the host or in a container.

The login must hold read privileges and nothing else. eiye checks that on every
connect, not once at registration, so a privilege granted afterwards cannot
silently remove the guarantee:

```sql
CREATE USER eiye IDENTIFIED BY '...';
GRANT CREATE SESSION TO eiye;
GRANT SELECT ON app.customers TO eiye;
```

The check reads *effective* privileges, because a login can hold a write three
ways and only one of them shows up in the obvious place. Direct grants appear in
`USER_TAB_PRIVS`; a privilege carried by a **role** does not, and neither does
one granted to **PUBLIC**. Both were observed letting an apparently read-only
login insert rows. The scope is deliberately your schemas rather than every
schema, because stock Oracle grants writes to PUBLIC on its own internal
objects, and refusing those would reject every real database.

Unlike the other SQL connectors, this one runs **no read-only transaction**, and
that is deliberate. Oracle's blocks DML, which the connector's bounding subquery
already makes a syntax error, so it adds nothing — while its transaction-level
snapshot fails valid reads with ORA-01466 for a few seconds after any change to
the table's definition. It would also not cover DDL: a `DROP` issued inside one
destroyed the table. The read-only login is the boundary; the subquery and
Oracle's refusal to accept statement batches are what keep it hard to reach.

### SQLite needs nothing, because the file is opened read-only

The strongest guarantee of the four, and the cheapest: `file:...?mode=ro` maps to
`SQLITE_OPEN_READONLY`, which the library refuses to write through at all — `DROP
TABLE` fails exactly the way `INSERT` does. No grant to provision, no driver to
install. `PRAGMA query_only` extends the refusal to any database `ATTACH`ed later,
which `mode=ro` alone would not cover.

Register it with an **absolute** path: `{"path": "/srv/data/app.db"}`. A file that
is not there is an error rather than a new empty database, which is the point of
opening read-only rather than checking first.

### Confluence is Cloud, space-scoped, and GET-only

```bash
pip install -e backend    # no extra: it speaks HTTP, like the REST connector
```

Register it with the site URL, the operator's account email, and an API token
minted at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens):

```json
{
  "base_url": "https://your-site.atlassian.net",
  "email": "ops@example.com",
  "api_token": "...",
  "space_key": "ENG"
}
```

`base_url` accepts the URL with or without the trailing `/wiki`. **`space_key` is
optional but is the point** — it confines discovery *and every query* to one
space, the way `prefix` does for S3 and `root` does for the filesystem, so a
datasource can expose one space without exposing the site. The scope holds on
page ids too: a caller who knows the id of a page in another space is refused
rather than served.

Each space becomes a table. `{"space": "ENG"}` lists that space's pages as
metadata; `{"page_id": "123"}` returns one page with its text, converted from
Confluence storage format. Listing deliberately does not fetch bodies — a space
of a thousand pages would otherwise be a thousand extra requests.

**Cloud, not Data Center.** Data Center was the original plan, to avoid eiye
operating an OAuth hop on a customer's behalf. Cloud turns out to meet that
constraint the same way: the API token is minted and held by the operator,
exactly like a database DSN. Data Center meanwhile has no official container
image and needs a licence Atlassian stopped self-serving in March 2026.

Two things worth knowing before you deploy it:

- **Atlassian API tokens expire after one year.** Renewal is a calendar item, not
  an error you will see coming.
- **The token carries its account's own permissions.** Confluence has no
  read-only credential, so this connector's read-only guarantee is entirely the
  structural one above — it issues GET and nothing else, enforced by the test
  suite. Give it an account that can see only what it should read, and use
  `space_key` as well.

### Jira is Cloud, project-scoped, and validates every key it puts into JQL

Same account, same API token, same `base_url` as Confluence — register it with
`project_key` instead of `space_key`:

```json
{
  "base_url": "https://your-site.atlassian.net",
  "email": "ops@example.com",
  "api_token": "...",
  "project_key": "ENG"
}
```

Each project becomes a table. `{"project": "ENG"}` lists that project's issues
as metadata; `{"issue_key": "ENG-1"}` returns one issue with its description,
converted from Atlassian Document Format. As with Confluence, listing does not
fetch descriptions.

**This connector builds a query language, and that is what makes it different
from the Confluence one.** A project key reaches JQL as `project = "ENG"`, so a
key containing a quote could rewrite the query. Keys are validated against the
shape Jira actually issues before they are interpolated, and anything else is
refused rather than escaped. Raw JQL is deliberately **not** accepted from
callers: JQL has no write form, so it is not a write risk, but it would step
straight past `project_key`.

Two Jira-specific things worth knowing:

- **Pagination is two mechanisms.** Issue search returns an opaque
  `nextPageToken` with no total; project search still pages by offset and ends
  on `isLast`. The old `/rest/api/3/search` endpoint that used `startAt` now
  answers **410 Gone** on Cloud.
- **`/rest/api/3/search/jql` also has a POST form**, for JQL too long for a
  query string. It reads identically and would break the GET-only guarantee, so
  this connector keeps its queries small enough that GET always suffices.

### ServiceNow names the tables it may read, and there is no “all tables”

```json
{
  "base_url": "https://acme.service-now.com",
  "username": "eiye_ro",
  "password": "...",
  "tables": ["incident", "change_request"]
}
```

**`tables` is required, and that is the whole design.** A ServiceNow instance
carries thousands of tables — `sys_user`, `sys_user_has_role`, every custom `u_`
table an admin ever created. A datasource that exposed all of them by default
would not be a governed surface, so the operator names what this one may read
and nothing outside that list can be discovered or queried. It is S3's `prefix`
and Confluence's `space_key`, made mandatory because the blast radius is larger.

Each allowed table becomes a table in the semantic surface, with columns read
from `sys_dictionary`. `{"table": "incident"}` returns its records; reference
fields are collapsed to their values rather than passed through as
`{link, value}` objects.

There is deliberately **no `sysparm_query` passthrough**. That is a query
language, and accepting one from a caller would let them dot-walk past the
allowlist — the same reason the Jira connector refuses raw JQL. Table names are
validated against the shape ServiceNow itself issues, because they reach both a
URL path and an encoded query.

The account needs read access to `sys_db_object` and `sys_dictionary` for schema
discovery, plus a read ACL on each table. Use a dedicated integration account
with read roles only: this connector never writes, but ServiceNow will serve
whatever the account can see.

### SharePoint applies no item-level permissions, and cannot

```json
{
  "tenant_id": "...",
  "client_id": "...",
  "client_secret": "...",
  "site_url": "https://contoso.sharepoint.com/sites/finance",
  "library": "Documents",
  "folder": "reports"
}
```

**Read this before registering a SharePoint datasource.** Microsoft calculates
application-only access by looking for a permission record on the resource *or
on a securable hierarchical parent*. A grant on a site or a library is that
parent, so this connector reads **everything beneath the grant, regardless of
unique permissions set on individual files or folders**. Only the delegated
flow intersects an application's permissions with a user's, and eiye's ABAC
subject is an API key id rather than an Entra user — there is no user to
intersect with. `GET /permissions` is also documented as not returning the full
permission set app-only, so eiye cannot reliably report the ACLs it is not
applying either.

That is the same contract every other connector offers: a Postgres datasource
exposes what its login can see, and ABAC decides which agents may query it. It
is stated this loudly because SharePoint carries an expectation of per-user
permissions that a purpose-provisioned database login does not. **Scope the
grant to one document library, and put nothing in it you would not show every
agent your policy allows.** `folder` narrows it further and is the only thing
bounding the datasource inside the library.

**eiye refuses a tenant-wide SharePoint credential.** This is the only HTTP
connector that can inspect its own credential: Entra application-only tokens
carry a `roles` claim listing the granted application permissions, so the
connector reads it and rejects `Sites.Read.All`, `Files.Read.All`,
`Sites.FullControl.All` and their siblings. One of the Selected scopes is
required — `Sites.Selected`, `Lists.SelectedOperations.Selected`,
`ListItems.SelectedOperations.Selected` or `Files.SelectedOperations.Selected`.
An unreadable token is refused too, so the check cannot quietly become optional.

Prefer `Lists.SelectedOperations.Selected` on the single library over
`Sites.Selected` on the whole site collection. Note that granting below site
level breaks permission inheritance on that resource and counts against
SharePoint's unique-security-scope limit; a site-level grant does not.

Setting it up takes three steps, and **missing any one leaves the app with no
access at all** — which is the point of the Selected model:

1. Register an app in *your own* Entra tenant and consent it a Selected scope.
   eiye never operates an OAuth app on your behalf.
2. Have an administrator grant that app a `read` role on the library:
   `POST /sites/{site-id}/lists/{list-id}/permissions`.
3. Give eiye the tenant id, client id and secret.

**The Graph search endpoint is never called.** `/search/query` does not enforce
`Sites.Selected` in application-only mode — it runs against the tenant-wide
search index — so the obvious way to implement "find a document" would defeat
the entire scoping model. The test suite fails the build if any request path
reaches it. `request` is `{"path": "reports/q3.csv"}`, relative to the
configured folder; there is no search, no `$filter` passthrough and no item-id
form. Files are extracted with the same CSV/XLSX/PDF/text readers the S3 and
filesystem connectors use.

Graph throttles far harder than the other HTTP sources. eiye surfaces the 429
and its `Retry-After` rather than sleeping and retrying: a connector that
silently waits turns a governed query into an unbounded one.

### S3 / MinIO is scoped by prefix, and only ever lists and gets

Two calls, ever: `ListObjectsV2` over the configured prefix, and `GetObject` for
one object. There is no request field through which a caller could ask for a
third — `request` is `{"key": "reports/q1.csv"}`, relative to the prefix, not a
query language. So unlike the SQL connectors, there is no wide text channel to
defend, and the read-only property is structural rather than credential-verified.

What is *not* claimed is that the credential itself cannot write: AWS offers no
way to ask "is this key read-only" that a read-only key is allowed to call, and
MinIO implements no equivalent, so a probe would either lie or write. Scope the
key yourself:

```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::my-exports",
   "Condition": {"StringLike": {"s3:prefix": ["exports/*"]}}},
  {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::my-exports/exports/*"}
]}
```

Config: `bucket` (required), `prefix`, `endpoint_url` (set for MinIO, omit for
AWS), `region`, and `access_key_id` / `secret_access_key` — omit both to use the
host's ambient AWS credentials. CSV, text, PDF and XLSX are extracted with the
same code as the filesystem connector. Object bytes are parsed in memory and
dropped; nothing is written to `eiye.db` and nothing is cached on disk.

Two limits worth knowing, because neither is silent: discovery lists at most
1,000 objects and reads column headers for the first 100 CSVs (the rest are
listed with no fields and still queryable), and a query refuses an object over
64 MiB rather than returning a truncated prefix of it.

## Upgrading a deployment (schema migrations)

The metadata store — the datasource registry, semantic relationships, metric
catalog, ABAC policies and audit log — is versioned with Alembic. A fresh
install needs nothing: the schema is created and stamped at the current revision
on first boot, and a database created by a release from before migrations
existed is stamped in place, rows intact.

When you pull a build whose schema moved, boot **warns and does not migrate**:

```
the metadata store is at revision abc123 but this build expects def456.
Schema changes are NOT applied at boot, deliberately — run
`alembic upgrade head` from backend/ before relying on anything that needs
the newer schema.
```

That is deliberate. Migrating a database as a side effect of starting a process
is how two replicas booting at once corrupt a schema, so the upgrade is a step
you take:

```bash
cd backend
alembic current          # where this database is
alembic history          # what exists
alembic upgrade head     # apply it
```

The database comes from `EIYE_DATABASE_URL`, the same setting the application
reads — `alembic.ini` ships with no URL in it so there is exactly one place that
says which database a deployment uses. To point a one-off somewhere else:

```bash
alembic -x url=sqlite:////srv/eiye/other.db upgrade head
```

Back up before upgrading. `alembic downgrade -1` reverses one revision and the
test suite checks the chain is reversible, but a restorable backup is the thing
you actually want when a migration surprises you.

## Pricing Tiers

| Tier | Datasources | Queries/mo | Price |
|------|-------------|------------|-------|
| Free | Up to 5 | 1,000 | $0 |
| Starter | Up to 15 | 25,000 | $100/mo |
| Pro | Up to 50 | 250,000 | $500/mo |
| Business | Up to 150 | 1M | $2,000/mo |
| Enterprise | Unlimited | Custom | Contact sales |

The Free tier is the Additional Use Grant in the [license](LICENSE), not a trial
that expires.

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

- Production use beyond those limits (see the pricing tiers above)
- Offering eiye_db to third parties as a hosted or managed service
- Enterprise features: SSO, compliance reports, multi-tenant control plane, advanced analytics

Each released version converts to **Apache-2.0 four years after publication**
(Change Date `2030-08-30` for the current version), so nothing is permanently
enclosed. Commercial licensing: max@eiye.ai.

### Entitlements

With no license file, eiye_db runs under the free grant above — 5 datasources,
1,000 queries/month — and enforces it. A commercial license is a signed file:

```bash
export EIYE_LICENSE_FILE=/etc/eiye/acme.license
```

Verification is offline against a public key built into the binary; there is no
phone-home, so air-gapped deployments work normally. `GET /api/v1/status` reports
the active tier, limits, expiry and current usage.

What happens at the boundary is deliberately asymmetric:

| | Unlicensed (free grant) | Licensed |
|---|---|---|
| Datasource cap | hard stop at 5 | hard stop at your tier |
| Query cap | hard stop at 1,000/mo | recorded as overage, **service continues** |
| Expiry | n/a | 30-day grace, then no new registrations or commercial features |

A paying customer is never taken offline mid-month by their own vendor — query
overage is a true-up conversation, not an outage. And **expiry never severs
access to what already exists**: existing sources keep serving, and the audit
trail and semantic-model export stay readable regardless of license state. You
may have a regulatory retention obligation against that audit log, and no
billing dispute should be able to put you out of compliance.
