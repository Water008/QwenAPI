import json
import time
import asyncio
import logging
import aiohttp
import tiktoken
import uuid
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import check_auth
from ..oauth import OAuthManager, TokenManager
from ..database import TokenDatabase
from ..models import TokenData
from ..utils import get_token_id
from ..utils.timezone_utils import get_local_today_iso
from ..utils.tool_registry import get_tool_registry
from ..utils.tool_executor import ToolCallExecutor
from ..config import API_PASSWORD, QWEN_API_ENDPOINT

logger = logging.getLogger(__name__)

_session = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=200,
            limit_per_host=50,
            ttl_dns_cache=300,
            use_dns_cache=True,
            keepalive_timeout=30,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=90, connect=10, sock_read=75)
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            connector_owner=True
        )
    return _session

router = APIRouter()
db = TokenDatabase()
oauth_manager = OAuthManager()
token_manager = TokenManager(db)
_version_manager = None
_tool_executor = None

def set_version_manager(version_manager):
    global _version_manager
    _version_manager = version_manager
    token_manager.set_version_manager(version_manager)
    oauth_manager.set_version_manager(version_manager)

def get_tool_executor():
    global _tool_executor
    if _tool_executor is None:
        _tool_executor = ToolCallExecutor(get_tool_registry())
    return _tool_executor

async def parse_json(request: Request) -> Dict[str, Any]:
    try:
        return await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

@router.post("/login")
async def api_login(request: Request):
    data = await parse_json(request)
    if data.get('password') == API_PASSWORD:
        return JSONResponse({'success': True})
    raise HTTPException(401, "Invalid password")

@router.post("/upload-token")
async def api_upload_token(request: Request, auth: bool = Depends(check_auth)):
    data = await parse_json(request)
    access_token = data.get('access_token')
    refresh_token = data.get('refresh_token')
    
    if not access_token or not refresh_token:
        raise HTTPException(400, "Missing token fields")
    
    token_id = get_token_id(refresh_token)
    token_data = TokenData(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=data.get('expiry_date'),
        uploaded_at=int(time.time() * 1000)
    )
    
    token_manager.save_token(token_id, token_data)
    return JSONResponse({'success': True})

@router.get("/token-status")
async def api_token_status(auth: bool = Depends(check_auth)):
    token_manager.load_tokens()
    return JSONResponse(token_manager.get_token_status())

@router.post("/refresh-single-token")
async def api_refresh_single_token(request: Request, auth: bool = Depends(check_auth)):
    data = await parse_json(request)
    token_id = data.get('tokenId')
    
    if not token_id:
        raise HTTPException(400, "Missing tokenId")
    
    logger.info(f"手动刷新单个token: {token_id}")
    token_manager.load_tokens()
    try:
        # 为手动刷新设置版本管理器
        if _version_manager:
            token_manager.set_version_manager(_version_manager)
        
        result = await token_manager.refresh_single_token(token_id)
        logger.info(f"手动刷新token {token_id} 结果: {result}")
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"手动刷新token {token_id} 失败: {e}", exc_info=True)
        return JSONResponse({'success': False, 'error': str(e)}, 500)

@router.post("/delete-token")
async def api_delete_token(request: Request, auth: bool = Depends(check_auth)):
    data = await parse_json(request)
    token_id = data.get('tokenId')
    
    if not token_id:
        raise HTTPException(400, "Missing tokenId")
    
    token_manager.load_tokens()
    if token_id not in token_manager.token_store:
        raise HTTPException(404, "Token not found")
    
    token_manager.delete_token(token_id)
    return JSONResponse({'success': True, 'tokenId': token_id})

@router.post("/delete-all-tokens")
async def api_delete_all_tokens(auth: bool = Depends(check_auth)):
    deleted_count = len(token_manager.token_store)
    token_manager.delete_all_tokens()
    return JSONResponse({'success': True, 'deletedCount': deleted_count})

@router.post("/refresh-token")
async def api_refresh_token(auth: bool = Depends(check_auth)):
    token_manager.load_tokens()
    try:
        return JSONResponse(await token_manager.refresh_all_tokens())
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, 500)

@router.post("/oauth-init")
async def api_oauth_init(auth: bool = Depends(check_auth)):
    try:
        result = await asyncio.wait_for(
            oauth_manager.init_oauth(), 
            timeout=12
        )
        return JSONResponse(result)
    except asyncio.TimeoutError:
        logger.error("OAuth初始化接口超时")
        return JSONResponse({
            'success': False,
            'error': 'Request timeout',
            'error_description': 'The OAuth initialization request timed out'
        })
    except Exception as e:
        logger.error(f"OAuth初始化接口错误: {e}")
        return JSONResponse({
            'success': False,
            'error': 'Internal error',
            'error_description': str(e)
        }, 500)

@router.post("/oauth-poll")
async def api_oauth_poll(request: Request, auth: bool = Depends(check_auth)):
    data = await parse_json(request)
    state_id = data.get('stateId')
    
    if not state_id:
        raise HTTPException(400, "Missing stateId")
    
    result = await oauth_manager.poll_oauth_status(state_id)
    
    if result.get('success') and result.get('tokenData'):
        token_data = result['tokenData']
        token_id = get_token_id(token_data.refresh_token)
        token_manager.save_token(token_id, token_data)
        return JSONResponse({'success': True, 'tokenId': token_id})
    
    return JSONResponse(result)

@router.post("/oauth-cancel")
async def api_oauth_cancel(request: Request, auth: bool = Depends(check_auth)):
    data = await parse_json(request)
    return JSONResponse(oauth_manager.cancel_oauth(data.get('stateId')))

@router.post("/chat")
async def api_chat(request: Request, auth: bool = Depends(check_auth)):
    return await handle_chat(await parse_json(request))

@router.get("/statistics/usage")
async def get_usage_statistics(request: Request, auth: bool = Depends(check_auth)):
    date = request.query_params.get('date') or get_local_today_iso()
    return JSONResponse(db.get_usage_stats(date))

@router.get("/statistics/available-dates")
async def get_available_dates(auth: bool = Depends(check_auth)):
    return JSONResponse({"dates": db.get_available_dates()})

@router.delete("/statistics/usage")
async def delete_usage_statistics(request: Request, auth: bool = Depends(check_auth)):
    data = await parse_json(request)
    date = data.get('date')
    if not date:
        raise HTTPException(400, "Missing date")
    
    return JSONResponse({'success': True, 'deletedCount': db.delete_usage_stats(date)})

@router.get("/health")
async def health_check():
    try:
        tokens = db.load_all_tokens()
        return JSONResponse({
            "status": "ok",
            "timestamp": time.time(),
            "database": {"status": "healthy", "token_count": len(tokens)}
        })
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, 503)

@router.get("/metrics")
async def get_metrics(auth: bool = Depends(check_auth)):
    try:
        tokens = db.load_all_tokens()
        valid = sum(1 for _, token in tokens.items() 
                   if not (token.expires_at and time.time() * 1000 > token.expires_at))
        
        return JSONResponse({
            "tokens": {"total": len(tokens), "valid": valid},
            "usage": {"today": db.get_usage_stats(get_local_today_iso())},
            "performance": {"timestamp": time.time()}
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

@router.get("/version")
async def get_version(auth: bool = Depends(check_auth)):
    try:
        if _version_manager:
            try:
                version = await asyncio.wait_for(
                    _version_manager.get_version(), 
                    timeout=8
                )
                return JSONResponse({"version": version})
            except asyncio.TimeoutError:
                return JSONResponse({"version": "获取超时", "timeout": True})
        else:
            return JSONResponse({"version": "未知"})
    except Exception as e:
        logger.error(f"版本接口错误: {e}")
        return JSONResponse({"version": "错误", "error": str(e)})



async def _make_api_request_with_retry(session, url, json_data, headers, max_retries=5):
    last_exception = None
    for attempt in range(max_retries):
        try:
            logger.debug(f"API请求尝试 {attempt + 1}/{max_retries}")
            response = await session.post(url, json=json_data, headers=headers)
            logger.debug(f"API请求成功，尝试次数: {attempt + 1}")
            return response
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_exception = e
            logger.debug(f"API请求失败，尝试 {attempt + 1}/{max_retries}，错误: {str(e)}")
            if attempt < max_retries - 1:
                sleep_time = 2 ** attempt
                logger.debug(f"等待 {sleep_time} 秒后重试")
                await asyncio.sleep(sleep_time)
                continue
            logger.error(f"API请求在 {max_retries} 次尝试后仍然失败: {str(e)}")
            raise last_exception

async def handle_chat(data: Dict[str, Any], max_tool_calls: int = 10):
    logger.debug(f"处理聊天请求: {data}")
    messages = data.get('messages', [])
    model = data.get('model', 'qwen3-coder-plus')
    stream = data.get('stream', False)
    tools = data.get('tools', [])
    tool_choice = data.get('tool_choice', 'auto')
    
    logger.debug(f"请求参数 - model: {model}, stream: {stream}, tools: {len(tools)}, messages数量: {len(messages)}")
    
    if not messages or not isinstance(messages, list):
        logger.error("无效的消息格式")
        raise HTTPException(400, "Invalid messages")

    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except:
        encoding = tiktoken.encoding_for_model("gpt-4")

    prompt_tokens = sum(len(encoding.encode(str(msg.get('content', '')))) for msg in messages)
    logger.debug(f"计算得到的提示tokens数量: {prompt_tokens}")
    token_manager.load_tokens()
    
    valid_token = await token_manager.get_valid_token()
    if not valid_token:
        logger.error("没有有效的token可用")
        raise HTTPException(400, "No valid token")
    
    token_id, current_token = valid_token
    logger.debug(f"选择的token ID: {token_id[:10]}...")
    session = await get_session()
    
    headers = {
        'Authorization': f'Bearer {current_token.access_token}',
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream' if stream else 'application/json'
    }
    
    if _version_manager:
        headers['User-Agent'] = await _version_manager.get_user_agent_async()

    # 构建请求体
    body = {
        'model': model,
        'messages': messages,
        'temperature': data.get('temperature', 0.5),
        'top_p': data.get('top_p', 1),
        'stream': stream
    }
    
    # 添加工具调用支持
    if tools:
        body['tools'] = tools
        body['tool_choice'] = tool_choice

    # 处理工具调用对话
    conversation_messages = messages.copy()
    tool_call_count = 0
    
    while tool_call_count < max_tool_calls:
        try:
            logger.debug(f"发送API请求到 {QWEN_API_ENDPOINT}")
            logger.debug(f"请求头: {headers}")
            logger.debug(f"请求体: {body}")
            response = await _make_api_request_with_retry(session, QWEN_API_ENDPOINT, body, headers)
            logger.debug(f"收到API响应，状态码: {response.status}")
            if response.status != 200:
                logger.error(f"API返回错误状态码: {response.status}")
                raise HTTPException(500, f'API error: {response.status}')
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.error(f"API request failed after retries: {str(e)}")
            raise HTTPException(500, f'Request failed: {str(e)}')

        if stream:
            # 流式响应处理
            return await _handle_stream_response(response, conversation_messages, token_id, model, encoding, prompt_tokens)
        
        result = await response.json()
        
        # 检查是否有工具调用
        has_tool_calls = False
        if 'choices' in result and len(result['choices']) > 0:
            choice = result['choices'][0]
            if 'message' in choice:
                message = choice['message']
                if 'tool_calls' in message and message['tool_calls']:
                    has_tool_calls = True
        
        if has_tool_calls:
            # 处理工具调用
            logger.debug(f"检测到工具调用，工具数量: {len(tool_calls)}")
            tool_executor = get_tool_executor()
            choice = result['choices'][0]
            message = choice['message']
            tool_calls = message['tool_calls']
            
            # 添加助手响应到对话
            conversation_messages.append({
                "role": "assistant",
                "content": message.get("content", ""),
                "tool_calls": tool_calls
            })
            
            # 执行工具调用
            logger.debug(f"开始执行工具调用")
            tool_results = await tool_executor.execute_tool_calls(tool_calls)
            logger.debug(f"工具调用执行完成，结果数量: {len(tool_results)}")
            
            # 添加工具结果到对话
            for tool_result in tool_results:
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_result.get("tool_call_id", ""),
                    "content": tool_result.get("content", "")
                })
            
            # 更新请求体以继续对话
            body['messages'] = conversation_messages
            tool_call_count += 1
        else:
            # 没有工具调用，返回结果
            if 'usage' in result:
                db.update_token_usage(get_local_today_iso(), model, result['usage'].get('total_tokens', 0))
                db.increment_token_usage_count(token_id)
            
            logger.debug(f"没有工具调用，返回最终结果，结果包含choices: {'choices' in result}")
            return JSONResponse(result)
    
    # 达到最大工具调用次数
    if 'usage' in result:
        db.update_token_usage(get_local_today_iso(), model, result['usage'].get('total_tokens', 0))
        db.increment_token_usage_count(token_id)
    
    logger.debug(f"达到最大工具调用次数或返回最终结果，结果包含choices: {'choices' in result}")
    return JSONResponse(result)


async def _handle_stream_response(response, conversation_messages, token_id, model, encoding, prompt_tokens):
    """处理流式响应"""
    logger.debug(f"开始处理流式响应，模型: {model}, token ID: {token_id[:10]}...")
    tool_executor = get_tool_executor()
    buffer = ""
    last_content = ""
    completion_text = ""
    tool_calls_detected = False
    
    async def generate():
        nonlocal buffer, last_content, completion_text, tool_calls_detected
        
        async for chunk in response.content.iter_any():
            buffer += chunk.decode('utf-8')
            
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line.startswith('data:'):
                    line_data = line[5:].strip()
                    if line_data and line_data != '[DONE]':
                        try:
                            json_data = json.loads(line_data)
                            delta = json_data.get('choices', [{}])[0].get('delta', {})
                            current_content = delta.get('content', '')
                            
                            # 检查工具调用
                            if 'tool_calls' in delta:
                                tool_calls_detected = True
                            
                            if current_content and current_content != last_content:
                                last_content = current_content
                                completion_text += current_content
                                yield line + '\n'
                            elif not current_content:
                                yield line + '\n'
                        except:
                            yield line + '\n'
                    else:
                        yield line + '\n'
                else:
                    yield line + '\n'
        
        if buffer:
            yield buffer
            
        # 更新使用统计
        logger.debug(f"流式响应处理完成，completion_text长度: {len(completion_text)}")
        if completion_text:
            tokens = len(encoding.encode(completion_text))
            logger.debug(f"编码后的tokens数量: {tokens}")
            db.update_token_usage(get_local_today_iso(), model, prompt_tokens + tokens)
            db.increment_token_usage_count(token_id)
            logger.debug(f"使用统计更新完成")
    
    return StreamingResponse(generate(), media_type="text/event-stream")