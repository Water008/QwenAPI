from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
import os
import asyncio
import logging
from contextlib import asynccontextmanager

from src.config.settings import PORT, HOST, DEBUG, LOG_LEVEL
from src.api import api_router, openai_router
from src.web import web_router
from src.oauth import TokenManager
from src.database import TokenDatabase
from src.utils.version_manager import initialize_version_manager, get_version_manager
from src.utils import initialize_tools

# 设置日志
log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

# 全局变量
_db = TokenDatabase()
_token_manager = TokenManager(_db)
_refresh_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_version_manager(_db)
    version_manager = get_version_manager()
    
    from src.api.routes import set_version_manager
    set_version_manager(version_manager)
    
    # 初始化工具系统
    try:
        tool_registry = initialize_tools()
    except Exception as e:
        pass
    
    try:
        initial_version = await version_manager.get_version()
    except Exception as e:
        pass
    
    _token_manager.load_tokens()
    
    global _refresh_task
    _refresh_task = asyncio.create_task(auto_refresh_tokens())
    
    yield
    
    if _refresh_task:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass

async def auto_refresh_tokens():
    # 每60秒检查一次token是否需要刷新
    refresh_interval = 60
    
    logger.info("启动自动刷新任务")
    # 获取版本管理器实例
    version_manager = get_version_manager()
    
    while True:
        try:
            logger.debug(f"开始执行token刷新检查，间隔: {refresh_interval}秒")
            await asyncio.sleep(refresh_interval)
            
            try:
                version = await version_manager.get_version()
                logger.debug(f"当前版本: {version}")
            except Exception as e:
                logger.error(f"获取版本失败: {e}", exc_info=True)
                pass
            
            logger.debug("加载token存储")
            _token_manager.load_tokens()
            # 设置版本管理器以确保User-Agent正确
            _token_manager.set_version_manager(version_manager)
            
            if _token_manager.token_store:
                logger.debug(f"发现 {len(_token_manager.token_store)} 个token，开始检查是否需要刷新")
                # 检查并刷新即将过期的token（在过期前2小时刷新）
                result = await _token_manager.refresh_expiring_tokens()
                logger.debug(f"Token刷新检查完成: {result}")
            else:
                logger.debug("没有可用的token")
                
        except asyncio.CancelledError:
            logger.info("Token自动刷新任务已取消")
            break
        except Exception as e:
            logger.error(f"Token刷新过程中发生错误: {e}", exc_info=True)
            # 减少等待时间，以便更快重试
            await asyncio.sleep(60)

app = FastAPI(title="Qwen Code API Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_path = os.path.join(os.path.dirname(__file__), '..', 'static')
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")

app.include_router(web_router)
app.include_router(api_router, prefix="/api")
app.include_router(openai_router)

if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=DEBUG)