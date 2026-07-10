# WeMai 发送微信原生语音消息 — 可行性调研报告（整合）

> 调研日期：2026-07-10 ｜ 用途：codex 决策依据
> 双路调研整合，结论交叉验证一致

---

## 一、核心结论（TL;DR）

| 问题 | 答案 |
|---|---|
| PC 微信能发原生语音消息（语音条）吗？ | ✅ 能，自 **微信 PC 4.1.9**（2026-04-21 发布）起原生支持 |
| wxauto 能发送原生语音条吗？ | ❌ **不能**。架构决定（纯 UIA，无 Hook/协议能力），全系（含商业版 wxautox4）无 SendVoice API |
| 有现成工具能发预制音频为语音条吗？ | ❌ 主流开源工具（wxauto/wxhelper/WeChatFerry/itchat）均不支持。仅 wxhook（WeChatHook）声明支持，但绑旧版微信 |
| 最可行方案 | ① 降级发音频文件（SendFiles，非语音条）；② wxauto + 虚拟声卡 + 模拟 Alt 键录音（实时，效率低）；③ wxhook + pilk（DLL 注入，绑 3.9.x，有封号风险） |

---

## 二、WeMai 现状

WeMai 当前发送能力（[wx_Listener.py:209](file:///workspace/WeMai-fix/wx_Listener.py#L209)）：
- `kind == "text"` → `chat.SendMsg(data)` 文本
- `kind in {"image", "file"}` → `chat.SendFiles(data)` 文件
- **无语音发送**

wxauto 的语音相关能力仅限**接收**：
- `GetNextNewMessage(savevoice=True)` 保存收到的语音转文字
- `_get_voice_text()` 语音转文字（[elements.py:162](file:///workspace/WeMai-fix/wxauto/elements.py#L162)）
- **无任何发送语音的 API**

---

## 三、PC 微信 4.1.9 原生语音功能

### 3.1 上线信息
- **2026-04-21**，微信派官宣微信 4.1.9 for Windows/Mac，上线"直接发送语音消息"（语音条）
- 被称为"等了十年的功能"
- 注意：4.1.8 的"语音输入"是语音转文字，**不是**语音条；4.1.9 才是语音条

### 3.2 交互方式
- 聊天框右下角麦克风图标，按住说话
- 快捷键：按住 **右 Alt**（Windows）/ **Option**（Mac），松开发送
- 最长 60 秒，带进度条
- 取消：拖至【取消】按钮

### 3.3 关键限制（对自动化致命）
- **录音是实时麦克风采集，没有"选择音频文件发送为语音"的入口**
- 这是所有自动化方案的核心障碍——PC 微信本身就不支持"把文件发成语音条"

### 3.4 语音格式
- 编码：SILK V3（腾讯魔改版）
- 采样率：24000 Hz（新版主流；旧资料有 16000 Hz）
- 单声道，16-bit PCM
- 微信 SILK 文件头：`\x02` + `#!SILK_V3`（比标准多一个 `\x02`），去掉结尾 `\xFF\xFF`
- 扩展名：.silk（PC）/ .amr（手机，实际是 SILK）

来源：
- [IT之家：微信 4.1.9 发布](https://www.ithome.com/0/942/316.htm)
- [掘金：微信 4.1.9 全平台更新](https://juejin.cn/post/7631404744758247470)
- [腾讯客服回应](https://www.huxiu.com/moment/1236282.html)
- [QQ新闻 2026-05-09](http://news.qq.com/rain/a/20260509A053TC00)

---

## 四、各自动化工具能力核查

### 4.1 wxauto / wxautox4（UIA 路线）

| 维度 | 现状 |
|---|---|
| 接收语音 | ✅ 支持（savevoice、_get_voice_text） |
| 发送语音条 | ❌ 无 SendVoice API |
| 发送音频文件 | ✅ SendFiles（但显示为文件，非语音条） |
| 官方计划 | 未公开承诺，FAQ 未列入"永不支持"，属空白 |
| 技术原理 | 纯 UIA，不注入/不破解/不抓包 |
| 版本支持 | wxauto(开源)→3.9.x；wxautox4→4.0.5；4.1.9 兼容性待验证 |

**关键**：wxauto 设计哲学是"模拟人操作"，而 PC 微信 4.1.9 发语音本质是"实时麦克风录音"——只要微信不提供"选文件作为语音发送"的入口，wxauto 就无法优雅地把预制音频发成语音条。

来源：
- [wxauto GitHub](https://github.com/cluic/wxauto)
- [wxautox4 PyPI](https://pypi.org/project/wxautox4/)
- [wxauto-restful-api（确认无语音接口）](https://github.com/cluic/wxauto-restful-api)
- [CSDN：wxauto能否发语音](https://wenku.csdn.net/answer/z91x8shpwf)

### 4.2 wxhelper（Hook 路线）
- 45 个接口，**无发送语音**，仅有"获取语音消息"（接收方向）
- 支持微信 3.9.5.81（最高），**不支持 4.x**
- 版本错位：语音条功能在 4.1.9，wxhelper 只到 3.9.x

来源：[wxhelper PyPI](https://pypi.org/project/wxhelper/)

### 4.3 WeChatFerry（Hook 路线）
- 协议枚举：`FUNC_GET_AUDIO_MSG=0x16`（仅接收），**无 FUNC_SEND_VOICE**
- smc 模块做 Silk↔MP3 转换，服务接收侧
- 不支持 4.x

来源：[WeChatFerry GitHub](https://github.com/lich0821/WeChatFerry)

### 4.4 wxhook / WeChatHook（Hook 路线）— 唯一声明支持发送语音 ✅
- 仓库：https://gitee.com/loveljsheng/WeChatHook
- README 接口列表第 10 项明确"发送语音"
- 适配微信：3.9.5.81 / 3.9.10.19 / 3.9.11.25
- 原理：DLL 注入 + Hook 微信内部发消息函数
- **目前公开资料中唯一明确声明支持 PC 微信发送原生语音的 Python 框架**
- 限制：绑旧版微信（3.9.x），与 wxauto 架构冲突，有封号风险

### 4.5 其他工具
| 工具 | 发送语音能力 |
|---|---|
| weixin-gateway (npm) | ✅ sendVoice（iLink/OpenClaw 协议，非 PC 客户端） |
| 企业微信 HOOK SDK | ✅ 操作码 0x101019 + CDN 上传 silk |
| 第三方 SaaS API (chuapi/weiti) | ✅ postVoice（付费+封号风险） |
| itchat/wxpy | ❌ Web 协议已失效，且只能发文件 |
| pywechat | 仅打语音电话，非语音消息 |

---

## 五、可行方案评估

### 方案 A：降级发音频文件（最简单，非原生）
- 用现有 `SendFiles` 发 .mp3/.silk
- 对方收到文件附件（可点击播放），**不是语音条**
- ✅ 现成可用，零改动
- ❌ 体验降级

### 方案 B：wxauto + 虚拟声卡 + 模拟 Alt 键录音（最现实的原生方案）
- 利用 4.1.9 的 Alt 键录音功能
- UI 自动化按下 Alt + 虚拟声卡（VB-Cable）播放预制音频 + 松开 Alt
- ✅ 产出真正的原生语音条，封号风险低
- ❌ 必须实时录制（60 秒语音占 60 秒），效率极低
- ❌ 需配置虚拟声卡，环境依赖重
- ❌ 时序控制难，需前台焦点
- ❌ wxauto 无此封装，需自行实现

### 方案 C：wxhook + pilk（唯一能发预制音频为语音条的现成方案）
- pilk 编码音频为微信 SILK 格式 → wxhook 发送语音接口
- ✅ 能发预制音频为原生语音条
- ❌ 绑定旧版微信 3.9.10.19，与 WeMai 的 wxauto 4.x 架构冲突
- ❌ DLL 注入有封号风险
- ❌ 需 POC 验证

### 方案 D：缓存文件劫持（高难高风险）
- 监控微信语音缓存目录，录音瞬间用预制 SILK 覆盖
- ❌ 4.x 缓存路径/加密未公开，需逆向
- ❌ 时序要求极精准
- ❌ 封号风险高

### 方案 E：自研 Hook（参考 wxhook/企微 HOOK）
- 仿操作码 0x101019：pilk 编码 silk → CDN 上传 → 触发语音消息
- ❌ 逆向工程量大，版本维护痛苦

---

## 六、SILK 编码工具

| 工具 | 语言 | 用途 |
|---|---|---|
| **pilk** | Python | 微信 SILK 编解码（推荐），`pip install pilk` |
| silk-v3-decoder | C/Shell | 批量 silk↔mp3 转换 |
| ffmpeg | C | PCM 重采样（中间格式） |

### pilk 用法
```python
import pilk
# PCM → SILK（tencent=True 加微信 \x02 头）
silk_data = pilk.encode(pcm_data, sample_rate=24000, target_rate=24000, tencent=True)
# SILK → WAV
duration = pilk.silk_to_wav("voice.silk", "voice.wav")
```

### mp3 → 微信 silk 流水线
```
mp3/wav
  → ffmpeg: ffmpeg -i in.mp3 -f s16le -ar 24000 -ac 1 out.pcm
  → pilk.encode(pcm, 24000, 24000, tencent=True)
  → out.silk（微信可识别）
```

来源：
- [pilk GitHub](https://github.com/foyoux/pilk)
- [silk-v3-decoder](https://github.com/kn007/silk-v3-decoder)

---

## 七、版本要求红线

- **微信 PC ≥ 4.1.9** 才有原生发语音能力
- wxhelper / wxauto(开源3.x版) 最高只到 3.9.x，**与 4.1.9 语音功能不兼容**
- wxautox4 支持 4.0.5，4.1.9 兼容性需验证
- wxhook 绑 3.9.10.19，与 4.x 不兼容

---

## 八、风险提示

- Hook 类工具（wxhelper/WeChatFerry/wxhook）有明确封号风险
- wxauto 官方文档警告"曾用 hook 类工具会导致风控"
- 缓存劫持方案风险最高
- UI 自动化方案（wxauto）风险最低，但能力受限

---

## 九、给 codex 的决策建议

### 9.1 如果目标是"发送预制音频文件并显示为语音条"
- **当前无现成工具可用**（wxhook 除外，但绑旧版微信，与 WeMai 架构冲突）
- 最现实路径：wxauto + 虚拟声卡 + 模拟 Alt 键录音（针对 4.1.9+），但效率低
- 需自研，预估难度中高

### 9.2 如果可接受"音频以文件形式发送"
- 直接用 wxauto `SendFiles` 发 .mp3，现成可用
- 非语音条，体验降级

### 9.3 推荐做法
1. **短期**：WeMai 用 SendFiles 发音频文件（已有能力，零改动）
2. **中期**：评估 wxautox4 对 4.1.9 的兼容性，若兼容则实现"模拟 Alt 键 + 虚拟声卡"方案
3. **长期**：关注 wxauto 官方是否推出 SendVoice，或微信是否开放"选文件发语音"入口

---

## 十、全部信息源

### PC 微信 4.1.9 语音功能
- [IT之家：微信 4.1.9 发布](https://www.ithome.com/0/942/316.htm)
- [掘金：微信 4.1.9 全平台更新](https://juejin.cn/post/7631404744758247470)
- [新浪：PC版语音输入与发送语音消息](https://cj.sina.cn/articles/view/7879848900/1d5acf3c406802zhtg)
- [虎嗅：腾讯客服回应](https://www.huxiu.com/moment/1236282.html)
- [QQ新闻 2026-05-09](http://news.qq.com/rain/a/20260509A053TC00)
- [什么值得买：微信PC版大升级](https://post.m.smzdm.com/p/axkeo7gw/)

### wxauto 体系
- [wxauto GitHub](https://github.com/cluic/wxauto)
- [wxauto 官方文档](https://docs.wxauto.org)
- [wxautox4 PyPI](https://pypi.org/project/wxautox4/)
- [wxauto-restful-api（确认无语音接口）](https://github.com/cluic/wxauto-restful-api)
- [wxauto-mcp](https://github.com/cluic/wxauto-mcp)
- [CSDN：wxauto能否发语音](https://wenku.csdn.net/answer/z91x8shpwf)
- [魔改wxauto打语音电话](https://blog.csdn.net/eatmulizi/article/details/137020520)

### Hook 类工具
- [wxhelper PyPI（45接口清单）](https://pypi.org/project/wxhelper/)
- [WeChatFerry GitHub](https://github.com/lich0821/WeChatFerry)
- [WeChatFerry 实战指南](https://blog.csdn.net/weixin_28725959/article/details/160997336)
- [wxhook/WeChatHook（支持发送语音）](https://gitee.com/loveljsheng/WeChatHook)

### 协议/SaaS
- [weixin-gateway(npm, sendVoice)](https://www.npmjs.com/package/weixin-gateway)
- [企业微信HOOK语音发送(操作码0x101019)](https://wenku.csdn.net/doc/4tchcnbh49)
- [第三方 postVoice API](https://weiti.apifox.cn/347297173e0)

### SILK 编码
- [pilk GitHub](https://github.com/foyoux/pilk)
- [pilk PyPI](https://pypi.org/project/pilk/)
- [silk-v3-decoder](https://github.com/kn007/silk-v3-decoder)
- [微信SILK格式解析](https://blog.csdn.net/weixin_29051811/article/details/159608386)
- [微信语音转Silk杂音分析(24kHz)](https://ask.csdn.net/questions/8859373)
- [pcm转silk](https://g.pconline.com.cn/x/1983/19836643.html)

### 替代方案
- [按键精灵语音替换（缓存劫持）](https://wenku.csdn.net/answer/cdye9in00dzz)
- [企业微信模拟发送语音（Java MP3→SILK）](https://blog.csdn.net/p6448777/article/details/155004340)
- [微信语音包插件原理](https://blog.csdn.net/weixin_29215391/article/details/158551572)
