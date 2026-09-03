# Onboarding — get up to speed fast

This file is the durable, in-repo orientation for any agent or engineer picking
up eiye_db cold. It captures the non-obvious operational facts that reading the
code alone won't give you. Read this, then `GOALS.md` (vision), `TODO.md`
(status), and the git log (`git log --stat`).

## What this is

A **semantic surface** that lets AI agents safely query an organization's data
sources. The product grew from a narrow governed-gateway wedge into a working
**agentic semantic layer**, built on one tenet:

> **AI drafts → human governs → engine enforces.**
> Agents (or discovery) *propose* relationships and metrics; a human *approves*
> them (admin-gated); the engine *persists, versions, and enforces* only what
> was approved. Candidates are labeled hints, never authoritative. This is the
> differentiator vs hand-authored (slow) and pure-LLM (hallucinating) layers.

What is **built** today, end to end:

- **Governed gateway (Tier 0).** register datasource → discover schema → run a
  read-only query → PII redacted → access audited. REST API + stdio MCP server.
- **Relationships (Tier 1).** intra-source foreign keys (auto-approved) +
  cross-source candidate joins from heuristic and behavioral discovery, exposed
  status-labeled in the schema surface; human review UI to approve/reject.
- **Semantic depth (Tier 2).** a **metric catalog** (named, typed, governed
  query templates), agent **propose_\*** draft tools, **result-level lineage**,
  **entity resolution** (cross-source name matching), **ABAC** (per-source
  access + column masking), and **NL→governed query** (`ask`) — deterministic by
  default with an optional LLM bootstrap.

An MCP agent can therefore discover what it's allowed to see, read approved
semantics as ground truth, ask questions answered only through governed
definitions, and propose new semantics for human approval — every step audited.

The remaining breadth in `GOALS.md`/`TODO.md` (row-level ABAC via predicate
push-down, more connectors, Redis cache, a policy-manager UI, multi-tenancy, ops
hardening) is a **deliberately deferred, market-gated backlog** — not missing
work. Do not treat those as bugs.

## Strategy context (why the plan looks the way it does)

`TODO.md` originally had a breadth-first 10-phase plan. It was deliberately
replaced (commit `01531d7`) with a vertical wedge so there's one end-to-end
demoable path to market-test before building breadth. The wedge shipped, then
the **semantic-layer roadmap (Tier 1 + Tier 2) was built on top of it** — see
the git log from `e8f7119` onward. All six semantic-layer design decisions were
locked at their recommended defaults (SQLite + YAML export over a graph DB;
structural→heuristic→behavioral discovery; approved-only authority; OpenPlanter
as an external proposal agent via MCP). The one still-open item is a *product*
decision, not code: **choosing market-test metrics** (time-to-first-datasource,
queries/session, PII-hit rate).

## Where things are

```
backend/eiye_db/
  main.py          FastAPI app (/health — the unauthenticated liveness probe); lifespan
                   configures DB, warns on open dev mode, fails loud if NER/NL unusable
  config.py        pydantic-settings, EIYE_ env prefix (keys, ABAC posture, NL/NER flags)
  models.py        pydantic domain models (StrEnum, Literal-typed closed sets)
  db.py            SQLite metadata store (SQLAlchemy 2.x): datasources, relationships,
                   metrics, policies, audit_logs
  registry.py      datasource CRUD; delete cascades relationships/metrics/policies
  service.py       ** the governance chain — shared by REST and MCP; every access path **
  security.py      API-key auth: EIYE_API_KEY/EIYE_ADMIN_API_KEY plus the EIYE_API_KEYS
                   named map (one subject per agent). Dev mode only when ALL THREE are
                   unset; admin gates raw PII, curation, policies, and raw /datasources
  audit.py         append-only audit trail
  pii.py           regex PII detection + recursive redaction (optional spaCy NER layer)
  policy.py        ABAC engine: allow/deny per source, column masking, subject/action
                   scoping; explain() backs the access review endpoint
  license.py       entitlements: offline Ed25519 verification, tier limits, expiry ladder
  semantic.py      relationship detection (structural/heuristic/behavioral), governance, YAML
  catalog.py       metric catalog: typed params, injection-hostile substitution, approval gate
  resolution.py    entity resolution: normalization + tiered name matching (stdlib-only)
  nl.py            NL→query: deterministic matcher + optional LLM bootstrap (llm_bind)
  metrics.py       operational metrics summary (audit-trail aggregation, admin-only)
  connectors/      base.py, factory in __init__; postgres/mysql/mssql/sqlite/oracle (sql.py shared),
                   filesystem/s3 (documents.py shared), rest.py,
                   confluence.py/jira.py (atlassian.py shared), servicenow.py
                   (http_basic.py shared by all three)
  api.py           REST routes (/api/v1/...), incl. /access/{key_id} access review
  mcp_server.py    stdio MCP server (FastMCP) — 9 tools, same service layer
backend/tests/     pytest suite (444 pass, 37 skipped on a bare install); conftest gives
                   a fresh DB + client per test; readonly_guards.py enforces the
                   structural read-only tier (REST / S3 / filesystem)
frontend/          React + Vite UI: datasource management, "Semantic model" review, and
                   "Access" (subject review, grant, policy list) — an operator console
examples/demo_data/     demo CSVs used by the README quickstart
examples/policies/      example_policies.json (boilerplate ABAC policies)
scripts/                quickstart.{py,sh}, mint_key.py (named keys), grant.py (allow
                        policies for default-deny), seed_example_policies.py, mcp_dogfood.sh
backend/alembic/         schema migrations; env.py takes the URL from the app's own
                        settings, not from alembic.ini (which ships blank)
.github/workflows/ci.yml  pytest + ruff on a 3.11/3.12 matrix, plus a frontend
                        build job (npm ci + tsc --noEmit + vite build)
```

CI runs pytest, ruff, and a frontend build. It is regression protection, not an
authorization check: the route-auth audit found real defects while this exact
gate was green. CI supplies Postgres, MySQL, MariaDB, SQL Server, Oracle and
MinIO, so **every SQL connector's live tests run there** — the residual skips are
not database connectors.

The frontend job is separate from `test`, sharing none of its Python matrix or
containers. It runs `npm ci` (reproducible: the lockfile is committed, unlike
the Python side) then `npm run build`, which is `tsc --noEmit && vite build`.
Before it existed the console's TypeScript was checked only by whoever
remembered to run `tsc` by hand, and that gap produced a real bug — the
datasource form fell through to the REST branch for an unrecognised type. Each
new connector adds another branch to that form.

### The 9 MCP tools (what an agent sees)

`list_datasources`, `get_schema` (returns status-labeled relationships),
`query_datasource` — the governed gateway. `list_metrics`, `query_metric` —
execute approved metrics. `propose_relationship`, `propose_metric` — draft
candidates for human review (never authoritative). `resolve_entities` —
cross-source name matching. `ask` — NL question answered only through approved
metrics. All run as the non-admin subject named by `EIYE_KEY_ID` (default
`mcp-stdio`), so ABAC policies govern agents directly, and all go through
`service.py`. That id is asserted by the client, not proved — under
default-deny each distinct id an agent claims needs its own grant.

## Run and test

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # runtime + pytest/ruff
pytest -q                       # 444 pass, 37 skipped (see below on the skips)
ruff check .                    # CI gates on this — keep it clean
uvicorn eiye_db.main:app --reload
python -m eiye_db.mcp_server     # the stdio MCP server
```

The 11 skips are connector suites with nothing to run against: three whole files
skip at import for a missing optional driver (`[mysql]`, `[mssql]`, `[s3]`), and
the rest are live tests waiting on `EIYE_TEST_*`. Install an extra or start a
server and both counts move — so compare a run against your own previous run,
not against a number in this file.

Optional extras. The connector drivers ship separately so a deployment installs
only what it connects to; `ner` and `nl` are **off by default** and each fails
loud at boot if enabled but unusable. SQLite, filesystem, Postgres and REST need
nothing extra.

```bash
pip install -e ".[mysql]"     # MySQL / MariaDB   (pymysql)
pip install -e ".[mssql]"     # SQL Server        (pymssql, bundles FreeTDS)
pip install -e ".[s3]"        # S3 / MinIO        (boto3)
pip install -e ".[ner]" && python -m spacy download en_core_web_sm  # EIYE_PII_NER_ENABLED=true
pip install -e ".[nl]"                                              # EIYE_NL_LLM_ENABLED=true
```

The README has the verified 5-minute quickstart (register demo CSVs → query →
see redaction + audit → connect an MCP client). Requires Python **3.11+**
(uses `StrEnum`, `X | Y` unions, `asyncio.timeout`).

## Load-bearing invariants — do not break these

**0. A model change is only half a schema change.** `db.Base` models and
`backend/alembic/versions/` must agree, because `create_all` adds missing
*tables* and never alters an existing one — so a column added to a model is
simply absent on every database that already exists. Generate the revision in
the same change:

```bash
cd backend && alembic revision --autogenerate -m "<what changed>"
```

`test_migrations.py::test_migrations_match_the_models` runs the chain and asserts
Alembic would autogenerate nothing further, so drift fails there rather than on
a deployment. Boot warns when a database is behind head and deliberately does
not upgrade it; `db.ensure_versioned` stamps a fresh or pre-migrations database
so the first real migration has a baseline.


This is a **security/governance product**. The first three guarantees must hold
**unconditionally** on *every* access path (REST and MCP). A gap in any is
high-severity:

1. **Read-only.** Two different claims, named as such in the README and never
   averaged into one badge. *Server-enforced* (the five SQL connectors): the
   engine refuses the write, the login is verified at every connect, and the
   mechanism is exercised against a live server in CI. *Structural* (REST, S3,
   filesystem): there is no server to refuse anything, so the guarantee is that
   the connector never asks — enforced by the guards in
   `backend/tests/readonly_guards.py`, which fail the build on a non-GET
   request, a non-read S3 operation, or a write-mode `open` along any exercised
   path. Confluence is the first connector *built* under the structural tier;
   its GET-only transport guard is the whole of its read-only guarantee, since
   Confluence has no read-only credential to verify. Scope (`space_key`,
   `project_key`) is the second half of the story for both Atlassian connectors
   and is enforced on the page-id and issue-key paths too. Jira additionally
   *builds* JQL, so it validates every key before interpolating one — the only
   connector outside the SQL family that constructs a query language. Those guards raise a `BaseException` on purpose: `documents.py` alone
   catches `Exception` six times, and a guard application code can swallow
   proves nothing. The structural proof is bounded by coverage — an untested
   branch could still hide a write — and that limit is stated in the README
   rather than smoothed over.

   Every connector states its own mechanism, because they do not generalise: Postgres layers a non-writing login over a `readonly=True`
   transaction and a bounding subquery — the transaction covers DDL, unlike
   MySQL's, but not `COPY ... TO PROGRAM` (the wrapper stops that) and not
   `dblink`, which opens a second session the transaction never reaches, so the
   login is the boundary here too and a superuser DSN is refused;
   SQLite is opened `mode=ro` + `query_only`;
   MySQL layers a non-writing login over a read-only transaction that covers DML
   but not DDL; SQL Server has no read-only transaction at all and rests on a
   login verified at every connect; Oracle deliberately runs *no* read-only
   transaction (it would block only DML, which the bounding subquery already
   makes a syntax error, and it fails valid reads with ORA-01466 right after
   DDL) and instead reads the login's *effective* privileges — direct, via role,
   and via PUBLIC, all three of which can carry a write; filesystem is
   root-scoped + traversal-safe;
   S3 is prefix-scoped and calls only ListObjectsV2/GetObject; REST is GET-only.
   Read the connector's module docstring before changing one.
2. **PII redaction.** `pii.redact_structure` redacts keys **and** values **and**
   numeric scalars. `include_pii=True` is REST-admin-only; the **MCP path always
   redacts** (no opt-out). The query *request* is also redacted before it's
   audited (SQL predicates can contain PII), as are metric params, resolution
   column names, and NL question text.
3. **Audit trail.** Every create/test/discover/query/propose/review/resolve/ask,
   including failures and policy denials, is recorded. `GET /api/v1/audit` is
   admin-gated. Permitted accesses carry `details.basis` — `"policy"` (ABAC was
   consulted and allowed) vs `"admin-bypass"` (nobody asked policy). It lives in
   the JSON `details`, not a column, because `create_all` cannot `ALTER` an
   existing `audit_logs` table and Alembic is still backlog; promote it when
   migrations land.

Three further rules are load-bearing for the semantic layer and the commercial
boundary:

4. **ABAC enforcement.** Policies are **allow-by-default** (a fresh install works
   with zero policies); `EIYE_ABAC_DEFAULT_DENY=true` flips the posture, and is
   a supported deployment mode rather than a flag to flip blind — see
   "Operating default-deny" below. When
   policies exist they are enforced on every data *and metadata* path — masked
   columns are dropped before redaction; denied sources vanish from schema,
   export, listings, and the propose_\* existence oracle. Admin bypasses (admins
   are the governors), recorded as `basis="admin-bypass"`. Order: explicit deny >
   explicit allow > default. Open dev mode requires **every** key setting unset
   — `EIYE_API_KEY`, `EIYE_ADMIN_API_KEY` and the `EIYE_API_KEYS` map — so no
   gate here is silently vacuous. Setting exactly one of the first two is
   refused at boot; so is a map that reuses the reserved ids `primary`/`admin`
   while their settings are live, or one with no admin in it and no
   `EIYE_ADMIN_API_KEY` beside it (which would lock the operator out entirely).
   **Subjects are only as fine-grained as the keys.** `EIYE_API_KEY` resolves
   every HTTP caller to `primary`, so `subjects` matching is a no-op over REST
   until named keys exist; that is what `EIYE_API_KEYS` is for, and why it had
   to land before `abac_default_deny` could mean anything per-agent. Over MCP
   the subject comes from `EIYE_KEY_ID`, which is asserted rather than proved.
5. **Entitlements are not access control.** `license.py` gates on what the
   *deployment* is licensed to do; ABAC gates on what a *subject* may see. Keep
   them apart: an admin bypasses ABAC because they govern their own data, but
   nobody bypasses the license, because nobody in the deployment is the
   licensor. Both checks live in `service.py` so REST and MCP share them —
   `check_query_quota` sits in `run_query` ahead of the policy gate, so a denied
   query costs no quota and an agent gets no unmetered path. The free-tier
   constants in `license.py` MUST equal the BSL Additional Use Grant in
   `/LICENSE`; they are one boundary written twice, and the license wins.
   Expiry degrades (no new registrations, no commercial features) but never
   blocks existing sources, the audit trail, or export — a customer may have a
   retention obligation against that log. It is measurement, not DRM: the source
   is available, so the license is what binds, and this code only makes usage
   visible.
6. **Approved-only authority.** Only `status="approved"` relationships/metrics
   are authoritative to agents. Candidates are labeled hints; rejected links are
   never shown. Agents (holding the primary/MCP key) may *propose* but only an
   *admin* may approve. Structural FKs come from the source DB and can't be
   rejected.

Both REST and MCP go through `service.py` precisely so these can't diverge. If
you add a connector, tool, or interface, route it through `service.py`.

**Two surfaces, one contract.** `/api/v1/datasources` is the *operator* surface:
it serves `DataSource.config` verbatim (Postgres DSN with password, REST auth
headers), and update/delete retarget or cascade the datasource id that policies,
metrics and relationships are keyed on. The whole group is admin-only. Agents
read `/api/v1/surface/sources`, which is policy-filtered and omits `config`.
Metadata that names a *second* source (cross-source relationships on a schema
response) is filtered on both endpoints, not just the one being requested — the
near side is gated by `check_schema_access`, the far side by
`visible_datasource_ids`. The React UI drives the operator surface, so it needs
`EIYE_ADMIN_API_KEY`; and because admins bypass ABAC, UI access *is* full
governance authority, not merely credential read-back.

## Operating default-deny

The flag has always existed; what was missing was any way to run a deployment
with it on. Three pieces close that, and the reasoning behind each is worth
keeping.

- **`scripts/grant.py`** writes the allow policy, resolving sources by name.
  Hand-writing policy JSON and looking up datasource ids is why the posture was
  easy to turn on and hard to run. Re-running skips an existing policy name
  rather than widening it, and an ambiguous source name is refused rather than
  resolved to whichever row sorted first — granting the wrong source is an
  access-control mistake, not a typo.
- **The console's Access tab** does all of it with a form: review a subject,
  grant per source or on everything, list and delete policies. It writes the
  same policy names as `grant.py`, so the two cannot leave one deployment with
  two naming conventions.
- **`GET /api/v1/access/{key_id}`** reports what one subject can reach:
  read, discover and masked columns per source, plus which setting configures
  that subject. Admin-only, for the same reason the policy list is. It calls
  `policy.check` and `policy.permits` rather than restating their order,
  because an explanation that could drift from enforcement is worse than none.
  It exists because the caller-facing denial is deliberately generic, so from
  outside the server a missing allow and an explicit deny are indistinguishable.
- **A boot warning** when default-deny is on and no allow policy exists at all.
  It **warns and does not refuse**, and that is deliberate: policies are created
  through the API, so a server that refused to start without one could never be
  used to write the first one. Admins bypass ABAC, so the state is always
  recoverable; it is just silent until every agent starts failing.

**Why the default was not flipped.** The decision (2026-09-02) was to support
the posture properly and leave `abac_default_deny` defaulting to False. Three
reasons, all still current: the README tells operators to give each MCP agent
its own `EIYE_KEY_ID`, and those ids are chosen after any seeding runs, so no
seeded allow can cover them — default-deny would put a policy-authoring step in
front of the wedge. Seeding for the subjects that *are* known (`primary`,
`quickstart`, `mcp-stdio`) reaches neither those agents nor named keys. And the
setting is read live on every check with no migration hook (Alembic is still
backlog), so changing the default changes behavior for every self-hoster on
upgrade, with "all my agents stopped working" as the symptom. Revisit at a
major version, with release notes, now that the tooling exists.

## Gotchas that cost time

- **`FastMCP`, not `MCPServer`.** The installed `mcp` SDK exposes
  `from mcp.server.fastmcp import FastMCP`. Newer docs show a renamed
  `MCPServer` — that import fails on the pinned version.
- **Shared registry across processes.** The API and the MCP server share the
  SQLite store via `EIYE_DATABASE_URL` (default `sqlite:///./eiye.db`, relative
  to cwd). MCP hosts spawn the server from an arbitrary cwd, so set an
  **absolute** path for both. See README.
- **NL LLM assist egresses data.** Enabling `EIYE_NL_LLM_ENABLED` sends raw
  question text + approved-metric metadata to the Anthropic API (documented
  operator consent; not pre-redacted, because redaction would mangle the very
  parameter values it exists to bind). Its output can only pick a shown metric
  and draft params that still pass catalog validation. Every call is audited as
  `ask_llm` egress. Floor: `anthropic>=0.77.0` (where `output_config` appears).
- **Live-PG tests are gated** behind `EIYE_TEST_PG_DSN`; they skip without it.
- **`httpx`/starlette TestClient deprecation warning** is benign (it suggests a
  likely-typosquat `httpx2`; we intentionally did not install it).
- **Live smokes: isolate ports.** The maintainer often runs dev servers on
  `:8000`/`:5173`; Vite auto-increments, so a smoke on default ports can
  silently hit their stack. Use `EIYE_PORT` + `vite --strictPort` + an `lsof`
  pre-check for any live test.

## History you can't see from the code

eiye_db was scaffolded inside `../OpenPlanter` on 2026-07-20. Load-bearing facts:

- **Do not run `OpenPlanter/generate_eiye*.py`.** Those scripts would overwrite
  this repo with 39 files of never-executed, buggy generated code (import-time
  errors, unwired connectors, `eval()`). This backend was written fresh,
  test-first. Mine the generators for *design intent* only.
- The canonical clean `GOALS.md`/`TODO.md` came from `OpenPlanter/eiye_db/`; the
  originals here were shell-heredoc-mangled and restored (commit `8f43834`).
- **The local planning docs are edited but NEVER committed or pushed:**
  `GOALS.md`, `TODO.md`, `PRD.md`, `NEXT_CONNECTORS.md`, `INDEPENDENT_TEST_PLAN.md`,
  `INDEPENDENT_TEST_REPORT.md`, `docs/SITE_MOVED.md` (all in `.gitignore`, so the
  rule survives a fresh clone), plus `AUTH_HARDENING.md`, `NAMED_KEYS.md`,
  `research-report.md` and `dogfood_out.txt` in `.git/info/exclude`, which
  protect only this machine.
  Stage commits by explicit file name; never `git add -A/-u`.
- OpenPlanter had **no packaged entity-resolution engine** — but its
  investigation scripts held genuinely good fuzzy matching (normalization,
  token-overlap, confidence tiers). Those *algorithms* were ported stdlib-only
  into `resolution.py` (commit `ee55f34`); the scripts themselves were never run.

## Working discipline (how this repo has been developed)

Every phase was **adversarially reviewed** (a find→verify multi-agent pass) and
the confirmed findings fixed with regression tests before moving on — that's how
the real PII-leak holes were caught (commit `b11baf8`), and how the Tier 2
features were hardened (dozens of confirmed findings across metrics, ABAC, and
NL). Continue this: after a substantive change, review and verify, don't just
ship. A caution learned the hard way: if a review workflow dies mid-run (e.g.
session limits), its "rejected" list is **unverified** — re-verify by hand
rather than trusting it.

## Fastest path back to context after a reboot

1. Read this file, then `git log --stat -8`.
2. `cd backend && source .venv/bin/activate && pytest -q` — a green suite = the
   invariants above still hold. On a bare install that is 444 pass, 37 skipped;
   installing an optional extra or pointing `EIYE_TEST_*` at a live server
   moves both numbers — and not in the same direction, since a live server
   turns skips into passes. Compare against your own last run in the same
   environment, never against a constant.
3. `TODO.md` shows what's done (Tier 0/1/2 complete) and the market-gated
   backlog; `GOALS.md` has the vision and Semantic Layer Strategy section.
