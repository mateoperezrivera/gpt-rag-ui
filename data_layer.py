import logging
import os
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, cast
import uuid
import aiofiles
import aiohttp
from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey, exceptions
from azure.identity.aio import DefaultAzureCredential
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from chainlit.context import context
from chainlit.data.base import BaseDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.data.utils import queue_until_user_message
from azure.cosmos import CosmosClient as SyncCosmosClient
import json
import base64

import asyncio

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
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.cosmos").setLevel(logging.ERROR)
logging.getLogger("azure.identity").setLevel(logging.ERROR)
logging.getLogger("azure.core").setLevel(logging.ERROR)

# Disable urllib3 connection pool logs
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# Optional: Disable HTTP request/response logs completely
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
    logging.ERROR
)


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
        credential = DefaultAzureCredential()
        account_endpoint = account_endpoint or os.environ.get("COSMOSDB_ENDPOINT")
        if not account_endpoint:
            raise ValueError("Account endpoint must be provided when using MSI")
        self.client = CosmosClient(account_endpoint, credential=credential)

        self.database_name = database_name
        self.container_name = container_name
        self.storage_provider = storage_provider
        self.user_thread_limit = user_thread_limit
        self.database = self.client.get_database_client(self.database_name)
        self.threads_container = self.database.get_container_client(
            f"{self.container_name}_items"
        )
        self.thread_list_container = self.database.get_container_client(
            f"{self.container_name}_thread_list"
        )
        self.user_container = self.database.get_container_client(
            f"{self.container_name}_users"
        )
        self.connection_string = connection_string
        self.use_msi = use_msi

    THREAD_ITEMS_CONTAINER_INDEXING_POLICY = {
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
            {
                "path": "/createdAt/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
        ],
        "excludedPaths": [{"path": "/*"}],
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
    THREAD_LIST_CONTAINER_INDEXING_POLICY = {
        "indexingMode": "consistent",
        "automatic": True,
        "includedPaths": [
            {
                "path": "/userId/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
            {
                "path": "/createdAt/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
            {
                "path": "/name/?",
                "indexes": [{"kind": "Range", "dataType": "String"}],
            },
        ],
        "excludedPaths": [{"path": "/*"}],
        "compositeIndexes": [
            [
                {"path": "/userId", "order": "ascending"},
                {"path": "/createdAt", "order": "descending"},
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
        sync_credential = SyncDefaultAzureCredential()
        principal_info = get_principal_info()
        print("Current Azure Identity:", principal_info)
        account_endpoint = os.environ.get("COSMOS_DB_ENDPOINT")
        if not account_endpoint:
            raise ValueError("Account endpoint must be provided when using MSI")
        sync_client = SyncCosmosClient(account_endpoint, credential=sync_credential)

        # Use synchronous operations
        database = sync_client.get_database_client(self.database_name)

        try:
            # Create thread items container (threads, steps, elements)
            database.create_container(
                id=f"{self.container_name}_items",
                partition_key=PartitionKey(path="/threadId"),
                indexing_policy=self.THREAD_ITEMS_CONTAINER_INDEXING_POLICY,
            )
        except exceptions.CosmosResourceExistsError:
            logging.warning(
                f"CosmosDB: Container {self.container_name}_items already exists. Skipping creation."
            )

        try:
            # Create thread list container (optimized for listing user threads)
            database.create_container(
                id=f"{self.container_name}_thread_list",
                partition_key=PartitionKey(path="/userId"),
                indexing_policy=self.THREAD_LIST_CONTAINER_INDEXING_POLICY,
            )
        except exceptions.CosmosResourceExistsError:
            logging.warning(
                f"CosmosDB: Container {self.container_name}_thread_list already exists. Skipping creation."
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
        # Respect provided thread_id, otherwise use session's thread id
        thread_id = thread_id or self.context.session.thread_id

        try:
            # Attempt a point read which is faster than query
            element = await self.threads_container.read_item(
                item=element_id, partition_key=thread_id
            )
            await self.threads_container.delete_item(item=element_id, partition_key=thread_id)
        except exceptions.CosmosResourceNotFoundError:
            # Nothing to delete if not found
            return

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
        try:
            # Use a point read which is much more efficient than querying
            step = await self.threads_container.read_item(item=step_id, partition_key=thread_id)
        except exceptions.CosmosResourceNotFoundError:
            await self.create_step(step_dict)
            return
        for key, value in step_dict.items():
            if value is not None:
                step[key] = value

        await self.threads_container.replace_item(item=step["id"], body=step)

    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        thread_id = self.context.session.thread_id
        try:
            # Attempt a point read first
            step = await self.threads_container.read_item(
                item=step_id, partition_key=thread_id
            )
            await self.threads_container.delete_item(item=step_id, partition_key=thread_id)
        except exceptions.CosmosResourceNotFoundError:
            # If not found, nothing to delete
            return

    async def delete_thread(self, thread_id: str):
        """Delete a thread and all its items from both containers"""
        # 1. Get thread info to extract userId for thread_list deletion
        try:
            # Direct point read by ID - more efficient than querying
            thread = await self.threads_container.read_item(
                item=thread_id, partition_key=thread_id
            )
            user_id = thread.get("userId")
        except exceptions.CosmosResourceNotFoundError:
            logging.warning(f"Thread {thread_id} not found when attempting to delete")
            user_id = None

        # 2. Delete from thread_list_container if user_id is available
        if user_id:
            try:
                await self.thread_list_container.delete_item(
                    item=thread_id, partition_key=user_id
                )
            except exceptions.CosmosResourceNotFoundError:
                logging.debug(
                    f"Thread list entry {thread_id} not found for user {user_id}"
                )

        # 3. Query all items in threads_container for this thread
        all_items_query = """
        SELECT c.id FROM c 
        WHERE c.threadId = @thread_id
        """
        parameters = [{"name": "@thread_id", "value": thread_id}]

        # Get all item IDs
        item_ids = [
            item["id"]
            async for item in self.threads_container.query_items(
                query=all_items_query, partition_key=thread_id, parameters=parameters
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
        """List threads using thread_list container optimized for listing user threads"""

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
            # Build query with optional search filter
            query = """
            SELECT * FROM c 
            WHERE c.userId = @userId
            """
            parameters = [{"name": "@userId", "value": filters.userId}]

            # Add search filter if provided
            if filters.search:
                query += " AND CONTAINS(LOWER(c.name), @searchTerm)"
                parameters.append({"name": "@searchTerm", "value": filters.search.lower()})

            query += " ORDER BY c.createdAt DESC"

            # Add OFFSET/LIMIT for database-level pagination
            # Fetch limit + 1 to detect if there's a next page
            query += f" OFFSET {offset} LIMIT {limit + 1}"

            # Fetch paginated results from database
            paged_threads = [
                thread
                async for thread in self.thread_list_container.query_items(
                    query=query, partition_key=filters.userId, parameters=parameters
                )
            ]

            # Handle case where user has no threads
            if not paged_threads:
                if filters.search:
                    logging.debug(
                        f"No threads found for user {filters.userId} matching search term '{filters.search}'"
                    )
                else:
                    logging.debug(f"User {filters.userId} has no threads")
                return PaginatedResponse(
                    data=[],
                    pageInfo=PageInfo(
                        hasNextPage=False,
                        startCursor=pagination.cursor,
                        endCursor=None,
                    ),
                )

            # Check if there's a next page (we fetched limit + 1)
            has_next_page = len(paged_threads) > limit

            if has_next_page:
                # Remove the extra item used for detection
                paged_threads = paged_threads[:limit]

            # Convert to ThreadDict objects
            thread_dicts: List[ThreadDict] = list()
            for thread in paged_threads:
                thread_dict = ThreadDict(
                    id=thread["id"],
                    createdAt=thread["createdAt"],
                    name=thread.get("name", "New Thread"),
                    userId=filters.userId,
                    userIdentifier=filters.userId,
                    tags=thread.get("tags", []),
                    metadata={},  # Limited metadata in thread list
                    steps=[],  # Steps not included in summary
                    elements=[],  # Elements not included in summary
                )
                thread_dicts.append(thread_dict)

            # Create pagination info
            end_cursor = str(offset + limit) if has_next_page else None

            logging.debug(
                f"Returning {len(thread_dicts)} threads for user {filters.userId} (offset: {offset})"
            )

            return PaginatedResponse(
                data=thread_dicts,
                pageInfo=PageInfo(
                    hasNextPage=has_next_page,
                    startCursor=pagination.cursor,
                    endCursor=end_cursor,
                ),
            )

        except Exception as e:
            logging.error(f"Error listing threads for user {filters.userId}: {str(e)}")
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
        """Update thread in both items and thread_list containers"""
        ts = self._get_current_timestamp()

        # Try a point read by ID which is faster than a query
        try:
            thread = await self.threads_container.read_item(
                item=thread_id, partition_key=thread_id
            )
            thread_exists = True
        except exceptions.CosmosResourceNotFoundError:
            thread = None
            thread_exists = False

        if thread_exists:
            # Track what changed
            name_changed = name is not None and thread.get("name") != name
            tags_changed = tags is not None
            metadata_changed = metadata is not None

            # Update thread properties
            if name_changed:
                thread["name"] = name
            if metadata_changed:
                thread["metadata"] = metadata
            if tags_changed:
                thread["tags"] = tags

            # Always update thread in items container if any changes
            if name_changed or metadata_changed or tags_changed:
                await self.threads_container.replace_item(
                    item=thread["id"], body=thread
                )

                # Update thread_list_container if name or tags changed
                if name_changed or tags_changed:
                    # Use provided user_id or extract from thread
                    thread_user_id = user_id or thread.get("userId")
                    if thread_user_id:
                        try:
                            # Get thread list entry
                            thread_list_entry = await self.thread_list_container.read_item(
                                item=thread_id, partition_key=thread_user_id
                            )

                            if name_changed:
                                thread_list_entry["name"] = name
                            if tags_changed:
                                thread_list_entry["tags"] = tags
                            thread_list_entry["updatedAt"] = ts

                            await self.thread_list_container.replace_item(
                                item=thread_list_entry["id"],
                                body=thread_list_entry,
                            )
                        except exceptions.CosmosResourceNotFoundError:
                            logging.debug(
                                f"Thread list entry {thread_id} not found for user {thread_user_id}"
                            )
        else:
            # Create new thread in both containers
            if user_id:
                try:
                    # Get user to retrieve identifier
                    user = await self.user_container.read_item(
                        item=user_id, partition_key=user_id
                    )
                    user_identifier = user["identifier"]

                    # Create thread document in items container
                    thread = {
                        "id": thread_id,
                        "threadId": thread_id,
                        "data_type": "THREAD",
                        "createdAt": ts,
                        "name": name or "New Thread",
                        "userId": user_id,
                        "userIdentifier": user_identifier,
                        "tags": tags or [],
                        "metadata": metadata or {},
                    }
                    await self.threads_container.create_item(body=thread)

                    # Create thread list entry
                    thread_list_entry = {
                        "id": thread_id,
                        "threadId": thread_id,
                        "userId": user_id,
                        "name": name or "New Thread",
                        "createdAt": ts,
                        "updatedAt": ts,
                        "tags": tags or [],
                    }
                    await self.thread_list_container.create_item(body=thread_list_entry)

                except exceptions.CosmosResourceNotFoundError:
                    logging.warning(f"User {user_id} not found when creating thread")

    async def build_debug_url(self) -> str:
        return ""


def get_principal_info(credential=None):
    """
    Gets information about the current authenticated principal
    using the DefaultAzureCredential in a synchronous manner.
    """
    try:
        # Use provided credential or create a new one with sync version
        cred = credential or SyncDefaultAzureCredential()

        # Get a token for ARM scope using sync method (no await)
        token = cred.get_token("https://management.azure.com/.default")

        if not token or not token.token:
            logging.error("No token returned from credential")
            return None

        # Parse JWT token (format: header.payload.signature)
        parts = token.token.split(".")
        if len(parts) != 3:
            logging.error("Token doesn't appear to be in JWT format")
            return None

        # Decode the payload (middle part)
        # Add padding if needed
        payload = parts[1]
        padding = len(payload) % 4
        if padding:
            payload += "=" * (4 - padding)

        decoded = base64.b64decode(payload)
        claims = json.loads(decoded)

        # Extract and print key information
        principal_info = {
            "object_id": claims.get("oid"),  # Object ID (principal ID)
            "tenant_id": claims.get("tid"),  # Tenant ID
            "name": claims.get("name"),  # User name (if available)
            "upn": claims.get("upn"),  # User Principal Name (if user)
            "app_id": claims.get("appid"),  # App ID (if service principal)
        }

        return principal_info

    except Exception as e:
        logging.error(f"Error getting principal info: {str(e)}")
        return {"error": str(e)}
