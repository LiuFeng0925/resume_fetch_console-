# 邮箱简历附件自动抓取

从多个企业邮箱 INBOX 拉取应聘邮件，按标题正则匹配后，将 PDF 等附件保存到各账号独立目录。

## 功能

- 多邮箱：`config.yaml` 的 `accounts` 列表，一次 `run` 顺序处理
- 手动 / 定时：同一命令 `python main.py run`，cron 每小时调用
- 标题正则 + 扩展名白名单
- 附件**不做文件去重**：每份简历独立保存（`{日期}_{时间}_{序号}_{原文件名}`）
- 邮件级去重：同一账号同一封邮件不会重复处理（SQLite）
- 首次运行不回溯历史邮件

## 快速开始

```bash
cd "/Users/liufeng/Documents/项目/重构 2.0/邮箱抓取简历"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.yaml.example config.yaml
# 编辑 config.yaml：邮箱、授权码、download.path 全部在文件内完成

python main.py run
python main.py run --account hr-boss      # 仅跑一个账号
```

## 定时任务（cron）

配置写好后，cron 无需再 export 密码：

```bash
0 * * * * cd /path/to/邮箱抓取简历 && /path/to/.venv/bin/python main.py run >> logs/cron.log 2>&1
```

## 配置说明

见 `config.yaml.example`。每个账号需配置：

- `imap.host` / `imap.username` — 邮箱服务器与账号
- `imap.password` — **IMAP 授权码**（推荐，直接写在配置文件）
- `imap.password_env` — 可选，从环境变量读密码（与 `password` 二选一）
- `download.path` — 该账号简历保存目录

`config.yaml` 已在 `.gitignore`，不会误提交到 git。

## 测试

```bash
pytest tests/ -v
```

## 目录结构

```
main.py              CLI 入口
src/                 业务模块
tests/               单元 / 集成测试
data/processed.db    运行后生成（邮件去重）
logs/                运行日志
```
