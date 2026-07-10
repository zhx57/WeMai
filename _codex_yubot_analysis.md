# 任务：分析 YUbot 的原生语音发送实现，评估移植 WeMai 的可行性

## 背景

我们发现了一个名为 YUbot 的机器人项目，它**似乎实现了发送微信原生语音消息（语音条）**。我之前调研认为 wxauto 路线"理论可行但无现成实现"，这个项目可能就是现成实现。请你深度分析它的实现机制，并判断能否移植到 WeMai。

## 参考文件（必读）

### YUbot 原生语音发送实现
- `/workspace/robot_analysis/YUbot/src/wechat_agent_adapter/pc_weixin_bridge.py` — PC 微信桥接
  - 重点看：`send_voice` 方法（约 3609 行）
  - 重点看：`_start_native_voice_recording` / `_submit_native_voice_recording` / `_cancel_voice_recording_mode`（约 3597-3607 行）
  - 重点看：语音按钮坐标常量 `VOICE_START_BUTTON_X` 等（约 61-77 行）
  - 重点看：`_click_window_fraction` 方法（搜索这个函数名）
  - 重点看：`_verify_visible_outgoing_bubble` 方法（验证语音气泡）
- `/workspace/robot_analysis/YUbot/src/wechat_agent_adapter/audio_routing.py` — 音频路由（1700+行）
  - 重点看：`begin_native_voice_audio_route`（约 782 行）
  - 重点看：`restore_native_voice_audio_route`（约 876 行）
  - 重点看：`prepare_native_voice_audio_playback` / `play_prepared_native_voice_audio_playback` / `stop_native_voice_audio_playback`
  - 重点看：`play_prepared_portaudio`（约 1248 行）
  - 重点看：`diagnose_native_voice_loopback`（约 1314 行）
  - 重点看：`native_voice_route_status`（约 113 行）
  - 重点看：虚拟声卡关键词 `VIRTUAL_CAPTURE_KEYWORDS` / `VIRTUAL_RENDER_KEYWORDS`（约 41-56 行）

### WeMai 现状（移植目标）
- `/workspace/WeMai-fix/wxauto/wxauto.py` — wxauto 主类（注意第 4 行 `VERSION = "3.9.11.17"`）
- `/workspace/WeMai-fix/wx_Listener.py` — 发送逻辑（约 209 行 `_send` 方法）
- `/workspace/WeMai-fix/wx_Processor.py` — 消息处理（约 275 行语音段处理）
- `/workspace/WeMai-fix/wxauto/elements.py` — wxauto 控件

### 之前的调研报告
- `/workspace/WeMai-fix/_voice_research.md` — 我之前的调研，结论是"方案B理论可行但无现成实现"

## 你的任务

### 问题 1：YUbot 的 send_voice 是怎么工作的？真的能发原生语音条吗？
请详细分析 `send_voice` 的完整流程，逐步说明：
1. 它怎么切换音频路由的（读注册表？改默认设备？）
2. 它怎么让微信"录到"预制音频的（虚拟声卡原理）
3. 它怎么点击录音/发送按钮的（UIA？坐标？）
4. 它怎么验证语音气泡确实发出了
5. 失败时怎么回滚
6. `prepare_native_voice_audio_playback` 为什么要"预热"（注释提到 dead air 问题）

最后明确回答：**这套方案真的能发出微信原生语音条吗？** 还是有隐藏的限制（比如其实录到的是空音/杂音）？

### 问题 2：这套方案的前提条件是什么？
逐一列出：
- 操作系统要求
- 微信版本要求（注意看坐标注释提到 "WeChat 4.x compose-bar voice buttons"）
- 硬件/驱动要求（虚拟声卡？）
- 微信设置要求（麦克风要设成什么？）
- 前台/焦点要求

### 问题 3：WeMai 当前能直接用这套方案吗？
关键判断点：
- WeMai 的 wxauto 是微信 3.9.11.17（`wxauto.py` 第 4 行），而 YUbot 的坐标注释说是 "WeChat 4.x"
- 微信 3.9.x 有没有发送栏的语音录音按钮？
- 如果没有，WeMai 要用这套方案需要先做什么？

明确回答：**能直接移植 / 需要改造 / 完全不可行**，并说明理由。

### 问题 4：如果要让 WeMai 也能发原生语音条，最现实的路线是什么？
基于 YUbot 的实现，给出具体路线：
1. 如果 WeMai 必须升级微信版本，说明升级到哪个版本、wxauto 要不要换
2. 音频路由模块能否直接复用 YUbot 的 audio_routing.py（它是 Windows 专用，读注册表）
3. UI 点击部分要怎么适配（WeMai 的 wxauto 是 UIA，YUbot 是坐标点击，两者怎么结合）
4. 工程量评估（小改/中改/大改）
5. 风险评估

### 问题 5：YUbot 这套方案有没有缺陷或坑？
分析它的已知问题，比如：
- 实时录音的效率问题（duration = 音频时长）
- 虚拟声卡依赖的环境问题
- 坐标点击的脆弱性（不同分辨率/DPI/微信版本）
- 并发问题（录音期间不能同时干别的）
- "dead air" 问题是否真的解决了
- 它的验证机制（截图对比）是否可靠

## 约束

- 这是**分析评估任务**，不要改任何代码
- 输出一份清晰的评估报告，回答上述 5 个问题
- 重点是问题 1（机制）和问题 3（WeMai 能否用）
- 如果发现 YUbot 的实现有致命缺陷，要明确指出
- 最终给出一句话总结：WeMai 能否借鉴 YUbot 发原生语音，最该怎么做
