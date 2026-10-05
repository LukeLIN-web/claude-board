# claude-board

## Live testing

- 需要用真实 session 实测(发送链路探针、landed-verify、菜单/overlay 解析等)时,**开一个 haiku 的专用测试 session**(`claude --model haiku`),探针只发到那里;不要往默认模型的 session 里发测试消息——每条探针都是一次真实模型调用,烧额度且污染正常工作的会话。
- 现成的入口:`pytest tests/live --run-live -v`。它在独立 tmux server(`-L claude-board-live-<pid>`)里开 haiku session,依次测 trust prompt、卡片发现、banner、send+landed、Rewind 面板、/model 对话框,跑完 kill 掉 server;一共两次 haiku 调用。失败时 pane 存到 `.live-captures/<claude 版本>/`,用它做单元测试 fixture(先过 `scripts/secrets-audit.py`)。CI 不跑这一层。
