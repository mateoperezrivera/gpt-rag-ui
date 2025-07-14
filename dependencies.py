"""
Provides dependencies for API calls.
"""
import logging
from fastapi import Depends, HTTPException
from connectors.appconfig import AppConfigClient      

__config: AppConfigClient = None

def get_config(action: str = None) -> AppConfigClient:
    global __config

    if action is not None and action=='refresh':
        __config = AppConfigClient()
    else:
        __config = __config or AppConfigClient()
    
    return __config

def handle_exception(exception: Exception, status_code: int = 500):
    logging.error(exception, stack_info=True, exc_info=True)
    raise HTTPException(
        status_code = status_code,
        detail = str(exception)
    ) from exception
