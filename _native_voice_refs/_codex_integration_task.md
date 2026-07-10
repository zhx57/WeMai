# 任务：在 WeMai 集成原生微信语音条发送能力（基于 YUbot 参考实现）

## 重要：这是你之前评估结论的执行任务

你已经分析过 YUbot 的原生语音发送实现（评估报告在 `_native_voice_refs/01_codex_yubot_evaluation.md`），结论是：

> WeMai 可以借鉴 YUbot 发原生语音，但必须先升级到微信 4.1.9+ 并更换或重适配微信 4.x 自动化层；最现实方案是复用 YUbot 的虚拟声卡路由和坐标录音 bridge，而不是在现有微信 3.9.11.17 的 wxauto 上直接加接口。

现在请在 `feature/native-voice` 分支上**执行这个集成**。这是当前工作目录 `/workspace/WeMai-fix`，已经在该分支上。

## 工作目录与分支

- 工作目录：`/workspace/WeMai-fix`
- 当前分支：`feature/native-voice`（已切换好，直接改代码即可）
- 不要切换分支，不要 merge main

## 必读参考文件（都在 _native_voice_refs/ 下）

### 评估与分析报告
- `_native_voice_refs/00_voice_research_report.md` — 之前的语音调研报告（wxauto/wxhelper/wxhook/SILK 编码等全景）
- `_native_voice_refs/01_codex_yubot_evaluation.md` — **你自己的评估报告**（最重要，包含完整机制分析、前提条件、移植路线、缺陷坑）
- `_native_voice_refs/02_codex_weimai_eval.md` — 你对 WeMai 发送语音的初步评估

### YUbot 参考源码（在 _native_voice_refs/yubot_src/ 下，请重点参考）
- `_native_voice_refs/yubot_src/pc_weixin_bridge.py` — YUbot 的 PC 微信桥接（含 `send_voice` 方法，约 3609 行；坐标常量约 61-77 行；`_start_native_voice_recording`/`_submit_native_voice_recording`/`_cancel_voice_recording_mode` 约 3597-3607 行）
- `_native_voice_refs/yubot_src/audio_routing.py` — YUbot 的音频路由模块（1700+行，含 `begin_native_voice_audio_route` 约 782 行、`restore_native_voice_audio_route` 约 876 行、`play_prepared_portaudio` 约 1248 行、`diagnose_native_voice_loopback` 约 1314 行、虚拟声卡关键词 `VIRTUAL_CAPTURE_KEYWORDS`/`VIRTUAL_RENDER_KEYWORDS` 约 41-56 行）
- `_native_voice_refs/yubot_src/asset_layout.py` — 资源布局
- `_native_voice_refs/yubot_src/config.example.json` — YUbot 配置示例（注意 voice_out/voice_in 字段、voice_output_dir、voice_triggers）
- `_native_voice_refs/yubot_src/requirements-voice.txt` — 语音依赖（easyocr、sounddevice）
- `_native_voice_refs/yubot_src/mobile-bridge-notes.md` — 移动桥接笔记
- `_native_voice_refs/yubot_src/README_YUbot.md` — YUbot README

### WeMai 现有代码（你要改的对象）
- `wxauto/wxauto.py` — wxauto 主类（注意 `VERSION = "3.9.11.17"`，第 4 行；版本门禁已改为警告）
- `wxauto/elements.py` — wxauto 控件（`ChatWnd`、`SendMsg`、`SendFiles`）
- `wx_Listener.py` — 发送逻辑（`_send` 方法约 209 行，只允许 `{"text","image","file"}`）
- `wx_Processer.py` — 消息处理（注意是 Processor 少了 s；约 275 行 `elif seg_type == "voice": data = "[语音消息]"`）
- `config.py` — 配置（从 .env 加载）
- `main.py` — 入口
- `chat_name_utils.py` — 聊天名规范化

## 你要做的集成工作

按照你评估报告"问题 4：最现实的移植路线"的推荐路线执行。核心是：**保留 WeMai 的消息处理和发送队列，把"微信 4.x 出站驱动"扩展为独立 voice 发送后端；复用 YUbot 的音频路由逻辑和坐标录音流程。**

### 必做项（按顺序）

#### 1. 新建原生语音发送后端模块
新建一个独立模块（建议命名 `native_voice.py` 或 `voice_bridge.py`），**不要塞进 wxauto 控件类**。它应包含：
- 音频路由管理：参考 YUbot `audio_routing.py` 的核心能力（枚举端点、识别虚拟声卡、切换默认录音设备、恢复原设备、PortAudio 播放、loopback 诊断）。抽成 WeMai 独立服务，不要直接复制整个文件（YUbot 有 1700 行，很多是诊断/可视化，WeMai 只要核心）。
- 坐标录音驱动：参考 YUbot `send_voice` 的流程（切换音频路由 → 预热播放 → 点击录音按钮 → 播放 WAV → 等待 → 点击发送 → 验证气泡 → 恢复设备）。坐标常量用环境变量可覆盖（参考 YUbot 的 `WECHAT_PC_VOICE_*` 模式）。
- 失败回滚：异常时取消录音、恢复音频设备。
- 关键增强（你评估报告里强调的）：**点击开始后先验证 UI 已进入录音模式，再开始播放**（YUbot 原版没做，避免把 WAV 播完却没在录音）。

#### 2. 扩展 WeMai 发送链路支持 voice 类型
- `wx_Processor.py`：不再把 voice 段转成 `[语音消息]` 文本。改为：解析 voice 段（base64 音频/URL/文件路径）→ 落地成 WAV（如果不是 WAV 先转换）→ 调用 voice 发送后端。
- `wx_Listener.py`：`_send` 的 kind 白名单增加 `"voice"`，或新增独立的 voice 发送路径（注意 voice 发送需要独占微信窗口，不能和 text/image/file 并发，建议在发送队列里加独占锁）。
- 注意 WeMai 的发送是在 UI 线程（`_assert_ui_thread`），voice 发送耗时较长（实时录制 = 音频时长），不能阻塞 UI 线程。考虑：要么 voice 发送走独立线程并临时让出 UI 线程，要么明确文档化"voice 发送期间其他发送会排队"。

#### 3. 配置项
在 `config.py` 增加（从 .env 加载，保持现有风格）：
- `NATIVE_VOICE_ENABLED`（默认 false，因为依赖虚拟声卡和微信 4.x，默认关闭避免破坏现有用户）
- `NATIVE_VOICE_VIRTUAL_CAPTURE_KEYWORDS`（虚拟录音设备关键词，默认含 cable output/vb-audio/voicemeeter）
- `NATIVE_VOICE_VOICE_START_X/Y`、`NATIVE_VOICE_CANCEL_X/Y`、`NATIVE_VOICE_SEND_X/Y`（录音按钮坐标，默认用 YUbot 实测值 0.863/0.955、0.734/0.955、0.947/0.955）
- `NATIVE_VOICE_MAX_RECORD_SECONDS`（默认 55）
- `NATIVE_VOICE_REQUIRE_CONTENT_PROVEN`（是否强制 loopback 验证，默认 false 先跟 YUbot 行为一致）
- `NATIVE_VOICE_VOICE_FALLBACK_TO_FILE`（voice 发送失败时是否降级为文件发送，默认 true，保证可用性）
- `NATIVE_VOICE_WAV_DIR`（WAV 临时目录，空则用插件数据目录）

#### 4. 依赖管理
- 新增 `requirements-voice.txt`（参考 YUbot 的）：`sounddevice`、`numpy`、（可选 easyocr 用于 OCR 验证，但 WeMai 可先用截图比对，不强制 easyocr）
- 这些是可选依赖，只有启用原生语音时才需要。在 `main.py` 或 voice 模块里做 import 守护：未启用或依赖缺失时降级为文件发送。

#### 5. 运维与恢复
- 进程退出时恢复音频设备（atexit + 信号处理）
- 启动时检查是否遗留在虚拟默认设备（上次崩溃未恢复）
- 诊断命令/函数：检查当前音频路由状态、虚拟声卡是否就绪

#### 6. 微信版本与自动化层
这是关键决策点。你评估报告说"WeMai 当前绑定微信 3.9.11.17，3.9.x 没有录音按钮"。

**处理方式**：
- 不要强行升级 wxauto 到 4.x（大工程，超出本次任务范围）
- voice 发送后端**独立于 wxauto**：它直接操作微信窗口（用 pywin32/uiautomation 获取窗口句柄和矩形，用坐标点击），不依赖 wxauto 的 ChatWnd 控件树
- 这样：text/image/file 继续走 wxauto（3.9.x 或 4.x 都行），voice 走独立的坐标驱动 bridge
- 在文档里明确：**voice 发送要求微信 4.1.9+**，text/image/file 不受影响
- 启动时检测微信版本（如果能拿到），若 < 4.1.9 且启用了 voice，警告并自动降级为文件发送

### 不要做的事
- 不要改 wxauto 的核心控件树适配（3.x→4.x 重适配是另一个大任务）
- 不要删除/破坏现有的 text/image/file 发送
- 不要把 YUbot 的 1700 行 audio_routing.py 整个复制进来，抽核心
- 不要引入 easyocr 作为硬依赖（可选）
- 不要切换分支、不要 merge main、不要 push（最后我来 push）

## 约束与质量要求

- 生产级代码：错误处理、日志、资源清理、线程安全
- 中文注释（与现有代码风格一致）
- 不破坏现有功能：`NATIVE_VOICE_ENABLED=false`（默认）时行为与现在完全一致
- Windows 平台专用模块要做好非 Windows 的 graceful degradation（import 失败时降级）
- 写测试（至少覆盖：voice 段解析、WAV 落地、配置加载、音频路由状态检查的 mock 测试；坐标点击和真实录音无法在沙箱测，mock 掉）

## 完成后

1. 运行 `python -m py_compile` 确认无语法错误
2. 运行 `python -m pytest tests/ -q` 确认测试通过（至少不破坏现有测试）
3. 在 `_native_voice_refs/IMPLEMENTATION_NOTES.md` 写一份实现说明：改了哪些文件、新增了什么、怎么配置、怎么用、已知限制
4. 用 `git add` 暂存所有改动（包括 _native_voice_refs），**不要 commit**，我会检查后提交

## 一句话目标

让 WeMai 在 `NATIVE_VOICE_ENABLED=true` + 微信 4.1.9+ + 虚拟声卡环境下，能把 MaiBot 发来的 voice 段作为原生语音条发出；环境不满足时自动降级为文件发送，不影响现有功能。
