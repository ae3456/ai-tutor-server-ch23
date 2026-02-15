# --- 18.2 节代码：Dockerfile ---

# 1. 基础镜像：选择一个轻量级的 Python 官方镜像作为起点
FROM python:3.10-slim

# 2. 设置工作目录：在容器内部创建一个 /app 文件夹，作为我们的工作目录
WORKDIR /app

# 3. 复制依赖文件：将 requirements.txt 复制到容器的 /app 目录
COPY requirements.txt .

# 4. 安装 Python 依赖：安装所有项目所需的库
# --no-cache-dir 减少镜像大小
RUN pip install --no-cache-dir -r requirements.txt

# 5. 复制应用程序代码：将本地的 code 文件夹内容复制到容器的 /app 目录
COPY code/ .

# 6. 暴露端口：声明容器内部的 8000 端口将被使用
EXPOSE 8000

# 7. 启动命令：定义容器启动时要执行的命令
# uvicorn 是 FastAPI 使用的 ASGI 服务器
CMD ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "8000"]