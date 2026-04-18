import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@app.get("/health")
async def health():
    return {"status": "ok"}
