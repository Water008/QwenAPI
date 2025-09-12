import asyncio
import aiohttp
import logging
import time
from typing import Optional
from ..database import TokenDatabase

logger = logging.getLogger(__name__)

class VersionManager:
    
    REGISTRY_URL = "https://registry.npmmirror.com/@qwen-code/qwen-code/latest"
    DEFAULT_VERSION = "0.0.10"
    CACHE_TTL = 3600
    REQUEST_TIMEOUT = 5
    MAX_RETRIES = 2
    
    def __init__(self, db: TokenDatabase):
        self.db = db
        self._cached_version: Optional[str] = None
        self._cache_timestamp: Optional[float] = None
        self._lock = asyncio.Lock()
    
    async def get_version(self) -> str:
        if self._is_cache_valid():
            return self._cached_version
        
        try:
            version = await asyncio.wait_for(
                self._get_version_with_retry(), 
                timeout=self.REQUEST_TIMEOUT + 1
            )
            if version:
                await self._update_cache_and_storage(version)
                return version
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            pass
        
        return self._get_fallback_version()
    
    async def _get_version_with_retry(self) -> Optional[str]:
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                version = await self._fetch_version_from_registry()
                if version:
                    return version
            except Exception as e:
                if attempt == self.MAX_RETRIES:
                    raise
                await asyncio.sleep(1 * (attempt + 1))
        
        return None
    
    def _get_fallback_version(self) -> str:
        if self._cached_version:
            return self._cached_version
        
        version = self.db.get_app_version()
        if version:
            self._cached_version = version
            self._cache_timestamp = time.time()
            return version
        
        return self.DEFAULT_VERSION
    
    async def refresh_version(self) -> str:
        async with self._lock:
            self._cached_version = None
            self._cache_timestamp = None
            return await self.get_version()
    
    async def get_user_agent_async(self) -> str:
        try:
            version = await asyncio.wait_for(
                self.get_version(), 
                timeout=2
            )
            return f"QwenCode/{version} (linux; x64)"
        except asyncio.TimeoutError:
            return self.get_user_agent()
        except Exception as e:
            return self.get_user_agent()
    
    def get_user_agent(self, version: Optional[str] = None) -> str:
        if version is None:
            if self._is_cache_valid():
                version = self._cached_version
            else:
                version = self.db.get_app_version() or self.DEFAULT_VERSION
        
        return f"QwenCode/{version} (linux; x64)"
    
    def _is_cache_valid(self) -> bool:
        if not self._cached_version or not self._cache_timestamp:
            return False
        return time.time() - self._cache_timestamp < self.CACHE_TTL
    
    async def _fetch_version_from_registry(self) -> Optional[str]:
        try:
            timeout = aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.REGISTRY_URL) as response:
                    if response.status == 200:
                        data = await response.json()
                        version = data.get('version')
                        if version:
                            return version
        except asyncio.TimeoutError:
            pass
        except aiohttp.ClientError:
            pass
        except Exception:
            pass
        
        return None
    
    async def _update_cache_and_storage(self, version: str):
        self._cached_version = version
        self._cache_timestamp = time.time()
        self.db.save_app_version(version)


_version_manager: Optional[VersionManager] = None

def initialize_version_manager(db: TokenDatabase):
    global _version_manager
    _version_manager = VersionManager(db)

def get_version_manager() -> VersionManager:
    if _version_manager is None:
        raise RuntimeError("版本管理器未初始化，请先调用 initialize_version_manager")
    return _version_manager