[English](README.md) | 中文

# Claude Fleet

同时开 5-7 个 Claude Code 和 Codex 窗口 vibe coding 的时候，你需要一个地方看到所有窗口在干嘛、谁卡了、谁做完了——并且不用满屏找终端 tab 就能直接操作它们。

**Claude Code** 和 **Codex** 的 session 都会显示为实时卡片，每张卡片标注所属 agent（蓝色 `cc` 或绿色 `codex` 徽章），一眼就能区分。

![](docs/screenshot-hero.png)

## 30 秒跑起来

```bash
git clone https://github.com/LukeLIN-web/claude-board
cd claude-board && bash run.sh
# 浏览器打开 http://127.0.0.1:7878
```

首次运行自动建 venv 装依赖，不用管。换端口：`CLAUDE_FLEET_PORT=9000 bash run.sh`。

## 解决什么问题

多窗口 vibe coding 的日常痛点：

- **Permission 通知一闪而过** → 红条常驻顶部，点一下跳回对应终端
- **不知道哪个窗口在干嘛** → 每张卡片显示当前任务、triage 状态、后台任务
- **做完的窗口忘记关** → patrol 引擎自动标 closeable，任意 session 一键关闭
- **为发一行字还得切终端很烦** → 直接在面板里新建 session、或给某个 session 发一条 prompt（Linux + tmux）
- **想找上周某个 session** → 全文搜索 50ms 返回，带 VS Code 风格匹配上下文
- **Skill 用了多少次不知道** → 三维统计（invoke + file read/write + bash 引用）
- **Memory 被谁改过** → 入度（↓被几个 session 参考）+ 出度（↑被几个 session 修改）

## 核心功能

### Triage 分类

不是简单的 busy/idle。Patrol 引擎读 transcript 的 `stop_reason`、`queue-operation` 事件和后台任务状态：

| 状态 | 含义 | 怎么判的 |
|------|------|---------|
| 🟢 working | 在干活 | busy 或有活跃 Monitor/Bash bg |
| 🔴 waiting | 等你批准 | permission prompt / dialog open |
| 🟡 stalled | 卡住了 | stop_reason=tool_use + 空闲>5min |
| 🔵 completed | 做完了 | stop_reason=end_turn + 空闲>5min |
| ⚪ closeable | 可以关了 | completed + 空闲>1h |

后台任务（Bash `run_in_background`、Monitor `persistent`）会追踪 tool_use/tool_result 配对，完成的自动清掉，不会误判成 working。

### 搜索

ripgrep 跨 Claude + Codex 全部 transcript，50ms 返回。不只搜 session 标题——搜 "hailuo" 能找到对话里提过 Hailuo 的 session，即使标题是 "你需要看下 klingai.com"。

每条结果带匹配上下文片段（最多 3 条），一眼看出为什么命中。

![](docs/screenshot-search.png)

### Skill / Memory 追踪

Skill 面板统计三个维度：

```
paper2video        333   1 invoke · ↓122 reads · ↑53 writes · 157 bash
feishu-notify       45  24 invokes · ↓7 reads · ↑7 writes · 7 bash
qzcli-topdowneval   12   3 invokes · ↓1 reads · ↑2 writes · 6 bash
```

只统计 `/skill-name` 正式调用的话是 44 次；加上 Read/Write/Edit skill 文件 + Bash 里引用 skills/ 的操作，实际是 431 次。

Memory 面板按 type 分组（user/feedback/project/reference），每条显示 `↓3 ↑2`（3 个 session 读过，2 个 session 改过）。

![](docs/screenshot-skills.png)
![](docs/screenshot-memory.png)

### 时间线 + Plan 历史

点开任意 session 看完整对话流，打开时自动定位到最新一条。Skill 调用紫色、Memory 读蓝色虚线、Memory 写粉红色。

Plan 版本历史：一个 session 通常迭代 5-14 次 plan，每次 Write 是完整快照，Edit 是红绿 diff。

![](docs/screenshot-timeline.png)

### 新建 & 发送（Linux + tmux）

Claude Fleet 默认只读，但有两个可选的、基于 tmux 的操作，让你不离开面板就能驱动 session。只有 tmux 可用时才显示。

- **新建 session** — 在顶部选 agent（**Claude Code** 或 **Codex**）和一个最近用过的目录（或自己输），
  点 **Spawn**。Fleet 执行 `tmux new-window … claude --dangerously-skip-permissions` 或
  `tmux new-window … codex --yolo`，新 session 全自动启动——不会卡在 permission 提示上。新窗口会在
  下一次 2s 轮询时出现。
- **发送 prompt** — 每张卡片有个 `Send a prompt…` 输入框。输入一行、回车，Fleet 通过
  `tmux send-keys` 把它注入该 session 的 tmux pane（字面文本 + 单独一个 Enter 提交）。

> `--dangerously-skip-permissions`（Claude）/ `--yolo`（Codex）会自动放行 spawn 出来的 session 里的
> 所有操作。本地驱动自己的 session 时这个权衡是合理的——只是别在你不信任的目录里 spawn。

> **Codex session 怎么探测的。** Codex 不像 Claude 那样写按 pid 索引的 session 文件，所以 Fleet 从运行
> 中的进程发现 Codex TUI（按控制 tty 分组），并在第一轮对话打开 `rollout-*.jsonl` 后通过
> `/proc/<pid>/fd` 关联到对应 transcript。刚 spawn 的 Codex 会立即显示成卡片，session id / transcript
> 在第一轮对话后补全。（仅 Linux；后台的 `codex mcp-server` / `app-server` 进程会被排除。）

### 操作

| 按钮 | 做什么 |
|------|--------|
| Focus | 跳到那个终端 tab |
| Timeline | 展开完整对话时间线 + plan 历史 |
| Send | 往 session 的 tmux pane 注入一行 prompt（Linux + tmux）|
| Fork | `claude --resume <sid> --fork-session`，新 session 继承对话历史 |
| Resume | `claude --resume <sid>`，继续原 session（在历史列表里）|
| Review | 向 session 发送 `/humanize:ask-codex review`（Linux + tmux）|
| Close | SIGTERM——每张卡片都有 |
| Export | 导出对话文档（带 timeline + plan 历史 + skill/memory 摘要）|

**Codex** 卡片上，平台无关的操作（Close、发 prompt、Esc、Commit）照常工作；Claude 专属的（Fork、Review、Clear、快速批准 permission）会隐藏，因为它们依赖 Claude 的斜杠命令或 `claude` 二进制。

### 按 session id 反查

外部工具（监督 skill、脚本、看着 transcript 文件名的你）手里通常只有 *session id*,
不是 pid 或 pane。两条等价的反查链路解决 `session id → pid → tty → tmux pane`：

- **API** — `GET /api/locate/<session-id>`（≥ 8 位的唯一前缀也行），返回 window 以及
  `tmux_pane` / `tmux_target`。覆盖在跑的 Claude 和 Codex session。
- **独立脚本** — [`scripts/locate-session.sh`](scripts/locate-session.sh)，只依赖
  bash+jq+tmux，不需要 server 在跑：

  ```console
  $ scripts/locate-session.sh 8ce5b822
  {"session_id":"8ce5b822-…","pid":116440,"tty":"/dev/pts/7","tmux_pane":"%3","tmux_target":"j1:2.0",…}
  ```

两者都基于一个事实：Claude Code 会把每个在跑的 session 登记到
`~/.claude/sessions/<pid>.json`（`{pid, sessionId, cwd, …}`），所以这是查表，不是猜。

> **Focus 设置（macOS）。** Focus 开箱即用，支持 Terminal.app 和 iTerm2——包括 session 跑在
> **tmux** 里的情况（自带的 [`scripts/focus-tty.sh`](scripts/focus-tty.sh) 会把进程 tty → 所属终端
> tab → 切过去）。想换别的终端 / 窗口管理器，放一个可执行的 `~/.claude/focus-tty.sh`（接收一个
> `<tty>` 参数）即可，它优先于自带默认。

## 架构

单文件前端（Alpine.js + Tailwind CDN，不需要 npm）。Python 后端从不写入 `~/.claude/` 和 `~/.codex/` 中存储的 harness 数据——这些数据保持只读。它**默认只读**：少数显式的、用户触发的操作（fork、close，以及 Linux 上基于 tmux 的新建会话 / 单条 prompt 注入，包括 Clear/Commit/Review 这几个 prompt 快捷按钮）作用于运行中的会话，而非存储的数据。

```
app.py                FastAPI + SSE (2s 轮询)
core/
  sessions.py         读 sessions/*.json，关联 TTY（Window + platform 字段）
  transcripts.py      解析 JSONL，提取 skill/memory/plan/后台任务
  patrol.py           triage 分类引擎
  codex.py            Codex session 解析 + 实时 session 发现（/proc + fd）
  search.py           ripgrep 跨平台搜索
  actions.py          focus / fork / close / export / 新建 / 发 prompt
  tmux.py             tmux 后端：新建窗口 + 注入 prompt（Linux）
  history.py          统一索引 + 全文 rg 搜索
  skills.py           skill 目录扫描
  memory.py           memory 文件解析
  plans.py            plan 关联（从 transcript 提取）
  perms.py            permission 事件
static/index.html     单文件 SPA
```

## 致谢

- [HarnessKit](https://github.com/RealZST/HarnessKit) — 跨平台 skill 管理的 UI 参考
- [Synergy](https://github.com/SII-Holos/synergy) — Memory engram 分类展示的灵感

## License

[MIT](LICENSE)
