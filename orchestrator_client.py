import os
import httpx
import logging
from azure.identity import ManagedIdentityCredential, AzureCliCredential, ChainedTokenCredential
from dependencies import get_config
config = get_config()

# Obtain an Azure AD token via Managed Identity or Azure CLI credentials
def get_managed_identity_token():
    credential = ChainedTokenCredential(
        ManagedIdentityCredential(),
        AzureCliCredential()
    )
    return credential.get_token("https://management.azure.com/.default").token

async def call_orchestrator_stream(conversation_id: str, question: str, auth_info: dict):
    # Read Dapr settings and target app ID
    dapr_port = os.getenv("DAPR_HTTP_PORT", "3500")
    # orchestrator_app_id = os.getenv("ORCHESTRATOR_APP_ID")
    # if not orchestrator_app_id:
    #     raise Exception("ORCHESTRATOR_APP_ID not set in environment variables")
    orchestrator_app_id = "orchestrator"  # Default app ID for local development
    # Build Dapr service invocation URL
    url = f"http://127.0.0.1:{dapr_port}/v1.0/invoke/{orchestrator_app_id}/method/orchestrator"

    # Read the Dapr sidecar API token
    dapr_token = os.getenv("DAPR_API_TOKEN")
    if not dapr_token:
        logging.warning("DAPR_API_TOKEN is not defined; Dapr calls may fail")

    # Prepare headers: content-type + Dapr token
    headers = {
        "Content-Type": "application/json",
        "dapr-api-token": dapr_token or ""
    }
    
    api_key = config.get("ORCHESTRATOR_APP_APIKEY")
    if api_key:
        headers['X-API-KEY'] = api_key
    
    # Construct request body
    payload = {
        "conversation_id": conversation_id,
        "question": question, #for backward compatibility
        "ask": question,
        "client_principal_id": auth_info.get('client_principal_id', 'no-auth'),
        "client_principal_name": auth_info.get('client_principal_name', 'anonymous'),
        "client_group_names": auth_info.get('client_group_names', []),
        "access_token": auth_info.get('access_token')
    }

    # Invoke through Dapr sidecar and stream response
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise Exception(
                    f"Error invoking via Dapr (HTTP {response.status_code}): "
                    f"{response.reason_phrase}. Details: {body.decode(errors='ignore')}"
                )
            async for chunk in response.aiter_text():
                if chunk:
                    yield chunk
