# MultiTTS

MultiTTS 是一个面向字幕批量配音的桌面工具，当前固定使用 **IndexTTS 1.5** 的 Gradio `/gen_single` 接口。

## 功能

- 解析和预处理字幕文件
- 单服务器或多服务器并发生成语音
- 自动裁剪参考音频并匹配字幕时长
- 合并音频片段并输出最终音轨
- 本地缓存、失败重试和服务器熔断
- PyQt 图形界面

## 运行要求

- Python 3.12 或更高版本
- 可访问的 IndexTTS 1.5 Gradio 服务
- FFmpeg
- Rubber Band（项目可使用 `rubberband/` 下的本地程序）

## 安装

推荐使用项目锁定的依赖：

```powershell
uv sync
```

也可以使用传统方式：

```powershell
pip install -r requirements.txt
```

## 使用

Windows 下可以直接运行：

```powershell
.\runTTS.bat
```

或启动图形界面：

```powershell
.venv\Scripts\python.exe .\gui.pyw
```

在设置中填写 IndexTTS 1.5 服务地址。多服务器地址按界面提示配置，所有节点都必须使用相同的 1.5 接口。

## 主要文件

- `gui.pyw`：图形界面
- `main.py`：配音处理流程
- `api_client.py`：单服务器连接管理
- `dispatcher.py`：多服务器任务调度
- `indextts_15.py`：IndexTTS 1.5 请求参数的唯一实现
- `audio_processor.py`：音频裁剪、时长调整与合并
- `subtitle_parser.py`：字幕解析
- `config.py`：运行配置

项目基于 [MultiDubber](https://github.com/ZENGXIANGXUAN/MultiDubber.git) 继续开发。
