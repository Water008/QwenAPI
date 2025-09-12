"""
Token management for Qwen Code API Server
"""
import time
import random
import aiohttp
import logging
from typing import Dict, Optional, Tuple, List, Any
from ..models import TokenData, RefreshResult
from ..database import TokenDatabase
from ..utils import get_token_id
from ..utils.timezone_utils import timestamp_to_local_datetime, format_local_datetime
from ..config import QWEN_OAUTH_TOKEN_ENDPOINT, QWEN_OAUTH_CLIENT_ID, DEBUG

# 设置日志
logger = logging.getLogger(__name__)


class TokenManager:
    
    def __init__(self, db: TokenDatabase):
        self.db = db
        self.token_store: Dict[str, TokenData] = {}
        self._version_manager = None
    
    def set_version_manager(self, version_manager):
        self._version_manager = version_manager
    
    def load_tokens(self) -> None:
        self.token_store = self.db.load_all_tokens()
    
    def save_token(self, token_id: str, token_data: TokenData) -> None:
        self.token_store[token_id] = token_data
        self.db.save_token(token_id, token_data)
    
    def delete_token(self, token_id: str) -> None:
        self.token_store.pop(token_id, None)
        self.db.delete_token(token_id)
    
    def delete_all_tokens(self) -> None:
        self.token_store.clear()
        self.db.delete_all_tokens()
    
    def get_token_status(self) -> Dict[str, Any]:
        token_list = []
        for token_id, token in self.token_store.items():
            is_expired = token.expires_at and (time.time() * 1000) > token.expires_at
            
            expires_at_str = format_local_datetime(timestamp_to_local_datetime(token.expires_at)) if token.expires_at else "未知"
            uploaded_at_str = format_local_datetime(timestamp_to_local_datetime(token.uploaded_at)) if token.uploaded_at else "未知"
            
            if is_expired:
                token_list.append({
                    'id': token_id,
                    'expiresAt': token.expires_at,
                    'expiresAtDisplay': expires_at_str,
                    'isExpired': True,
                    'uploadedAt': token.uploaded_at,
                    'uploadedAtDisplay': uploaded_at_str,
                    'usageCount': token.usage_count,
                    'refreshFailed': True
                })
            else:
                token_list.append({
                    'id': token_id,
                    'expiresAt': token.expires_at,
                    'expiresAtDisplay': expires_at_str,
                    'isExpired': False,
                    'uploadedAt': token.uploaded_at,
                    'uploadedAtDisplay': uploaded_at_str,
                    'usageCount': token.usage_count
                })
        
        return {
            'hasToken': len(self.token_store) > 0,
            'tokenCount': len(self.token_store),
            'tokens': token_list
        }
    
    async def refresh_single_token(self, token_id: str) -> Dict[str, Any]:
        token = self.token_store.get(token_id)
        if not token:
            raise Exception("Token不存在")
        
        logger.info(f"手动强制刷新单个token: {token_id}")
        # 强制刷新单个token
        refreshed_token = await self._force_refresh_token(token_id, token)
        
        if refreshed_token:
            return {
                'success': True,
                'tokenId': token_id,
                'message': 'Token刷新成功'
            }
        else:
            # 刷新失败，移除token
            logger.warning(f"手动刷新token {token_id} 失败，移除该token")
            self.delete_token(token_id)
            raise Exception("Token刷新失败，已删除")
    
    async def _force_refresh_token(self, token_id: str, token: TokenData) -> Optional[TokenData]:
        try:
            headers = {}
            if self._version_manager:
                user_agent = await self._version_manager.get_user_agent_async()
                headers['User-Agent'] = user_agent
                logger.info(f"使用User-Agent: {user_agent}")
            
            logger.info(f"开始刷新Token {token_id}")
            logger.debug(f"Token {token_id} 的刷新令牌: {token.refresh_token[:10]}...")
            logger.debug(f"OAuth客户端ID: {QWEN_OAUTH_CLIENT_ID}")
            logger.debug(f"OAuth端点: {QWEN_OAUTH_TOKEN_ENDPOINT}")
            
            # 创建新的会话用于刷新token
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                data = aiohttp.FormData()
                data.add_field('grant_type', 'refresh_token')
                data.add_field('refresh_token', token.refresh_token)
                data.add_field('client_id', QWEN_OAUTH_CLIENT_ID)
                
                logger.debug(f"发送刷新请求到 {QWEN_OAUTH_TOKEN_ENDPOINT}")
                logger.debug(f"请求头: {headers}")
                async with session.post(QWEN_OAUTH_TOKEN_ENDPOINT, data=data, headers=headers) as response:
                    logger.info(f"收到响应状态码: {response.status}")
                    
                    # 读取响应内容
                    text = await response.text()
                    logger.debug(f"响应内容类型: {response.content_type}")
                    logger.debug(f"响应内容长度: {len(text)}")
                    
                    # 检查是否是HTML响应（验证页面）
                    if response.content_type == 'text/html' or '<html' in text.lower() or 'cn_tips' in text:
                        logger.warning(f"刷新Token {token_id} 返回了HTML验证页面，可能需要人工验证")
                        logger.debug(f"HTML响应内容: {text[:500]}...")  # 只记录前500个字符
                        return None
                    
                    if response.status != 200:
                        logger.error(f"刷新Token {token_id} 失败，状态码: {response.status}")
                        logger.error(f"响应内容: {text}")
                        return None
                    
                    try:
                        result = await response.json()
                        logger.debug(f"响应JSON: {result}")
                    except Exception as json_error:
                        logger.error(f"解析JSON响应失败: {json_error}")
                        logger.error(f"响应内容: {text}")
                        return None
                    
                    if 'error' in result:
                        logger.error(f"刷新Token {token_id} 返回错误: {result['error']}")
                        if 'error_description' in result:
                            logger.error(f"错误描述: {result['error_description']}")
                        return None
                    
                    updated_token = TokenData(
                        access_token=result['access_token'],
                        refresh_token=result.get('refresh_token', token.refresh_token),
                        expires_at=int(time.time() * 1000) + result.get('expires_in', 3600) * 1000,
                        uploaded_at=token.uploaded_at,
                        usage_count=token.usage_count
                    )
                    
                    logger.info(f"Token {token_id} 刷新成功")
                    self.save_token(token_id, updated_token)
                    
                    return updated_token
        except Exception as error:
            logger.error(f"刷新Token {token_id} 时发生异常: {error}", exc_info=True)
            return None
    
    async def refresh_all_tokens(self) -> Dict[str, Any]:
        if not self.token_store:
            raise Exception("没有可用的token")
        
        refresh_results = []
        tokens_to_remove = []
        
        for token_id, token in self.token_store.items():
            refreshed_token = await self._force_refresh_token(token_id, token)
            
            if refreshed_token:
                refresh_results.append({'id': token_id, 'success': True})
            else:
                refresh_results.append({'id': token_id, 'success': False, 'error': 'Token刷新失败'})
                tokens_to_remove.append(token_id)
        
        for token_id in tokens_to_remove:
            self.delete_token(token_id)
        
        return {
            'success': True,
            'refreshResults': refresh_results,
            'remainingTokens': len(self.token_store),
            'isForcedRefresh': True
        }
    
    async def get_valid_token(self) -> Optional[Tuple[str, TokenData]]:
        if not self.token_store:
            return None
        
        valid_tokens = []
        token_entries = list(self.token_store.items())
        
        random.shuffle(token_entries)
        
        for token_id, token in token_entries:
            is_expired = token.expires_at and (time.time() * 1000) > token.expires_at
            
            if not is_expired:
                valid_tokens.append((token_id, token))
            else:
                refreshed_token = await self._force_refresh_token(token_id, token)
                if refreshed_token:
                    valid_tokens.append((token_id, refreshed_token))
        
        if valid_tokens:
            return random.choice(valid_tokens)
        
        return None
    
    async def refresh_expiring_tokens(self) -> Dict[str, Any]:
        """
        检查并刷新即将过期的token（在过期前2小时刷新）
        """
        if not self.token_store:
            logger.debug("没有可用的token")
            return {'success': True, 'message': '没有可用的token'}
        
        refresh_results = []
        tokens_to_remove = [] 
        # 提前2小时刷新（毫秒）
        refresh_threshold = 2 * 60 * 60 * 1000
        
        # 记录调试日志
        logger.debug(f"开始检查token刷新，共有 {len(self.token_store)} 个token")
        
        for token_id, token in self.token_store.items():
            # 检查token是否即将过期（在过期前2小时以内）
            if token.expires_at:
                current_time = time.time() * 1000
                time_to_expiry = token.expires_at - current_time
                
                # 记录调试日志
                logger.debug(f"Token {token_id} 的过期时间: {token.expires_at}, 当前时间: {current_time}, 距离过期: {time_to_expiry}ms")
                
                # 如果token即将过期（在2小时内过期），则刷新它
                if time_to_expiry < refresh_threshold:
                    logger.info(f"Token {token_id} 即将过期，开始刷新...")
                    
                    refreshed_token = await self._force_refresh_token(token_id, token)
                    
                    if refreshed_token:
                        refresh_results.append({'id': token_id, 'success': True, 'message': 'Token已刷新'})
                        logger.info(f"Token {token_id} 刷新成功")
                    else:
                        refresh_results.append({'id': token_id, 'success': False, 'error': 'Token刷新失败'})
                        tokens_to_remove.append(token_id)
                        logger.warning(f"Token {token_id} 刷新失败，将被移除")
                else:
                    logger.debug(f"Token {token_id} 无需刷新")
            else:
                logger.debug(f"Token {token_id} 没有设置过期时间")
        
        # 记录需要移除的token
        if tokens_to_remove:
            logger.info(f"以下token将被移除: {tokens_to_remove}")
        
        # 移除刷新失败的token
        for token_id in tokens_to_remove:
            logger.info(f"移除token {token_id}")
            self.delete_token(token_id)
        
        success_count = len([r for r in refresh_results if r['success']])
        logger.debug(f"Token刷新检查完成，成功刷新 {success_count} 个token")
        
        return {
            'success': True,
            'refreshResults': refresh_results,
            'remainingTokens': len(self.token_store)
        }