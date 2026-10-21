# API 接入说明（2026-10-19 主控已实测）

本文件不含任何 key。key 在 `/Users/macbook/Desktop/MaxLoop/需求/legal_benchmark/法律benchmark改造/.secrets.env`（权限 600）。
Bash 的环境变量不会在多次调用之间保留，每次需要调用 API 的命令都先加载：

```bash
set -a; . /Users/macbook/Desktop/MaxLoop/需求/legal_benchmark/法律benchmark改造/.secrets.env; set +a
```

代码一律从环境变量读取 `PACKY_API_KEY`、`PACKY_BASE_URL`、`ARK_API_KEY`、`ARK_BASE_URL`。
不要把 key 写进代码、配置、日志、运行记录或报告；不要 `echo`/`print` 它们。

## 1. GPT（Packy 网关，OpenAI Responses API 兼容）

- 端点：`POST $PACKY_BASE_URL/responses`（即 `https://cf.api.fan/v1/responses`）
- 头：`Authorization: Bearer $PACKY_API_KEY`，`Content-Type: application/json`
- 这把 key 可用的模型（`GET $PACKY_BASE_URL/models` 实测）：`gpt-5.6-sol`、`gpt-6-astra`、`gpt-6-sol`、`gpt-6.1-sol`、`codex-auto-review`
- 已实测可用：`gpt-5.6-sol`、`gpt-6.1-sol`（`gpt-6-sol` 未单独实测，用前先发一个最小请求）
- 请求体示例：

```json
{
  "model": "gpt-5.6-sol",
  "instructions": "<角色系统提示>",
  "input": "<本轮输入，字符串或消息数组>",
  "reasoning": {"effort": "medium"},
  "max_output_tokens": 16384,
  "text": {"format": {"type": "json_schema", "name": "action", "schema": {...}, "strict": true}}
}
```

- 结构化输出：`text.format` 的 `json_schema` + `strict: true` 实测可用（gpt-5.6-sol）。
- 回复：`output` 数组里 `type == "message"` 的项，`content[].text` 是回答。
- 用量：`usage.input_tokens`、`usage.input_tokens_details.cached_tokens`、`usage.output_tokens`、`usage.output_tokens_details.reasoning_tokens`、`usage.total_tokens`；另有 `usage.attribution` 明细。
- **重要的坑**：请求里**不传 `instructions` 时，网关会自动注入约 4,300 tokens 的隐藏系统提示**（`usage.attribution.request_fields.instructions` 显示 4380 tokens）；传了自己的 `instructions` 后，这一项就只剩自己的那几十个 token。所以**每个请求都必须显式传 `instructions`**，否则 GPT 一侧会多一段看不见的提示，污染实验。

## 2. Seed（火山方舟 Responses API）

- 文档：https://docs.volcengine.com/docs/ark/create-model-responses-api?lang=zh （页面需 JS 渲染，curl 抓不到正文）
- 端点：`POST $ARK_BASE_URL/responses`（即 `https://ark.cn-beijing.volces.com/api/v3/responses`）
- 头：`Authorization: Bearer $ARK_API_KEY`，`Content-Type: application/json`
- 模型：`doubao-seed-evolving`；回复里报告的模型名是 `doubao-seed-evolving-latest-version`（核验身份时两者都接受）
- `reasoning.effort`：`minimal`（关闭思考）、`low`、`medium`、`high`；`xhigh`/`max` 会按 `high` 处理；不传时默认 `high`
- `max_output_tokens` 同时限制思考和回答，设小了会把回答截断；建议 ≥ 32768
- 结构化输出：`text.format` 的 `json_schema` + `strict: true` 实测可用
- 回复：`output` 里先有 `type == "reasoning"` 的项，再有 `type == "message"` 的项；取 message 的 `content[].text`
- 用量字段与上面相同：`input_tokens`、`output_tokens`、`total_tokens`、`input_tokens_details.cached_tokens`、`output_tokens_details.reasoning_tokens`
- 原交付代码（`vendor/v2/backends.py`）用的是方舟的 chat/completions 端点；这次按用户给的文档改用 `/responses`。
- 方舟 Responses API 默认会在服务端存储请求；如果 `store: false` 被接受就加上（先试）。

## 3. 通用要求

- 每个 agent 同时最多 3 个并发请求；遇到 429、5xx、超时用指数退避重试（最多 4 次），并在 usage_ledger 里记录每次尝试。
- 不要因为格式错误无限重试：结构化输出解析失败最多重试 1 次，第二次失败就记为失败并按规则处理（对候选模型：返回错误信息让它重做一次，不扣行动次数）。
- 每次调用记录：时间、角色、模型、effort、输入/输出/缓存/思考 tokens、耗时、请求哈希、是否成功。
- 提示词结构："稳定前缀（角色设定 + 案件材料）+ 追加的历史"，利于缓存命中。

## 4. 本次实验的模型分配（两道题统一）

| 用途 | 模型 | effort |
|---|---|---|
| 候选辩护律师 A | gpt-5.6-sol（Packy） | medium |
| 候选辩护律师 B | doubao-seed-evolving（方舟） | medium |
| 环境角色（检察官、法官、证人、被告、陪审员、开示/调查裁量、专家） | gpt-6.1-sol（Packy） | medium；检察官、法官可用 high |
| 主评分 | gpt-6-sol（Packy） | high |
| 副评分 | doubao-seed-evolving（方舟） | high |

候选模型与原交付相同（原交付 GPT 用 gpt-5.6-sol、豆包用 doubao-seed-evolving，effort 都是 medium），这样新旧分数的变化才能归因到题目本身。不要使用其他模型或其他 key。
