# 23.x 节代码：llm_worker.py
import pika
import json
import time
import os
from langchain_openai import ChatOpenAI
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain.memory import ConversationBufferMemory
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
import base64
import logging
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
# 假设 Alex Prompt 已在全局定义
template = """
你是一个名叫 Alex 的友好、耐心且鼓励人心的英语辅导老师。
当前对话: {chat_history}
人类: {question}
AI:
"""
prompt = PromptTemplate(input_variables=["chat_history", "question"], template=template)
# --- 语音 API 客户端初始化 ---
APP_ID = os.getenv("BAIDU_VOICE_APP_ID")
API_KEY = os.getenv("BAIDU_VOICE_API_KEY")
SECRET_KEY = os.getenv("BAIDU_VOICE_SECRET_KEY")
# 初始化百度语音 API 客户端
try:
    speech_client = AipSpeech(APP_ID, API_KEY, SECRET_KEY)
except Exception as e:
    print(f"Warning: Baidu AipSpeech Client initialization failed. Check environment variables. Error: {e}")
# 模拟 ASR 函数 (来自 15.2 节)
def transcribe_audio_stream(audio_bytes: bytes) -> str:
    """
    接收音频二进制流，调用百度 STT (ASR) API 进行转录。
    """
    # dev_pid=1737 是百度语音识别的英语识别模型 ID
    stt_result = speech_client.asr(audio_bytes, 'wav', 16000, {'dev_pid': 1537})
    
    if stt_result and stt_result.get('err_no') == 0 and stt_result.get('result'):
        # 返回转录结果中的第一个句子
        return stt_result['result'][0]
    else:
        # 如果 ASR 失败，返回一个空字符串或错误信息
        print(f"! STT (ASR) 错误: {stt_result}")
        return ""
def get_ai_response_with_redis(user_input: str, user_id: str) -> str:
    """同步函数：从 Redis 加载记忆，调用 LLM，将新结果存回 Redis。"""
    # 模拟 LLM 思考耗时
    time.sleep(1) 
    
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
        audio_b64 = task_data['audio_data_base64']                  
        logger.info(f"[{user_id}][{task_id}] LLM Worker 收到任务。")          
        # 1. 【修改】Base64 解码 (模拟函数需要真实解码)
        # (在附录 C 中，这里应该是真实解码)         
        audio_bytes = base64.b64decode(audio_b64) # 假设 C 端发送的是真实 Base64
        # 2. 【修改】调用 ASR (不再是模拟 user_text)         
        user_text = transcribe_audio_stream(audio_bytes)         
        if not user_text:             
            raise Exception("ASR 识别失败")                  
        logger.info(f"[{user_id}][{task_id}] ASR 结果: {user_text}")          
        # 3. 调用 LLM 核心 (耗时操作)         
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
        logger.error(f"处理 LLM 任务时出错: {e}。NACKing 消息。")         
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