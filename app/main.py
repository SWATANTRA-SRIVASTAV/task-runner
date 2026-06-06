"""
Application entrypoint.

FastAPI's lifespan context manager handles startup and shutdown in one place:
  - on startup: connect to Docker, init DB, start scheduler loop
  - on shutdown: signal scheduler to drain, close Docker client

This replaces the older @app.on_event("startup") pattern, which doesn't
compose well with dependency injection and makes testing harder.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes
from app.core.container import ContainerManager
from app.core.scheduler import JobScheduler
from app.db.repository import JobRepository
from app.utils.config import Settings
from app.utils.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    configure_logging(settings.log_level)

    # Initialise shared components
    repo = JobRepository(db_path=settings.db_path)
    containers = ContainerManager(docker_url=settings.docker_url or None)
    scheduler = JobScheduler(
        repository=repo,
        container_manager=containers,
        poll_interval_seconds=settings.poll_interval_seconds,
        max_concurrent_jobs=settings.max_concurrent_jobs,
    )

    # Inject into route handlers via dependency overrides
    app.dependency_overrides[routes.get_repository] = lambda: repo
    app.dependency_overrides[routes.get_container_manager] = lambda: containers

    # Start the scheduler as a background task
    scheduler_task = asyncio.create_task(scheduler.start())
    logger.info("Task runner started. DB=%s MaxJobs=%d", settings.db_path, settings.max_concurrent_jobs)

    try:
        yield  # Application runs here
    finally:
        await scheduler.shutdown()
        scheduler_task.cancel()
        containers.close()
        logger.info("Task runner shut down cleanly")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Distributed Task Runner",
        description="Run isolated workloads in Docker containers via a REST API.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(routes.router)
    return app


app = create_app()
