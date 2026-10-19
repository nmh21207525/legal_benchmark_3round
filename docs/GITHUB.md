# 上传 GitHub 与同事协作

代码仓库名称：`legal_agent_3round`。建议建 **Private** 空仓库，并在仓库 Settings → Collaborators 中邀请同事。上传整个工作区的代码和固定数据，不是只上传一个 ZIP。

## 首次上传

在 GitHub 新建仓库时，不勾选生成 README、license 或 gitignore，因为本包已经有 README 和 gitignore。

在自己服务器中执行，仓库地址替换为 GitHub 页面显示的 HTTPS 地址：

```bash
cd ~/legal_agent_3round
git init
git add .
git status --short
git commit -m "Add three-round court experiments and frozen rubrics"
git branch -M main
git remote add origin https://github.com/你的用户名/legal_agent_3round.git
git push -u origin main
```

首次 commit 如果提示缺少身份，只在这个仓库设置：

```bash
git config user.name "你的GitHub用户名"
git config user.email "你的GitHub邮箱"
```

然后重新执行 commit 和后续命令。HTTPS 推送使用你已配置的 GitHub 凭据；若终端要求 Password，应使用 GitHub 访问令牌，而非账号登录密码。不要把令牌写进仓库地址或 README。

已排除 `runs/`、本地 Python/CLI 安装目录、`config.local.json`、密钥文件和运行日志。`git status --short` 中应没有这些文件。不要把模型 API key 写入任何要提交的文件。

## 同事下载

同事接受邀请后，在自己服务器执行：

```bash
cd ~
git clone https://github.com/你的用户名/legal_agent_3round.git legal_agent_3round
cd legal_agent_3round
bash setup.sh
```

环境检查通过后，按根目录 README 运行 `bash run_005.sh --background`。你运行 `bash run_006.sh --background`。

## 固定同一个版本

开始实验前，两人执行 `git rev-parse HEAD` 并记录相同提交号。运行期间不 pull、不改代码/rubric。GitHub 同步代码和输入；两台服务器各自的运行结果不会自动上传到 GitHub。

同事完成后只需把 `runs/formal-all-005/第005题-完整交付.zip` 和 `results.csv` 交给你；006 的文件对应在你自己的服务器。保留各自原始运行目录。

官方操作参考：[GitHub：将本地代码添加到仓库](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)。
