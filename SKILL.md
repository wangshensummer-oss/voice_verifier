---
name: voice-verifier
description: 验证候选人提交的录音是否可信。两个检查：(1) 是不是 AI 合成/克隆的声音，(2) 这条声线是否和已入库的其他人重复（refer 作假、一人多号）。当用户提到"验证录音"、"是不是 AI 声音"、"声线重复"、"refer 作假"、"voice verify"，或给出一批候选人音频要筛查时使用。
---

# Voice Verifier

对候选人录音做两层检查，输出 PASS / REVIEW / FLAG。**任何一层都只给出"需要人工复核"的信号，不要仅凭工具结果直接拒掉一个人**——检测器对没见过的 TTS 模型、压缩过的音频、短音频都会出错。

脚本在本 skill 目录的 `scripts/` 下，下面用 `$SKILL` 指代本目录。

## 准备（只需一次）

```bash
python3 -m venv $SKILL/.venv && $SKILL/.venv/bin/pip install -r $SKILL/requirements.txt
cp $SKILL/.env.example $SKILL/.env   # 然后让用户自己填 API key
```

API key 由用户自己填进 `.env`，不要让用户把 key 贴到对话里，也不要把 key 打印出来。
m4a / aac / amr 等格式做声纹比对需要 `ffmpeg`；wav / flac / mp3 / ogg 不需要。

## 检查 1：是不是 AI 声音

```bash
$SKILL/.venv/bin/python $SKILL/scripts/detect_ai.py <音频...> [--json] [--zero-retention]
```

- 会调用 `.env` 里配置了 key 的所有检测器（Modulate、Resemble Detect、Reality Defender），逐个列出结论，再给一个综合结论。
- 综合规则：任一检测器判 fake → `FLAG`；有检测器判"部分合成"或结果缺失 → `REVIEW`；全部判 real → `PASS`（只有一个检测器时会标注 single detector）。
- 不要把几家的分数平均成一个"概率"，它们的分数没有校准到同一尺度。
- 音频会被上传到第三方。候选人的声音是个人敏感信息，默认建议加 `--zero-retention`（目前只有 Resemble 支持该参数）。
- ElevenLabs 的 AI Speech Classifier 只对 ElevenLabs 自家生成的音频可靠，没有确认到可用的公开 API，所以不在脚本里；需要时让用户在网页上手动查，结果只当"是否出自 ElevenLabs"的旁证。

## 检查 2：声线是否重复

声纹在本地计算（SpeechBrain ECAPA-TDNN），音频不出本机。声纹库默认在当前目录的 `voiceprints/`。

```bash
# 新人提交 → 先查重，再入库
$SKILL/.venv/bin/python $SKILL/scripts/speaker_match.py check  <音频> [--top 5]
$SKILL/.venv/bin/python $SKILL/scripts/speaker_match.py enroll <person_id> <音频...>

# 全库两两比对，找出疑似同一人的多个账号；同时打印分数分布用于校准阈值
$SKILL/.venv/bin/python $SKILL/scripts/speaker_match.py scan
```

- `check` 的顺序很重要：先 `check` 再 `enroll`，否则会匹配到自己。
- 默认阈值：余弦相似度 ≥ 0.50 → `MATCH`，0.30–0.50 → `REVIEW`，更低 → 不同人。这是通用起点，不是针对用户数据校准过的值。库里有 30 人以上后跑一次 `scan`，按它打印的"不同人分数分布"的高分位调 `--match` / `--review`。
- 处理 refer 时，优先看被推荐人和推荐人、以及同一推荐人名下各人之间的分数。
- 误报来源：亲属、同一支麦克风/同一房间、音频过短（<5 秒）。`MATCH` 之后让人听一遍两段录音再下结论。
- `person_id` 用内部编号，不要用真实姓名。声纹属于生物识别信息，`voiceprints/` 不要提交到 git、不要外传。

## 两个检查互相补位

- 同一个人换号重录 → 检查 2 能抓，检查 1 抓不到（是真人）。
- 用 AI 给每个假账号生成不同声音 → 检查 2 抓不到（声线确实不同），靠检查 1。
- 用变声/voice conversion 实时处理真人声音 → 两者都可能漏。这种情况靠流程兜底，见下。

## 流程兜底（比任何检测器都可靠）

发现可疑或金额/名额较大时，建议用户加一步"现场随机口令"：录音前才给出一句随机句子加一串随机数字，限时提交，或直接在通话里念。预先生成的 AI 音频过不了这一关；内容对不上直接复核。

## 汇报格式

逐个文件给一行：文件、AI 检测结论（列出各家结果）、声线查重结论（最高分及对应的 person_id）、建议动作。有 `FLAG` / `MATCH` 的排在最前面。工具报错或某家没返回时照实写，不要当作通过。
