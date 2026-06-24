# 邮箱简历附件自动抓取

## Problem Statement

用户的企业邮箱 INBOX 中会持续收到来自招聘平台（如 BOSS 直聘）的应聘邮件。这些邮件通常带有 PDF 等格式的简历附件，但邮件量大、附件分散，人工逐封打开并保存到本地既耗时又容易遗漏。

用户需要一个自动化工具：按固定频率连接邮箱，识别符合规则的应聘邮件，将其中指定类型的附件下载到本地目录，且已处理过的邮件在后续运行中不再重复处理。团队内可能有多位招聘负责人、多个企业邮箱需要同时抓取。

## Solution

提供一个可通过系统定时任务（cron）每小时触发一次的 Python 程序。用户在 `config.yaml` 的 **`accounts` 列表**中配置一个或多个邮箱；每次运行（手动或 cron）**按顺序**逐个连接 IMAP、扫描 INBOX，当邮件标题匹配全局正则规则时，将该账号白名单内的附件以 `{日期}_{原附件名}` 保存到**该账号各自的 `download.path`**。程序使用 SQLite 以 **`(account_name, Message-ID)`** 联合去重，确保同一账号内不重复下载；不同账号即使 Message-ID 相同也互不影响。单个账号连接失败时记录错误并继续处理其余账号。

首次运行时只处理触发时刻之后收到的新邮件，不回溯历史邮件。

## User Stories

1. As a 招聘负责人, I want the system to connect to my enterprise mailbox via IMAP, so that I can automatically fetch resume attachments without manual login each time.

2. As a 招聘负责人, I want the system to scan only the INBOX folder, so that irrelevant folders (sent, trash, custom archives) are not processed.

3. As a 招聘负责人, I want to configure one or more regular expressions for email subject lines, so that only job-application emails (e.g. containing "应聘" and "【BOSS直聘】") trigger attachment downloads.

4. As a 招聘负责人, I want emails whose subjects do not match any configured pattern to be skipped, so that non-resume emails do not pollute my download folder.

5. As a 招聘负责人, I want to configure an attachment extension whitelist (e.g. `.pdf`), so that only desired file types are saved when a matching email is found.

6. As a 招聘负责人, I want all whitelisted attachments from a matching email to be downloaded, so that I receive every relevant file attached to that message.

7. As a 招聘负责人, I want downloaded files to be saved with the naming pattern `{YYYYMMDD}_{HHMMSS}_{original_filename}`, so that every attachment is stored without overwriting prior files (no file-level deduplication).

8. As a 招聘负责人, I want every whitelisted attachment from a matching email to be saved even when filenames repeat on the same day, so that no resume is dropped due to name collisions.

9. As a 招聘负责人, I want processed emails to be recorded locally by Message-ID, so that subsequent scheduled runs skip already-handled messages.

10. As a 招聘负责人, I want the system to run automatically every hour via cron, so that new resumes appear in my local folder without manual intervention.

11. As a 招聘负责人, I want the first run to process only emails received after the program is deployed, so that years of historical inbox mail are not bulk-downloaded on initial setup.

12. As a 招聘负责人, I want IMAP credentials stored via environment variables rather than plain-text config files, so that account passwords are not committed to disk in readable form.

13. As a 招聘负责人, I want to configure the local download directory path, so that files are saved to my preferred folder on macOS.

14. As a 招聘负责人, I want to configure IMAP host, port, SSL, and username, so that the tool works with my specific enterprise mail provider.

15. As a 招聘负责人, I want execution logs written to a log file, so that I can diagnose connection failures or download errors after a cron run.

16. As a 招聘负责人, I want the program to exit cleanly and log errors when IMAP connection fails, so that the next hourly run can retry without corrupting state.

17. As a 招聘负责人, I want a matching email with no whitelisted attachments to still be marked as processed, so that the same email is not re-evaluated on every run.

18. As a 招聘负责人, I want attachment filenames sanitized for macOS filesystem compatibility, so that special characters in original names do not cause save failures.

19. As a 招聘负责人, I want to manually trigger an immediate run via `python main.py run` at any time (independent of cron), so that I can fetch new resumes on demand without waiting for the next hourly schedule.

20. As a 招聘负责人, I want manual runs and scheduled runs to share the same deduplication state, so that manually fetching once does not cause cron to re-download the same emails.

21. As a 招聘负责人, I want manual runs to print a summary to the terminal when finished, so that I can see results immediately after triggering.

22. As a 招聘负责人, I want subject patterns and extension whitelist to be editable in a config file without code changes, so that I can adapt rules as recruitment channels change.

23. As a 招聘负责人, I want the SQLite state database to track processing timestamp and saved file paths, so that I can audit what was downloaded and when.

24. As a 招聘负责人, I want emails from platforms like BOSS 直聘—with subjects such as `刘烨 | 7年，应聘 AI产品经理 | 北京30-40K【BOSS直聘】` and attachments such as `【AI产品经理_北京_30-40K】刘烨_7年.pdf`—to be correctly identified and saved as `20250529_【AI产品经理_北京_30-40K】刘烨_7年.pdf`, so that the workflow matches real-world hiring mail.

25. As a 招聘负责人, I want to configure multiple email accounts in one `config.yaml`, so that several recruiters' mailboxes are fetched in a single scheduled or manual run.

26. As a 招聘负责人, I want each account to have its own IMAP credentials and download directory, so that resumes from different mailboxes are saved separately.

27. As a 招聘负责人, I want each account to use a distinct `password_env` environment variable, so that multiple mailbox passwords can be injected securely without sharing one secret.

28. As a 招聘负责人, I want deduplication scoped per account (`account_name` + Message-ID), so that processing one mailbox does not skip legitimate mail in another.

29. As a 招聘负责人, I want a failure in one account (e.g. wrong password) to not block other accounts in the same run, so that partial outages do not stop the whole team.

30. As a 招聘负责人, I want to optionally run only one account via `python main.py run --account hr-boss`, so that I can test or re-fetch a single mailbox without touching others.

## Implementation Decisions

### Architecture Overview

The system is a single-process, command-line batch job triggered by cron or manual CLI. Each run loads config, iterates **`accounts` in list order**, and for each account: connect IMAP → fetch new INBOX messages → filter by subject regex → download to that account's path → update SQLite → disconnect. After all accounts finish, print an aggregated summary and exit. No long-running daemon is required.

### Modules

The following deep modules encapsulate distinct responsibilities behind simple interfaces:

1. **Config Loader**
   - Loads YAML: `accounts[]` (each with `name`, `imap`, `mailbox`, `download.path`), global `subject_patterns`, `attachment_extensions`, `state`, `log`.
   - Resolves each account's password from its own `imap.password_env`.
   - Validates: `accounts` non-empty; each `name` unique; required IMAP/download fields present.
   - Fails fast at startup on invalid config or missing env vars (optional: warn and skip account if env missing—v1 **fail fast at start** for configured accounts).

2. **Processed Mail Store**
   - SQLite-backed persistence keyed by **`(account_name, message_id)`** composite primary key.
   - Interface: `is_processed(account, message_id)`, `mark_processed(account, message_id, metadata)`.
   - Per-account watermark stored separately (e.g. `watermarks` table: `account_name`, `since_uid` or `since_timestamp`).
   - Metadata includes subject, mail_date, processed_at, saved file paths.

3. **IMAP Mail Fetcher**
   - Instantiated per account with that account's IMAP settings.
   - Opens configured mailbox (default INBOX only).
   - On first run **per account**: records watermark; no historical backfill.
   - Returns structured mail objects: Message-ID, subject, date, attachment list.

4. **Subject Matcher**
   - Accepts a list of regex patterns from configuration.
   - Interface: `matches(subject) -> boolean` — returns true if any pattern matches.
   - Pure function module with no I/O; easy to unit test in isolation.

5. **Attachment Filter**
   - Accepts extension whitelist from configuration.
   - Interface: `is_allowed(filename) -> boolean` — case-insensitive extension check.
   - Pure function module; composes with Subject Matcher in the orchestration layer.

6. **Attachment Downloader**
   - Accepts mail date, original filename, and file bytes.
   - Produces local path using `{YYYYMMDD}_{HHMMSS}_{original_filename}` (time from mail_date; no overwrite, no file-level dedup).
   - Sanitizes filename characters unsafe on macOS.
   - Always writes a new file; never skips because a same-named file already exists.
   - Writes file to configured download directory (created if missing).

7. **Job Orchestrator (Entry Point)**
   - Exposes CLI subcommand `run` (default): executes one full fetch cycle across all accounts (or one if `--account` specified).
   - Outer loop: for each account in `accounts` → inner loop: fetch → match → download → mark processed.
   - On account-level IMAP failure: log error, record in summary, **continue next account**; exit non-zero if any account failed.
   - Prints per-account and aggregated summary to stdout; same written to log file.
   - Ensures each IMAP session is closed before moving to next account.

### Execution Modes（两种触发方式，同一套逻辑）

手动与定时**共用同一个入口、同一套处理流程**（拉信 → 匹配 → 下载 → 去重），区别仅在于谁触发、何时触发。

| 模式 | 触发方式 | 典型场景 |
|------|----------|----------|
| **手动立即执行** | 终端运行 `python main.py run` | 刚配好 config 想立刻验证；不想等下一整点 cron |
| **定时自动执行** | 系统 cron 每小时调用同一命令 | 日常无人值守，新简历自动入库 |

**CLI 约定：**

```bash
# 手动：立即跑一轮，处理完即退出
python main.py run

# 等价写法（run 为默认子命令）
python main.py

# 指定配置文件（可选）
python main.py run --config /path/to/config.yaml

# 只处理某一个账号（可选，name 与 config 里 accounts[].name 一致）
python main.py run --account hr-boss
```

**cron 示例（与手动命令相同；多账号需注入多个环境变量）：**

```bash
0 * * * * cd /path/to/邮箱抓取简历 && IMAP_PASSWORD_HR='***' IMAP_PASSWORD_R2='***' /usr/bin/python3 main.py run >> logs/cron.log 2>&1
```

**行为一致性与输出：**

- 手动跑与 cron 跑使用相同 `config.yaml`、相同 SQLite 去重库，**不会重复下载**已处理邮件（按账号隔离）。
- 手动跑结束后打印**每个账号**摘要 + 汇总；同时写入 `log.path`。
- 任一账号失败：其余账号仍执行；进程 exit code 非零表示至少一个账号失败。

### Scheduling

- **Frequency:** once per hour via system cron (`0 * * * *`).
- **Command:** `python main.py run`（与手动触发相同）。

### First-Run Behavior

- No backfill of existing inbox history.
- **Per account:** on first successful run for that `account_name`, establish a watermark and only process new mail thereafter.
- Watermarks are independent per account.

### Configuration Contract

用户通过 **`config.yaml`** 配置（从 `config.yaml.example` 复制）。

#### 1. 多邮箱账号列表（`accounts`，至少 1 个）

每个列表项：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 账号唯一标识，如 `hr-boss`；用于去重、日志、`--account` |
| `imap.host` | 是 | IMAP 服务器地址 |
| `imap.port` | 是 | 端口，SSL 通常 `993` |
| `imap.ssl` | 是 | 是否 SSL |
| `imap.username` | 是 | 登录邮箱 |
| `imap.password_env` | 是 | 该账号密码的环境变量名（各账号可不同） |
| `mailbox` | 否 | 默认 `INBOX` |
| `download.path` | 是 | **该账号**简历保存目录，账号间可不同 |

#### 2. 全局规则（所有账号共用）

| 字段 | 说明 |
|------|------|
| `subject_patterns` | 邮件标题正则列表 |
| `attachment_extensions` | 扩展名白名单 |
| `state.db_path` | SQLite 路径（所有账号共用一库，表内按 `account_name` 区分） |
| `log.path` | 日志路径 |

#### 配置示例（两邮箱）

```yaml
accounts:
  - name: hr-boss
    imap:
      host: imap.exmail.qq.com
      port: 993
      ssl: true
      username: hr@company.com
      password_env: IMAP_PASSWORD_HR
    mailbox: INBOX
    download:
      path: /Users/liufeng/Resumes/hr

  - name: recruiter-2
    imap:
      host: imap.exmail.qq.com
      port: 993
      ssl: true
      username: recruiter2@company.com
      password_env: IMAP_PASSWORD_R2
    mailbox: INBOX
    download:
      path: /Users/liufeng/Resumes/recruiter2

subject_patterns:
  - ".*应聘.*【BOSS直聘】"
attachment_extensions:
  - ".pdf"
```

### Example Subject Patterns (initial defaults)

- `.*应聘.*【BOSS直聘】` — BOSS 直聘 channel
- `.*应聘.*` — broader fallback; user may tighten in config

### Error Handling

- **Single account** IMAP failure: log, skip to next account; do not corrupt other accounts' state.
- **Any account failed** in run: exit non-zero after all accounts attempted.
- Individual attachment write failure: log and continue other attachments; mark email processed per policy.
- Invalid regex or duplicate `account.name`: fail at startup.
- Missing `password_env` value at startup: fail fast with which account/env is missing.

### Technology Choices

- Python 3.10+
- Standard/library IMAP client (`imaplib` or thin wrapper such as `imap-tools`)
- SQLite3 for state
- YAML for configuration
- macOS cron for scheduling

## Testing Decisions

### Principles

- Test **external behavior** of each module through its public interface, not internal implementation details.
- Prefer pure modules (Subject Matcher, Attachment Filter, Attachment Downloader naming logic) for fast unit tests without network or filesystem side effects where possible.
- Integration tests may mock IMAP responses rather than hitting a live mailbox in CI.

### Modules to Test

| Module | Priority | Rationale |
|--------|----------|-----------|
| Subject Matcher | **High** | Core business rule; pure regex matching |
| Attachment Filter | **High** | Whitelist logic is simple but critical |
| Attachment Downloader (naming & collision) | **High** | Must produce correct `{date}_{original}` paths and handle duplicates |
| Processed Mail Store | **Medium** | Composite key `(account_name, message_id)` dedup |
| Config Loader | **Medium** | Multi-account validation, unique names |
| IMAP Mail Fetcher | **Low in unit tests** | Mock at orchestrator level; optional manual smoke test against real mailbox |
| Job Orchestrator | **Medium** | Multi-account loop; one account failure continues others |

### Prior Art

Greenfield project; no existing test patterns in repository. Establish `pytest` as test runner with `tmp_path` fixtures for download and SQLite tests.

## Out of Scope

- Web UI or desktop GUI for configuration or monitoring
- Gmail OAuth or Microsoft Graph API (IMAP-only for enterprise/self-hosted mail)
- Scanning mail folders other than INBOX
- Resume identification by attachment filename or PDF body text
- Parsing or structuring resume content (OCR, field extraction)
- Email sending, replying, or marking messages read/unread on the server
- Parallel/concurrent IMAP connections (accounts processed **sequentially** in v1)
- Cloud deployment, Docker, or remote server hosting
- Notification integrations (Slack, email alerts) on success or failure
- Historical backfill of existing inbox messages on first run
- Attachment types beyond user-configured extension whitelist
- Duplicate detection by file content hash (only Message-ID and filename collision handling)

## Further Notes

### Reference Example

| Field | Value |
|-------|-------|
| Account | `hr-boss` → saves under `/Users/liufeng/Resumes/hr/` |
| Email subject | `刘烨 \| 7年，应聘 AI产品经理 \| 北京30-40K【BOSS直聘】` |
| Attachment name | `【AI产品经理_北京_30-40K】刘烨_7年.pdf` |
| Saved as | `20250529_【AI产品经理_北京_30-40K】刘烨_7年.pdf` |
| Dedup key | `(hr-boss, <Message-ID>)` |

### Operational Notes

- User should create a cron entry after verifying a manual run succeeds.
- IMAP password should be injected via shell environment or a secure mechanism compatible with cron (e.g. launchd environment on macOS if cron env is insufficient).
- Log rotation is not in scope for v1; user may configure system logrotate separately if logs grow large.

### Future Considerations (not in v1)

- Additional subject patterns for 猎聘、智联等其他渠道
- Optional PDF-only content validation
- Simple CLI status command (`--stats`) showing processed count and last run time
