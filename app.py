import os
import re
import uuid
import logging
import urllib.parse
from typing import Optional, Set, Tuple
from datetime import datetime, timedelta

import chainlit as cl

from orchestrator_client import call_orchestrator_stream
from feedback import register_feedback_handlers,create_feedback_actions
from dependencies import get_config
from connectors import BlobClient

from constants import APPLICATION_INSIGHTS_CONNECTION_STRING, APP_NAME, UUID_REGEX, REFERENCE_REGEX, TERMINATE_TOKEN
from telemetry import Telemetry
from opentelemetry.trace import SpanKind

config = get_config()

Telemetry.configure_monitoring(config, APPLICATION_INSIGHTS_CONNECTION_STRING, APP_NAME)

ENABLE_FEEDBACK = config.get("ENABLE_USER_FEEDBACK", False, bool)
STORAGE_ACCOUNT_NAME = config.get("STORAGE_ACCOUNT_NAME", "", str)


def _normalize_container_name(container: Optional[str]) -> str:
    if not container:
        return ""
    return container.strip().strip("/")


DOCUMENTS_CONTAINER = _normalize_container_name(
    config.get("DOCUMENTS_STORAGE_CONTAINER", "", str)
)
IMAGES_CONTAINER = _normalize_container_name(
    config.get("DOCUMENTS_IMAGES_STORAGE_CONTAINER", "", str)
)
IMAGE_EXTENSIONS = {"bmp", "jpeg", "jpg", "png", "tiff"}

def extract_conversation_id_from_chunk(chunk: str) -> Tuple[Optional[str], str]:
    match = UUID_REGEX.match(chunk)
    if match:
        conv_id = match.group(1)
        logging.info("[app] Extracted Conversation ID: %s", conv_id)
        return conv_id, chunk[match.end():]
    return None, chunk

def generate_blob_sas_url(container: str, blob_name: str, expiry_hours: int = 1) -> str:
    """
    Generate a time-limited SAS URL for direct blob download.
    This bypasses Container Apps routing completely.
    """
    try:
        blob_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{container}/{blob_name}"
        blob_client = BlobClient(blob_url=blob_url)
        
        # Generate SAS token with read permission
        from datetime import datetime, timedelta, timezone
        expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
        
        # Try to generate SAS URL (requires azure-storage-blob with SAS support)
        try:
            sas_url = blob_client.generate_sas_url(expiry=expiry, permissions="r")
            logging.info(f"[app] Generated SAS URL for {container}/{blob_name} (expires in {expiry_hours}h)")
            return sas_url
        except AttributeError:
            # Fallback: return direct blob URL (relies on public access or managed identity at client side)
            logging.warning(f"[app] SAS generation not supported, using direct blob URL for {container}/{blob_name}")
            return blob_url
    except Exception as e:
        logging.error(f"[app] Failed to generate blob URL for {container}/{blob_name}: {e}")
        # Fallback to the old /api/download/ route as last resort
        return f"/api/download/{container}/{blob_name}"

def resolve_reference_href(raw_href: str) -> str:
    href = (raw_href or "").strip()
    if not href:
        return href

    split_href = urllib.parse.urlsplit(href)
    if split_href.scheme or split_href.netloc:
        return href

    if href.startswith("/api/download/") or href.startswith("api/download/"):
        return href

    path = urllib.parse.unquote(split_href.path.replace("\\", "/")).lstrip("/")
    query = f"?{split_href.query}" if split_href.query else ""
    fragment = f"#{split_href.fragment}" if split_href.fragment else ""

    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    container = DOCUMENTS_CONTAINER
    if extension in IMAGE_EXTENSIONS and IMAGES_CONTAINER:
        container = IMAGES_CONTAINER
    elif not container and IMAGES_CONTAINER:
        container = IMAGES_CONTAINER

    # Extract clean blob name
    if container:
        if path.startswith(f"{container}/"):
            blob_name = path[len(container)+1:]
        elif path:
            blob_name = path
        else:
            blob_name = ""
    else:
        blob_name = path

    if not blob_name:
        return href

    # Generate direct SAS URL to Azure Blob Storage (bypasses Container Apps completely)
    sas_url = generate_blob_sas_url(container, blob_name)
    
    # Add original query and fragment if present
    if query or fragment:
        separator = "&" if "?" in sas_url else "?"
        return f"{sas_url}{separator}{query.lstrip('?')}{fragment}"
    
    return sas_url


def replace_source_reference_links(text: str, references: Optional[Set[str]] = None) -> str:
    def replacer(match):
        display_text = match.group(1)
        raw_href = match.group(2)
        resolved_href = resolve_reference_href(raw_href)
        if references is not None:
            references.add(resolved_href)
        logging.debug("[app] Resolved reference '%s' -> '%s'", raw_href, resolved_href)
        return f"[{display_text}]({resolved_href})"

    return REFERENCE_REGEX.sub(replacer, text)

def check_authorization() -> dict:
    app_user = cl.user_session.get("user")
    if app_user:
        metadata = app_user.metadata or {}
        return {
            'authorized': metadata.get('authorized', True),
            'client_principal_id': metadata.get('client_principal_id', 'no-auth'),
            'client_principal_name': metadata.get('client_principal_name', 'anonymous'),
            'client_group_names': metadata.get('client_group_names', []),
            'access_token': metadata.get('access_token')
        }

    return {
        'authorized': True,
        'client_principal_id': 'no-auth',
        'client_principal_name': 'anonymous',
        'client_group_names': [],
        'access_token': None
    }

# Check if authentication is enabled
ENABLE_AUTHENTICATION = config.get("ENABLE_AUTHENTICATION", False, bool)
if ENABLE_AUTHENTICATION:
    import auth

tracer = Telemetry.get_tracer(__name__)

# Register feedback handlers
if ENABLE_FEEDBACK:
    register_feedback_handlers(check_authorization)

# Chainlit event handlers
@cl.on_chat_start
async def on_chat_start():
    pass
    # app_user = cl.user_session.get("user")
    # if app_user:
        # await cl.Message(content=f"Hello {app_user.metadata.get('user_name')}").send()

@cl.on_message
async def handle_message(message: cl.Message):
    
    with tracer.start_as_current_span('handle_message', kind=SpanKind.SERVER) as span:

        message.id = message.id or str(uuid.uuid4())
        conversation_id = cl.user_session.get("conversation_id") or ""
        response_msg = cl.Message(content="")

        app_user = cl.user_session.get("user")
        if app_user and not app_user.metadata.get('authorized', True):
            await response_msg.stream_token(
                "Oops! It looks like you don’t have access to this service. "
                "If you think you should, please reach out to your administrator for help."
            )
            await response_msg.send()
            return
        
        span.set_attribute('question_id', message.id)
        span.set_attribute('conversation_id', conversation_id)
        span.set_attribute('user_id', app_user.metadata.get('client_principal_id', 'no-auth') if app_user else 'anonymous')

        await response_msg.stream_token(" ")

        buffer = ""
        full_text = ""
        references = set()
        auth_info = check_authorization()
        generator = call_orchestrator_stream(conversation_id, message.content, auth_info, message.id)

        try:
            async for chunk in generator:
                # logging.info("[app] Chunk received: %s", chunk)

                # Extract and update conversation ID
                extracted_id, cleaned_chunk = extract_conversation_id_from_chunk(chunk)
                if extracted_id:
                    conversation_id = extracted_id

                cleaned_chunk = cleaned_chunk.replace("\\n", "\n")

                # Track and rewrite references as blob download links
                chunk_refs: Set[str] = set()
                cleaned_chunk = replace_source_reference_links(cleaned_chunk, chunk_refs)
                if chunk_refs:
                    references.update(chunk_refs)
                    logging.info("[app] Found file references: %s", chunk_refs)

                buffer += cleaned_chunk
                full_text += cleaned_chunk

                # Handle TERMINATE token
                token_index = buffer.find(TERMINATE_TOKEN)
                if token_index != -1:
                    if token_index > 0:
                        await response_msg.stream_token(buffer[:token_index])
                    logging.info("[app] TERMINATE token detected. Draining remaining chunks...")
                    async for _ in generator:
                        pass  # drain
                    break

                # Stream safe part of buffer
                if token_index != -1:
                    safe_flush_length = len(buffer) - (len(TERMINATE_TOKEN) - 1)
                else:
                    safe_flush_length = len(buffer)

                if safe_flush_length > 0:
                    await response_msg.stream_token(buffer[:safe_flush_length])
                    buffer = buffer[safe_flush_length:]

        except Exception as e:
            error_message = (
                "I'm sorry, I had a problem with the request. Please report the error. "
                f"Details: {e}"
            )
            logging.exception(f"[app] Error during message handling.{e}")
            await response_msg.stream_token(error_message)           
            await response_msg.send()
            return
        finally:
            try:
                await generator.aclose()
            except RuntimeError as exc:
                if "async generator ignored GeneratorExit" not in str(exc):
                    raise

        cl.user_session.set("conversation_id", conversation_id)
        if ENABLE_FEEDBACK:
            response_msg.actions = create_feedback_actions(
                message.id, conversation_id, message.content
            )
        final_text = replace_source_reference_links(
            full_text.replace(TERMINATE_TOKEN, ""), references
        )
        response_msg.content = final_text
        await response_msg.send()

@cl.data_layer
def get_data_layer():
    try:
        from data_layer import CosmosDBDataLayer
    except ModuleNotFoundError:
        # Fallback: import by file path when running as top-level script or in non-package context
        import importlib.util
        import pathlib

        data_layer_path = pathlib.Path(__file__).parent / "data_layer.py"
        spec = importlib.util.spec_from_file_location("data_layer", str(data_layer_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore
        CosmosDBDataLayer = getattr(module, "CosmosDBDataLayer")

    datalayer = CosmosDBDataLayer(
        "cosmos-dbwrbdken34r274",
        "chainlit",
        account_endpoint="https://cosmos-wrbdken34r274.documents.azure.com:443/",
    )
    return datalayer