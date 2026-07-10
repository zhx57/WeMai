# WeChat Agent Adapter

这是给“agent 植入微信”的本地适配器骨架，目标是绕开 `cc-connect` 的固定能力边界，把微信连接器、agent/人格和收发媒体能力做成可组合模块。

已覆盖的能力边界：

- 文本自动回复和主动发送
- 普通文字消息发送接口
- 动画表情/GIF 发送接口
- 普通语音消息接口，和“音频文件兜底”明确分开
- 实时语音/通话模式入口，后续接 ASR -> agent -> TTS -> 虚拟麦克风/微信通话
- 复用 `he-laoshi-backend` 的人格上下文 `/api/context`
- 支持 OpenAI-compatible `chat/completions` 和 `audio/speech`
- 在线控制平面：任意 PC/Android/iOS/协议网关连接器上报入站消息，后台统一生成回复并按连接器排队

## 先跑 dry-run

```powershell
cd path\to\wechat-agent-adapter
python -m wechat_agent_adapter.cli check --config .\config.example.json
python -m wechat_agent_adapter.cli dry-run --config .\config.example.json --text "/he-laoshi 发个表情"
python -m wechat_agent_adapter.cli dry-run --config .\config.example.json --text "/voice 我想听你说一句晚安"
```

默认不会真的操作微信。要接真实微信，把 `wechat.driver` 改成 `wxauto`，再运行发送命令时加 `--yes`。

## 打开 Web UI

```powershell
python -m wechat_agent_adapter.cli serve-ui --config .\config.example.json
```

打开：

```text
http://127.0.0.1:8899
```

管理台里可以看当前微信通道、人格后端状态、云端配置，并干跑文本、动画表情、普通语音消息动作。真实发送仍然需要配置非 dry-run driver，并在请求里显式确认。

## 密钥校验

默认不设置 `WECHAT_AGENT_SECRET` 时，API 不拦截，方便本地开发。上云、内网穿透或开放给手机连接器时，先设置共享密钥：

```powershell
$env:WECHAT_AGENT_SECRET = "your-shared-secret"
python -m wechat_agent_adapter.cli serve-ui --config .\config.example.json --host 0.0.0.0 --port 8899
```

开启后所有 `/api/*` 请求都需要带下面任意一种认证方式：

```powershell
$headers = @{ "X-WeChat-Agent-Secret" = $env:WECHAT_AGENT_SECRET }
Invoke-RestMethod http://127.0.0.1:8899/api/status -Headers $headers

$headers = @{ "Authorization" = "Bearer $env:WECHAT_AGENT_SECRET" }
Invoke-RestMethod http://127.0.0.1:8899/api/status -Headers $headers
```

Web UI 右上角可以输入管理密钥，密钥只保存在当前浏览器的 `localStorage`，不会写进 `config.example.json`。

## 在线控制平面

这个项目现在按“云端后台 + 多端连接器”设计，不要求 agent 和微信客户端在同一台机器上：

```text
微信端连接器 -> POST /api/inbound -> agent/persona/router -> connector queue
微信端连接器 <- GET /api/connectors/{connector_id}/poll <- 待发文字/语音/动画表情动作
微信端连接器 -> POST /api/connectors/{connector_id}/ack -> 确认已执行
```

云服务器、本机服务、内网穿透服务都可以作为控制平面。只要某个连接器在线并持有微信登录态，从任何端给这个微信号发来的消息都可以被连接器上报，后台生成回复，再由同一个连接器执行回发。

## link / 三方网关预留

`wechat_link` 是给 cc-connect `weixin link`、ilink 或同类三方网关预留的连接器。它不假设网关一定支持所有微信能力，而是通过能力矩阵明确边界：

- 文字收发：默认按可用设计。
- 图片收发：默认按可用或高概率可用设计。
- 文件、视频：保留动作和队列，需要真实 token/会话实测上游是否放行。
- 普通语音消息：接口已预留；link 当前实测不能发原生微信语音气泡，`voice_out=reserved-unsupported-now` 时后端会把投递给 `wechat_link` 的 voice 动作降级成文字。
- 动画表情：先按不支持原生表情处理，可退化为 GIF/图片/文件。
- 实时语音：link 网关不作为主路线，后续走 PC/Android/iOS UI 自动化或专门 RTC 方案。

三方网关可以直接上报到连接器专属入口：

```text
POST /api/connectors/wechat_link/inbound
POST /api/gateways/wechat_link/inbound
GET  /api/connectors/wechat_link/capabilities
GET  /api/connectors/wechat_link/poll?limit=10
POST /api/connectors/wechat_link/ack
```

网关入站字段可以用本项目标准字段，也兼容常见别名：

```json
{
  "msg_id": "link-123",
  "msg_type": "text",
  "from_user": "wxid_friend",
  "to_user": "wechat-main",
  "content": "你好",
  "timestamp": 1710000000
}
```

如果后续换了更强的三方网关，或 link 官方开放原生语音发送，只需要在 `connectors.wechat_link.capabilities` 里把对应项从 `reserved-unsupported-now`、`unsupported` 或 `needs-live-test` 改成 `ready`，后端队列协议不用变。

模拟一条 iOS 微信入站消息：

```powershell
$body = @{
  connector_id = "ios_phone_1"
  platform = "ios_wechat"
  account_id = "wechat-main"
  conversation_id = "wxid_or_room"
  sender_id = "friend_or_room_member"
  sender_name = "昵称"
  message_id = "msg-123"
  message_type = "text"
  text = "你好"
} | ConvertTo-Json -Depth 5
$headers = @{ "X-WeChat-Agent-Secret" = $env:WECHAT_AGENT_SECRET }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8899/api/inbound -Headers $headers -ContentType application/json -Body $body
Invoke-RestMethod http://127.0.0.1:8899/api/connectors/ios_phone_1/poll?limit=10 -Headers $headers
```

确认连接器已经执行：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8899/api/connectors/ios_phone_1/ack -Headers $headers -ContentType application/json -Body '{"id":"队列项ID"}'
```

## 发送普通文字消息

```powershell
python -m wechat_agent_adapter.cli send-text --config .\config.example.json --to 文件传输助手 --text "这是一条普通文字消息"
```

真实发送时同样需要把 `wechat.driver` 改成真实通道，并加 `--yes`：

```powershell
python -m wechat_agent_adapter.cli send-text --config .\config.example.json --to 文件传输助手 --text "这是一条普通文字消息" --yes
```

## 发送动画表情

```powershell
python -m wechat_agent_adapter.cli send-sticker --config .\config.example.json --to 文件传输助手 --path C:\path\to\sticker.gif
```

`dry_run=true` 时只打印动作。真实驱动会优先尝试 `SendEmotion` / `SendEmoticon` / `SendEmoji`，不支持时再尝试文件/图片发送路线。

## 发送普通语音消息

已有音频文件：

```powershell
python -m wechat_agent_adapter.cli send-voice --config .\config.example.json --to 文件传输助手 --path C:\path\to\voice.mp3
```

用 TTS 生成：

```powershell
python -m wechat_agent_adapter.cli send-voice --config .\config.example.json --to 文件传输助手 --text "晚安，别胡思乱想。"
```

注意：微信“语音气泡”不是普通文件发送。当前 PC 微信 4.x 驱动如果没有真正的 `SendVoice` 能力，本项目默认会报错，不会假装已经发成语音气泡。确实要允许把音频当文件发，可以把 `allow_voice_file_fallback` 改成 `true`。

## 当前本机要注意

你本机是 Python 3.14。wxauto v4 文档主要标 Python 3.9-3.12；如果真实驱动安装不兼容，建议单独装 Python 3.12 给这个适配器使用。

你本机微信之前看到是 4.1.10.27。wxauto 文档/下载页标的兼容版本可能低于这个版本，所以代码里把微信驱动做成可替换模块：如果 wxauto 不稳，下一步可以换成 Android 模拟器/Appium 驱动，尤其是普通语音消息和实时通话。

## 后续接入顺序

1. 先用 `dry-run` 确认人格上下文、回复拆分、媒体动作 JSON 都正确。
2. 接 `wxauto` 真实发送文本/GIF，只对 `文件传输助手` 测试。
3. 验证当前微信版本是否有真正语音消息能力。
4. 如果 PC 微信发不了语音气泡，切安卓模拟器驱动。
5. 最后做实时通话：ASR -> agent -> TTS -> 虚拟声卡/虚拟麦克风。

## 手机端微信和云端

手机端微信不要和 PC 微信绑死在一个实现里。本项目预留了三种真实通道：

- `wxauto`: PC 微信 UI 自动化。
- `android_appium`: 手机真机/安卓模拟器，适合普通语音消息、动画表情、通话 UI。
- `ios_phone`: iOS 微信连接器预留，通常需要 Mac + WebDriverAgent、辅助自动化或越狱环境。
- `protocol_gateway`: 第三方/非官方个人微信协议网关，接入成本低但账号风控风险高。
- `official_account` / `work_wechat`: 公众号、企业微信等官方接口，最稳，但入口不是普通个人微信聊天。
- `cloud_queue`: 云服务器推荐模式，云端管理台写队列，本机或手机桥接器主动轮询并执行。

个人微信通常没有官方开放的“后台收发个人号消息 API”。如果目标是个人微信号，实际可选路线是：

1. PC 微信连接器：快，适合先打通文字/GIF，语音气泡能力受 PC 客户端和自动化库限制。
2. Android 连接器：更适合普通语音消息、动画表情、通话 UI。
3. iOS 连接器：能预留，但比 Android 难，通常需要 Mac/WDA 或更高风险的系统级方案。
4. 非官方协议网关：最像“微信后台”，但存在封号/风控/稳定性风险。
5. 公众号/企业微信：官方、稳定，适合能改变聊天入口的场景。

部署样例在 `deploy/`，Docker 样例在 `Dockerfile`。
