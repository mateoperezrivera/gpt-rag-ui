import logging
import os
from io import BytesIO

from fastapi import Response, Request, FastAPI
from fastapi.responses import StreamingResponse

# Configure logging FIRST
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'
)

from connectors import BlobClient
from connectors import AppConfigClient
from dependencies import get_config

# Load environment variables from Azure App Configuration
config : AppConfigClient = get_config()
logging.info("[main] Configuration loaded")

# Import chainlit_app AFTER config is ready
from chainlit.server import app as chainlit_app
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

def download_from_blob(file_name: str) -> bytes:
    logging.info("[chainlit_app] Downloading file: %s", file_name)

    blob_url = f"https://{account_name}.blob.core.windows.net/{file_name}"
    logging.debug(f"[chainlit_app] Constructed blob URL: {blob_url}")
    
    try:
        blob_client = BlobClient(blob_url=blob_url)
        blob_data = blob_client.download_blob()
        logging.debug(f"[chainlit_app] Successfully downloaded blob data: {file_name}")
        return blob_data
    except Exception as e:
        logging.error(f"[chainlit_app] Error downloading blob {file_name}: {e}")
        raise

account_name = config.get("STORAGE_ACCOUNT_NAME")
documents_container = config.get("DOCUMENTS_STORAGE_CONTAINER")
images_container = config.get("DOCUMENTS_IMAGES_STORAGE_CONTAINER")

def handle_file_download(file_path: str):
    try:
        file_bytes = download_from_blob(file_path)
        if not file_bytes:
            return Response("File not found or empty.", status_code=404, media_type="text/plain")
    except Exception as e:
        error_message = str(e)
        status_code = 404 if "BlobNotFound" in error_message else 500
        logging.exception(f"[chainlit_app] Download error: {error_message}")
        return Response(
            f"{'Blob not found' if status_code == 404 else 'Internal server error'}: {error_message}.",
            status_code=status_code,
            media_type="text/plain"
        )
    
    actual_file_name = os.path.basename(file_path)
    return StreamingResponse(
        BytesIO(file_bytes),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{actual_file_name}"'}
    )

# TODO: Validate blob metadata_security_id to prevent unauthorized access.

# Create a separate FastAPI app for blob downloads that will be mounted
blob_download_app = FastAPI()
logging.info("[main] Created blob_download_app FastAPI instance")

@blob_download_app.get("/{container_name}/{file_path:path}")
async def download_blob_file(container_name: str, file_path: str):
    logging.info(f"[BLOB_DOWNLOAD_APP] Download request: container={container_name}, file={file_path}")
    normalized = container_name.strip().strip("/")
    target_container = None
    if normalized == documents_container:
        target_container = documents_container
    elif normalized == images_container:
        target_container = images_container
    
    if not target_container:
        logging.warning(f"[BLOB_DOWNLOAD_APP] Unknown container: {container_name}")
        return Response("Container not found", status_code=404, media_type="text/plain")
    
    return handle_file_download(f"{target_container}/{file_path}")

logging.info("[main] Registered download_blob_file route on blob_download_app")

# Mount the blob download app BEFORE importing chainlit handlers
try:
    chainlit_app.mount("/api/download", blob_download_app)
    logging.info("[main] ✅ Blob download app successfully mounted at /api/download")
    logging.info(f"[main] Chainlit app routes after mount: {[r.path for r in chainlit_app.routes]}")
except Exception as e:
    logging.error(f"[main] ❌ Failed to mount blob_download_app: {e}")
    raise

# Import Chainlit event handlers
import app as chainlit_handlers

logging.info("[main] Chainlit handlers imported")

# ASGI entry point
app = chainlit_app

# Provide friendly app metadata used by OpenAPI (read version from VERSION file when present)
chainlit_app.title = getattr(chainlit_app, "title", "GPT-RAG UI")
try:
    if os.path.exists("VERSION"):
        chainlit_app.version = open("VERSION").read().strip()
except Exception:
    chainlit_app.version = getattr(chainlit_app, "version", "dev")

# Safe OpenAPI generator: try normal get_openapi, fall back to minimal schema on error
from fastapi.openapi.utils import get_openapi

def _safe_openapi():
    if getattr(chainlit_app, "openapi_schema", None):
        return chainlit_app.openapi_schema
    try:
        chainlit_app.openapi_schema = get_openapi(
            title=chainlit_app.title,
            version=chainlit_app.version,
            routes=chainlit_app.routes,
        )
    except Exception as exc:
        # Log the original exception and return a tiny fallback openapi schema so /docs and /openapi.json don't 500
        logging.exception("OpenAPI generation failed; returning fallback schema: %s", exc)
        chainlit_app.openapi_schema = {
            "openapi": "3.0.0",
            "info": {"title": chainlit_app.title, "version": chainlit_app.version},
            "paths": {},
        }
    return chainlit_app.openapi_schema

chainlit_app.openapi = _safe_openapi

FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()