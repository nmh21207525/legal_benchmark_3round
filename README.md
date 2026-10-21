# 005、006 三轮律师工作实验工程

本工程按教师《法律benchmark改造》规格及复核结论重新实现005、006，保留三轮设计，候选改为GPT、Seed每题各运行两次。003、004不在本次重交范围。人物为化名；原始判决锚点与后续构建的模拟事实分别标注，新增卷宗不冒充真实案件原件。

初始源码交付附带离线参考／弱路径演练，不能据此声称真实实验已完成。**本目录的当前实验状态以各题 [RESULTS.md](005_Williams/RESULTS.md)、[RESULTS.md](006_Wilson/RESULTS.md)、`进度.md`及`status`命令为准；正式结果资格由严格导出重新核验。** 运行后这些结果页和单题README状态区会自动更新。

## 运行前准备

需要Linux或macOS、Python 3.11+、两个接口的有效密钥和访问权限。运行及测试仅用Python标准库，无须安装第三方包。将完整工程解压到例如`/home/nmh/legal_agent_3round/`；已有旧目录时先完整改名备份，不覆盖原运行。

```bash
cd /home/nmh/legal_agent_3round
bash setup.sh
bash run_005.sh offline
bash run_006.sh offline
```

`setup.sh`创建`.venv`并执行离线检查；`offline`不调用API，可在原目录重复执行，使用已完成记录。测试及解压验证报告见 [docs/verification_report.json](docs/verification_report.json) 和 [docs/extraction_verification.json](docs/extraction_verification.json)。这些检查不替代真实预检与校准。

## 开始真实实验

同事负责005、你负责006时，各自在自己的完整工程目录启动对应题目；单机也可同时运行两题，每题内部串行。

```bash
bash run_005.sh
# 另一终端或另一台机器：
bash run_006.sh
```

交互终端会自动隐藏输入提示，依次读取`PACKY_API_KEY`和`ARK_API_KEY`；已设置的环境变量会复用。密钥不写入工程或命令历史。默认接口为`https://cf.api.fan/v1`及`https://ark.cn-beijing.volces.com/api/v3`，可分别通过`PACKY_BASE_URL`、`ARK_BASE_URL`覆盖。非交互运行须预先通过安全方式设置密钥环境变量。

默认后台启动，可关闭SSH窗口。完整流程为：API预检 → 真实参考路径与双评分 → 真实弱路径与双评分 → 校准门禁与冻结 → GPT第1、2次 → Seed第1、2次 → 每场双评分 → 汇总及回放 → 严格验收 → 自动生成本题最终ZIP。也可用`bash run_006.sh pipeline`前台运行。

校准须两评分器均满足参考≥85%、弱≤40%，且至少一罪结局更优、没有反向改善。未通过会停止并保留证据，不能自动改分。正式候选开始前冻结世界、法源、评分、配置与代码；不得在正式实验中运行重建脚本或修改冻结文件。外部服务兼容性、真实校准能否通过及候选表现，均须由实际运行确定。

```bash
bash run_006.sh status
# 可选：只检查接口，不开始正式实验
bash run_006.sh preflight
# 观察后台日志，Ctrl+C只退出tail
tail -f 006_Wilson/pipeline.log
```

安全暂停、恢复和传输故障处理：

```bash
bash run_006.sh stop
bash run_006.sh status
bash run_006.sh resume
```

`stop`请求在当前API调用结束并保存后，于安全边界暂停；`resume`清除停止请求并后台继续。已完成且验证通过的运行和双评分会复用，不重新计费评分。不要同时启动同题多个流水线。若四次传输尝试耗尽，修好网络或权限后先记录恢复原因，再继续：

```bash
bash run_006.sh retry-batch --run gpt_r1 --reason '网络已恢复，保留原尝试记录后继续'
bash run_006.sh
# 若此前使用stop，最后一行改用 bash run_006.sh resume
```

格式或非法动作不消耗法律动作额度；累计协议错误达到阈值会暂停。接口、预算或记录完整性故障不会伪装成法律能力零分。`进度.md`、日志与恢复记录保存原因。

## 模型、环境与次数

| 用途 | 模型 | effort |
|---|---|---|
| GPT候选 | gpt-5.6-sol | medium |
| Seed候选 | doubao-seed-evolving | medium |
| 环境角色 | gpt-6.1-sol | medium；法官、检察官high |
| 主评分器 | gpt-6-sol | high |
| 副评分器 | doubao-seed-evolving | high |

每题4场正式候选，两题合计8场；每题另有参考／弱路径各1场真实校准，不计入正式重复。三轮是程序设计；若逐罪无罪动议依法终结全案，记录真实终止阶段，不强行生成后续庭审或陪审投票。部分无罪仅对余罪继续评议。

接口使用Responses API、显式instructions、strict JSON schema及真实User-Agent；Seed输出上限32768、store=false。每次运行额度30M token、每题全部调用200M、单次上下文150K；预检、校准、候选、环境和评分均记账。未知用量单列并保守预留，不能按零用量冒充完整计费。

模型、effort、协议、预算、重复次数及目录按教师要求固化。教师没有提供OS镜像或完整依赖锁，本工程不声称操作系统及Python补丁版本与教师电脑逐位相同；实际环境见 [ENVIRONMENT.json](ENVIRONMENT.json)。

## 最终能交付什么

| 材料 | 路径及内容 |
|---|---|
| 可复现工程与案件世界 | 根共享代码、配置、入口、两题`world`、`legal_corpus`、`rubric`及题内引擎镜像 |
| 运行与冻结证据 | `runs/`原始事件、状态、真实模型调用记录、用量、预注册与真实校准冻结记录 |
| 候选实际工作产物 | `runs/<名称>/artifacts/`中的文件及全部版本；缺失如实记录，不事后代写 |
| 每场完整评分 | `scores/<名称>/`机械评分、两个独立评分器及依据、原始／适用分母、严重错误 |
| 总表及对照 | `RESULTS.md`、`scores/summary.md`、`summary.csv`、`replicates.csv`、`disagreements.csv`、`severe_errors.csv`、`legacy_comparison.csv` |
| 可读回放 | 每场`replay.html`包含全部事件、实际工作文件正文与版本，可按角色、阶段和文本检索 |
| 交付ZIP | `deliverables/`下本题或两题合并包，含`DELIVERY.md`和逐文件SHA256清单`DELIVERY_MANIFEST.json` |

每题60项、原始满分120：42机械项、17双评分质量项、1结局项，结局占1.67%。汇总分别列两个评分器得分、双评分均值、每候选两次均值与范围、质量项一致率和分歧。N/A只按预注册机会规则产生；依法全案提前终结后未到达的阶段可以不适用，自行漏做或API失败不能当N/A。严重错误另列并按事件／类别去重。

每题流水线完成后自动严格导出；也可手动重新生成报告、验收和导出：

```bash
bash run_005.sh report
bash run_006.sh report
bash run_005.sh validate
bash run_006.sh validate
bash run_005.sh export
bash run_006.sh export
# 两题结果都放在当前同一个完整工程中后：
bash run_006.sh export-all
# 等价快捷入口：
bash export_all.sh
```

默认输出到`deliverables/`，可加`--out /绝对路径/交付目录`。两人分机运行时，将两题各自完整结果目录汇集到同一版本工程再合并导出，勿覆盖另一题。严格导出要求真实校准冻结、4场live候选全部完成双评分、轨迹与版本哈希、配置及评分一致、预算合规和无未结调用；不会把离线或缺失结果标为最终完成。单题ZIP保留全部共享运行依赖，另题仅作为离线测试静态模板，清单明确区分。

仅需传递未完成工程／排查记录时可使用`bash run_006.sh export --allow-partial`，其标记为`partial_not_final`，不能作为已完成实验。严格通过的包标记为`verified_final_records`；其含义是记录与评分验收通过，不替代人工法律复核。回放含私下会见和工作记录，仅用于管理与评分，不提供给候选或陪审团。

## 导入旧成绩作对照

本包不会推测原交付成绩，也不会借用003／004数字。拿到005或006真实旧成绩后，在对应题目根目录建立`legacy_results.json`，顶层为`{"results": []}`；每条旧运行记录填写以下字段，再运行本题`report`：

| 字段 | 要求 |
|---|---|
| `candidate` | `gpt`或`seed` |
| `percent` | 该次旧运行的真实百分制数字，0至100；不是字符串 |
| `source` | 可追溯的旧结果文件路径或记录标识 |

可为同一候选填多条真实记录，汇总会计算旧均值与范围。未提供时明确标为`not_provided`。旧005为46项／92分、旧006为48项／96分，本版每题60项／120分；世界与评分尺已改变，百分比对照不能直接解释为同尺度能力提升。

## 数据来源与法律边界

005保留19条原始锚点并补写170条，共189条；006保留16条并补写166条，共182条。两题各32份文书、初始9份，补写细节预先固定。教师原包及原始数据的逐文件遍历和来源哈希可在`teacher_reference/`与`docs/`追溯；原始题目材料在各题`source_reference/original/`，不向候选开放。

法源检索条目005共43条、006共47条。已补核旧§290、§23153核心、当时陪审指示、§23612及第三方传票依据，修正Evidence1101别名日期与截止标志。005历史指示采用原判保存的2006引文；006使用2000年前判决中的CALJIC依据，避免以之后CALCRIM冒充当时指示。

完整核验范围以 [docs/law_verification.md](docs/law_verification.md) 为准：这不是完整历史法典／指示册复原，新增判例保存的是注明性质的短摘录与研究摘要，部分日期为“至迟已可用”日期。中文指示是实验改写；补写事实与全部法律争议尚无执业律师全面人工审核。模拟庭期为设计日期。真实模型仍可能漂移或犯错，接口仍可能失败；工程负责保留证据、检查并暂停，不保证所有外部调用必然成功。

教师标准对应、改动及验收范围另见 [docs/教师标准对照.md](docs/教师标准对照.md)、[docs/这次修改了什么.md](docs/这次修改了什么.md)、[docs/发行验收.md](docs/发行验收.md)。
