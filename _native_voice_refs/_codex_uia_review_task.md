# 任务：以 Akasha UIA 实现为参考，审查 WeMai native_voice.py 的 UIA/Win32 操作是否有错

## 背景

用户的问题原意是：**借鉴 Akasha-WeChat 的 UIA 实现作为参考标杆，检查前面 WeMai feature/native-voice 分支里 native_voice.py 的 UIA/Win32 操作有没有错误。**

不是审查 Akasha 本身，Akasha 是参考标杆。

## 工作目录与分支

- 工作目录：`/workspace/WeMai-fix`
- 分支：`feature/native-voice`（已包含 native_voice.py）
- **这是只读审查任务，不要改任何代码**

## 必读文件

### 审查对象（WeMai 的 native_voice.py）
- `/workspace/WeMai-fix/native_voice.py`（446 行）— **核心审查对象**
  - 重点看 UIA/Win32 相关函数：
    - `_window_rect_and_handle`（约 296 行）— 微信窗口查找
    - `_click`（约 315 行）— 坐标点击
    - `_compose_signature` / `_chat_signature` / `_visible_message_changed` / `_recording_mode_changed`（约 328-356 行）— 截图验证
    - `NativeVoiceSender.send`（约 361 行）— 完整发送流程
    - `detect_wechat_version`（约 41 行）— 版本检测
    - `AudioRoute` 类（约 212 行）— COM 音频路由
    - `_core_audio_objects` / `_default_endpoint_ids` / `_set_defaults` / `_find_mmdevice_id`（约 111-197 行）— Core Audio 操作
- `/workspace/WeMai-fix/wx_Listener.py` — 看 voice 怎么接入发送队列（约 _send 方法）
- `/workspace/WeMai-fix/wx_Processor.py` — 看 voice 段怎么落地

### 参考标杆（Akasha 的 UIA 实现）
- `/workspace/Akasha-WeChat/wechat-weflow-bridge-ob11-public/uia_sender.py`（655 行）— **生产级参考**
  - 注意它用的是真正的 UIA（uiautomation 库 + ValuePattern + InvokePattern）
  - 注意它的线程模型、COM 初始化、窗口查找、控件定位、错误处理、资源管理

### 对比参考（我们之前改进的 WeFlow-Adapter）
- `/workspace/MaiBot-WeChat-WeFlow-Adapter/uia_sender.py` — 我们参考 Akasha 改进后的版本，可以对比看 native_voice.py 有没有遗漏的增强

## 审查要点

WeMai 的 native_voice.py 用的是 **Win32 API 坐标点击**（SetCursorPos + mouse_event），**不是** Akasha 那样的真正 UIA（ValuePattern/InvokePattern）。这是设计选择（因为微信 4.1.9 的录音按钮是按住式交互，UIA InvokePattern 不适用），但要审查这个选择带来的风险和实现是否正确。

请逐项检查：

### 1. 窗口查找（_window_rect_and_handle）
- `EnumWindows` + `IsWindowVisible` + `GetWindowText`/`GetClassName` 的用法是否正确？
- 窗口筛选条件 `cls in {"WeChatMainWndForPC", "ChatWnd"}` 和尺寸过滤 `>500x>400` 是否合理？
- 微信 4.x 的窗口 ClassName 还是这些吗？（Akasha 注释说微信 4.0 是 Electron/Chromium）
- 多窗口/多微信实例时 `candidates.sort` 的排序逻辑是否正确？
- 有没有遗漏微信最小化到托盘的情况？

### 2. 坐标点击（_click）
- `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` 的顺序和用法是否正确？
- Windows 前台窗口锁定（ForegroundLockTimeout）有没有处理？Akasha 有没有更好的做法？
- `SetCursorPos` + `mouse_event` 的组合是否可靠？要不要用 `SendInput` 替代？
- 按住式录音按钮（微信 4.1.9 是"按住说话"）——当前实现是"点击开始+点击发送"，这和微信 4.1.9 的实际交互（按住 Alt 键或按住按钮）匹配吗？**这是关键问题**：YUbot 的实现也是点击式，但微信 4.1.9 官方说明是"按住右 Alt 键说话，松开发送"。点击式真的能触发录音吗？
- 坐标计算 `rect[0] + (rect[2]-rect[0]) * fraction` 是否正确？DPI 缩放有没有问题？

### 3. 截图验证（_compose_signature / _chat_signature / _visible_message_changed / _recording_mode_changed）
- `PIL.ImageGrab` 截图的区域计算是否正确？
- `ImageStat` / `ImageChops` 的对比逻辑能否可靠检测"录音模式出现"和"新气泡出现"？
- 截图时机是否合理（点击后立即截 vs 等 sleep 后截）？
- 多显示器/HiDPI 下 `ImageGrab` 的坐标是否正确？

### 4. COM 与音频路由（AudioRoute / _core_audio_objects）
- `comtypes` 的 COM 初始化是否正确（CoInitialize 配对）？
- `IPolicyConfig.SetDefaultEndpoint` 的调用是否正确？
- 多角色（console/multimedia/communications）切换是否完整？
- 恢复逻辑是否可靠？崩溃后能恢复吗？
- 有没有 COM 对象泄漏？

### 5. 线程安全
- native_voice.py 的函数被 wx_Listener 的 UI 线程调用，有没有跨线程问题？
- `AudioRoute` 的状态保存/恢复有没有竞态？
- atexit 和信号处理恢复是否可靠？

### 6. 版本检测（detect_wechat_version）
- `psutil` + `win32api.GetFileVersionInfo` 的用法是否正确？
- 会不会误判（比如检测到 WeChatAppEx 而不是主进程）？
- 4.1.9 的版本号比较逻辑是否正确？

### 7. 与 Akasha 对比的关键差距
- Akasha 用 ValuePattern 设值、InvokePattern 点击，native_voice 用坐标点击——这个降级在"录音按钮"场景下是否必要？有没有可能用 UIA 找到录音按钮再 Invoke？
- Akasha 的错误处理/重试/超时机制，native_voice 有没有遗漏？
- Akasha 的资源管理（线程生命周期、COM 释放），native_voice 有没有遗漏？

## 约束

- **只读审查，不要改代码**
- 输出审查报告，每个问题标注：**严重程度**（致命/严重/一般/建议）+ **文件:行号** + **问题描述** + **修复建议**
- 如果某项没问题，明确说"OK"
- 特别关注第 2 点的"按住式录音按钮"问题——这是整个方案能不能工作的关键
- 最后总结：native_voice.py 的 UIA/Win32 实现整体质量如何，最关键的问题是什么，能不能作为生产级使用
