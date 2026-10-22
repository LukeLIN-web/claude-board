# Core Engineering Principles

These principles apply unless a repository defines stricter rules.

## 1. Occam’s Razor

Do not introduce concepts, abstractions, states, mechanisms, or exceptions unless they are necessary.

This does not mean minimizing complexity at all costs. Complete systems may be complex, but their complexity should emerge from a small set of **distilled, essential structures**, composed through abstraction, encapsulation, and nesting.

Distilled is not the same as simple.

A superficially simple primitive may merely push complexity onto every caller. Prefer a small number of expressive, coherent primitives over many narrow primitives connected by ad hoc conventions.

Optimize for the minimum **conceptual surface area**, not the minimum line count.

Prefer:

* composition over special cases;
* general invariants over repeated local patches;
* a small coherent core over many overlapping mechanisms;
* removing a concept over adding machinery to compensate for it.

Before adding a new entity, determine whether it expresses an essential distinction or merely preserves accidental complexity.

## 2. Single Source of Truth

Every fact, rule, schema, configuration value, or state transition must have one semantic owner.

Other representations must be derived, generated, referenced, cached, or validated against that source. They must not become independent authorities.

When duplication is necessary, make the relationship explicit:

* identify the canonical source;
* derive secondary forms mechanically where possible;
* define synchronization and invalidation rules;
* prevent silent divergence.

SSOT does not require centralizing all code. It requires that each truth be defined exactly once.

When two representations disagree, remove the competing authority rather than adding reconciliation logic.

## 3. Eliminate Slop

我们要的是：基于凝练的原语，通过封装、抽象形成的复杂完整的系统.

Do not over-engineer unstable functionality. Early implementation should establish the correct behavior and expose the essential model, not anticipate every hypothetical requirement.

After the behavior is proven, perform a deliberate simplification pass.

Remove:

* speculative abstractions;
* one-use wrappers and interfaces;
* redundant validation;
* temporary branches and flags;
* stale compatibility paths;
* unnecessary configuration;
* duplicated logic;
* defensive handling for states that should be structurally impossible;
* tests that preserve implementation shape rather than semantics.

Ask:

1. Is this design appropriate for the project’s current stage?
2. Can the same behavior be expressed with fewer concepts, states, or execution paths?
3. Did implementation reveal that any abstraction is unnecessary?
4. Can a stronger invariant replace several defensive patches?

Prefer deleting code over explaining unnecessary code. Prefer repairing the model over accumulating exceptions around it.

# claude-board

## Live testing

- 需要用真实 session 实测(发送链路探针、landed-verify、菜单/overlay 解析等)时,**开一个 haiku 的专用测试 session**(`claude --model haiku`),探针只发到那里;不要往默认模型的 session 里发测试消息——每条探针都是一次真实模型调用,烧额度且污染正常工作的会话。
- 现成的入口:`pytest tests/live --run-live -v`。它在独立 tmux server(`-L claude-board-live-<pid>`)里开 haiku session,依次测 trust prompt、卡片发现、banner、send+landed、Rewind 面板、/model 对话框,跑完 kill 掉 server;一共两次 haiku 调用。失败时 pane 存到 `.live-captures/<claude 版本>/`,用它做单元测试 fixture(先过 `scripts/secrets-audit.py`)。CI 不跑这一层。
