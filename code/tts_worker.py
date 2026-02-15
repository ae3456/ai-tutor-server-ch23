# 16.4 节代码：tts_worker.py
import pika
import json
import os
import base64
from aip import AipSpeech # 导入百度语音 API SDK
import logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("TTS_Worker")
# from dotenv import load_dotenv
# load_dotenv()
# 假设 TTS API 和 Base64 编码逻辑已导入
# --- 语音 API 客户端初始化 ---
APP_ID = os.getenv("BAIDU_VOICE_APP_ID")
API_KEY = os.getenv("BAIDU_VOICE_API_KEY")
SECRET_KEY = os.getenv("BAIDU_VOICE_SECRET_KEY")
# 初始化百度语音 API 客户端
try:
    speech_client = AipSpeech(APP_ID, API_KEY, SECRET_KEY)
except Exception as e:
    print(f"Warning: Baidu AipSpeech Client initialization failed. Check environment variables. Error: {e}")

# --- 外部依赖模拟/假设已导入 ---
def synthesize_speech_stream(text: str) -> bytes:
    """
    接收文本，调用百度 TTS API 合成语音，返回音频二进制流。
    """
    # per=4 (女声), spd=5 (语速), pit=5 (语调)
    tts_result = speech_client.synthesis(
        text, 'zh', 1, 
        {
            'vol': 5, 
            'per': 5118, 
            'spd': 3, 
            'pit': 5, 
            'aue': 6,
            'audio_ctrl': {"sampling_rate":16000}}
    )
    # 百度 API 在成功时返回 bytes，失败时返回一个包含错误信息的字典
    if not isinstance(tts_result, dict):
        return tts_result # 成功，返回音频二进制数据
    else:
        print(f"! TTS (文本转语音) 错误: {tts_result}")
        return b"" # 失败，返回空 bytes
# -----------------------------------

# --- 1. TTS 核心函数 ---
# 假设 LLM/Prompt 等已初始化

# --- 2. 消息处理回调函数 (Consumer 的核心) ---
def callback(ch, method, properties, body):
    """TTS Worker Service 从队列中接收消息时的处理函数。"""
    try:
        # 1. 解析来自 LLM Worker 的消息
        task_data = json.loads(body)         
        user_id = task_data['user_id']         
        task_id = task_data['task_id']         
        ai_response_text = task_data['ai_response_text'] # 接收 AI 回复文本                  
        logger.info(f"[{user_id}][{task_id}] TTS Worker 收到任务。")
        # 2. TTS 合成 (耗时操作)
        response_audio_bytes = synthesize_speech_stream(ai_response_text)
        
        # 3. Base64 编码
        encoded_audio = base64.b64encode(response_audio_bytes).decode('utf-8')
        # 4. 构建最终的 WebSocket 回传消息         
        final_message = {
            # "status": "complete",             
            "ai_response": ai_response_text,             
            "audio_data_base64": encoded_audio,             
            "user_id": user_id,             
            # "task_id": task_id         
        }
        # 5. 【修改】将最终结果发布到 'websocket_response_queue'
        ch.basic_publish(             
            exchange='',             
            routing_key='websocket_response_queue', # 发送到最终回复队列             
            body=json.dumps(final_message),             
            properties=pika.BasicProperties(                 
                delivery_mode=pika.DeliveryMode.Persistent             
            )       
        )          
        # 6. 确认消息处理完成 (ACK)         
        ch.basic_ack(delivery_tag=method.delivery_tag)         
        logger.info(f"[{user_id}][{task_id}] TTS 处理完毕，已发送到 websocket_response_queue。")      
    except Exception as e:         
        logger.error(f"处理 TTS 任务时出错: {e}")         
        ch.basic_nack(delivery_tag=method.delivery_tag)

# --- 3. 主程序入口 (启动 Consumer) ---
if __name__ == '__main__':
    logger.info('TTS Worker Service: Starting up...')
    MQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")

    connection = pika.BlockingConnection(pika.ConnectionParameters(MQ_HOST))
    # connection = pika.BlockingConnection(pika.ConnectionParameters('rabbitmq'))
    channel = connection.channel()
    
    # 声明队列 (假设 LLM Worker 会将结果发送到一个名为 tts_request_queue 的队列)
    channel.queue_declare(queue='tts_request_queue', durable=True)
    channel.queue_declare(queue='websocket_response_queue', durable=True)
    
    channel.basic_qos(prefetch_count=1) 
    channel.basic_consume(queue='tts_request_queue', on_message_callback=callback)
    
    logger.info(' [*] Waiting for messages. To exit press CTRL+C')
    channel.start_consuming()