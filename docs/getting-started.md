# Getting Started with Xylocopa

> A beginner's walkthrough. Capture a task, organize it into a project, and watch an agent execute it.
>
> 中文版：[getting-started-zh.md](getting-started-zh.md)

This guide is for people who just installed Xylocopa ([host setup](../README.md#getting-started)) and want to know what to do next. By the end you will have captured a task, created a project, and watched a live agent work from your browser. It doesn't repeat the README's feature list; it answers the three questions new users actually hit.

1. What are the buttons in the task-input panel?
2. What do Task, Inbox, Project, Agent, and Session mean, and how do they fit together?
3. What's the minimum workflow to be productive?

---

## The loop at a glance

Xylocopa follows [GTD](https://gettingthingsdone.com/what-is-gtd/). Get ideas out of your head, decide what to do with them later, and act when the time is right. The difference is that the acting is done by an agent.

```
  idea ──▶ Task ──▶ Inbox ──▶ Project ──▶ Agent ──▶ Session
         (capture)  (park)    (assign)   (execute)  (review & remember)
             ▲                                          │
             └────── lessons return via PROGRESS.md ────┘
```

You capture an idea as a task, park it in the inbox, and assign it to a project when you're ready. Dispatching spawns an agent, a Claude Code session working inside that project's directory. When the agent finishes or gets stuck you review the session, then close the task or retry. Lessons worth keeping go into the project's `PROGRESS.md`.

---

## Core concepts

### Task
A single unit of work, like "add a contact form" or "pay the electricity bill". Tasks start in the inbox and leave it when dispatched.

### Inbox
One shared queue across every project. You capture into it and process from it.

<p align="center"><img src="getting-started/02-inbox.png" alt="Inbox with several queued tasks" width="320"></p>

The inbox is shared so that capturing never requires deciding which project an idea belongs to. Sorting comes later.

### Project
A bucket for related work, usually backed by a git repo, either one you already have or one Xylocopa clones from a GitHub URL. Agents dispatched under a project run inside that project's working tree.

<p align="center"><img src="getting-started/03-projects.png" alt="Projects list" width="360"></p>

One catch-all project is fine. The author keeps a `random-things` project for everything that doesn't deserve its own repo, like bills, shopping research, and one-off scripts. You can split things out later.

### Agent
A running Claude Code session managed by Xylocopa. Each agent lives in a tmux session named `xy-<short-id>`, and you can attach to it from any terminal and keep working there, because sync with the web app is two-way.

### Session
Every conversation is persisted as a session, meaning the JSONL Claude writes on disk plus Xylocopa's per-message cache. Sessions don't expire unless you delete them, and any session can be resumed later with its full context.

---

## Your first five minutes

### 1. Capture a task

Tap the `+` button in the bottom nav to open the **New Task** sheet.

<p align="center"><img src="getting-started/06-new-task-dispatch-ready.png" alt="New Task sheet" width="320"></p>

The title is optional and gets derived from the description if you leave it blank. The description is the prompt the agent sees. On a fresh install leave the project blank and triage later. Four knobs sit below the text.

- **Model.** Fable 5, Opus 5.5, Opus 5, Opus 4.6, Sonnet 5, or Haiku 4.5. Opus 5.5 is the default; pick a cheaper tier for simple tasks.
- **Effort.** Low, Medium, High, XHigh, or Max. Higher means more thinking, slower and more expensive.
- **Worktree.** Runs the agent in an isolated [git worktree](https://git-scm.com/docs/git-worktree) so it won't collide with anything else you have open.
- **Auto.** See [Auto mode and safety](#auto-mode-and-safety).

### 2. Leave the sheet

Four buttons sit on the input bar.

| Icon | Name | What it does |
|---|---|---|
| `+` | Attach files | Images, PDFs, and text files, passed to the agent as context. |
| 🎙️ | Voice input | Dictation via OpenAI Whisper (needs `OPENAI_API_KEY`). |
| 📥 | Save to inbox | Park the task and close the sheet. |
| ✈️ | Launch agent | Create the task and dispatch it immediately (⌘/Ctrl+Enter). Enabled once a project is selected. |

While you're still capturing, save to the inbox. Once the task has a project, launch.

### 3. Create a project

To dispatch, a task needs a project, because the agent works inside that project's directory.

<p align="center"><img src="getting-started/04-new-project.png" alt="New project form" width="320"></p>

Long-press the `+` button in the bottom nav and the **New Project** form opens. Name it with lowercase letters, numbers, hyphens, underscores, or dots, optionally paste a Git URL, and hit **Create Project**. With a URL Xylocopa clones the repo; without one you get an empty folder under `~/xylocopa-projects/<name>/`.

Once the project exists, open your inbox task, assign the project, and hit **Dispatch**.

### 4. Watch it run

After dispatch you land in the agent's chat.

<p align="center"><img src="getting-started/12-chat-header-annotated-en.png" alt="Chat header with status, action pill, and tag strip" width="640"></p>

The header shows a status chip and one action, **Stop** while the agent runs or **Resume** after it stops, sometimes joined by a Continued link and a context-usage pill. The tag strip below carries the project, a worktree pill, an Auto chip, the task chip, and a short id pill you can long-press to copy. From the chat you read output as it streams, approve or deny tool calls when Auto is off, and send follow-ups.

The round button in the corner is the attention button. It turns cyan with a count when any agent has unread messages. Tapping it jumps to the oldest unread chat, and a long press opens split screen with up to four agents side by side.

<p align="center"><img src="getting-started/09-desktop-inbox.png" alt="Desktop view" width="640"></p>

That's the whole loop. Capture, dispatch, review. Everything below is refinement.

---

## Processing the inbox

There are three ways to drain a backlog.

1. Tap a task, edit it, pick a project, and hit **Dispatch**.
2. Drag the `≡` handle to reorder. The top of the list means "do first".
3. Tap the **AI** button in the top-right for batch triage. An agent reads every task, refines the prompts, and assigns projects, and you review before anything dispatches.

To hide a task until later, expand its card and tap the ⌛ **defer** button. Pick a date and the task moves into a collapsed **Deferred** section until then.

<p align="center"><img src="getting-started/10-inbox-defer-annotated-en.png" alt="Defer button and Deferred section" width="360"></p>

---

## Launching an agent from inside a project

Every project detail page has a **New Agent** card at the top, the fastest path when you already know where the work belongs.

<p align="center"><img src="getting-started/11-new-agent-annotated-en.png" alt="New Agent card inside a project" width="520"></p>

It adds two things the New Task sheet doesn't have. **Schedule** (🕐) launches at a future time instead of immediately, say a refactor at 2am. The **Task toggle** controls whether the run is tracked as a Task record; off gives an ephemeral session for quick questions, on gives a linked Task that shows up in task lists with a summary afterwards. Model, Effort, Worktree, and Auto work as in [the New Task sheet](#1-capture-a-task).

---

## Bookmark what's worth keeping

A long run can be hundreds of turns, and the one you'll want next month is buried in the middle. Double-tap any chat bubble and choose **Bookmark**, then type a one-line note or let a generated title stand in.

There are two granularities. ⭐ **Starred** in the chat header pins the whole session to the top of its project, while 📑 **Bookmarks** pins a single message. Each project has a **Bookmarks** tab next to **Starred**, and tapping a row jumps back to the original message with a brief highlight.

<p align="center"><img src="getting-started/13-bookmarks-list.png" alt="Project Bookmarks list" width="360"></p>

---

## Auto mode and safety

The **Auto** toggle launches the agent with `claude --dangerously-skip-permissions`, so it doesn't pause to ask before each tool call. The deterministic [safety hook](../README.md#safety-guardrails) still blocks destructive commands regardless of Auto mode, including a few beyond the README's list, such as `git clean -f`, `git checkout -- .`, `git restore .`, and `TRUNCATE`, while `git reset --hard` stays allowed inside a worktree the agent owns. Use Auto for low-risk work like documentation, UI tweaks, and isolated refactors in a worktree, and leave it off when you want to approve every tool call.

---

## When the agent misses

Stop the agent from the chat header. Xylocopa summarizes the attempt automatically, recording what was tried, what didn't work, and what looks promising. Edit the task if you want to add a correction, then hit **Retry** in the task detail. The fresh agent starts with that summary in its context, and you can read it later under **Previous attempt context**.

Lessons that outlive a single attempt go to the project's `PROGRESS.md`, which is retrieved for future agents automatically.

---

## Beyond the basics

- **Fork a conversation.** Double-tap a bubble and choose **Diverge** to branch the chat at that message into a new agent with full context. The [gestures cheat sheet](gestures.md) lists the rest of the touch shortcuts.
- **Web terminal.** The terminal icon in the chat header attaches to the agent's live tmux session from any browser, phone included.
- **Themes.** Monitor → Display has five preset palettes, a custom theme editor, an e-ink mode, and the experimental orb assistant toggle.
- **Third-party models.** The model picker lists Anthropic models only, but Claude Code's Bedrock, Vertex, and LiteLLM support carries over through the usual env vars, with `CC_MODEL` in `.env` setting the default (LiteLLM walkthrough at [unsloth.ai/docs/basics/claude-code](https://unsloth.ai/docs/basics/claude-code)).

---

## Common scenarios

**An idea on the subway.** Open the PWA, tap `+`, dictate it, save to inbox. Triage later.

**Ten tasks stacked up.** Open the inbox and tap **AI**. Review the triage result, then bulk dispatch.

**No interest in organizing.** Use one `random-things` project for everything. You lose per-project lesson retrieval, and everything else works.

**Agent going in circles.** Stop it and retry with a one-line correction, as in [When the agent misses](#when-the-agent-misses).

**Watching several agents.** Long-press the attention button for split screen, or tap it to jump to the oldest unread agent.

---

## Where to go next

- [README](../README.md) for the feature list and install.
- [workflow.md](workflow.md) for a worked example of one day's tasks, capture through retry.
- [ARCHITECTURE.md](ARCHITECTURE.md) for the system design.
- [install-cert.md](install-cert.md) to trust the self-signed HTTPS cert on client devices.
