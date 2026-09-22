# Xylocopa 新手入门

> 实操教程。提一条任务、建一个项目、看 agent 跑完一个闭环。
>
> English: [getting-started.md](getting-started.md)

这份文档写给刚装完 Xylocopa（[安装步骤](../README.md#getting-started)）、不知道下一步做什么的新人。读完这一篇，你会提交一条任务、建一个项目，并在浏览器里看着一个 agent 把活干完。它不重复 README 的功能列表，只回答新用户实际会卡住的三个问题。

1. 任务输入框底下那排按钮都是什么？
2. Task / Inbox / Project / Agent / Session 各是什么意思，怎么配合？
3. 至少掌握哪些操作就能跑起来？

---

## 整体流程一览

Xylocopa 沿用 [GTD](https://gettingthingsdone.com/what-is-gtd/) 的思路。把想法从脑子里倒出来，之后再决定怎么处理，时机到了再动手。区别在于动手的是 agent。

```
  灵感 ──▶ Task ──▶ Inbox ──▶ Project ──▶ Agent ──▶ Session
          (捕捉)    (暂存)     (归属)      (执行)    (复盘与记忆)
             ▲                                         │
             └────── 经验沉淀，写回 PROGRESS.md ────────┘
```

灵感先记成一条 task 暂存在 inbox，想清楚了再归到某个 project。派发会启动一个 agent，也就是一个在项目目录里干活的 Claude Code 会话。agent 跑完或卡住，你复盘 session，确认完成或重试。值得留的经验写进项目的 `PROGRESS.md`。

---

## 核心概念

### Task（任务）
一件具体的事，比如"加一个联系表单"或"交电费"。新建的 task 进 inbox，派发时离开。

### Inbox（收件箱）
跨所有 project 共享的一个队列。先进来，再处理。

<p align="center"><img src="getting-started/02-inbox.png" alt="Inbox 列表（多条待处理任务）" width="320"></p>

共用一个 inbox 是为了让记录这个动作不需要先回答"归哪个项目"。归类之后再说。

### Project（项目）
一组相关工作的容器，通常对应磁盘上一个 git 仓库，可以是已有仓库，也可以让 Xylocopa 从 GitHub URL 克隆。project 下派发的 agent 在该项目的目录里运行。

<p align="center"><img src="getting-started/03-projects.png" alt="项目列表页" width="360"></p>

只建一个大杂烩项目也完全可行。作者自己有一个 `random-things`，水电费、调研、一次性脚本都往里放，以后想拆再拆。

### Agent
Xylocopa 管理的一个运行中的 Claude Code 会话。每个 agent 住在名为 `xy-<短 id>` 的 tmux 会话里，从任何终端 attach 上去都能继续聊，因为和网页是双向同步的。

### Session（会话记录）
每次对话都持久化为一个 session，也就是 Claude 写在磁盘上的 JSONL 加上 Xylocopa 的按消息缓存。session 不会过期，除非手动删除，任何 session 都能带着完整上下文恢复。

---

## 最少 5 分钟上手

### 1. 提一条任务

短按底部导航栏的 `+`，**New Task** 面板从底部滑上来。

<p align="center"><img src="getting-started/06-new-task-dispatch-ready.png" alt="New Task 面板" width="320"></p>

标题可以不填，会从描述自动生成。描述就是发给 agent 的 prompt。刚装好时 project 留空，之后再归类。文本框下面有四个参数。

- **Model。** Fable 5、Opus 5.5、Opus 5、Opus 4.6、Sonnet 5、Haiku 4.5。默认 Opus 5.5，简单任务选便宜的档。
- **Effort。** Low、Medium、High、XHigh、Max。越高思考越多，也越慢越贵。
- **Worktree。** 打开后 agent 在隔离的 [git worktree](https://git-scm.com/docs/git-worktree) 里干活，不和你或其他 agent 打架。
- **Auto。** 见 [Auto 模式与安全](#auto-模式与安全)。

### 2. 离开面板

输入框底下有四个按钮。

| 图标 | 名称 | 作用 |
|---|---|---|
| `+` | 附件 | 图片、PDF、文本文件，作为上下文传给 agent。 |
| 🎙️ | 语音输入 | 用 OpenAI Whisper 转文字（需要 `OPENAI_API_KEY`）。 |
| 📥 | 存进 inbox | 存下任务并收起面板。 |
| ✈️ | 立刻派发 | 建任务并立即派发（⌘/Ctrl+Enter）。选了 project 才可用。 |

还在记想法阶段就存进 inbox，任务已经有 project 了就直接派发。

### 3. 建一个 project

派发前任务需要归属一个 project，因为 agent 是在项目目录里干活的。

<p align="center"><img src="getting-started/04-new-project.png" alt="新建项目表单" width="320"></p>

长按底部导航栏的 `+`，**New Project** 表单直接打开。名字用小写字母、数字、`- _ .`，可选粘贴 Git URL，点 **Create Project**。填了 URL 会自动克隆仓库，不填就在 `~/xylocopa-projects/<name>/` 下建一个空目录。先建一个 `random-things` 当杂物间也很合理。

project 建好后，回 inbox 点开刚才那条任务，选上 project，点 **Dispatch**。

### 4. 看 agent 跑

派发后进入 agent 对话页。

<p align="center"><img src="getting-started/12-chat-header-annotated-en.png" alt="Chat header：状态、操作按钮和标签条" width="640"></p>

顶部是一个状态标签加一个操作按钮，运行中显示 **Stop**，停止后显示 **Resume**，有时还会出现 Continued 链接和上下文用量指示。下面一行标签条是项目、worktree、Auto、Task 和一个短 id（长按可复制）。在对话页里你可以实时看输出、在 Auto 关闭时审批或拒绝工具调用、发后续消息纠偏。

角落里的圆形按钮是 attention 按钮。任何 agent 有未读消息时它会变成青色并显示数量，点击跳到最早的未读对话，长按打开分屏，最多同时盯四个 agent。

<p align="center"><img src="getting-started/09-desktop-inbox.png" alt="桌面端视图" width="640"></p>

到这里闭环已经完整。记录、派发、复盘，下面的内容都是锦上添花。

---

## 处理 inbox

积压了就用三种方式消化。

1. 点进任务，编辑、选 project、点 **Dispatch**。
2. 拖左边的 `≡` 手柄重排，最上面的先做。
3. 点右上角的 **AI** 按钮批量 triage。一个 agent 把所有任务读一遍、润色 prompt、分配 project，你确认后再派发。

暂时做不了的任务可以延期。展开卡片点 ⌛ 按钮选个日期，任务挪进底部折叠的 **Deferred** 分组，到期自动回来。

<p align="center"><img src="getting-started/10-inbox-defer-annotated-zh.png" alt="延期按钮和延期分组" width="360"></p>

---

## 在 project 里直接起 agent

每个 project 详情页顶部有一张 **New Agent** 卡片，已经知道任务归属时从这里启动最快。

<p align="center"><img src="getting-started/11-new-agent-annotated-zh.png" alt="项目内的 New Agent 卡片" width="520"></p>

它比 New Task 面板多两样东西。**定时**（🕐）不立刻派发，到设定时间自动启动，比如夜里 2 点跑重构。**Task 开关**决定这次运行要不要建 Task 记录，关掉是一次性会话，适合快问快答，打开则创建关联的 Task，出现在任务列表里并在跑完后生成摘要。Model、Effort、Worktree、Auto 与 New Task 面板一致，见[第 1 节](#1-提一条任务)。

---

## 收藏值得留的内容

一次长任务可能有几百条消息，下个月还会用到的往往就一条。双击那条消息选 **Bookmark**，写一句备注，或者直接跳过让自动生成的标题顶上。

收藏有两种粒度。chat header 里的 ⭐ **Starred** 置顶整段会话，📑 **Bookmarks** 只钉住一条消息。每个项目详情页有 **Bookmarks** tab，就在 **Starred** 旁边，点任意一行会跳回原消息并短暂高亮。

<p align="center"><img src="getting-started/13-bookmarks-list.png" alt="项目 Bookmarks 列表" width="360"></p>

---

## Auto 模式与安全

打开 **Auto** 后，agent 以 `claude --dangerously-skip-permissions` 启动，执行工具调用不再逐个询问。无论 Auto 开关与否，[safety hook](../README.md#safety-guardrails) 都会硬拦破坏性操作，除 README 列出的以外还包括 `git clean -f`、`git checkout -- .`、`git restore .` 和 `TRUNCATE`，而 agent 自己的 worktree 里 `git reset --hard` 是放行的。低风险任务（文档、UI 细节、worktree 里的隔离重构）开 Auto，想逐个审批工具调用就关着。

---

## Agent 跑偏了怎么办

在 chat header 停掉 agent。Xylocopa 会自动把这次尝试总结成一份摘要，记下试过什么、什么没成、下一步的线索。想补充纠正就先编辑任务，然后在任务详情页点 **Retry**。新 agent 带着这份摘要开始，之后你也能在 **Previous attempt context** 里翻到它。

超出单次尝试的经验沉淀到项目的 `PROGRESS.md`，派发新 agent 时自动取回。

---

## 进阶功能

- **分叉对话。** 双击消息选 **Diverge**，从那条消息把对话分叉给一个带完整上下文的新 agent。其余触控操作见[手势速查](gestures.md)。
- **网页终端。** chat header 里的终端图标直接 attach 到 agent 的 tmux 会话，任何浏览器都行，手机也可以。
- **主题。** Monitor → Display 里有五套预设配色、自定义主题编辑器、墨水屏模式，以及实验性的 orb 助手开关。
- **第三方 / 本地模型。** 模型下拉只列 Anthropic 型号，但 Claude Code 对 Bedrock、Vertex、LiteLLM 的支持照常生效，设好环境变量即可，`.env` 里的 `CC_MODEL` 决定默认值（LiteLLM 实操见 [unsloth.ai/docs/basics/claude-code](https://unsloth.ai/docs/basics/claude-code)）。

---

## 常见场景

**地铁上想到一件事。** 打开 PWA，点 `+`，对麦克风说一句，存进 inbox，回头再处理。

**攒了十几条任务。** 打开 inbox 点 **AI**，确认 triage 结果后批量派发。

**不想整理。** 建一个 `random-things` 装所有任务。代价是没有按项目的经验检索，其他功能照常。

**Agent 原地打转。** 停掉它，加一句纠正再重试，流程见[上一节](#agent-跑偏了怎么办)。

**同时盯几个 agent。** 长按 attention 按钮开分屏，或点击它跳到最早的未读对话。

---

## 接下来看什么

- [README](../README.md)，功能列表与安装。
- [workflow.md](workflow.md)，一天任务从记录到重试的完整走例。
- [ARCHITECTURE.md](ARCHITECTURE.md)，系统架构。
- [install-cert.md](install-cert.md)，在客户端设备上信任自签名 HTTPS 证书。
