import logging
import os
from io import BytesIO

from fastapi import Response
from fastapi.responses import StreamingResponse
from chainlit.server import app as chainlit_app
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from connectors import BlobClient
from connectors import AppConfigClient

from dependencies import get_config

# Load environment variables from Azure App Configuration
config : AppConfigClient = get_config()

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

@chainlit_app.get(f"/{documents_container}/" + "{file_path:path}")
def download_document(file_path: str):
    return handle_file_download(f"{documents_container}/{file_path}")

@chainlit_app.get(f"/{images_container}/" + "{file_path:path}")
def download_image(file_path: str):
    return handle_file_download(f"{images_container}/{file_path}")


# -----------------------------------
# Ensure source routes are prioritized
# -----------------------------------
try:
    images_route = next(route for route in chainlit_app.router.routes if getattr(route, "path", "").startswith(f"/{documents_container}/"))
    chainlit_app.router.routes.remove(images_route)
    chainlit_app.router.routes.insert(0, images_route)
    documents_route = next(route for route in chainlit_app.router.routes if getattr(route, "path", "").startswith(f"/{images_container}/"))
    chainlit_app.router.routes.remove(documents_route)
    chainlit_app.router.routes.insert(0, documents_route)
    logging.info("[chainlit_app] Moved source routes to the top of the route list.")
except StopIteration:
    logging.warning("[chainlit_app] source route not found; skipping reorder.")

# Import Chainlit event handlers (side-effect registration)
import app

# ASGI entry point
app = chainlit_app

FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()