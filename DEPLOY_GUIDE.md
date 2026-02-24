# 智能音箱 + LED 远程控制项目部署指南

## 项目说明

这是一个智能音箱创意项目：
- **智能音箱** (ESP32): 录制语音 → 发送到服务器 → 播放 AI 回复
- **远程 LED 控制**: 当音箱说话时，远在外省的组员 LED 灯亮起

## 架构

```
ESP32 智能音箱 ──WebSocket──► 阿里云服务器 ──WebSocket──► 组员 ESP32 (LED)
                                     │
                                     ▼
                              ┌──────────────┐
                              │   RabbitMQ   │
                              └──────────────┘
                                     │
                    ┌────────────────┴────────────────┐
                    ▼                                 ▼
            ┌──────────────┐                  ┌──────────────┐
            │  LLM Worker  │                  │  TTS Worker  │
            │  语音识别+LLM │                  │  文本转语音   │
            └──────────────┘                  └──────────────┘
```

## 部署步骤

### 1. 阿里云服务器准备

```bash
# 登录阿里云服务器
ssh root@139.196.221.55

# 安装 Docker 和 Docker Compose
# (如果还没安装)
curl -fsSL https://get.docker.com | sh

# 克隆项目
cd /opt
git clone <你的项目仓库>
cd ai-tutor-server-ch23
```

### 2. 配置环境变量

创建 `.env` 文件：

```bash
cat > .env << 'EOF'
# 百度语音 API (必需)
BAIDU_VOICE_APP_ID=你的APP_ID
BAIDU_VOICE_API_KEY=你的API_KEY
BAIDU_VOICE_SECRET_KEY=你的SECRET_KEY

# 大模型 API (必需)
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY=你的ARK_API_KEY

# RabbitMQ
RABBITMQ_HOST=rabbitmq

# Redis
REDIS_HOST=redis

# 可选: 火山引擎语音合成
VOLCENGINE_APPID=你的APPID
VOLCENGINE_ACCESS_TOKEN=你的TOKEN
EOF
```

### 3. 测试百度 API

```bash
# 安装依赖
pip install baidu-aip

# 运行测试脚本
python test_baidu_api.py
```

**必须确保此测试通过！** 如果失败，检查：
- API 密钥是否正确
- 百度云账户是否有余额
- 语音合成服务是否已开通

### 4. 启动服务

```bash
# 启动 RabbitMQ 和 Redis
docker-compose up -d rabbitmq redis

# 等待服务启动
sleep 10

# 检查 RabbitMQ 状态
docker-compose ps

# 安装 Python 依赖
pip install -r requirements.txt

# 启动 FastAPI (窗口1)
python code/fastapi_app.py

# 启动 LLM Worker (窗口2)
python code/llm_worker.py

# 启动 TTS Worker (窗口3)
python code/tts_worker.py
```

### 5. 配置防火墙

在阿里云控制台开放端口：
- `8888/tcp` - WebSocket 端口
- `5672/tcp` - RabbitMQ (可选，仅内部使用)
- `15672/tcp` - RabbitMQ 管理界面 (可选)

```bash
# 或者使用 ufw
ufw allow 8888/tcp
ufw reload
```

### 6. ESP32 配置

在 `esp32-ai-voice/main/main.cc` 中：

```cpp
#define WS_URI "ws://139.196.221.55:8888/ws/esp32"
```

编译并烧录：
```bash
cd ~/esp/esp32-ai-voice
idf.py build
idf.py flash
idf.py monitor
```

### 7. LED 控制器配置

组员的 ESP32 连接 `/ws/led` 端点：

```cpp
// LED 控制器代码示例
const char* ws_uri = "ws://139.196.221.55:8888/ws/led";

// 当收到 "1" 时点亮 LED
// 当收到 "0" 时熄灭 LED
```

## 测试流程

### 1. 测试 LED 控制

使用 wscat 测试：

```bash
# 安装 wscat
npm install -g wscat

# 连接 LED 端点 (终端1)
wscat -c ws://139.196.221.55:8888/ws/led

# 连接音箱端点 (终端2)
wscat -c ws://139.196.221.55:8888/ws/test_device

# 在终端2发送测试消息
> 1
# 终端1应该收到 "1"

> 0
# 终端1应该收到 "0"
```

### 2. 测试完整流程

1. **唤醒音箱**: 说 "你好小智"
2. **说话**: 问一个问题
3. **观察**:
   - 音箱播放回复时，LED 应该亮起
   - 播放结束后，LED 应该熄灭

## 常见问题

### Q1: ESP32 收到 24 字节数据后断开

**原因**: TTS 返回空音频，服务器只发送了 `{"event": "response_finished"}`

**解决**: 
1. 运行 `test_baidu_api.py` 检查百度 API
2. 检查 TTS Worker 日志

### Q2: LED 不亮

**原因**: LED 控制器未连接或连接断开

**解决**:
1. 检查 LED 控制器是否连接到 `/ws/led`
2. 检查 FastAPI 日志中的转发消息

### Q3: 语音识别失败

**原因**: 百度 ASR API 问题

**解决**:
1. 检查 `llm_worker.py` 中的百度 API 配置
2. 确保录音格式是 PCM 16kHz 16bit 单声道

### Q4: WebSocket 连接不稳定

**解决**:
1. 检查 ESP32 的 WiFi 信号强度
2. 在 ESP32 代码中增加重连机制
3. 检查阿里云服务器网络状态

## 日志查看

```bash
# FastAPI 日志
tail -f /var/log/fastapi.log

# Worker 日志
tail -f /var/log/llm_worker.log
tail -f /var/log/tts_worker.log

# Docker 日志
docker-compose logs -f rabbitmq
docker-compose logs -f redis
```

## 重启服务

```bash
# 重启所有服务
docker-compose restart
pkill -f fastapi_app.py
pkill -f llm_worker.py
pkill -f tts_worker.py

# 重新启动
python code/fastapi_app.py &
python code/llm_worker.py &
python code/tts_worker.py &
```

## 联系支持

如有问题，请检查：
1. 百度云控制台: https://console.bce.baidu.com/
2. 阿里云控制台: https://ecs.console.aliyun.com/
3. 项目文档: PROJECT_ANALYSIS.md
