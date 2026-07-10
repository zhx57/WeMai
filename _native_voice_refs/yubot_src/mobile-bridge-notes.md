# Mobile WeChat Bridge Notes

手机端微信建议作为独立 bridge，不直接塞进云服务器进程里。

推荐链路：

```text
手机/PC/协议 bridge -> POST /api/inbound -> 云服务器 Web UI/API -> connector queue
手机/PC/协议 bridge <- GET /api/connectors/{connector_id}/poll -> 执行动作
手机/PC/协议 bridge -> POST /api/connectors/{connector_id}/ack -> 确认完成
```

要实现的手机 bridge 能力：

- 普通文字消息：定位聊天输入框，输入文字，点击发送。
- 动画表情/GIF：用系统分享、文件选择器，或微信表情入口发送。
- 普通语音消息：长按语音按钮，播放/注入 TTS 音频，松开发送。
- 实时语音：接管通话页，ASR 监听，对 agent 输出做 TTS，并通过虚拟音频或扬声器/麦克风链路注入。

云端 API 已预留：

- `POST /api/inbound`
- `GET /api/connectors/{connector_id}/poll?limit=10`
- `POST /api/connectors/{connector_id}/ack` with `{"id":"..."}`
- `GET /api/cloud/poll?limit=10`
- `POST /api/cloud/ack` with `{"id":"..."}`
- `POST /api/cloud/enqueue`

如果云端设置了 `WECHAT_AGENT_SECRET`，bridge 的所有 `/api/*` 请求都必须带：

```http
X-WeChat-Agent-Secret: <WECHAT_AGENT_SECRET>
```

也可以使用：

```http
Authorization: Bearer <WECHAT_AGENT_SECRET>
```

## Android 连接器

推荐先做 Android，因为 Appium/uiautomator2、无障碍服务、模拟器方案都更成熟：

- 入站：监听通知、无障碍事件，或轮询微信聊天列表。
- 出站文字：打开会话，输入文字，点击发送。
- 出站动画表情：通过微信表情入口、系统分享或文件选择器。
- 出站普通语音：长按语音按钮，播放 TTS 音频，松开。
- 实时通话：识别通话页，ASR -> agent -> TTS，音频链路用虚拟声卡或外放/麦克风。

## iOS 连接器

iOS 可以接入，但不要把它当成和 Android 一样简单：

- 常规自动化通常需要 Mac + WebDriverAgent/XCUITest。
- 微信沙盒和系统限制会让通知读取、后台常驻、文件注入更麻烦。
- 普通文字可以优先通过 UI 自动化打通。
- 语音气泡和实时通话通常要配合音频路由、辅助设备或更高权限方案。
- 越狱/非官方方案能降低操作难度，但会明显增加稳定性和账号风险。

如果你能接受非个人微信入口，公众号/企业微信/小程序客服是更稳的官方路线。个人微信号要实现“后台在线收发”，本质上仍需要已登录客户端桥接器或非官方协议网关。
