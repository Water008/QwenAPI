FROM python:3.11-alpine as builder

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 生产阶段
FROM python:3.11-alpine

# 安装必要工具（Alpine兼容）
RUN apk add --no-cache curl

# 设置工作目录
WORKDIR /app

# 复制已安装的依赖
COPY --from=builder /usr/local /usr/local

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p data

# 健康检查（Alpine兼容）
HEALTHCHECK --interval=600s --timeout=10s --start-period=40s --retries=3 CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000

# 使用优化的启动命令
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]