# 任务：判断 WeMai 能否实现发送微信原生语音，给出工程建议

## 背景

WeMai 是一个基于 wxauto 的微信自动化项目（`/workspace/WeMai-fix/`）。当前只能发文本（SendMsg）和文件（SendFiles），用户想知道能否实现发送微信原生语音消息（语音条）。

我已经做了详尽的调研，报告在 `_voice_research.md`，**请先完整读这份报告**，它包含了：
- wxauto 官方能力（无 SendVoice API）
- PC 微信 4.1.9 新增原生语音条功能（Alt 键录音）
- 各 Hook 工具（wxhelper/WeChatFerry/wxhook）的语音能力核查
- SILK 编码格式与 pilk 库
- 5 种可行方案评估

## 你的任务

1. **读 `_voice_research.md` 报告**
2. **读 WeMai 现有发送逻辑**：
   - `wx_Listener.py` 的 `_send` 方法（约 209 行）
   - `wxauto/elements.py` 的 `SendMsg` 和 `SendFiles`（约 236/269 行）
   - `wx_Processer.py` 的语音消息处理（约 275 行）
3. **判断并回答**：

### 问题 1：WeMai 用 wxauto 能否发送微信原生语音条？
基于报告和代码，给出明确结论（能/不能/有条件能），说明理由。

### 问题 2：如果"不能"或"有条件能"，最实际的工程方案是什么？
对照报告里的 5 个方案（A 降级发文件 / B 虚拟声卡+Alt键 / C wxhook+pilk / D 缓存劫持 / E 自研Hook），结合 WeMai 现有架构（wxauto 4.x 体系），给出**推荐方案**和**不推荐方案的理由**。

### 问题 3：如果要实现"降级发音频文件"（方案 A），WeMai 需要改什么？
具体指出：
- 哪个文件哪行需要改
- 如何让上游（MaiBot）传来的语音 base64 落地成 .mp3 文件
- 如何调 SendFiles 发出去
- 是否需要新增 voice 消息类型到 `_send` 的 kind 判断里

**只给方案和代码示意，不要实际改代码**（这是判断任务，不是实现任务）。

### 问题 4：报告里有没有遗漏或错误的地方？
检查报告的技术结论是否准确，有没有过时信息或误导内容。特别是：
- PC 微信 4.1.9 语音功能是否属实
- wxhook 是否真的支持发送语音
- SILK 编码参数（24kHz vs 16kHz）是否正确
- wxauto 真的完全不能发语音吗（有没有漏看的 API）

## 约束

- 这是**判断和建议任务**，不要改任何代码
- 输出一份清晰的评估报告，回答上述 4 个问题
- 如果报告结论正确，明确说"报告结论可信"
- 如果发现错误，指出并纠正
- 最终给出一句话总结：WeMai 能不能发原生语音，如果不能最该怎么做

## 参考文件
- `/workspace/WeMai-fix/_voice_research.md` — 调研报告（必读）
- `/workspace/WeMai-fix/wx_Listener.py` — 发送逻辑
- `/workspace/WeMai-fix/wxauto/elements.py` — wxauto 控件
- `/workspace/WeMai-fix/wx_Processer.py` — 消息处理
- `/workspace/WeMai-fix/wxauto/wxauto.py` — wxauto 主类
