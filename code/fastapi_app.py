# 14.2.2 节代码：fastapi_app.py (FastAPI 骨架)

import json
import time
import os
import asyncio
import aio_pika # 
import uuid
import base64
from contextlib import asynccontextmanager
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import logging
# 配置日志格式
logging.basicConfig(
    level=logging.INFO, # 设置默认级别为 INFO
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("FastAPI_WS") # 为 FastAPI 模块创建 Logger
# --- 1. 配置与初始化 ---
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_URL = f"amqp://guest:guest@{RABBITMQ_HOST}/"
LLM_REQUEST_QUEUE = "llm_request_queue"
TTS_REQUEST_QUEUE = "tts_request_queue" # TTS 队列
WEBSOCKET_RESPONSE_QUEUE = "websocket_response_queue" # 回复队列



#】用于存储 user_id 和 WebSocket 连接的对应关系
# 这是一个简化的连接管理器
active_connections: Dict[str, WebSocket] = {}


# --- 2. RabbitMQ 连接管理 (使用 aio-pika) ---
async def listen_for_responses(channel: aio_pika.Channel):
    """
    后台任务：监听 RabbitMQ，收到 TTS 结果后，
    执行【流式切片 (Chunking)】策略发回给 ESP32。
    """
    queue = await channel.get_queue(WEBSOCKET_RESPONSE_QUEUE)
    logger.info(" [*] 正在监听结果队列，准备流式转发...")

    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                try:
                    data = json.loads(message.body.decode())
                    user_id = data.get("user_id")
                    
                    if user_id not in active_connections:
                        logger.warning(f"[{user_id}] 客户端已离线，丢弃消息")
                        continue

                    ws = active_connections[user_id]
                    
                    # 处理音频数据 (Base64 -> Bytes -> 流式发送)
                    audio_b64 = data.get("audio_data_base64")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        total_size = len(audio_bytes)
                        logger.info(f"[{user_id}] 开始流式发送音频 ({total_size} bytes)...")

                        # --- 流式发送策略 (Burst and Yield) ---
                        CHUNK_SIZE = 1024  # 每次发 1KB
                        BURST_SIZE = 8    # 连续发 8次 (8KB) 后休息一下
                        burst_count = 0

                        for i in range(0, total_size, CHUNK_SIZE):
                            # 检查连接是否还活着
                            if ws.client_state.name == "DISCONNECTED":
                                break

                            chunk = audio_bytes[i : i + CHUNK_SIZE]
                            await ws.send_bytes(chunk)
                            
                            burst_count += 1
                            if burst_count >= BURST_SIZE:
                                burst_count = 0
                                # 极短休眠，防止 ESP32 网络栈溢出，同时让出 CPU
                                await asyncio.sleep(0.001) 
                        
                        logger.info(f"[{user_id}] 音频流发送完毕")

                    # 3. 发送结束标志 (这是 ESP32 停止播放的关键)
                    await ws.send_text(json.dumps({"event": "response_finished"}))
                        
                except Exception as e:
                    logger.error(f"转发消息异常: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("系统启动中，正在连接 RabbitMQ...")
    retry_interval = 3  # 重试间隔秒数
    while True:
        try:
            # 尝试建立连接
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            # 建立 Channel
            channel = await connection.channel()
            
            # 声明队列
            await channel.declare_queue(LLM_REQUEST_QUEUE, durable=True)
            await channel.declare_queue(TTS_REQUEST_QUEUE, durable=True)
            await channel.declare_queue(WEBSOCKET_RESPONSE_QUEUE, durable=True)

            # 将连接对象挂载到 app.state
            app.state.rabbitmq_connection = connection
            app.state.rabbitmq_channel = channel
            logger.info("RabbitMQ 连接成功！")

            # 启动后台消费者任务
            task = asyncio.create_task(listen_for_responses(channel))
            app.state.consumer_task = task
            logger.info("后台消息监听任务已启动")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ 连接失败，{retry_interval}秒后重试... 错误: {e}")
            # 这里不要 break，而是等待后重试
            await asyncio.sleep(retry_interval)

    yield # 应用运行期间

    # --- Shutdown ---
    logger.info("系统关闭中...")
    # 1. 先取消消费者任务
    if hasattr(app.state, "consumer_task"):
        logger.info("正在停止后台监听任务...")
        app.state.consumer_task.cancel()
        try:
            await app.state.consumer_task
        except asyncio.CancelledError:
            pass # 任务正常取消
    # 2. 关闭 RabbitMQ 连接
    if hasattr(app.state, "rabbitmq_connection"):
        await app.state.rabbitmq_connection.close()

    logger.info("资源已释放")

# 创建 FastAPI 应用实例
app = FastAPI(lifespan=lifespan)

# --- 3. WebSocket 路由 (生产者核心) ---
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    # 存储活跃连接
    active_connections[user_id] = websocket
    print(f"--- WebSocket: 客户端连接成功 User ID: {user_id} ---")
    
    # [关键逻辑] 维护每个连接的音频缓冲区
    client_state = {
        "is_recording": False,
        "audio_buffer": bytearray()
    }

    channel = app.state.rabbitmq_channel # 获取通道

    try:
        while True:
            # 使用通用的 receive() 来同时处理 text 和 bytes
            message = await websocket.receive()

            # A. 处理文本控制消息 (JSON)
            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    event = data.get("event")

                    if event == "recording_started":
                        logger.info(f"[{user_id}] 开始录音...")
                        client_state["is_recording"] = True
                        client_state["audio_buffer"].clear()

                    elif event == "recording_ended":
                        logger.info(f"[{user_id}] 录音结束")
                        client_state["is_recording"] = False
                        
                        if not client_state["audio_buffer"]:
                            continue

                        # --- [核心变更] ---
                        # 不在本地做 ASR/LLM，而是打包发送到 RabbitMQ
                        
                        logger.info(f"[{user_id}] 打包音频数据 ({len(client_state['audio_buffer'])} bytes) 发往队列...")

                        # 1. 将 PCM 二进制转为 Base64 字符串以便放入 JSON
                        audio_b64 = base64.b64encode(client_state["audio_buffer"]).decode('utf-8')

                        # 2. 构建任务消息
                        task_id = str(uuid.uuid4())
                        task_message = {
                            "task_id": task_id,
                            "user_id": user_id,
                            "audio_data_base64": audio_b64, # 原始 PCM 数据的 Base64
                            "timestamp": time.time()
                        }

                        # 3. 发送到 llm_request_queue
                        # 注意: Worker 端需要先做 ASR (语音转文字) -> LLM -> TTS
                        await channel.default_exchange.publish(
                            aio_pika.Message(
                                body=json.dumps(task_message).encode(),
                                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                            ),
                            routing_key=LLM_REQUEST_QUEUE
                        )

                        # 4. 告知客户端：收到任务了
                        await websocket.send_text(json.dumps({
                            "event": "server_processing",
                            "message": "Audio received, processing..."
                        }))
                        logger.info(f"[{user_id}] 任务 {task_id} 已发送到 llm_request_queue。")

                    elif event == "recording_cancelled":
                        client_state["is_recording"] = False
                        client_state["audio_buffer"].clear()
                except json.JSONDecodeError:
                    pass

            # B. 处理二进制音频数据
            # elif "bytes" in message:
            #     if client_state["is_recording"]:
            #         # 将数据块追加到缓冲区
            #         client_state["audio_buffer"].extend(message["bytes"])
            elif "bytes" in message:
                # 【修改】：如果收到音频数据但状态是未录音，自动修正为正在录音
                if not client_state["is_recording"]:
                    # 只有当缓冲区为空时才打印这个日志，防止刷屏
                    if len(client_state["audio_buffer"]) == 0:
                        logger.info(f"[{user_id}] 检测到音频流但未收到开始指令，自动激活录音状态 (隐式开启)")
                    client_state["is_recording"] = True
                
                # 接收数据
                client_state["audio_buffer"].extend(message["bytes"])

    except WebSocketDisconnect:
        logger.error(f"--- WebSocket: 客户端断开连接 User ID: {user_id} ---")
    except Exception as e:
        logger.error(f"--- WebSocket 错误 User ID: {user_id}: {e} ---")
    finally:
        # 移除断开的连接
        if user_id in active_connections:
            del active_connections[user_id]        
