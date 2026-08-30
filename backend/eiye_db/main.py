"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from eiye_db import __version__, db, pii
from eiye_db.api import router
from eiye_db.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.configure()
    if settings.api_key is None:
        import logging

        logging.getLogger("eiye_db").warning(
            "EIYE_API_KEY is unset (open dev mode): every caller is admin — "
            "can approve semantics and view raw PII. Set keys before exposing this."
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
