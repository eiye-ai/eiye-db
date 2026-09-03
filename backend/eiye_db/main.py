"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import JSONResponse

from eiye_db import __version__, db, license, pii, security
from eiye_db.api import router
from eiye_db.config import settings


def _log_license(entitlements) -> None:
    import logging

    log = logging.getLogger("eiye_db")
    if not entitlements.licensed:
        log.info(
            "No license file (EIYE_LICENSE_FILE unset): running under the BSL Additional Use "
            "Grant — up to %d datasources and %d queries/month.",
            entitlements.max_datasources,
            entitlements.max_queries_per_month,
        )
    elif entitlements.degraded:
        log.warning(
            "License %s expired %s and is past its grace period: new datasource registrations "
            "and commercial features are disabled. Existing sources, the audit trail and "
            "semantic-model export are unaffected.",
            entitlements.license_id,
            entitlements.expires_at.date() if entitlements.expires_at else "?",
        )
    elif entitlements.expired:
        log.warning(
            "License %s expired %s — in grace period, everything still working. Renew to avoid "
            "losing new registrations and commercial features.",
            entitlements.license_id,
            entitlements.expires_at.date() if entitlements.expires_at else "?",
        )
    else:
        log.info("Licensed to %s (tier=%s, expires %s).", entitlements.customer, entitlements.tier,
                 entitlements.expires_at.date() if entitlements.expires_at else "never")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Refuse the half-configured state before doing any setup work: an operator
    # who set one key believes the service is secured, and a warning they may
    # never read is not enough to correct that belief.
    # An empty setting is not an unset one. `EIYE_API_KEY=$TYPO` in a unit file
    # or compose file expands to "", which reads as configured, boots without
    # complaint, and then authenticates an empty X-API-Key header as admin.
    for name, value in (("EIYE_API_KEY", settings.api_key), ("EIYE_ADMIN_API_KEY", settings.admin_api_key)):
        if value is not None and not value.strip():
            raise RuntimeError(
                f"{name} is set but empty — most likely a variable that expanded to nothing. "
                "That leaves the service looking configured while it is not. Give it a value, "
                "or unset it deliberately for open dev mode."
            )
    if (settings.api_key is None) != (settings.admin_api_key is None):
        raise RuntimeError(
            "partially configured auth: set both EIYE_API_KEY and EIYE_ADMIN_API_KEY, "
            "or neither (open dev mode). Setting only one leaves the other role unreachable."
        )
    # A named entry that reuses a reserved id while the setting occupying it is
    # live gives two different secrets one ABAC subject and one audit
    # principal, erasing the distinction the map exists to draw.
    for reserved, occupied_by in (("primary", settings.api_key), ("admin", settings.admin_api_key)):
        if occupied_by is not None and reserved in settings.api_keys:
            setting = "EIYE_ADMIN_API_KEY" if reserved == "admin" else "EIYE_API_KEY"
            raise RuntimeError(
                f"EIYE_API_KEYS defines the reserved key id '{reserved}', which {setting} already "
                f"occupies. Two credentials would share one subject and one audit identity: rename "
                f"the entry, or unset {setting}."
            )
    # A map with no admin in it, and no EIYE_ADMIN_API_KEY beside it, locks the
    # operator out of registrations, policies, the audit log and every approval
    # -- with no way back in that does not involve a restart.
    if settings.api_keys and settings.admin_api_key is None:
        if not any(entry.is_admin for entry in settings.api_keys.values()):
            raise RuntimeError(
                'no admin principal: no entry in EIYE_API_KEYS sets "is_admin": true and '
                "EIYE_ADMIN_API_KEY is unset. The whole admin surface would be unreachable."
            )
    # A licence that is configured but unusable fails the boot. An operator who
    # paid and mis-deployed the file must learn that now, not by silently
    # running on free-tier limits and hitting them in production.
    entitlements = license.current()
    _log_license(entitlements)
    db.configure()
    # Reconciles the schema with the migration history: stamps a database that
    # predates migrations, and warns — never silently upgrades — when one is
    # behind head. Applying migrations as a side effect of starting a process
    # is how two replicas booting together corrupt a schema.
    db.ensure_versioned()
    if not security.auth_configured():
        import logging

        logging.getLogger("eiye_db").warning(
            "No API keys are set (open dev mode): every caller is admin — "
            "can approve semantics and view raw PII. Set keys before exposing this."
        )
    if settings.abac_default_deny:
        # Warn, never refuse. Policies are created through the API, so a server
        # that refused to start without one could not be used to write the
        # first one — the flag would be unadoptable. Admins bypass ABAC, so
        # this state is recoverable; it is just silent until someone notices
        # every agent failing.
        from eiye_db import policy

        if not any(p["effect"] == "allow" for p in policy.list_policies()):
            import logging

            logging.getLogger("eiye_db").warning(
                "EIYE_ABAC_DEFAULT_DENY is on and no allow policy exists: every non-admin "
                "caller is denied on every source, including MCP agents. Admin keys still "
                "work. Grant access with scripts/grant.py, and check a subject with "
                "GET /api/v1/access/{key_id}."
            )
    if settings.pii_ner_enabled:
        # Fail loud at boot if the NER model is missing, rather than 500-ing (or
        # worse, silently under-redacting) on the first query.
        pii._load_ner()
    if settings.nl_llm_enabled:
        from eiye_db import nl

        nl.ensure_llm_ready()  # fail loud at boot, not on the first /semantic/ask
    yield


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.exception_handler(license.LicenseLimitExceeded)
def _license_limit(_request, exc: license.LicenseLimitExceeded) -> JSONResponse:
    """402 for every entitlement refusal, declared once on the app so a new
    route cannot forget to translate it."""
    return JSONResponse(status_code=402, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "eiye_db.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
