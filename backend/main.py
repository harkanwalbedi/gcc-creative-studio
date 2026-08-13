# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# --- Setup Logging Globally First ---
from src.config.logger_config import setup_logging

setup_logging()

import asyncio
import logging

logger = logging.getLogger(__name__)
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from os import getenv

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from google.cloud import storage

from src.admin.admin_controller import router as admin_router
from src.audios.audio_controller import router as audio_router
from src.brand_guidelines.brand_guideline_controller import (
    router as brand_guideline_router,
)
from src.config.config_service import config_service
from src.galleries.gallery_controller import router as gallery_router
from src.generation_options.generation_options_controller import (
    router as generation_options_router,
)
from src.images.imagen_controller import router as imagen_router
from src.media_templates.media_templates_controller import (
    router as media_template_router,
)
from src.multimodal.gemini_controller import router as gemini_router
from src.source_assets.source_asset_controller import (
    router as source_asset_router,
)
from src.tags.tags_controller import router as tags_router
from src.users.user_controller import router as user_router
from src.videos.veo_controller import router as video_router
from src.videos.vpe_controller import router as vpe_router
from src.workbench.router import router as workbench_router
from src.workflows.workflow_controller import router as workflow_router
from src.workflows_executor.workflows_executor_controller import (
    router as workflows_executor_router,
)
from src.workspaces.workspace_controller import router as workspace_router


def configure_cors(app):
    """Configures CORS middleware based on the environment."""
    raw_env = config_service.ENVIRONMENT
    environment = (
        raw_env.strip() if raw_env and raw_env.strip() else "development"
    ).lower()
    allowed_origins = []

    if environment == "production":
        frontend_url = getenv("FRONTEND_URL")
        if not frontend_url:
            raise ValueError(
                "FRONTEND_URL environment variable not set in production"
            )
        allowed_origins.append(frontend_url)
    elif environment in ["development", "test", "local"]:
        allowed_origins.append("*")  # Allow all origins in development
    else:
        raise ValueError(
            f"Invalid ENVIRONMENT: {environment}. Must be 'production', 'development' or 'local'",
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=environment == "production",
        allow_methods=["*"],
        allow_headers=["*"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for the FastAPI application.
    Handles startup and shutdown logic.
    """
    # --- Startup ---
    logger.info("Starting up application...")

    # Initialize Firebase Admin SDK (Auth only)
    try:
        from src.auth.firebase_client_service import firebase_client

        # Trigger initialization
        _ = firebase_client
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")

    # Run Database Migrations
    try:
        from src.database_migrations import run_pending_migrations

        await run_pending_migrations()
    except Exception as e:
        logger.error(f"Failed to run database migrations: {e}")
        # We might want to stop startup here if migrations fail
        raise e

    logger.info("Creating ThreadPoolExecutor...")
    # Create the pool and attach it to the app's state
    app.state.executor = ThreadPoolExecutor(max_workers=4)

    # VPE gets a pool of its own rather than sharing the one above, because
    # its jobs are an order of magnitude longer than anything else queued
    # there. A background worker holds its thread for the whole job - the
    # polling loops sleep rather than yield the thread - and a measured VPE
    # upscale takes around 315 seconds. Four of them would occupy every
    # worker the application has, so a user upscaling a library would stall
    # every unrelated generation and concatenation until it finished.
    # A separate pool means VPE saturation only ever delays VPE.
    app.state.vpe_executor = ThreadPoolExecutor(
        max_workers=config_service.VPE_MAX_CONCURRENT_JOBS,
        thread_name_prefix="vpe",
    )

    yield

    logger.info("Application shutdown terminating")

    logger.info("Closing ThreadPoolExecutor...")
    app.state.executor.shutdown(wait=True)
    # Waiting here would block shutdown for the length of a VPE job, which
    # can be several minutes. The operation is long-running and server-side,
    # so an interrupted worker loses the polling loop rather than the job;
    # it stays recoverable from the operation name recorded on the row.
    app.state.vpe_executor.shutdown(wait=False, cancel_futures=True)
    # Your shutdown logic here, e.g., closing database connections


app = FastAPI(
    lifespan=lifespan,
    title="Creative Studio API",
    description="""GenMedia Creative Studio is an app that highlights the capabilities
    of Google Cloud Vertex AI generative AI creative APIs, including Imagen, Veo, Lyria, Chirp and more! 🚀""",
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """This is the global 'catch-all' exception handler.
    It catches any exception that is not specifically handled by other exception handlers.
    """
    # Log the full error for debugging purposes
    logger.error(
        f"Unhandled exception for request {request.method} {request.url}: {exc}",
        exc_info=True,
    )

    # Return a standardized 500 Internal Server Error response
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


@app.get("/", tags=["Health Check"])
async def root():
    return "You are calling Creative Studio Backend"


@app.get("/api/version", tags=["Health Check"])
def version():
    return "v0.0.1"


@app.get("/api/media/stream", tags=["Media Proxy"])
async def stream_media(gcs_uri: str, request: Request):
    """Streams a GCS media asset directly to the browser with Range support."""
    if not gcs_uri.startswith("gs://"):
        raise HTTPException(status_code=400, detail="Invalid GCS URI")

    try:
        bucket_name, blob_name = gcs_uri.replace("gs://", "").split("/", 1)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        exists = await asyncio.to_thread(blob.exists)
        if not exists:
            raise HTTPException(
                status_code=404, detail="Media object not found in GCS"
            )

        await asyncio.to_thread(blob.reload)
        content_type = (
            blob.content_type
            or mimetypes.guess_type(blob_name)[0]
            or "application/octet-stream"
        )
        size = blob.size or 0

        range_header = request.headers.get("range")
        if range_header and size > 0:
            range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start = int(range_match.group(1))
                end = (
                    int(range_match.group(2))
                    if range_match.group(2)
                    else size - 1
                )
                length = end - start + 1

                def iter_range():
                    with blob.open("rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk_size = min(remaining, 64 * 1024)
                            data = f.read(chunk_size)
                            if not data:
                                break
                            remaining -= len(data)
                            yield data

                headers = {
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Content-Type": content_type,
                    "Cache-Control": "public, max-age=86400",
                }
                return StreamingResponse(
                    iter_range(), status_code=206, headers=headers
                )

        def iter_all():
            with blob.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(size),
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        }
        return StreamingResponse(iter_all(), headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to stream media: {e}"
        ) from e


configure_cors(app)

app.include_router(imagen_router)
app.include_router(admin_router)
app.include_router(audio_router)
app.include_router(video_router)
# Mounted unconditionally. Its own dependency answers 503 when VPE is off,
# which keeps the "is this deployment allowlisted?" decision in one place
# instead of splitting it between here and there.
app.include_router(vpe_router)
app.include_router(gallery_router)
app.include_router(gemini_router)
app.include_router(user_router)
app.include_router(generation_options_router)
app.include_router(media_template_router)
app.include_router(source_asset_router)
app.include_router(tags_router)
app.include_router(workspace_router)
app.include_router(brand_guideline_router)
app.include_router(workflow_router)
app.include_router(workflows_executor_router)
app.include_router(workbench_router)
