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

首次运行自动建 venv 装依赖，不用管。换端口：`CLAUDE_FLEET_PORT=9000 bash run.sh`。默认的后台模式有 supervisor，board 退出后会自动重启。可用 `bash run.sh status` / `restart` / `stop` 管理。

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

### 远程访问

server 只监听 loopback。从手机打开面板有两条路，区别在于**那层登录挡在哪里**。

> **无论选哪条，那层登录都是承重的。**
> `POST /api/windows/<pid>/keys` 会往 tmux pane 里敲键，所以不设防的隧道就是在一个
> 会被扫描的域名上开了个公网 shell。下面两个脚本都拒绝发布一个它无法证明是设防的面板。

**固定域名，登录挡在边缘** —— [`scripts/tunnel.sh`](scripts/tunnel.sh) 发布到一个
固定的 [ngrok](https://ngrok.com) 域名上，前面是 Google 登录：

```console
$ scripts/tunnel.sh start          # 还有：stop | status
[tunnel] up -> https://<your-domain>.ngrok-free.dev (Google sign-in required)
```

域名和放行的邮箱写在 `.env.local` 的 `FLEET_TUNNEL_DOMAIN` /
`FLEET_TUNNEL_ALLOWED_EMAILS`，authtoken 用 `ngrok config add-authtoken` 存到
ngrok 自己的配置里——这些都不该进仓库。脚本自己生成 traffic policy，没有 policy 就
拒绝启动。两条规则缺一不可：先要 Google 登录，再查白名单，否则全世界任何一个 Google
账号都算数。

**免费随机域名，登录挡在应用里** —— [`scripts/cf-tunnel.sh`](scripts/cf-tunnel.sh)
用 [Cloudflare](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
quick tunnel，不需要账号，也不需要域名：

```console
$ scripts/cf-tunnel.sh start       # 还有：stop | status | url
[cf-tunnel] up -> https://<random-words>.trycloudflare.com (password required)
```

quick tunnel 在边缘没有任何策略可言，所以这里的门是应用自己的密码
（[`core/auth.py`](core/auth.py)）：在 `.env.local` 里设 `FLEET_AUTH_PASSWORD`，然后
重启面板。脚本会**实际探测**跑着的 server，匿名请求确实被挡下来了才肯发布——它不是去读
那个环境变量，因为 `run.sh` 是 detached 起 uvicorn 的，启动之后才加的密码只存在于你的
shell 里，那个正在服务的进程根本不知道。URL 是随机的、每次启动都变，`cf-tunnel.sh url`
可以再打印一遍。

两条路互不依赖。密码门在哪种隧道后面都能用，不开隧道也能用——设了
`FLEET_AUTH_PASSWORD` 就是开，没设就是关（只适合 loopback 自用）。

> **代码里没有任何对 loopback 的放行**，这是故意的：隧道进程是连 `127.0.0.1` 的，所以
> 每个远端请求看起来都像本地请求，"信任本地请求"就是一把公开的万能钥匙。坐在这台机器前
> 面的人也一样要登录。

脚本访问 API 这一点上两者不同。ngrok 的 OAuth 之后 API 只能在浏览器里用——`curl` 打
`/api/*` 会被重定向到 Google。密码门则接受 `Authorization: Bearer $FLEET_API_TOKEN`
（如果你设了这个变量）：

```console
$ curl -H "Authorization: Bearer $FLEET_API_TOKEN" http://127.0.0.1:7879/api/windows
```

### 多台机器，一个页面

两台机器、两个 Claude 账号、一个面板。每台机器各跑一份 board，各读自己的
`~/.claude`、各驱动自己的 tmux；开着隧道的那台负责聚合——你原来那个 URL 就会
在同一个网格里显示所有机器的卡片，每张带上它所属机器的标签，卡片上的每个按钮
仍然作用在这张卡片真正所在的那台机器上。（标签取 `CLAUDE_FLEET_LABEL`，没设的话
就是那台机器 IP 的最后一段。）

```
                    ┌── A 上的 board ──┐  读 A 的 ~/.claude，驱动 A 的 tmux
  公网 URL ────────▶│  聚合            │
                    └────────┬─────────┘
                             │ ssh -L 7880:127.0.0.1:7879
                    ┌────────▼─────────┐
                    │  B 上的 board    │  读 B 的 ~/.claude，驱动 B 的 tmux
                    └──────────────────┘
```

**为什么不直接从共享挂载去读另一台的 `~/.claude`?** 因为一张卡片"活着"的依据是
*本机*的 `/proc/<pid>` 存在——远端的 pid 要么查不到，更糟的是撞上一个毫不相干的
本地进程;前端用 pid 当卡片的 key,两台机器必然冲突;而所有操作都是本机的 tmux
调用。会话能画出来,但没有一个是真的。所以每台机器各留一份 board,卡片改用带
主机前缀的 key(`b:1234`)寻址,路由据此决定*在本地执行*还是*转发过去*。

在负责聚合的那台机器上，写进 `.env.local.<hostname>`（`run.sh` 会在共享的
`.env.local` 之上再叠加这个按主机名区分的文件）：

```bash
FLEET_PEERS=b=http://127.0.0.1:7880
FLEET_PEER_TUNNELS="7880:hostb:7879"   # <本地端口>:<ssh 主机>:<对端端口>
```

```console
$ scripts/peer-tunnel.sh start     # 还有：stop | status
[peer-tunnel] 7880:hostb:7879 — up
$ ./run.sh                         # 重启一次，board 才会读到 FLEET_PEERS
```

对端是通过 loopback 访问的，而不是它的局域网地址：board 能往 tmux pane 里打字，
所以每份 board 都只绑 `127.0.0.1`；ssh 转发给聚合方一个本地端口去轮询，对端在
网络上依旧和以前一样不可达。密码门同样管着这一跳——共享 `.env.local` 里的
`FLEET_API_TOKEN` 就是穿过它的凭据。

对端卡片来自后台轮询（2s），不会在请求路径里发起网络调用，所以一个卡住的 peer
只会让它自己的卡片变旧——10 秒后变暗，两分钟后消失——不会拖住别的东西。顶栏会
多出每台机器一个的 chip：点击只看那台机器，board 不再应答的 peer 会变红、悬停
说明原因。**Spawn** 会多一个机器选择器，目录列表跟着它走。搜索 / History /
Skills / Memory 目前仍然只显示聚合机本身的数据。

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
  peers.py            多机：轮询对端 board、转发卡片操作
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
