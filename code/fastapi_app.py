# 14.2.2 节代码：fastapi_app.py (FastAPI 骨架)

import json
import time
import os
import asyncio
import aio_pika # 
import uuid
import base64
from contextlib import asynccontextmanager
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import logging
# 配置日志格式
logging.basicConfig(
    level=logging.INFO, # 设置默认级别为 INFO
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("FastAPI_WS") # 为 FastAPI 模块创建 Logger
# --- 1. 配置与初始化 ---
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")  # Docker环境下使用服务名
RABBITMQ_URL = f"amqp://guest:guest@{RABBITMQ_HOST}/"
LLM_REQUEST_QUEUE = "llm_request_queue"
TTS_REQUEST_QUEUE = "tts_request_queue" # TTS 队列
WEBSOCKET_RESPONSE_QUEUE = "websocket_response_queue" # 回复队列

# 错误消息常量
ERROR_MESSAGES = {
    "asr_failed": "语音识别失败，请重试",
    "tts_failed": "语音合成失败，请重试",
    "llm_failed": "AI处理失败，请重试",
    "timeout": "请求超时，请重试",
    "unknown": "系统错误，请重试"
}

#】用于存储 user_id 和 WebSocket 连接的对应关系
# 这是一个简化的连接管理器
active_connections: Dict[str, WebSocket] = {}

# LED控制器连接（用于转发说话状态）
led_controller_connection: Optional[WebSocket] = None
led_controller_lock = asyncio.Lock()


# 设置超时参数
WEBSOCKET_TIMEOUT = 60.0  # 60秒超时，更及时检测连接状态

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

                    # 【关键修复】检查WebSocket连接状态 - 增强版
                    # 方法1：检查close_code属性
                    if hasattr(ws, 'close_code') and ws.close_code is not None:
                        logger.warning(f"[{user_id}] WebSocket连接已关闭 (close_code={ws.close_code})，移除连接")
                        if user_id in active_connections:
                            del active_connections[user_id]
                        continue

                    # 方法2：主动发送测试消息验证连接是否真的存活
                    logger.info(f"[{user_id}] 开始连接测试...")
                    try:
                        # 发送一个小的测试包，设置短超时
                        test_start = time.time()
                        await asyncio.wait_for(
                            ws.send_text(json.dumps({"event": "connection_test", "timestamp": time.time()})),
                            timeout=2.0
                        )
                        test_time = time.time() - test_start
                        logger.info(f"[{user_id}] 连接测试通过，耗时 {test_time:.3f} 秒")
                    except asyncio.TimeoutError:
                        logger.warning(f"[{user_id}] 连接测试超时(2秒)，连接可能已断开")
                        if user_id in active_connections:
                            del active_connections[user_id]
                            logger.info(f"[{user_id}] 已从活跃连接中移除（测试超时）")
                        continue
                    except Exception as e:
                        logger.warning(f"[{user_id}] 连接测试失败: {e}，移除连接")
                        if user_id in active_connections:
                            del active_connections[user_id]
                            logger.info(f"[{user_id}] 已从活跃连接中移除（测试失败）")
                        continue

                    # 处理音频数据 (Base64 -> Bytes -> 流式发送)
                    audio_b64 = data.get("audio_data_base64")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        total_size = len(audio_bytes)
                        logger.info(f"[{user_id}] 开始流式发送音频 ({total_size} bytes)...")
                        
                        # 【修复】检查音频大小
                        if total_size == 0:
                            logger.error(f"[{user_id}] 收到空音频数据！")
                            await ws.send_text(json.dumps({
                                "event": "error",
                                "type": "tts_failed",
                                "message": ERROR_MESSAGES["tts_failed"]
                            }))
                        else:
                            # --- 优化流式发送策略 ---
                            # 减小块大小，减轻客户端处理压力
                            CHUNK_SIZE = 1600   # 从3200减小到1600字节（匹配客户端100ms缓冲区）
                            BURST_SIZE = 2      # 连续发2个块后休息
                            burst_count = 0
                            sent_chunks = 0
                            total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE  # 向上取整

                            logger.info(f"[{user_id}] 音频总大小: {total_size} bytes, 分为 {total_chunks} 个块发送")


                            try:
                                for i in range(0, total_size, CHUNK_SIZE):
                                    chunk = audio_bytes[i : i + CHUNK_SIZE]
                                    chunk_index = i // CHUNK_SIZE + 1

                                try:
                                    # 【关键修复】避免发送过小的数据块
                                    if len(chunk) < 128:
                                        # 如果是最后一个块且很小，合并到前一个块或跳过
                                        if i + CHUNK_SIZE >= total_size:
                                            logger.debug(f"[{user_id}] 跳过过小的最后一个音频块: {len(chunk)} 字节")
                                            continue
                                        else:
                                            next_chunk_size = min(CHUNK_SIZE, total_size - (i + CHUNK_SIZE))
                                            if next_chunk_size > 0:
                                                chunk = audio_bytes[i : i + CHUNK_SIZE + next_chunk_size]
                                                logger.debug(f"[{user_id}] 合并小音频块: {len(chunk)} 字节")

                                    # 发送前快速检查连接状态
                                    if hasattr(ws, 'close_code') and ws.close_code is not None:
                                        logger.warning(f"[{user_id}] 发送前检测到连接已关闭，停止发送")
                                        if user_id in active_connections:
                                            del active_connections[user_id]
                                        break

                                    # 发送音频块
                                    await ws.send_bytes(chunk)
                                    sent_chunks += 1

                                    # 每10个块记录一次进度
                                    if sent_chunks % 10 == 0:
                                        logger.debug(f"[{user_id}] 发送进度: {sent_chunks}/{total_chunks} 个块 ({chunk_index}/{total_chunks})")

                                except Exception as e:
                                    logger.warning(f"[{user_id}] 发送第 {chunk_index} 个音频块时连接断开: {e}")
                                    # 连接断开，从活跃连接中移除
                                    if user_id in active_connections:
                                        del active_connections[user_id]
                                        logger.info(f"[{user_id}] 已从活跃连接中移除")
                                    break

                                burst_count += 1
                                if burst_count >= BURST_SIZE:
                                    burst_count = 0
                                    # 增加休眠时间，给 ESP32 足够处理时间
                                    await asyncio.sleep(0.015)  # 从10ms增加到15ms

                            logger.info(f"[{user_id}] 音频流发送完毕，成功发送 {sent_chunks}/{total_chunks} 个块")
                            finally:

                    # 3. 发送结束标志 (这是 ESP32 停止播放的关键)
                    try:
                        await ws.send_text(json.dumps({"event": "response_finished"}))
                    except Exception as e:
                        logger.warning(f"[{user_id}] 发送结束标志失败: {e}")
                        # 连接可能已断开，清理连接
                        if user_id in active_connections:
                            del active_connections[user_id]
                            logger.info(f"[{user_id}] 已从活跃连接中移除")
                        
                except Exception as e:
                    logger.error(f"转发消息异常: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("系统启动中，正在连接 RabbitMQ...")
    retry_count = 0
    max_retry_interval = 60  # 最大重试间隔60秒
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
            retry_count += 1
            # 指数退避，上限60秒
            retry_interval = min(3 * (2 ** min(retry_count, 5)), max_retry_interval)
            logger.warning(f"RabbitMQ 连接失败，第{retry_count}次重试，{retry_interval}秒后重试... 错误: {e}")
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
    global led_controller_connection
    await websocket.accept()
    # 存储活跃连接
    active_connections[user_id] = websocket
    client_ip = websocket.client.host if websocket.client else "unknown"
    logger.info(f"--- WebSocket连接建立: User ID={user_id}, IP={client_ip} ---")
    print(f"--- WebSocket: 客户端连接成功 User ID: {user_id}, IP: {client_ip} ---")
    
    # [关键逻辑] 维护每个连接的音频缓冲区
    client_state = {
        "is_recording": False,
        "audio_buffer": bytearray()
    }

    channel = app.state.rabbitmq_channel # 获取通道

    try:
        while True:
            # 使用通用的 receive() 来同时处理 text 和 bytes
            # 添加超时防止永久阻塞
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=WEBSOCKET_TIMEOUT)
            except asyncio.TimeoutError:
                # 发送心跳保持连接
                try:
                    await websocket.send_text(json.dumps({"event": "ping"}))
                    logger.debug(f"[{user_id}] 心跳发送成功")
                except Exception as e:
                    logger.warning(f"[{user_id}] 心跳发送失败，连接可能已断开: {e}")
                    break  # 心跳失败，退出循环
                continue

            # A. 处理文本控制消息 (JSON)
            if "text" in message:
                text = message["text"]
                try:
                    data = json.loads(text)
                    # 确保解析后是一个字典
                    if not isinstance(data, dict):
                        # 如果不是字典（如纯数字"1"或"0"），当作简单文本处理
                        if text == "1" or text == "0":
                            async with led_controller_lock:
                                if led_controller_connection:
                                    try:
                                        await led_controller_connection.send_text(text)
                                        logger.info(f"[{user_id}] 转发说话状态 '{text}' 到 LED 控制器")
                                    except Exception as e:
                                        logger.warning(f"转发到 LED 控制器失败: {e}")
                                        led_controller_connection = None
                        continue
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
                    # 不是 JSON，可能是简单的文本消息（如 "1" 和 "0" 用于 LED 控制）
                    if text == "1" or text == "0":
                        async with led_controller_lock:
                            if led_controller_connection:
                                try:
                                    await led_controller_connection.send_text(text)
                                    logger.info(f"[{user_id}] 转发说话状态 '{text}' 到 LED 控制器")
                                except Exception as e:
                                    logger.warning(f"转发到 LED 控制器失败: {e}")
                                    led_controller_connection = None

            # B. 处理二进制音频数据
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
        logger.info(f"--- WebSocket: 客户端正常断开 User ID: {user_id} ---")
    except RuntimeError as e:
        # 捕获特定错误：Cannot call "receive" once a disconnect message has been received
        if "disconnect" in str(e).lower() or "receive" in str(e).lower():
            logger.warning(f"--- WebSocket 连接已断开 User ID: {user_id}: {e} ---")
        else:
            logger.error(f"--- WebSocket RuntimeError User ID: {user_id}: {e} ---")
    except Exception as e:
        logger.error(f"--- WebSocket 错误 User ID: {user_id}: {e} ---")
    finally:
        # 移除断开的连接
        if user_id in active_connections:
            del active_connections[user_id]
            logger.info(f"[{user_id}] 已从活跃连接中清理")


# --- 4. LED控制器 WebSocket 端点 ---
@app.websocket("/ws/led")
async def led_websocket_endpoint(websocket: WebSocket):
    """
    供组员的ESP32连接，接收说话状态控制LED
    """
    global led_controller_connection
    await websocket.accept()
    async with led_controller_lock:
        led_controller_connection = websocket
    logger.info("LED控制器已连接")
    
    try:
        while True:
            # 保持连接，等待断开
            data = await websocket.receive_text()
            logger.debug(f"LED控制器消息: {data}")
    except WebSocketDisconnect:
        logger.info("LED控制器断开连接")
    except Exception as e:
        logger.error(f"LED控制器连接错误: {e}")
    finally:
        async with led_controller_lock:
            led_controller_connection = None        
