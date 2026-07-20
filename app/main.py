import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db_sync
from app.services import rabbitmq, sai_sync
from app.webhook import router
from app.api import router as api_router
from app.sai_router import router as sai_router
from app.sim import router as sim_router

init_db_sync()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Auto-registro no catalogo do SAI + poll do snapshot do painel (15 min).
    # Fallback do push /sai/config: garante que o catalogo (produtos e
    # empreendimentos) esteja no Redis mesmo se o push se perder.
    sai_task = asyncio.create_task(sai_sync.start_polling())
    try:
        yield
    finally:
        sai_task.cancel()
        try:
            await sai_task
        except (asyncio.CancelledError, Exception):
            pass
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
app.include_router(sai_router)
app.include_router(sim_router)

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
