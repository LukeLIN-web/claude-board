[English](README.md) | 中文

# Claude Fleet

同时开 5-7 个 Claude Code 和 Codex 窗口 vibe coding 的时候，你需要一个地方看到所有窗口在干嘛、谁卡了、谁做完了——并且不用满屏找终端 tab 就能直接操作它们。

Claude Code、Codex 和 hmz 的 session 都会显示为实时卡片，每张卡片标注所属 agent。

![](docs/screenshot-hero.png)

## 30 秒跑起来

```bash
git clone https://github.com/LukeLIN-web/claude-board
cd claude-board && bash run.sh
# 浏览器打开 http://127.0.0.1:7879
```

首次运行自动建 venv 装依赖。后台运行的 board 用 `bash run.sh status` / `restart` / `stop` 管理；换端口：`CLAUDE_FLEET_PORT=9000 bash run.sh`。

## 解决什么问题

- **Permission 通知一闪而过** → 红条常驻顶部，点一下跳回对应终端
- **不知道哪个窗口在干嘛** → 每张卡片显示当前任务、triage 状态、后台任务
- **做完的窗口忘记关** → 自动标 `closeable`，任意 session 一键关闭
- **为发一行字还得切终端很烦** → 直接在面板里新建 session、或给它发一条 prompt（Linux + tmux）
- **想找上周某个 session** → 跨全部 transcript 的全文搜索，带匹配上下文
- **Skill 用了多少次不知道** → invoke、文件读写、Bash 引用合起来统计
- **Memory 被谁改过** → 每条被几个 session 读过（↓）、改过（↑）
- **不知道套餐额度还剩多少** → **Usage** 显示 Claude 和 Codex 的 session / 每周额度用了多少、几点重置，不调用模型（Linux + tmux）

## 核心功能

### Triage 分类

每张卡片按 transcript 和后台任务分类，不是简单的 busy/idle：

| 状态 | 含义 |
|------|------|
| 🟢 working | 在干活，或还有后台任务在跑 |
| 🔴 waiting | 等你：permission 提示或对话框 |
| 🟡 stalled | 停在工具调用中途，空闲 |
| 🔵 completed | 这一轮做完了，空闲 |
| ⚪ closeable | 做完且空闲很久了 |

### 搜索

ripgrep 跨 Claude + Codex 全部 transcript，不只搜标题；每条结果带命中的那几行。

![](docs/screenshot-search.png)

### Skill / Memory 追踪

![](docs/screenshot-skills.png)
![](docs/screenshot-memory.png)

### 时间线 + Plan 历史

点开任意 session 看完整对话流，skill 调用和 memory 读写都有标记，plan 的每个版本都在。

![](docs/screenshot-timeline.png)

### 新建 & 发送（Linux + tmux）

Fleet 默认只读。下面这些操作通过 tmux 驱动 session，只有 tmux 可用时才显示。

- **新建** — 在顶部选 agent（Claude Code、Codex 或 hmz）和目录。
- **发送** — 在卡片的 `Send a prompt…` 输入框里打字，Fleet 把它打进那个 session 的 pane。

> 新建的 Claude / Codex session 带 `--dangerously-skip-permissions` / `--yolo` 运行，所有操作自动放行。别在你不信任的目录里新建。

想让辅助 session 继续运行但不占卡片，就在它所在的机器上给它的 tmux pane 加标记，标记随 pane 存在：

```bash
tmux set-option -p -t %3 @fleet-passive 1     # 隐藏
tmux set-option -p -u -t %3 @fleet-passive    # 恢复显示
```

### 按 session id 反查

`GET /api/locate/<session-id>`（≥ 8 位的唯一前缀也行）返回在跑 session 的 window 和 tmux pane。[`scripts/locate-session.sh`](scripts/locate-session.sh) 对 Claude session 做同样的事，不需要 server。

> **Focus（macOS）** 支持 Terminal.app 和 iTerm2，包括跑在 tmux 里的情况。换别的终端，放一个可执行的 `~/.claude/focus-tty.sh <tty>`，它优先于自带的 [`scripts/focus-tty.sh`](scripts/focus-tty.sh)。

### 远程访问

server 只监听 loopback。`POST /api/windows/<pid>/keys` 会往 tmux pane 里敲键，所以**不设防的隧道就是公网 shell**。两个隧道脚本都拒绝发布无法证明设防的面板。

- [`scripts/tunnel.sh`](scripts/tunnel.sh) `start | stop | status` —— 固定的 [ngrok](https://ngrok.com) 域名，前面是 Google 登录加邮箱白名单。在 `.env.local` 里设 `FLEET_TUNNEL_DOMAIN` 和 `FLEET_TUNNEL_ALLOWED_EMAILS`，authtoken 用 `ngrok config add-authtoken` 存。
- [`scripts/cf-tunnel.sh`](scripts/cf-tunnel.sh) `start | stop | status | url` —— 免费随机域名的 [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)，靠面板自己的密码挡：在 `.env.local` 里设 `FLEET_AUTH_PASSWORD`，然后 `bash run.sh restart`。

设了 `FLEET_AUTH_PASSWORD` 后处处都要密码，loopback 也不例外。脚本用 `Authorization: Bearer $FLEET_API_TOKEN` 通过；ngrok 后面的 API 只能在浏览器里用。

### 多台机器，一个页面

每台机器各跑一份 board。其中一台通过 ssh 端口转发轮询其他几台，在一个页面里显示所有机器的卡片；每张卡片标上所属机器（`CLAUDE_FLEET_LABEL`，默认是 IP 最后一段），按钮作用在那台机器上。在负责聚合的机器上，写进 `.env.local.<hostname>`：

```bash
FLEET_PEERS=b=http://127.0.0.1:7880
FLEET_PEER_TUNNELS="7880:hostb:7879"   # <本地端口>:<ssh 主机>:<对端端口>
```

然后 `scripts/peer-tunnel.sh start`、`bash run.sh restart`。共享 `.env.local` 里的 `FLEET_API_TOKEN` 用来通过对端的密码门。搜索 / History / Skills / Memory 只显示聚合机本身的数据。

### GPU

每份 board 每 15 秒读一次本机的 `nvidia-smi`。页头的 **GPUs** 列出每台机器的每张卡上有什么；会话起的进程在哪张卡上，那张卡片上就标出来（`GPU b:3`）。`/api/windows` 的 `gpus` 字段是同一份数据，给排卡的脚本用。设了 `FLEET_GPU_LOCK_DIR`（`gpu_wait.py` 的锁目录），还会列出经它占住的卡和排队等卡的作业。想在页面上看到某台机器的作业，那台就要跑一份 board，哪怕上面没有会话。

## 致谢

- [HarnessKit](https://github.com/RealZST/HarnessKit) — 跨平台 skill 管理的 UI 参考
- [Synergy](https://github.com/SII-Holos/synergy) — Memory engram 分类展示的灵感

## License

[MIT](LICENSE)
