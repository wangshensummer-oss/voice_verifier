# voice-verifier

一个 Claude Code skill，用来筛查候选人提交的录音：

1. **AI 声音检测**：`scripts/detect_ai.py` 把音频发给多家检测器（Modulate、Resemble Detect、Reality Defender），逐家列出结论。
2. **声线查重**：`scripts/speaker_match.py` 在本地算声纹，找出一人多号 / refer 作假。

用法和判读规则见 [SKILL.md](SKILL.md)。

## 安装

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # 填入 API key
ln -s "$PWD" ~/.claude/skills/voice-verifier
```

## 注意

- 检测结果是复核信号，不是定论。
- `voiceprints/` 和 `.env` 已在 `.gitignore` 里；声纹是生物识别信息，采集前应取得当事人同意。
