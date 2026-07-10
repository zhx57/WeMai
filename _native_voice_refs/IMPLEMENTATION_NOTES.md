# WeMai 原生微信语音条实现说明

## 实现范围

本次没有升级或改写 `wxauto` 的 3.x 控件树。文字、图片、文件仍走原有
`wxauto` 路径；只有启用后的出站 `voice` 段走独立的 `native_voice.py` 后端。

- `native_voice.py`：虚拟声卡发现、默认录音端点切换/恢复、PortAudio 预热与
  播放、loopback 诊断、微信窗口比例坐标点击、录音态截图验证和发送后聊天区
  变化验证。
- `wx_Processer.py`：支持 voice 的本地路径、HTTP(S) URL、裸 base64 和
  `data:audio/*;base64,...`；统一转为 48 kHz 单声道 16-bit PCM WAV。
- `wx_Listener.py`：发送类型增加 `voice`。voice 与原发送共享 FIFO 队列并独占
  UI worker；录制期间会刷新 worker 心跳，其他消息排队，避免并发点击微信窗口。
- `config.py` / `.env.example`：增加全部 `NATIVE_VOICE_*` 配置。
- `requirements-voice.txt`：可选的 `sounddevice`、`numpy` 依赖。
- `main.py`：启动诊断、低版本告警、正常退出/信号退出的路由恢复。
- `tests/test_native_voice.py`：覆盖段解析、WAV 落地、配置、路由诊断降级。

## 安装与配置

1. 使用微信 **4.1.9 或更高版本**。微信 3.9.x 没有 PC 原生语音录制入口。
2. 安装 VB-CABLE 或 VoiceMeeter，确认播放端（如 `CABLE Input`）与录音端
   （如 `CABLE Output`）均启用。
3. 安装可选依赖：`pip install -r requirements-voice.txt`。
4. 若 MaiBot 可能发送 MP3/OGG 等压缩格式，在系统 PATH 中安装 `ffmpeg`。
5. 在 `.env` 设置 `NATIVE_VOICE_ENABLED=true`。建议先保持
   `NATIVE_VOICE_VOICE_FALLBACK_TO_FILE=true`。

主要配置默认值：

```dotenv
NATIVE_VOICE_ENABLED=false
NATIVE_VOICE_VIRTUAL_CAPTURE_KEYWORDS=cable output,vb-audio,voicemeeter output
NATIVE_VOICE_VOICE_START_X=0.863
NATIVE_VOICE_VOICE_START_Y=0.955
NATIVE_VOICE_VOICE_CANCEL_X=0.734
NATIVE_VOICE_VOICE_CANCEL_Y=0.955
NATIVE_VOICE_VOICE_SEND_X=0.947
NATIVE_VOICE_VOICE_SEND_Y=0.955
NATIVE_VOICE_MAX_RECORD_SECONDS=55
NATIVE_VOICE_REQUIRE_CONTENT_PROVEN=false
NATIVE_VOICE_VOICE_FALLBACK_TO_FILE=true
NATIVE_VOICE_WAV_DIR=
```

坐标是相对微信窗口的比例值。微信布局、缩放或版本变化时需实机重新标定。
`NATIVE_VOICE_WAV_DIR` 为空时使用系统临时目录。

## 运行流程与恢复

每次发送会保存三个角色的默认录音端点，将它们临时切到虚拟录音设备，把 WAV
定向播放到虚拟播放设备。点击录音后，后端先比较输入栏截图；只有确认界面切换
才播放 WAV。成功点击发送后还会检查聊天区是否发生可见变化。

异常时会停止播放、点击取消并恢复端点。活动路由还注册了 `atexit` 恢复；主程序
收到 SIGINT/SIGTERM 后也会显式恢复。启动诊断若发现默认录音端点仍匹配虚拟声卡
关键词，会告警，提醒检查上次强杀遗留状态。

可在 Python 中调用诊断：

```python
from native_voice import diagnose_native_voice_route, diagnose_native_voice_loopback

print(diagnose_native_voice_route())
print(diagnose_native_voice_loopback(r"C:\path\probe.wav"))
```

打开 `NATIVE_VOICE_REQUIRE_CONTENT_PROVEN=true` 后，每次发送前都必须通过短时
loopback RMS 验证；默认关闭，与参考实现的宽松行为一致。

## 降级与已知限制

- 默认关闭时，voice 段仍按历史行为发送 `[语音消息]` 文本，现有部署不变。
- 启用后若微信低于 4.1.9、缺少 Windows/依赖/虚拟声卡、坐标验证失败或路由
  失败，默认把规范化 WAV 作为文件发送；关闭 fallback 后错误进入原死信流程。
- 原生录音是实时操作，30 秒音频约占用 30 秒；这段时间同一发送队列里的其他
  消息会等待，监听循环也不会并发操作窗口。
- 截图验证只能证明录音态和聊天区发生变化，不能证明接收端内容逐样本正确。
- 微信 4.x 与当前 wxauto 3.x 自动化层的整体兼容性仍取决于现场环境；本后端只
  隔离了语音按钮和音频路由，没有完成微信 4.x 的全控件树重适配。
- 进程被 `TerminateProcess`、断电或系统崩溃时 Python 清理无法执行，仍需人工
  把默认麦克风切回物理设备。
