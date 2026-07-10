# 任务：修复 native_voice.py 的 Win32 严重问题

## 背景

之前审查发现 WeMai `feature/native-voice` 分支的 `native_voice.py` 有 4 个严重的 Win32 实现问题。现在请直接修复（只修这些严重问题，不要大改架构）。

## 工作目录

- `/workspace/WeMai-fix`
- 分支：`feature/native-voice`

## 必读文件

- `/workspace/WeMai-fix/native_voice.py` — 要修复的文件
- `/workspace/Akasha-WeChat/wechat-weflow-bridge-ob11-public/uia_sender.py` — 参考标杆，重点看 `_activate` 方法（约 538-568 行）用了 AttachThreadInput

## 要修的问题（按严重程度）

### 问题 1：窗口激活缺少 AttachThreadInput（严重）

**位置**：`native_voice.py` 的 `_click` 函数（约 315-325 行）

**现状**：
```python
def _click(hwnd, rect, x_fraction, y_fraction):
    import win32api
    import win32con
    import win32gui
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    ...
```

**问题**：Windows 前台窗口锁定（ForegroundLockTimeout）会导致 `SetForegroundWindow` 只闪烁任务栏而不会真正激活，后续 `SetCursorPos`+`mouse_event` 点击到错误位置。

**修复**：参照 Akasha [uia_sender.py:538-554](file:///workspace/Akasha-WeChat/wechat-weflow-bridge-ob11-public/uia_sender.py#L538) 的 `_activate` 方法，用 `AttachThreadInput` 绕过前台锁定：
```python
def _click(hwnd, rect, x_fraction, y_fraction):
    import ctypes
    import win32api
    import win32con
    import win32gui
    user32 = ctypes.windll.user32
    # 先恢复窗口（避免最小化）
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    # AttachThreadInput 绕过前台窗口锁定
    we_chat_tid = user32.GetWindowThreadProcessId(hwnd, None)
    current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(current_tid, we_chat_tid, True)
    try:
        win32gui.SetForegroundWindow(hwnd)
        win32gui.BringWindowToTop(hwnd)
    finally:
        user32.AttachThreadInput(current_tid, we_chat_tid, False)
    time.sleep(0.1)  # 给窗口激活一点时间
    ...
```

### 问题 2：mouse_event 应改用 SendInput（严重）

**位置**：`native_voice.py` 的 `_click` 函数

**现状**：
```python
win32api.SetCursorPos((x, y))
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
```

**问题**：`mouse_event` 是老 API，在 UIPI、高 DPI、远程桌面场景下可能失效。

**修复**：改用 `SendInput`（现代 API，兼容性更好）。用 ctypes 实现：
```python
# SendInput 结构和调用
INPUT_MOUSE = 0
MOUSEINPUT = ctypes.c_uint * 7  # 简化，实际要用正式结构

# 或者更简洁：用 win32api 的 SendInput 封装（如果 pywin32 版本支持）
# 保留 SetCursorPos 定位 + SendInput 点击
```

请用标准的 SendInput 实现（MOUSEINPUT 结构 + SendInput 函数），替换 mouse_event。注意要同时支持"点击"和未来可能的"按住"场景（虽然当前只改点击，但结构要便于扩展按住式）。

### 问题 3：DPI 缩放未处理（严重）

**位置**：`native_voice.py` 的 `_click` 函数和 `_window_rect_and_handle`、截图函数

**问题**：`GetWindowRect` 返回逻辑像素，`SetCursorPos` 需要物理像素。DPI > 100% 的显示器上坐标会偏移。

**修复**：
1. 在模块导入时（或首次调用时）设置进程 DPI 感知：
```python
def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
```
2. 在模块顶部调用一次 `_enable_dpi_awareness()`
3. 这样 `GetWindowRect` 和 `SetCursorPos` 就都是物理像素，坐标一致

注意：DPI 感知必须在进程启动早期设置，且只能设置一次。如果 wxauto 或其他模块已经设置过，不要重复设置（捕获异常即可）。

### 问题 4：COM 初始化无显式 CoInitialize（一般→建议修）

**位置**：`native_voice.py` 的 COM 相关函数（`_core_audio_objects` 约 111 行、`AudioRoute` 约 212 行）

**问题**：comtypes 会自动初始化 COM，但和 UI 线程的 COM 模式可能冲突。

**修复**：在 `AudioRoute.begin()` 和 `diagnose_native_voice_loopback` 等需要 COM 的函数入口，显式调用 `pythoncom.CoInitialize()`，在 finally 里 `CoUninitialize()`：
```python
def begin(self):
    import pythoncom
    pythoncom.CoInitialize()
    try:
        # 原有逻辑
        ...
    finally:
        pythoncom.CoUninitialize()
```

注意：`pythoncom.CoInitialize` 是幂等的（重复调用不会出错，但要配对 CoUninitialize）。如果 UI 线程已经是 STA，CoInitialize 会兼容。

## 不要做的事

- **不要改"点击式录音"为"按住式"**——这个需要实机验证，不在本次修复范围
- 不要改架构、不要改发送流程、不要改音频路由逻辑
- 不要改测试文件（除非新代码破坏了现有测试）
- 不要 commit、不要 push

## 完成后

1. `python -m py_compile native_voice.py` 确认无语法错误
2. `python -m pytest tests/ -q` 确认测试通过
3. `git add native_voice.py` 暂存（不要 commit）
4. 简要说明改了哪些行、怎么改的
