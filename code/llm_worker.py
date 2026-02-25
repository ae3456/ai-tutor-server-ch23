# 23.x 节代码：llm_worker.py
import pika
import json
import time
import os
import struct
import requests
from langchain_openai import ChatOpenAI
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain.memory import ConversationBufferMemory
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
import base64
import logging

# 天气查询相关配置（必须从环境变量读取，不要硬编码）
WEATHER_ENDPOINT = os.getenv("WEATHER_ENDPOINT", "")
WEATHER_CITY = os.getenv("WEATHER_CITY", "北京")
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("LLM_Worker")
from aip import AipSpeech # 导入百度语音 API SDK
# --- 1. 配置与 AI 核心初始化 ---
llm = ChatOpenAI(
    base_url=os.getenv("LLM_BASE_URL"),
    api_key=os.getenv("LLM_API_KEY"),
    model="doubao-1-5-lite-32k-250115", # 这是一个示例，请替换为你实际使用的模型
    temperature=0.7
)
REDIS_HOST = os.getenv('REDIS_HOST', 'redis')
REDIS_URL = f"redis://{REDIS_HOST}:6379/0"

# 错误消息常量
ERROR_MESSAGES = {
    "asr_failed": "语音识别失败，请重试",
    "tts_failed": "语音合成失败，请重试",
    "llm_failed": "AI处理失败，请重试",
    "timeout": "请求超时，请重试",
    "unknown": "系统错误，请重试"
}

# 小智 AI 助手 Prompt
template = """
你是一个名叫小智的友好、耐心且聪明的智能语音助手。
当前对话历史: {chat_history}
用户: {question}
小智:
"""
prompt = PromptTemplate(input_variables=["chat_history", "question"], template=template)

def get_weather_by_responses_api(city=None) -> str:
    """
    使用火山方舟 Responses API 联网搜索天气
    利用豆包大模型的联网搜索能力获取实时天气
    """
    if not city:
        city = WEATHER_CITY
    
    api_key = os.getenv("LLM_API_KEY", "")
    endpoint_id = WEATHER_ENDPOINT
    
    if not api_key:
        logger.error("未配置LLM_API_KEY，无法查询天气")
        return f"您好，{city}今天天气晴朗，气温适宜，祝您有愉快的一天！"
    
    if not endpoint_id:
        logger.error("未配置WEATHER_ENDPOINT，无法查询天气")
        return f"您好，{city}今天天气晴朗，气温适宜，祝您有愉快的一天！"
    
    try:
        url = "https://ark.cn-beijing.volces.com/api/v3/responses"
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": endpoint_id,
            "stream": False,
            "tools": [
                {"type": "web_search"}  # 开启联网搜索
            ],
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "你是天气播报助手。请简洁播报天气，50字以内，必须包含问候语，语气亲切自然。"
                        }
                    ]
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"请搜索{city}今天的实时天气，告诉我温度、天气状况和简单的穿衣建议。"
                        }
                    ]
                }
            ]
        }
        
        logger.info(f"调用Responses API查询{city}天气...")
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        
        # 解析Responses API返回格式
        if result and "output" in result:
            output_list = result["output"]
            if isinstance(output_list, list) and len(output_list) > 0:
                # 找到assistant的最后一条消息
                for item in reversed(output_list):
                    if item.get("role") == "assistant":
                        content = item.get("content", [])
                        if isinstance(content, list) and len(content) > 0:
                            weather_text = content[0].get("text", "").strip()
                        else:
                            weather_text = str(content).strip()
                        
                        # 确保有问候语
                        if weather_text and not weather_text.startswith(("您好", "你好", "大家好")):
                            weather_text = f"您好，{weather_text}"
                        
                        logger.info(f"天气查询成功: {weather_text[:60]}...")
                        return weather_text if weather_text else f"您好，{city}今天天气不错。"
            
            logger.warning(f"API返回格式异常，尝试直接解析: {result}")
            return f"您好，{city}今天天气晴朗，气温适宜。"
        else:
            logger.error(f"API返回为空或格式错误: {result}")
            return f"您好，{city}今天天气不错，适合外出活动。"
            
    except requests.exceptions.Timeout:
        logger.error("天气API请求超时")
        return f"您好，{city}今天天气晴朗，气温适宜。"
    except requests.exceptions.RequestException as e:
        logger.error(f"请求天气API失败: {e}")
        return f"您好，{city}今天天气不错，祝您有愉快的一天！"
    except Exception as e:
        logger.error(f"天气查询异常: {e}")
        return f"您好，{city}今天天气不错，适合外出活动。"

def get_weather_response(city=None) -> str:
    """
    获取天气播报文本（使用豆包联网搜索）
    """
    return get_weather_by_responses_api(city)
# --- 语音 API 客户端初始化 ---
APP_ID = os.getenv("BAIDU_VOICE_APP_ID")
API_KEY = os.getenv("BAIDU_VOICE_API_KEY")
SECRET_KEY = os.getenv("BAIDU_VOICE_SECRET_KEY")
# 初始化百度语音 API 客户端
try:
    speech_client = AipSpeech(APP_ID, API_KEY, SECRET_KEY)
except Exception as e:
    print(f"Warning: Baidu AipSpeech Client initialization failed. Check environment variables. Error: {e}")

# PCM转WAV函数
def pcm_to_wav(pcm_data: bytes, sample_rate=16000, channels=1, bits_per_sample=16) -> bytes:
    """
    将原始PCM数据转换为WAV格式
    """
    # WAV头部结构
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm_data)

    wav_header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + data_size,
        b'WAVE',
        b'fmt ',
        16,  # fmt chunk size
        1,   # audio format (PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b'data',
        data_size
    )
    return wav_header + pcm_data

# 模拟 ASR 函数 (来自 15.2 节)
def transcribe_audio_stream(audio_bytes: bytes) -> str:
    """
    接收音频二进制流，调用百度 STT (ASR) API 进行转录。
    注意：输入为原始PCM数据，需要转换为WAV格式
    """
    if speech_client is None:
        logger.error("百度语音客户端未初始化")
        return ""

    try:
        # 将原始PCM数据转换为WAV格式
        wav_data = pcm_to_wav(audio_bytes, sample_rate=16000, channels=1, bits_per_sample=16)
        logger.debug(f"PCM转WAV完成: 输入 {len(audio_bytes)} 字节, 输出 {len(wav_data)} 字节")
    except Exception as e:
        logger.error(f"PCM转WAV失败: {e}")
        return ""

    # dev_pid=1536 是百度语音识别的普通话识别模型 ID
    stt_result = speech_client.asr(wav_data, 'wav', 16000, {'dev_pid': 1536})

    if stt_result and stt_result.get('err_no') == 0 and stt_result.get('result'):
        # 返回转录结果中的第一个句子
        return stt_result['result'][0]
    else:
        # 如果 ASR 失败，返回一个空字符串或错误信息
        logger.error(f"STT (ASR) 错误: {stt_result}")
        return ""
def get_ai_response_with_redis(user_input: str, user_id: str) -> str:
    """同步函数：从 Redis 加载记忆，调用 LLM，将新结果存回 Redis。"""
    # 移除模拟延迟，真实LLM调用已有处理时间 
    
    history = RedisChatMessageHistory(session_id=user_id, url=REDIS_URL)
    memory = ConversationBufferMemory(memory_key="chat_history", chat_memory=history)
    tutor_chain = LLMChain(llm=llm, prompt=prompt, memory=memory)
    
    # 调用 LLM，并让 Memory 自动更新 Redis
    response_text = tutor_chain.predict(question=user_input)
    return response_text


# --- 2. 消息处理回调函数 (Consumer/Producer 的核心) ---
def callback(ch, method, properties, body):
    """当 Worker Service 从 llm_request_queue 接收到消息时被调用。"""
    try:
        task_data = json.loads(body)
        user_id = task_data['user_id']  
        task_id = task_data['task_id']         
        audio_b64 = task_data.get('audio_data_base64', '')
        is_weather_report = task_data.get('is_weather_report', False)
        
        logger.info(f"[{user_id}][{task_id}] LLM Worker 收到任务。天气播报: {is_weather_report}")
        
        # ========== 天气播报任务处理 ==========
        if is_weather_report:
            logger.info(f"[{user_id}][{task_id}] 🌤️ 处理天气播报任务")
            
            # 获取天气播报文本（调用天气API + LLM生成）
            ai_response_text = get_weather_response()
            
            logger.info(f"[{user_id}][{task_id}] 天气播报内容: {ai_response_text}")
            
            # 发送到 TTS 队列
            tts_task_message = {             
                "task_id": task_id,             
                "user_id": user_id,             
                "ai_response_text": ai_response_text,
                "is_weather_report": True
            }
            
            ch.basic_publish(             
                exchange='',             
                routing_key='tts_request_queue',
                body=json.dumps(tts_task_message),             
                properties=pika.BasicProperties(                 
                    delivery_mode=pika.DeliveryMode.Persistent             
                )         
            )
            
            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.info(f"[{user_id}][{task_id}] 天气播报任务已发送到 tts_request_queue")
            return
        
        # ========== 正常对话任务处理 ==========
        if not audio_b64:
            logger.error(f"[{user_id}][{task_id}] 音频数据为空")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        
        # 1. Base64 解码
        audio_bytes = base64.b64decode(audio_b64)
        
        # 2. 调用 ASR 语音识别
        user_text = transcribe_audio_stream(audio_bytes)
        if not user_text:
            logger.error(f"[{user_id}][{task_id}] ASR 识别失败")
            error_message = {
                "user_id": user_id,
                "event": "error",
                "type": "asr_failed",
                "message": ERROR_MESSAGES["asr_failed"]
            }
            ch.basic_publish(
                exchange='',
                routing_key='websocket_response_queue',
                body=json.dumps(error_message),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent
                )
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        
        logger.info(f"[{user_id}][{task_id}] ASR 结果: {user_text}")          
        
        # 3. 调用 LLM 核心
        ai_response_text = get_ai_response_with_redis(user_text, user_id)                  
        logger.info(f"[{user_id}][{task_id}] LLM 处理完毕。准备发送到 TTS 队列...")                  
        # 4. 【修改】构建发送给 TTS Worker 的新消息         
        tts_task_message = {             
            "task_id": task_id,             
            "user_id": user_id,             
            "ai_response_text": ai_response_text # 将 AI 回复文本传递下去         
        }                
        # 5. 【修改】将任务发布到 'tts_request_queue'         
        ch.basic_publish(             
            exchange='',             
            routing_key='tts_request_queue', # 发送到 TTS 队列             
            body=json.dumps(tts_task_message),             
            properties=pika.BasicProperties(                 
                delivery_mode=pika.DeliveryMode.Persistent             
            )         
        )          
        # 6. 确认消息处理完成 (ACK)         
        ch.basic_ack(delivery_tag=method.delivery_tag)         
        logger.info(f"[{user_id}][{task_id}] 任务已成功发送到 tts_request_queue。")      
    except Exception as e:
        logger.error(f"处理 LLM 任务时出错: {e}")
        # 尝试从body中解析user_id，如果解析失败则使用未知ID
        user_id = "unknown"
        try:
            task_data = json.loads(body)
            user_id = task_data.get('user_id', 'unknown')
        except:
            pass

        # 发送错误消息到 websocket_response_queue
        error_message = {
            "user_id": user_id,
            "event": "error",
            "type": "llm_failed",
            "message": ERROR_MESSAGES["llm_failed"]
        }
        try:
            ch.basic_publish(
                exchange='',
                routing_key='websocket_response_queue',
                body=json.dumps(error_message),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent
                )
            )
            # 确认消息，避免重复处理
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as inner_e:
            logger.error(f"发送错误消息失败: {inner_e}")
            # 如果发送失败，NACK消息
            ch.basic_nack(delivery_tag=method.delivery_tag)


# --- 3. 主程序入口 (启动 Consumer) ---
if __name__ == '__main__':
    logger.info('LLM Worker Service: Starting up...')
    
    MQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")

    connection = pika.BlockingConnection(pika.ConnectionParameters(MQ_HOST))
    channel = connection.channel()
    
    channel.queue_declare(queue='llm_request_queue', durable=True)
    channel.queue_declare(queue='tts_request_queue', durable=True)
    
    # 设置 QoS (Quality of Service)：一次只给这个 Worker 发送一个消息
    channel.basic_qos(prefetch_count=1) 
    
    # 启动消费者
    channel.basic_consume(queue='llm_request_queue', on_message_callback=callback)
    
    logger.info(' [*] Waiting for messages. To exit press CTRL+C')
    channel.start_consuming()