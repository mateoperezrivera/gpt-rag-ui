import os
from typing import Dict, Any
from azure.identity import ChainedTokenCredential, ManagedIdentityCredential, AzureCliCredential
from azure.appconfiguration import AzureAppConfigurationClient
from azure.core.exceptions import AzureError

class AppConfigClient:
    
    allow_env_vars : bool = True

    def __init__(self):
        """
        Bulk-loads all keys labeled 'gpt-rag-ui' and 'gpt-rag' into an in-memory dict,
        giving precedence to 'gpt-rag-ui' where a key exists in both.
        """
        endpoint = os.getenv("APP_CONFIG_ENDPOINT")
        if not endpoint:
            raise EnvironmentError("APP_CONFIG_ENDPOINT must be set")

        credential = ChainedTokenCredential(ManagedIdentityCredential(), AzureCliCredential())
        # make client available to other methods
        self.client = AzureAppConfigurationClient(base_url=endpoint, credential=credential)

        self._settings: Dict[str, str] = {}
        self._load_settings()

    def _load_settings(self):
        # 1) Load everything labeled “gpt-rag-ui”
        try:
            for setting in self.client.list_configuration_settings(label_filter="gpt-rag-ui"):
                self._settings[setting.key] = setting.value
        except AzureError as e:
            raise RuntimeError(f"Failed to bulk-load 'gpt-rag-ui' settings: {e}")

        # 2) Load “gpt-rag” ones only if not already present
        try:
            for setting in self.client.list_configuration_settings(label_filter="gpt-rag"):
                self._settings.setdefault(setting.key, setting.value)
        except AzureError as e:
            raise RuntimeError(f"Failed to bulk-load 'gpt-rag' settings: {e}")

    def apply_environment_settings(self) -> None:
        """
        Pushes loaded settings into os.environ.
        Keys from 'gpt-rag-ui' will overwrite any existing env-vars;
        keys from 'gpt-rag' will only be set if not already present.
        """
        # first, frontend (always overwrite)
        for setting in self.client.list_configuration_settings(label_filter="gpt-rag-ui"):
            os.environ[setting.key] = setting.value

        # then, rag—but only if the key isn't already in os.environ
        for setting in self.client.list_configuration_settings(label_filter="gpt-rag"):
            os.environ.setdefault(setting.key, setting.value)

    def get(self, key: str, default: Any = None, type: type = str) -> Any:
        """
        Returns the in-memory value for the given key.

        If the key was not found under either label, returns `default`.
        """
        if self.allow_env_vars is True:
            value = os.environ.get(key)
            
        if value is None:
            value = self._settings.get(key, default)

        if value is None:
            return default

        if value is not None:
            if type is not None:
                if type is bool:
                    if isinstance(value, str):
                        value = value.lower() in ['true', '1', 'yes']
                else:
                    try:
                        value = type(value)
                    except ValueError as e:
                        raise Exception(f'Value for {key} could not be converted to {type.__name__}. Error: {e}')
            
        return value