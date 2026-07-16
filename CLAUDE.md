# claude-board

## Live testing

- 需要用真实 session 实测(发送链路探针、landed-verify、菜单/overlay 解析等)时,**开一个 haiku 的专用测试 session**(`claude --model haiku`),探针只发到那里;不要往默认模型的 session 里发测试消息——每条探针都是一次真实模型调用,烧额度且污染正常工作的会话。
