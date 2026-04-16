# groq-whisper

这个目录把两部分现有成果合并成了一个后端服务：

- `groq-whisper-algo-demo` 里的 rolling stable-prefix 聚合算法
- `mic-speaker-mixer` 里的 Windows 麦克风 + 扬声器 loopback 混音采集

目标是启动服务后，通过显式 `POST /start` 控制持续采集 Windows 上的混合音频，并且每 5 秒向 Groq Whisper 发送最近 30 秒窗口，随后流式输出转录 patch 事件。

## 当前实现边界

- 保留了现有 stable-prefix 聚合逻辑
- 保留了现有 mixer 的核心参数：
  - `FRAMES_PER_BUFFER = 960`
  - `MIC_HIGH_PASS_HZ = 80.0`
  - `MIC_TARGET_DBFS = -23.0`
  - `MIC_GATE_DBFS = -55.0`
  - `MIC_MAX_BOOST_DB = 12.0`
  - `MIC_MIN_GAIN_DB = -6.0`
  - `SPEAKER_GAIN = 0.88`
  - `PEAK_CEILING = 0.98`
  - `VOICE_ACTIVITY_DBFS = -35.0`
  - `DUCKING_DB = 3.0`
- 服务层只负责：
  - 持续采集最近音频窗口
  - 调用 Groq
  - 把 `PatchEvent` 以 SSE 方式流式输出

## 依赖

- Python 3.11+
- Windows
- `ffmpeg` / `ffprobe`
- Groq API Key

安装依赖：

```powershell
pip install -e .
```

设置 CLI / backend shell 的 API Key：

```powershell
$env:GROQ_API_KEY = "your-groq-api-key"
```

WinUI product flow no longer reads a plaintext key file. The desktop app stores the key in Windows user-scoped secure storage from the Settings page and sends it to the backend only in `POST /start`.

## 启动服务

直接从仓库根目录启动：

```powershell
py serve.py --host 127.0.0.1 --port 8000
```

或者：

```powershell
python -m groq_whisper_service --host 127.0.0.1 --port 8000
```

## 接口

- `GET /healthz`
- `GET /state`
- `GET /events`
- `GET /settings`
- `PUT /settings`
- `POST /start`
- `POST /stop`
- `POST /pause`
- `POST /resume`
- `GET /devices`
- `GET /sessions`

`/events` 返回 SSE，事件类型包括：

- `service.ready`
- `transcription.patch`
- `transcription.final`
- `service.error`

`transcription.patch` / `transcription.final` payload 会保留 algo demo 的核心字段：

- `replace_from_char`
- `replacement_text`
- `committed_text`
- `tail_text`
- `display_text`
- `window_end_s`

## 说明

- `transcribe.py` 仍然保留为离线 rolling CLI 入口，便于复用原 demo 的测试和调试路径。It now requires `GROQ_API_KEY`; `--key-file` is no longer supported.
- 当前实现是单会话，需显式调用 `POST /start` 开始采集和转写。
