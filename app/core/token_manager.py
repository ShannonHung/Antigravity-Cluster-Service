"""
app/core/token_manager.py

Generic, extensible token management system.

Allows multiple external services (deploy-service, user-service, etc.) 
to implement their own token acquisition and refresh logic while
keeping the interface consistent.
"""

from __future__ import annotations

import time
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Optional

_logger = logging.getLogger(__name__)

class TokenManager(ABC):
    """Abstract interface for managing service tokens."""

    def __init__(self, initial_token: str = ""):
        self._token: str = initial_token
        self._expire_at: float = 0
        self._lock = asyncio.Lock()

    @abstractmethod
    async def _fetch_new_token(self) -> tuple[str, int]:
        """
        Fetch a new token from the source.
        Returns: (access_token, expires_in_seconds)
        """
        pass

    async def get_token(self) -> str:
        """Get a valid token, refreshing if necessary."""
        async with self._lock:
            if not self._token or self._is_expired():
                _logger.info("Token expired or missing, refreshing...")
                await self.refresh()
            return self._token

    async def refresh(self) -> None:
        """Force a token refresh."""
        # Note: Caller should usually hold the lock, 
        # but we also check it here just in case.
        token, expires_in = await self._fetch_new_token()
        self._token = token
        # Buffer of 30 seconds to avoid edge cases
        self._expire_at = time.time() + expires_in - 30
        _logger.info(f"Token refreshed. Expires in {expires_in}s")

    def _is_expired(self) -> bool:
        """Check if the current token is near expiration."""
        return time.time() >= self._expire_at


class DeployServiceTokenManager(TokenManager):
    """Concrete implementation for deploy-service token management."""

    def __init__(self, base_url: str, username: str, password: str, initial_token: str = ""):
        super().__init__(initial_token)
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password

    async def _fetch_new_token(self) -> tuple[str, int]:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/token",
                data={
                    "username": self._username,
                    "password": self._password,
                    "grant_type": "password"
                },
                timeout=10.0
            )
            
            if response.status_code != 200:
                _logger.error(f"Failed to fetch token from deploy-service: {response.text}")
                # We could raise a specific exception here if needed
                response.raise_for_status()
                
            data = response.json()
            # access_token and expires_in (seconds)
            return data["access_token"], data.get("expires_in", 3600)
