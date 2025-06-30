import httpx

from dependencies import get_config

config = get_config()

async def call_orchestrator_stream(conversation_id: str, question: str, auth_info: dict):

    url = config.get("ORCHESTRATOR_APP_ENDPOINT")
    if not url:
        raise Exception("ORCHESTRATOR_APP_ENDPOINT not set in environment variables")

    url = url.rstrip('/') + '/orchestrator'

    api_key = config.get("ORCHESTRATOR_APP_APIKEY")

    headers = {
            'Content-Type': 'application/json',
        }
    
    if api_key:
        headers['X-API-KEY'] = api_key

    payload = {
        "conversation_id": conversation_id,
        "question": question, #for backward compatibility
        "ask": question,
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


