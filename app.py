import os
import re
import uuid
import logging
import urllib.parse
from typing import Optional, Tuple

import chainlit as cl

from orchestrator_client import call_orchestrator_stream
from dependencies import get_config

from constants import APPLICATION_INSIGHTS_CONNECTION_STRING, APP_NAME, UUID_REGEX, REFERENCE_REGEX, TERMINATE_TOKEN
from telemetry import Telemetry
from opentelemetry.trace import SpanKind

config = get_config()

Telemetry.configure_monitoring(config, APPLICATION_INSIGHTS_CONNECTION_STRING, APP_NAME)

def extract_conversation_id_from_chunk(chunk: str) -> Tuple[Optional[str], str]:
    match = UUID_REGEX.match(chunk)
    if match:
        conv_id = match.group(1)
        logging.info("[app] Extracted Conversation ID: %s", conv_id)
        return conv_id, chunk[match.end():]
    return None, chunk

def replace_source_reference_links(text: str) -> str:
    def replacer(match):
        source_file = match.group(1)
        decoded = urllib.parse.unquote(source_file)
        encoded = urllib.parse.quote(decoded)
        return f"[{decoded}](/source/{encoded})"
    return re.sub(REFERENCE_REGEX, replacer, text)

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
            return
        
        span.set_attribute('message_id', message.id)
        span.set_attribute('conversation_id', conversation_id)
        span.set_attribute('user_id', app_user.metadata.get('client_principal_id', 'no-auth') if app_user else 'anonymous')

        await response_msg.stream_token(" ")

        buffer = ""
        full_text = ""
        references = set()
        auth_info = check_authorization()
        generator = call_orchestrator_stream(conversation_id, message.content, auth_info)

        try:
            async for chunk in generator:
                # logging.info("[app] Chunk received: %s", chunk)

                # Extract and update conversation ID
                extracted_id, cleaned_chunk = extract_conversation_id_from_chunk(chunk)
                if extracted_id:
                    conversation_id = extracted_id

                cleaned_chunk = cleaned_chunk.replace("\\n", "\n")

                # Track and clean references
                found_refs = set(REFERENCE_REGEX.findall(cleaned_chunk))
                if found_refs:
                    logging.info("[app] Found file references: %s", found_refs)
                references.update(found_refs)
                cleaned_chunk = REFERENCE_REGEX.sub("", cleaned_chunk)

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
            logging.exception("[app] Error during message handling.")
            await response_msg.stream_token(error_message)

        finally:
            try:
                await generator.aclose()
            except RuntimeError as exc:
                if "async generator ignored GeneratorExit" not in str(exc):
                    raise

        cl.user_session.set("conversation_id", conversation_id)
        await response_msg.update()

        # Final reference handling and update
        # references.update(REFERENCE_REGEX.findall(full_text))
        # final_text = replace_source_reference_links(full_text.replace(TERMINATE_TOKEN, ""))
        # response_msg.content = final_text
        await response_msg.update()
