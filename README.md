# voice-verifier

一个 Claude Code skill，用来筛查候选人提交的录音：

1. **AI 声音检测**：`scripts/detect_ai.py` 把音频发给多家检测器（Modulate、Resemble Detect、Reality Defender），逐家列出结论。
2. **声线查重**：`scripts/speaker_match.py` 在本地算声纹，找出一人多号 / refer 作假。

用法和判读规则见 [SKILL.md](SKILL.md)。

## Lark bot

`scripts/lark_bot.py` 让同事在 Lark 里直接做 AI 声音检测：私聊把音频文件发给 bot，或在群里回复音频文件消息并 @bot，bot 回复检测结论。

1. 在 Lark 开发者后台创建企业自建应用，开启机器人能力。
2. 权限：读取私聊消息、读取群内 @机器人的消息、获取消息中的资源文件、以机器人身份发消息。
3. 事件订阅选"长连接"，订阅 `im.message.receive_v1`。
4. 发布应用版本，把 App ID / App Secret 填进 `.env`。
5. 运行 `.venv/bin/python scripts/lark_bot.py`，保持进程不退出。

bot 通过长连接主动连 Lark，不需要公网地址。能用到 bot 的人都会消耗检测额度，并把音频上传给检测服务，请在后台控制应用的可用范围。

## 安装

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # 填入 API key
ln -s "$PWD" ~/.claude/skills/voice-verifier
```

## 注意

- 检测结果是复核信号，不是定论。
- `voiceprints/` 和 `.env` 已在 `.gitignore` 里；声纹是生物识别信息，采集前应取得当事人同意。
