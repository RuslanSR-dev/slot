import os
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from booking import __version__

SERVICE_NAME = "booking"
# Reported when the build did not say which commit it is. Deliberately loud:
# a plausible-looking default like "dev" would hide a broken pipeline.
UNKNOWN_BUILD = "unknown"


class Health(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    build_sha: str


def create_app() -> FastAPI:
    app = FastAPI(title="Slot Booking", version=__version__)

    @app.get("/health")
    def health() -> Health:
        return Health(
            status="ok",
            service=SERVICE_NAME,
            version=__version__,
            # Set at image build time, so a smoke test can prove which build is running.
            # Missing and empty are the same failure, so both map to UNKNOWN_BUILD.
            build_sha=os.environ.get("SLOT_BUILD_SHA") or UNKNOWN_BUILD,
        )

    return app


app = create_app()
