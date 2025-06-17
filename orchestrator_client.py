import os
import json
import httpx
import logging
from azure.identity import ManagedIdentityCredential, AzureCliCredential, ChainedTokenCredential
import requests

# Obtain the token using Managed Identity
def get_managed_identity_token():
    credential = ChainedTokenCredential(
        ManagedIdentityCredential(),
        AzureCliCredential()
    )
    token = credential.get_token("https://management.azure.com/.default").token
    return token

async def call_orchestrator_stream(conversation_id: str, question: str, auth_info: dict):

    url = os.getenv("ORCHESTRATOR_APP_ENDPOINT")
    if not url:
        raise Exception("ORCHESTRATOR_APP_ENDPOINT not set in environment variables")

    url = url.rstrip("/") + "/orcstream"

    headers = {
            'Content-Type': 'application/json'
        }

    payload = {
        "conversation_id": conversation_id,
        "question": question,
        "client_principal_id": auth_info.get('client_principal_id', 'no-auth'),
        "client_principal_name": auth_info.get('client_principal_name', 'anonymous'),
        "client_group_names": auth_info.get('client_group_names', []),
        "access_token": auth_info.get('access_token')
    }

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                raise Exception(f"Error calling orchestrator. HTTP status code: {response.status_code}. Details: {response.reason_phrase}")
            async for chunk in response.aiter_text():
                if not chunk:
                    continue
                yield chunk
                # logging.info("[orchestrator_client] Yielding text chunk: %s", chunk)


