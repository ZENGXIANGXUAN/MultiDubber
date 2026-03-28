# MultiTTS - 多功能文本转语音工具

MultiTTS 是一个基于多种 TTS 引擎的文本转语音工具，支持批量处理、音频处理等功能。

## 特性

- 支持多种 TTS 引擎
- 批量文本转语音
- 音频格式转换和处理
- 图形用户界面
- 字幕文件处理

## 重要说明

本项目目前仅支持 IndexTTS 1.5 接口。如需使用其他接口，请按照以下步骤进行修改：

### 修改接口方法

1. 找到 `api_client.py` 文件
2. 定位到 TTS 请求的相关函数
3. 修改 API 端点 URL 和请求参数以适配新的接口
4. 根据新接口的文档调整认证方式和数据格式

例如，如果要切换到另一个 TTS 服务：
```python
# 修改前（IndexTTS 1.5）
def call_tts_api(text, voice_params):
    url = "https://index-tts-api.example.com/v1.5/synthesize"
    # ... 其他代码

# 修改后（新接口示例）
def call_tts_api(text, voice_params):
    url = "https://new-tts-api.example.com/v2/synthesize"
    # ... 根据新接口调整的代码
```

## 安装

1. 克隆仓库：
   ```bash
   git clone https://github.com/ZENGXIANGXUAN/MultiDubber.git
   cd MultiDubber
   ```

2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 使用

运行主程序：
```bash
python main.py
```

或者使用批处理文件：
```bash
runTTS.bat
```

## 文件说明

- `main.py` - 主程序入口
- `api_client.py` - API 客户端，用于与 TTS 服务通信
- `gui.pyw` - 图形用户界面
- `model.py` - 模型相关功能
- `audio_processor.py` - 音频处理功能
- `subtitle_parser.py` - 字幕解析功能
- `utils.py` - 工具函数
- `config.py` - 配置文件
- `dispatcher.py` - 任务调度器
- `requirements.txt` - 项目依赖

## 注意事项

- `API_CLOSE.py` 文件由于包含敏感密钥信息，已添加到 `.gitignore` 中，不会被提交到版本控制系统
- 请确保在使用时遵守相应 TTS 服务的使用条款
- 音频处理功能依赖于外部库，如 `pyrubberband` 等

## 项目来源

本项目基于 [MultiDubber](https://github.com/ZENGXIANGXUAN/MultiDubber.git) 进行开发。