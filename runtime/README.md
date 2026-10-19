# 两题四律师：完整法律工作流 v3

这是新的独立实验。原来的 `three-lawyers-02` 不会被加载、改分或覆盖。
两道题均包含庭前工作区和原有两轮庭审；Seed、DeepSeek、Opus、GPT 各跑两道题，共八场。

## 在现有云服务器启动

把压缩包上传到 `/home/nmh/`，解压后运行：

```bash
unzip -n /home/nmh/legal_full_workflow_v3.zip -d /home/nmh
bash /home/nmh/legal_full_workflow_v3/start.sh
```

默认使用已有 `/home/nmh/legal_agent/.venv/bin/python`、原 `config.codex.json` 和已安装的 Codex、Claude CLI。
不需要 sudo、tmux 或修改旧恢复入口。终端先进行不付费的材料检查，再隐藏输入所需密钥并检查六条模型路由；通过后后台串行运行。
密钥只存在于进程环境中，不写入配置、脚本、日志或命令参数。看到“已后台启动”及 PID 后，可关闭本地电脑；服务器仍需保持开机。

如果已有环境缺少 Python 依赖，安装到当前虚拟环境后重跑同一启动命令：

```bash
/home/nmh/legal_agent/.venv/bin/python -m pip install -r /home/nmh/legal_full_workflow_v3/requirements.txt
```

只检查文件、不调用模型：

```bash
bash /home/nmh/legal_full_workflow_v3/start.sh --validate-only
```

前台运行可加 `--foreground`。高级参数见 `python -B runner.py --help`。
模型账户权限、余额和接口连接需要运行时验证；离线测试不等于八场真实模型实验已经完成。

## 模型与比较口径

| 用途 | 配置 |
| --- | --- |
| 被评律师 Seed | `doubao-seed-evolving` |
| 被评律师 DeepSeek | `deepseek-flash` |
| 被评律师 Opus | `claude-opus-5-5` |
| 被评律师 GPT | `gpt-5.6-sol` |
| 检察官、法官、证人等模型角色 | `gpt-5.6-sol` |
| 独立评分器 | `gpt-6-sol` |

GPT 律师和环境角色使用独立后端实例；只有 `R_DEFENSE` 使用候选路由。四个候选采用相同题目、可见材料、工作区和行动预算。
各供应商的推理设置及 CLI 的输出长度控制能力并不完全相同，实际限制会写入实验清单，不能把“配置了 token 数”当作都已强制执行。

正式评分表位于 `data/workflows/CA-PILOT-003/rubric.json` 和 `data/workflows/CA-PILOT-004/rubric.json`。每题 28 项、满分 56 分：庭前 5 项、调查反馈 3 项、文件产物 4 项、庭审 16 项。原案包 `evaluation/rubric.json` 保留原始状态，不用于 v3 评分。

新版评分表在开始模型运行前绑定哈希；评分器逐项给 0/1/2 分，分母固定，缺失或无效评分不会补成 0 分。
新增流程改变了实验任务，因此这些分数不能与旧四场的覆盖率直接混算。
新 rubric 基于本次材料和公开任务要求编制，不依据旧模型分数倒推；来源及制作说明随题保存。法律适用的专家审核状态如实保留。

## 新增的实际工作

1. 律师通过文件工具枚举、分段阅读和检索授权材料。
2. 律师将自己的时间线、证据表、准备计划和询问提纲写成实际 UTF-8 文件；保存每个版本、内容哈希及对应动作。
3. 律师可向已配置的调查或核验服务提交请求；环境执行规则，返回报告、来源、可披露内容或材料不可得的明确反馈。
4. 律师根据反馈更新准备文件，再进入原有两轮庭审；发现或取得材料不等于已经被法庭采纳。
5. 独立评分器读取完整可观察事件及经哈希核验的文件内容，对冻结 rubric 评分；交付端导出按角色、阶段浏览的回放及每题环境。

原有 `web_search` 仍是授权本地材料检索，不是互联网搜索。四个模型访问同一冻结语料，避免实时网页变化导致不公平比较。

## 这些文件是什么

本次上传来源包含原判决和法律快照。公开案卷是保留转述与不确定性的中文摘要，**不是原始警察笔录、录像逐字稿或毒理报告**。
原判决全文、角色底稿、旧制作参考和评分表仅供制作/审核/评分端；律师只能看到明确授权的文件和反馈。

原案未提供的物证、医学结论、邻居证言、视频原话不会由环境编造。调查可以返回已记载的材料或“在所供记录中不可取得”，并让律师说明这对策略的影响。
这两道来源约束题不模拟凭空发现新实物。未来若编写有“找到手套”等分支的新题，必须先提供证据依据或明确标注经过授权的合成设定。

## 结果、恢复与交付

默认运行目录：

```text
/home/nmh/legal_agent_codex_030/legal_court_agent/runs/four-lawyers-full-v3-pf2
```

`launcher.log` 是后台启动后的实时输出；`results.csv` 是八场汇总。各场目录包含动作记录、请求/响应账本、文件产物、庭审结果和评分记录。
具体位置如下（相对于运行目录）：

| 内容 | 路径 |
| --- | --- |
| 八场汇总 | `results.csv`、`index.html` |
| 后台实时日志 | `launcher.log` |
| 每题交付总入口 | `deliveries/CA-PILOT-003/index.html`、`deliveries/CA-PILOT-004/index.html` |
| 每题可重跑环境 | `deliveries/<case_id>/environment/` |
| 原始对话与动作账本 | `courts/<模型>/<case_id>/private_model_io.jsonl`、`action_attempts.jsonl` |
| 实际文件产物 | `courts/<模型>/<case_id>/court_attempts/attempt-*/artifacts/` |
| 独立评分 | `courts/<模型>/<case_id>/full_workflow_score/score.json`、`report.html` |

交付目录在整轮调度结束或到达指定场次停止点后生成；运行中先看日志及各场目录。

查看后台输出：

```bash
tail -n 60 /home/nmh/legal_agent_codex_030/legal_court_agent/runs/four-lawyers-full-v3-pf2/launcher.log
```

断线会按固定的累计重试策略处理；已接受的请求只重放，已完成的庭审与评分复用。格式、模型身份、权限或数据错误不能当作网络错误无限重试。
发生未完成状态时，保留原输出目录并阅读错误；不要删除账本、修改冻结输入或启动旧的恢复补丁来绕过限制。
服务器进程结束后再次运行相同命令可恢复可重试的工作；若变更题目、模型、rubric 或代码，需要新输出目录和新实验版本。

每题交付包含案件输入、角色与权限、工作流规则、原始来源、冻结评分表、运行代码及依赖说明，以及本次实际对话/工具事件/产物/回放。
作者视图含角色私有内容，适合交付老师审核，不应重新作为律师的输入。回放不记录模型隐藏思维链，只记录实际请求、最终回答和可观察行动。

## 复验

```bash
cd /home/nmh/legal_full_workflow_v3
/home/nmh/legal_agent/.venv/bin/python -B -m unittest discover -s tests -v
```

离线测试与演练仅验证工程流程，不提供法律正确性签字或真实模型能力成绩。

## 零调用全流程演练

下面的命令使用固定的本地测试响应，不需要密钥；八场只验证文件和调度流程，不产生模型分数。

```bash
cd /home/nmh/legal_full_workflow_v3
/home/nmh/legal_agent/.venv/bin/python -B runner.py --project-root "$PWD" --config config.example.json --out ./offline-demo --mock
```

新题可参考 `examples/rubric.template.json` 的结构，但不能仅替换案名直接用于评分：需要先编制题目、授权输入文件、角色私有信息、工具可返回的事实、文件产物要求和逐项来源映射，再做权限检查与离线联调后冻结。

## PF2 接口预检修正

预检使用真实动作 JSON schema，但只要求一到两句中性文本（16–500字符），不再要求无关的1200字长文。正式庭审、28项rubric、输出解析及累计重试策略不变。默认新目录为 four-lawyers-full-v3-pf2，旧目录的失败预检原样保留。预检通过不保证正式长回复始终合法。
