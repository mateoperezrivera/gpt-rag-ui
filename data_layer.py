import logging
import os
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, cast
import uuid
import aiofiles
from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey, exceptions
from azure.identity.aio import DefaultAzureCredential
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from chainlit.context import context
from chainlit.data.base import BaseDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.data.utils import queue_until_user_message
from azure.cosmos import CosmosClient as SyncCosmosClient
from chainlit.data.storage_clients.azure_blob import AzureBlobStorageClient
import chainlit as cl

import asyncio

from connectors import blob
if TYPE_CHECKING:
    from chainlit.element import Element, ElementDict
from chainlit.logger import logger
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

_logger = logger.getChild("CosmosDB")
_logger.setLevel(logging.WARNING)
# Disable verbose Azure SDK logging
logging.getLogger('azure').setLevel(logging.WARNING)
logging.getLogger('azure.cosmos').setLevel(logging.ERROR)
logging.getLogger('azure.identity').setLevel(logging.ERROR)
logging.getLogger('azure.core').setLevel(logging.ERROR)

# Disable urllib3 connection pool logs
logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)

# Optional: Disable HTTP request/response logs completely
logging.getLogger('azure.core.pipeline.policies.http_logging_policy').setLevel(logging.ERROR)

@cl.data_layer
def get_data_layer():
    storage_account = os.getenv("BLOB_STORAGE_ACCOUNT")
    account_endpoint = os.getenv("COSMOS_DB_ENDPOINT")
    database_name = os.getenv("COSMOS_DB_DATABASE")
    blob_storage_container = os.getenv("BLOB_STORAGE_CONTAINER")
    if not all([storage_account, account_endpoint, database_name,blob_storage_container]):
        logging.warning(
            "CosmosDB data layer not configured properly.Chat history and user data will not be persisted.")
        return None
    else:
        storage_client = AzureBlobStorageClient(
            storage_account=storage_account,
            container_name=blob_storage_container,
        )
        datalayer = CosmosDBDataLayer(
            account_endpoint=account_endpoint,
            database_name=database_name,
            container_name=os.getenv("COSMOS_DB_CONTAINER"),
            use_msi=True,
            storage_provider=storage_client,
        )
        datalayer._init_database_and_container()
        return datalayer

class CosmosDBDataLayer(BaseDataLayer):
    def __init__(
        self,
        database_name: str,
        container_name: str,
        connection_string: str | None = None,
        account_endpoint: str | None = None,
        account_key: str | None = None,
        use_msi: bool = False,
        storage_provider: BaseStorageClient | None = None,
        user_thread_limit: int = 10,
    ):
        """
        Initialize the Cosmos DB data layer.

        Args:
            database_name: The name of the Cosmos DB database
            container_name: The name of the container to use
            connection_string: A connection string for Cosmos DB (alternative to account_endpoint/key)
            account_endpoint: The Cosmos DB account endpoint (URL)
            account_key: The Cosmos DB account key
            use_msi: Whether to use Managed Identity for authentication
            storage_provider: Optional storage provider for larger content
            user_thread_limit: Maximum number of threads to return per user
        """
        if connection_string:
            self.client = CosmosClient.from_connection_string(connection_string)
        elif use_msi:
            credential = DefaultAzureCredential()
            account_endpoint = account_endpoint or os.environ.get("COSMOSDB_ENDPOINT")
            if not account_endpoint:
                raise ValueError("Account endpoint must be provided when using MSI")
            self.client = CosmosClient(account_endpoint, credential=credential)
        elif account_endpoint and account_key:
            self.client = CosmosClient(account_endpoint, credential=account_key)
        else:
            account_endpoint = os.environ.get("COSMOSDB_ENDPOINT")
            account_key = os.environ.get("COSMOSDB_KEY")
            if not (account_endpoint and account_key):
                raise ValueError(
                    "Either connection_string or both account_endpoint and account_key must be provided"
                )
            self.client = CosmosClient(account_endpoint, credential=account_key)

        self.database_name = database_name
        self.container_name = container_name or "chainlit"
        self.storage_provider = storage_provider
        self.user_thread_limit = user_thread_limit
        self.database = self.client.get_database_client(self.database_name)
        self.threads_container = self.database.get_container_client(f"{self.container_name}_items")
        self.user_container = self.database.get_container_client(
            f"{self.container_name}_users"
        )
        self.connection_string = connection_string
        self.use_msi = use_msi

    THREAD_CONTAINER_INDEXING_POLICY = {
        "indexingMode": "consistent",
        "automatic": True,  # Let CosmosDB handle the root path
        "includedPaths": [
            {
                "path": "/data_type/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
            {
                "path": "/threadId/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
            {"path": "/userId/?", "indexes": [{"kind": "Range", "dataType": "String"}]},
            {
                "path": "/createdAt/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
        ],
        "excludedPaths": [
            {"path": "/*"} 
        ],
        "compositeIndexes": [
            [
                {"path": "/threadId", "order": "ascending"},
                {"path": "/data_type", "order": "ascending"},
            ],
            [
                {"path": "/threadId", "order": "ascending"},
                {"path": "/createdAt", "order": "ascending"},
            ],
        ],
    }
    USER_CONTAINER_INDEXING_POLICY = {
        "indexingMode": "consistent",
        "automatic": True,
        "includedPaths": [
            {
                "path": "/identifier/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
        ],
        "excludedPaths": [
            {"path": "/*"}  # Exclude paths not explicitly included
        ],
    }

    def _init_database_and_container(self):
        """Initialize the database and containers if they don't exist"""
        # Create a synchronous client for initialization
        if hasattr(self, "connection_string") and self.connection_string:
            sync_client = SyncCosmosClient.from_connection_string(
                self.connection_string
            )
        elif hasattr(self, "use_msi") and self.use_msi:
            sync_credential = SyncDefaultAzureCredential()
            account_endpoint = "https://dbgpt0-ifqbichztvg3i.documents.azure.com:443/"
            if not account_endpoint:
                raise ValueError("Account endpoint must be provided when using MSI")
            sync_client = SyncCosmosClient(account_endpoint, credential=sync_credential)
        else:
            account_endpoint = getattr(
                self, "account_endpoint", None
            ) or os.environ.get("COSMOSDB_ENDPOINT")
            account_key = getattr(self, "account_key", None) or os.environ.get(
                "COSMOSDB_KEY"
            )
            if not (account_endpoint and account_key):
                raise ValueError(
                    "Either connection_string or both account_endpoint and account_key must be provided"
                )
            sync_client = SyncCosmosClient(account_endpoint, credential=account_key)

        # Use synchronous operations
        database = sync_client.get_database_client(self.database_name)

        try:
            # Create main thread container (existing)
            database.create_container(
                id=f"{self.container_name}_items",
                partition_key=PartitionKey(path="/threadId"),
                indexing_policy=self.THREAD_CONTAINER_INDEXING_POLICY,
            )
        except exceptions.CosmosResourceExistsError:
            logging.warning(
                f"CosmosDB: Container {self.container_name} already exists. Skipping creation."
            )

        try:
            # Create user container
            database.create_container(
                id=f"{self.container_name}_users",
                partition_key=PartitionKey(path="/id"),
                indexing_policy=self.USER_CONTAINER_INDEXING_POLICY,
            )
        except exceptions.CosmosResourceExistsError:
            logging.warning(
                f"CosmosDB: Container {self.container_name}_users already exists. Skipping creation."
            )

    def _get_current_timestamp(self) -> str:
        return datetime.now().isoformat() + "Z"

    @property
    def context(self):
        return context

    async def get_user(self, identifier: str) -> Optional["PersistedUser"]:

        query = "SELECT * FROM c WHERE c.identifier = @identifier"
        parameters = [{"name": "@identifier", "value": identifier}]
        users = []
        async for item in self.user_container.query_items(
            query=query, parameters=parameters
        ):
            users.append(item)

        if not users:
            return None

        user = users[0]
        return PersistedUser(
            id=user["id"],
            identifier=user["identifier"],
            createdAt=user["createdAt"],
            metadata=user.get("metadata", {}),
            display_name=user.get("display_name", None),
        )

    async def create_user(self, user: "User") -> Optional["PersistedUser"]:
        ts = self._get_current_timestamp()
        id = str(uuid.uuid4())
        item = {
            "id": id,
            "identifier": user.identifier,
            "metadata": user.metadata,
            "createdAt": ts,
            "threads": {},
            "display_name": user.display_name,
        }

        await self.user_container.create_item(body=item)

        return PersistedUser(
            id=id,
            identifier=user.identifier,
            createdAt=ts,
            metadata=user.metadata,
            display_name=user.display_name,
        )

    async def delete_feedback(self, feedback_id: str) -> bool:
        # Parse the feedback ID format from THREAD#{thread_id}::STEP#{step_id}
        thread_id, step_id = feedback_id.split("::")
        thread_id = thread_id.strip("THREAD#")
        step_id = step_id.strip("STEP#")

        parameters = [
            {"name": "@data_type", "value": "STEP"},
            {"name": "@step_id", "value": step_id},
        ]
        query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @step_id
        """

        steps = [
            step
            async for step in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]

        if not steps:
            return False

        step = steps[0]
        if "feedback" in step:
            del step["feedback"]
            await self.threads_container.replace_item(item=step["id"], body=step)

        return True

    async def upsert_feedback(self, feedback: Feedback) -> str:
        if not feedback.forId:
            raise ValueError(
                "CosmosDB data layer expects value for feedback.threadId got None"
            )

        parameters = [
            {"name": "@data_type", "value": "STEP"},
            {"name": "@for_id", "value": feedback.forId},
        ]
        query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @for_id
        """
        steps = [
            step
            async for step in self.threads_container.query_items(
                query=query, partition_key=feedback.threadId, parameters=parameters
            )
        ]

        if not steps:
            raise ValueError(f"Step not found for feedback: {feedback.forId}")

        step = steps[0]
        feedback.id = f"THREAD#{feedback.threadId}::STEP#{feedback.forId}"
        step["feedback"] = asdict(feedback)
        await self.threads_container.replace_item(item=step["id"], body=step)

        return feedback.id

    @queue_until_user_message()
    async def create_element(self, element: "Element"):
        if not element.for_id:
            return

        if not self.storage_provider:
            _logger.warning(
                "CosmosDB: create_element error. No storage_provider is configured!"
            )
            return

        content: bytes | str | None = None

        if element.content:
            content = element.content
        elif element.path:
            async with aiofiles.open(element.path, "rb") as f:
                content = await f.read()
        elif element.url:
            async with aiohttp.ClientSession() as session:
                async with session.get(element.url) as response:
                    if response.status == 200:
                        content = await response.read()
                    else:
                        raise ValueError(
                            f"Failed to read content from {element.url} status {response.status}",
                        )
        else:
            raise ValueError("Element url, path or content must be provided")

        if content is None:
            raise ValueError("Content is None, cannot upload file")

        if not element.mime:
            element.mime = "application/octet-stream"

        context_user = self.context.session.user
        user_folder = getattr(context_user, "id", "unknown")
        file_object_key = f"{user_folder}/{element.thread_id}/{element.id}"

        uploaded_file = await self.storage_provider.upload_file(
            object_key=file_object_key,
            data=content,
            mime=element.mime,
            overwrite=True,
        )

        if not uploaded_file:
            raise ValueError(
                "CosmosDB Error: create_element, Failed to persist data in storage_provider",
            )

        element_dict = element.to_dict()
        cosmos_element = {
            **element_dict,
            "id": element.id,
            "threadId": element.thread_id,
            "url": uploaded_file.get("url"),
            "objectKey": uploaded_file.get("object_key"),
            "data_type": "ELEMENT",
        }

        await self.threads_container.create_item(body=cosmos_element)

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional["ElementDict"]:
        parameters = [
            {"name": "@data_type", "value": "ELEMENT"},
            {"name": "@element_id", "value": element_id},
        ]
        query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type
        AND c.id = @element_id
        """

        elements = [
            element
            async for element in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]

        if not elements:
            return None

        return cast("ElementDict", elements[0])

    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: str | None = None):
        thread_id = self.context.session.thread_id

        parameters = [
            {"name": "@data_type", "value": "ELEMENT"},
            {"name": "@element_id", "value": element_id},
        ]
        query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @element_id
        """

        elements = [
            element
            async for element in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]

        if elements:
            element = elements[0]
            await self.threads_container.delete_item(
                item=element["id"], partition_key=thread_id
            )

    @queue_until_user_message()
    async def create_step(self, step_dict: "StepDict") -> None:
        item = dict(step_dict)
        item["data_type"] = "STEP"
        await self.threads_container.create_item(body=item)
        

    @queue_until_user_message()
    async def update_step(self, step_dict: "StepDict"):
        if "threadId" not in step_dict or "id" not in step_dict:
            raise ValueError("Missing required threadId or id in step_dict")
        thread_id = step_dict["threadId"]
        step_id = step_dict["id"]
        parameters = [
            {"name": "@data_type", "value": "STEP"},
            {"name": "@step_id", "value": step_id},
        ]
        query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @step_id
        """

        steps = [
            step
            async for step in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]


        step = steps[0]
        for key, value in step_dict.items():
            if value is not None:
                step[key] = value

        await self.threads_container.replace_item(item=step["id"], body=step)

    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        thread_id = self.context.session.thread_id
        parameters = [
            {"name": "@data_type", "value": "STEP"},
            {"name": "@step_id", "value": step_id},
        ]
        query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @step_id
        """
        steps = [
            step
            async for step in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]

        if steps:
            step = steps[0]
            await self.threads_container.delete_item(
                item=step["id"], partition_key=thread_id
            )

    async def delete_thread(self, thread_id: str):
        """Delete a thread and remove it from the user's threads list"""
        # 1. Get thread info using direct lookup instead of query
        try:
            # Direct point read by ID - more efficient than querying
            thread = await self.threads_container.read_item(
                item=thread_id, 
                partition_key=thread_id
            )
            # Extract user ID
            user_id = thread.get("userId")
            if user_id:
                try:
                    # Update user document
                    user = await self.user_container.read_item(
                        item=user_id, partition_key=user_id
                    )

                    if "threads" in user and thread_id in user["threads"]:
                        del user["threads"][thread_id]
                        await self.user_container.replace_item(item=user["id"], body=user)

                except exceptions.CosmosResourceNotFoundError:
                    logging.warning(f"User {user_id} not found when deleting thread {thread_id}")
        
        except exceptions.CosmosResourceNotFoundError:
            logging.warning(f"Thread {thread_id} not found when attempting to delete")
            
        # 2. Query all items in this thread
        all_items_query = """
        SELECT c.id FROM c 
        WHERE c.threadId = @thread_id
        """
        parameters = [{"name": "@thread_id", "value": thread_id}]
        
        # Get all item IDs
        item_ids = [
            item["id"] 
            async for item in self.threads_container.query_items(
                query=all_items_query, 
                partition_key=thread_id, 
                parameters=parameters
            )
        ]
        delete_tasks = [
            self.threads_container.delete_item(item=item_id, partition_key=thread_id)
            for item_id in item_ids
        ]
        await asyncio.gather(*delete_tasks)
        

    async def list_threads(
        self, pagination: "Pagination", filters: "ThreadFilter"
    ) -> "PaginatedResponse[ThreadDict]":
        """List threads using only user container with optimized in-memory filtering"""

        # Validate required userId
        if not filters.userId:
            logging.warning(
                "No userId provided for list_threads, returning empty result"
            )
            return PaginatedResponse(
                data=[],
                pageInfo=PageInfo(
                    hasNextPage=False,
                    startCursor=pagination.cursor,
                    endCursor=None,
                ),
            )

        # Initialize pagination
        offset = 0
        if pagination.cursor:
            try:
                offset = int(pagination.cursor)
            except ValueError:
                pass

        limit = pagination.first

        try:
            # Direct lookup by user ID - uses partition key for optimal performance
            user = await self.user_container.read_item(
                item=filters.userId, partition_key=filters.userId
            )

            # Handle case where user has no threads
            if "threads" not in user or not user["threads"]:
                logging.debug(f"User {filters.userId} has no threads")
                return PaginatedResponse(
                    data=[],
                    pageInfo=PageInfo(
                        hasNextPage=False,
                        startCursor=pagination.cursor,
                        endCursor=None,
                    ),
                )

            # Convert the threads dictionary to a list of tuples for sorting/filtering
            thread_items = list(user["threads"].items())

            # Apply name search filter if provided
            if filters.search:
                search_term = filters.search.lower()
                thread_items = [
                    (id, details)
                    for id, details in thread_items
                    if search_term in details.get("name", "").lower()
                ]
                logging.debug(
                    f"Filtered to {len(thread_items)} threads matching search term '{filters.search}'"
                )

            # Sort by most recent first (based on createdAt)
            thread_items.sort(key=lambda x: x[1].get("createdAt", ""), reverse=True)

            # Apply pagination
            total_threads = len(thread_items)
            end_pos = min(offset + limit + 1, total_threads)
            paged_threads = thread_items[offset:end_pos]

            has_next_page = len(paged_threads) > limit

            if has_next_page:
                paged_threads = paged_threads[:limit]

            # Convert to ThreadDict objects
            thread_dicts: List[ThreadDict] = list()
            for thread_id, thread_details in paged_threads:
                thread_dict = ThreadDict(
                    id=thread_id,
                    createdAt=thread_details["createdAt"],
                    name=thread_details.get("name", "New Thread"),
                    userId=filters.userId,
                    userIdentifier=filters.userId,
                    tags=thread_details.get("tags", []),
                    metadata={},  # Limited metadata in user's thread list
                    steps=[],  # Steps not included in summary
                    elements=[],  # Elements not included in summary
                )
                thread_dicts.append(thread_dict)

            # Create pagination info
            end_cursor = str(offset + limit) if has_next_page else None

            logging.debug(
                f"Returning {len(thread_dicts)} threads for user {filters.userId}"
            )

            return PaginatedResponse(
                data=thread_dicts,
                pageInfo=PageInfo(
                    hasNextPage=has_next_page,
                    startCursor=pagination.cursor,
                    endCursor=end_cursor,
                ),
            )

        except exceptions.CosmosResourceNotFoundError:
            # User not found
            logging.warning(f"User {filters.userId} not found")
            return PaginatedResponse(
                data=[],
                pageInfo=PageInfo(
                    hasNextPage=False,
                    startCursor=pagination.cursor,
                    endCursor=None,
                ),
            )

    async def get_thread(self, thread_id: str) -> "ThreadDict | None":
        parameters = [{"name": "@thread_id", "value": thread_id}]
        query = """
        SELECT * FROM c 
        WHERE c.threadId = @thread_id
        ORDER BY c.createdAt ASC
        """

        items = [
            item
            async for item in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]

        if not items:
            return None
        logging.debug(f"Found {len(items)} items for thread {thread_id}")
        # Separate items by type
        thread = None
        steps = []
        elements = []

        for item in items:
            # Handle the thread document
            if item.get("data_type") == "THREAD":
                thread = item
            # Handle step documents
            elif item.get("data_type") == "STEP":
                steps.append(item)
            # Handle element documents (note the inconsistency in the field name)
            elif item.get("data_type") == "ELEMENT" or item.get("type") == "ELEMENT":
                elements.append(item)

        # If no thread document found, return None
        if not thread:
            return None

        # Build the response
        thread["steps"] = steps
        thread["elements"] = elements
        return thread

    async def get_thread_author(self, thread_id: str) -> str:
        parameters = [
            {"name": "@data_type", "value": "THREAD"},
            {"name": "@thread_id", "value": thread_id},
        ]
        query = """
        SELECT c.userIdentifier 
        FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @thread_id
        """

        threads = [
            thread
            async for thread in self.threads_container.query_items(
                query=query, partition_key=thread_id, parameters=parameters
            )
        ]

        if not threads:
            raise ValueError(f"Author not found for thread_id {thread_id}")

        return threads[0]["userIdentifier"]

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: Dict | None = None,
        tags: List[str] | None = None,
    ):
        """Optimized thread update with minimal container operations"""
        ts = self._get_current_timestamp()

        parameters = [
            {"name": "@data_type", "value": "THREAD"},
            {"name": "@thread_id", "value": thread_id},
        ]
        thread_query = """
        SELECT * FROM c 
        WHERE c.data_type = @data_type 
        AND c.id = @thread_id
        """

        threads = [
            thread
            async for thread in self.threads_container.query_items(
                query=thread_query, partition_key=thread_id, parameters=parameters
            )
        ]

        if threads:
            # EXISTING THREAD PATH: Update only what changed
            thread = threads[0]

            # Track if user thread summary needs update (name or tags only)
            name_changed = name is not None and thread.get("name") != name
            tags_changed = tags is not None
            user_update_needed = name_changed or tags_changed

            # Update thread properties
            if name_changed:
                thread["name"] = name
            if metadata is not None:
                thread["metadata"] = metadata
            if tags is not None:
                thread["tags"] = tags

            # Always update thread in threads container if any changes
            if name_changed or metadata is not None or tags_changed:
                await self.threads_container.replace_item(
                    item=thread["id"], body=thread
                )

                # 3. Only update user container if name or tags changed
                if user_update_needed and user_id:
                    try:
                        # Direct ID lookup (faster than query)
                        user = await self.user_container.read_item(
                            item=user_id, partition_key=user_id
                        )

                        # Only update if thread exists in user's threads
                        if "threads" in user and thread_id in user["threads"]:
                            updated = False

                            # Only update fields that changed
                            if name_changed:
                                user["threads"][thread_id]["name"] = name
                                updated = True
                            if tags_changed:
                                user["threads"][thread_id]["tags"] = tags
                                updated = True

                            if updated:
                                user["threads"][thread_id]["updatedAt"] = ts
                                await self.user_container.replace_item(
                                    item=user["id"], body=user
                                )

                    except exceptions.CosmosResourceNotFoundError:
                        # Continue even if user not found - thread update already succeeded
                        pass
        else:
            # Only attempt user container update if we have a user ID
            if user_id:
                try:
                    # Add thread to user's threads with direct ID lookup
                    user = await self.user_container.read_item(
                        item=user_id, partition_key=user_id
                    )
                    thread = {
                        "id": thread_id,
                        "threadId": thread_id,
                        "data_type": "THREAD",
                        "createdAt": ts,
                        "name": name or "New Thread",
                        "userId": user_id,
                        "userIdentifier": user["identifier"],
                        "tags": tags or [],
                        "metadata": metadata or {},
                    }

                    await self.threads_container.create_item(body=thread)
                    if "threads" not in user:
                        user["threads"] = {}

                    user["threads"][thread_id] = {
                        "name": name or "New Thread",
                        "createdAt": ts,
                        "updatedAt": ts,
                        "tags": tags or [],
                        "hasFeedback": False,
                        "feedbackValue": None,
                    }

                    await self.user_container.replace_item(item=user["id"], body=user)
                except exceptions.CosmosResourceNotFoundError:
                    # Thread is created even if user update fails
                    pass

    async def build_debug_url(self) -> str:
        return ""
