"""FastAPI app: webhook and Mini App routes."""

from fastapi import FastAPI

from kanakko import __version__

app = FastAPI(title="Kanakko", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
