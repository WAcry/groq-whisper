# groq based whisper 转录 Windows UI

阅读 /home/coder/git/groq-whisper/backend/README.md 它实现了流式转录后端服务。

/home/coder/git/groq-whisper/playground 里则是一些测试用代码，用于proof of concept，验证端点和算法，主程序不应该依赖它。

我们希望在 /home/coder/git/groq-whisper/ui 里实现一个 UI 前端；并改进后端实现数据持久化。

我会直接给你一个明确判断：**这个项目最适合走“WinUI 3 原生前端 + 你现有 groq-whisper Python 服务做本地后端”**。从官方路线看，WinUI 是 Microsoft 现在推荐的新 Windows 原生 UI 框架，基于 Windows App SDK，目标就是做现代、Fluent 风格的桌面体验；WPF 仍然成熟，但更适合存量项目，.NET MAUI 更适合你明确要跨 Android/iOS/macOS/Windows 共用一套 UI 的场景。对你这个明显是 Windows-first 的实时转写产品，我会把 WinUI 3 放第一位

方向其实已经很对了：现在后端已经是 **Windows 音频采集 + Groq 转写 + FastAPI + SSE** 的服务形态。这个基础非常适合做桌面产品，并且算法，录音，混音功能已经经过精心调试，所以**别重写底层算法，只专注于增加新 UI ，日志和持久化**。最优解是让 WinUI 3 负责窗口、导航、状态展示、设置页、历史页和实时文本可视化；Python 服务继续负责采集、混音、调用 Groq、输出 patch 事件。前端只需要在本地拉起后端进程，轮询 `/healthz`，订阅 `/events`，再通过 REST 做控制。

我会这样拆：

### 1. 先把产品形态定成“桌面应用”，别做成“把接口堆上去的工具”

首页不要只是一个大文本框。建议做成四个一级区块：**实时转写、历史记录、音频设备、设置**。
实时页是主战场，布局可以这样：

* 顶部：标题栏 + 当前状态条（Groq、ffmpeg、麦克风、扬声器 loopback、网络）
* 中间：大面积转写区
* 右侧：会话信息（模型、延迟、运行时长、错误提示/日志）
* 底部：开始/暂停/停止、模型切换、导出、复制

你现在的后端已经有 `replace_from_char / committed_text / tail_text / display_text` 这类字段，这非常值钱。UI 不要每 5 秒整块重绘全文，而要**按 patch 增量更新**：
`committed_text` 用正常高对比文字，`tail_text` 用稍浅颜色或细一点的样式，表示“还可能变”。这一个细节会让用户立刻感觉这是个“高级实时转写产品”，而不是“不断闪烁的字幕框”。

### 2. 视觉风格别自创，直接吃 Fluent 2 红利

要“现代、精美、好用”，最稳的办法不是自己发明一套视觉语言，而是**直接以 Fluent 2 为母体**。Microsoft 的设计资源明确建议 Windows 体验使用 WinUI 组件；Fluent 2 也提供了 Figma UI kits，而且这些 kits 就是为了和代码库对齐、方便设计到开发交接。布局上用 Fluent 的 spacing ramp，基础单位是 **4px**；字体走 Windows 的 **Segoe UI Variable** 层级；文案尽量用 sentence case，不要大写乱飞；正文对比度要满足至少 **4.5:1**。

视觉上我会给你一个很明确的口味：**轻背景、强内容、弱装饰**。Fluent 的设计原则本身就强调 “Built for focus”，核心不是堆玻璃、堆阴影、堆渐变，而是让用户快速进入状态、少被噪音打扰。也就是说：Mica 可以有，但只放在壳层；内容区要稳；主色只用来强调关键操作；动画轻一点，别炫技。

### 3. Groq/Whisper 的能力要变成 UI 卖点，不只是后端参数

Groq 的 speech-to-text 是 **OpenAI-compatible** 的，官方提供 transcriptions 和 translations 两个端点；而且可以返回 `verbose_json`，再带上 `word` / `segment` 级时间戳。这个能力特别适合做精致 UI，比如逐词高亮、时间轴跳转、局部替换动画、导出带时间戳文本。

模型策略也建议直接做进产品里：

* **极速模式**：`whisper-large-v3-turbo`，更偏实时、价格也更友好，适合默认 live 模式。
* **高精度模式**：`whisper-large-v3`，适合对准确率更敏感的场景。

这个设计会比简单下拉框高级很多：用户看到的是“极速 / 高精度”，而不是一堆模型 ID。

### 4. 你这个仓库，真正产品化前还该补几刀

你现在后端更像“启动就录、单会话一直跑”的服务。对真实桌面产品来说，这个交互太硬了。建议把服务状态改成：

`Idle -> Preflight -> Running -> Paused -> Error`

然后补几个最关键的控制接口：

* `/start`
* `/stop`
* `/pause`
* `/resume`
* `/devices`
* `/settings`
* `/sessions`

这样 UI 才能做出真正舒服的体验：第一次打开先做环境体检，再让用户显式开始录制，而不是一启动就偷偷采集。
另外，历史记录建议落本地 SQLite，至少保存：开始/结束时间、模型、语言、完整文本、导出路径、异常日志。否则你做完漂亮 UI，用户第二天就会问一句：“我昨天那段去哪了？” 用户在 UI 上可以查看之前的每次转录的完整内容。

使用 git 管理 
git/groq-whisper/ui
git/groq-whisper/backend
忽略其他文件夹 in .git/info/exclude 或 .gitignore

/home/coder/git/groq-whisper/GROQ_APIKEY 里是测试用 API KEY 有一定的 RATE LIMIT
