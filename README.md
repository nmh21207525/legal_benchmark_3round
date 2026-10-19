# legal_agent_3round

005、006 两道题的三轮庭审工作区。**在自己的服务器跑 005，在自己的服务器跑 006；每题四个律师模型，逐场串行运行，庭审完成后自动独立评分。** 两台机器使用同一份代码、固定配置与预先冻结的 rubric。

保留两轮版本的庭前阅读、检索、咨询与调查反馈、文件产出、角色权限、累计重试及中文交付目录。新增第三轮，旧 003／004 工程和结果不需要改动。

## 1. 安装一次

把本文件夹放到 `~/legal_agent_3round`，或从共享 GitHub 私有仓库克隆到这个位置。两道题的数据和 rubric 已包含，不需要单独 scp 数据。

```bash
cd ~/legal_agent_3round
bash setup.sh
```

要求 Linux、Python 3.10+、可用模型服务。安装脚本在本文件夹创建 Python 环境，无需 sudo。若提示缺 CLI，按提示执行：

```bash
npm install --prefix .tools @openai/codex@0.160.0 @anthropic-ai/claude-code@2.1.300
bash setup.sh --check-only
```

这两个版本来自原实验的 CLI 记录；需要服务器的 npm 源提供对应版本。已有可用 CLI 可直接复用。新机器如没有 npm，请先使用自己的 Node/npm 环境；不需要把任何人的账号登录文件传给同事。

默认使用本包 `config.example.json`，不会读取旧实验配置。若已有 Codex 仅在特定绝对路径，复制示例为 `config.local.json`，只修改 `codex.executable`。API key 不写进配置，启动时隐藏输入。两人使用同一 Git 提交号；行动预算等实验设置固定，修改会明确报错。

## 2. 各自启动自己的题

**005，四个模型，自动评分。**

```bash
cd ~/legal_agent_3round
bash run_005.sh --validate-only
bash run_005.sh --background
```

** 006，四个模型，自动评分。**

```bash
cd ~/legal_agent_3round
bash run_006.sh --validate-only
bash run_006.sh --background
```

按提示输入 seed、deepseek、opus、GPT／环境／评分器的密钥。看到“后台已启动”即可关闭 SSH 或本地电脑，服务器保持开机即可。不需要 tmux 或 sudo。`--validate-only` 不收费，只检查本地包；正式启动还会预检模型接口，预检失败时不会直接开庭。

每个脚本固定一个题号，另一题不会顺带运行。只想先跑一个律师模型时，可加 `--candidate gpt`；该运行会写到独立的 `formal-gpt-005` 或 `formal-gpt-006` 目录，不算四模型完整对比。

## 3. 进度、日志、结果

| 操作 | 005 | 006 |
|---|---|---|
| 看状态 | `bash status.sh --case-id 005` | `bash status.sh --case-id 006` |
| 正常停止 | `bash status.sh --case-id 005 --stop` | `bash status.sh --case-id 006 --stop` |
| 输出目录 | `runs/formal-all-005/` | `runs/formal-all-006/` |

例如 006 查看实时日志和成绩：

```bash
tail -n 40 -f ~/legal_agent_3round/runs/formal-all-006/launcher.log
cat ~/legal_agent_3round/runs/formal-all-006/results.csv
```

`Ctrl+C` 只退出 `tail`。停止实验请用上表命令，它会等当前调用落盘再停。恢复时原样重跑启动命令，已完成庭审与评分复用，累计尝试次数保留。不要删除账本或在运行中更新代码。

每场的顺序是：庭前准备 → 第一轮 → 第二轮 → 第三轮 → 独立评分 → 更新 CSV 和 HTML → 下一模型。四行均为 `complete`，且 `delivery_status.json` 的 `complete_courts_and_scores` 为 4、`teacher_packages_created` 为 true，表示本题完成。错误或未完成的场次分数留空，不当作 0 分。

## 4. 评分标准

正式使用 `rubrics/CA-PILOT-005.json`、`rubrics/CA-PILOT-006.json`，可读版本在 [005 rubric](docs/rubric-005.md)、[006 rubric](docs/rubric-006.md)。两题覆盖庭前准备、调查、四类实际文件、三轮法律论证与证据处理；每项都有固定的 0／1／2 分条件。

| 题目 | 项目数 | 满分 |
|---|---:|---:|
| 005 | 46 | 92 |
| 006 | 48 | 96 |

- 独立评分器为 `gpt-6-sol`，评价律师实际表现；不因最终胜诉、文风或工具次数直接给分。
- 满足全部必要条件才得 2 分，部分达到得 1 分，未展示或错误得 0 分。允许条目列明的合法替代路线，不因清晰组合提问本身扣分。
- 分母为 `2 × 项目数`，覆盖率为得分／分母。评分器一次读取整场过程、实际产物和全部条目，逐项给分与理由；程序计算总分。
- 引用疑点保留为复核提示，不擅自修改原分数。非 0／1／2、漏项、重复项等结构错误才触发重试。
- rubric 在本次运行前固定，不向庭审角色提供，也不读以前的分数或设定目标覆盖率。AI 辅助编写并做条目核查，尚未经过独立人工法律专家复核；冻结记录如实注明这一点，不冒称已做真实开发试跑。

不要在运行中修改 rubric。需要改题或标准时，应发布新版本并使用新的输出目录，不把不同版本混为同一批结果。原始案包里的空 `evaluation/rubric.json` 保留作来源记录；实际评分读取的是上面 `rubrics/` 中的冻结版。

## 5. 固定实验设置

| 设置 | 值 |
|---|---|
| 律师候选 | `doubao-seed-evolving`、`deepseek-flash`、`claude-opus-5-5`、`gpt-5.6-sol` |
| 环境角色／评分器 | `gpt-5.6-sol`／`gpt-6-sol` |
| 庭前行动／每阶段每方行动 | 20／3，模型可提前结束 |
| 总庭审律师行动／单案模型调用 | 150／360，模型调用含重试 |
| 每逻辑决策累计尝试／超时 | 最多 8 次／300 秒，重启不清零 |
| 配置输出上限 | 16384；HTTP 路由可强制，CLI 不宣称强制上限 |
| 执行方式 | 单机串行；两人可以在各自服务器同时跑不同题 |

工具包含文件读取、冻结材料和法律资料检索、来源约束的调查反馈、写文件；不是联网 web search。只有授权资料才能进入各角色视图，调查请求不会自动变成新事实。006 的延迟披露与两题各自的证人权限由环境控制。评分严格度不会靠隐藏降低行动预算实现。

## 6. 交付文件

每题输出目录包含 `results.csv`、`launcher.log`、`index.html`、`report.html`、`delivery_status.json`、`courts/` 和 `交付材料/`。页面从运行初期建立，在场次保存后更新。

中文交付结构保持：`01_题目与数据`、`02_rubric`、`03_实验结果`、`04_运行环境`、`05_来源与校验`。HTML 展示实际庭前问答、工具请求和反馈、文件链接、三轮对话及每项评分。四类律师文件保持 `case_timeline.csv`、`evidence_register.csv`、`pretrial_plan.md`、`examination_outline.md`。

本题全部完成后自动生成 `第005题-完整交付.zip` 或 `第006题-完整交付.zip`；同事把 005 的 ZIP 和 CSV 发给你即可。保存记录可离线原样查看；重新调用模型不保证逐字产生相同内容。

GitHub 上传与克隆步骤见 [docs/GITHUB.md](docs/GITHUB.md)。仓库同步代码和输入，不自动同步两台机器的运行结果。

## 7. 无付费检查与仅评分恢复

```bash
bash check.sh
bash start.sh --mode development --mock --candidate gpt --case-id CA-PILOT-006
```

`--mock` 是离线流程测试，不是真实模型表现，也不生成正式成绩。真实网络、接口权限和额度由启动预检确认。正式庭审已完整、仅评分失败时，修复报告中的问题后可用 `bash run_006.sh --score-only --background`；005 对应替换脚本名。该选项不会重新开庭。
