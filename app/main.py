import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db_sync
from app.services import rabbitmq
from app.webhook import router
from app.api import router as api_router

init_db_sync()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await rabbitmq.close()


app = FastAPI(title=f"{settings.BUSINESS_NAME} - API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(api_router)

_media_dir = Path(__file__).parent.parent / "media"
if _media_dir.is_dir():
    app.mount(
        f"{settings.WEBHOOK_PATH}/media",
        StaticFiles(directory=_media_dir),
        name="media",
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
