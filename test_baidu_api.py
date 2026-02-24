#!/usr/bin/env python3
"""
测试百度语音 API 是否正常工作
在阿里云服务器上运行此脚本验证 TTS 和 ASR
"""

import os
from aip import AipSpeech

# 从环境变量读取配置
APP_ID = os.getenv("BAIDU_VOICE_APP_ID")
API_KEY = os.getenv("BAIDU_VOICE_API_KEY")
SECRET_KEY = os.getenv("BAIDU_VOICE_SECRET_KEY")

print("=" * 60)
print("百度语音 API 测试脚本")
print("=" * 60)

# 检查环境变量
print("\n1. 检查环境变量:")
print(f"   APP_ID: {'已设置' if APP_ID else '未设置!'} ({APP_ID})")
print(f"   API_KEY: {'已设置' if API_KEY else '未设置!'} ({API_KEY[:10] if API_KEY else 'None'}...)")
print(f"   SECRET_KEY: {'已设置' if SECRET_KEY else '未设置!'} ({SECRET_KEY[:10] if SECRET_KEY else 'None'}...)")

if not all([APP_ID, API_KEY, SECRET_KEY]):
    print("\n   错误: 环境变量未设置完整！")
    print("   请在 .env 文件中设置以下变量:")
    print("   - BAIDU_VOICE_APP_ID")
    print("   - BAIDU_VOICE_API_KEY")
    print("   - BAIDU_VOICE_SECRET_KEY")
    exit(1)

# 初始化客户端
print("\n2. 初始化百度语音客户端...")
try:
    client = AipSpeech(APP_ID, API_KEY, SECRET_KEY)
    print("   初始化成功!")
except Exception as e:
    print(f"   初始化失败: {e}")
    exit(1)

# 测试 TTS
print("\n3. 测试 TTS (文本转语音)...")
test_text = "你好，这是一个测试"
print(f"   测试文本: '{test_text}'")

try:
    result = client.synthesis(
        test_text, 
        'zh', 
        1, 
        {
            'vol': 5, 
            'per': 5118,  # 度小美
            'spd': 3, 
            'pit': 5, 
            'aue': 6,     # wav 格式
            'audio_ctrl': {"sampling_rate": 16000}
        }
    )
    
    if not isinstance(result, dict):
        print(f"   TTS 成功!")
        print(f"   音频大小: {len(result)} bytes")
        # 保存测试音频
        with open("/tmp/test_tts.wav", "wb") as f:
            f.write(result)
        print(f"   音频已保存到: /tmp/test_tts.wav")
    else:
        print(f"   TTS 失败!")
        print(f"   错误信息: {result}")
        print("\n   常见错误:")
        print("   - 鉴权失败: 检查 API_KEY 和 SECRET_KEY 是否正确")
        print("   - 余额不足: 登录百度云控制台检查账户余额")
        print("   - 服务未开通: 确保已开通语音合成服务")
        exit(1)
except Exception as e:
    print(f"   TTS 测试出错: {e}")
    exit(1)

# 测试 ASR
print("\n4. 测试 ASR (语音转文本)...")
try:
    # 读取刚才生成的音频
    with open("/tmp/test_tts.wav", "rb") as f:
        audio_data = f.read()
    
    print(f"   使用刚才生成的音频 ({len(audio_data)} bytes)")
    
    result = client.asr(audio_data, 'wav', 16000, {'dev_pid': 1537})
    
    if result and result.get('err_no') == 0 and result.get('result'):
        print(f"   ASR 成功!")
        print(f"   识别结果: {result['result'][0]}")
    else:
        print(f"   ASR 失败!")
        print(f"   错误信息: {result}")
except Exception as e:
    print(f"   ASR 测试出错: {e}")

print("\n" + "=" * 60)
print("测试完成!")
print("=" * 60)
