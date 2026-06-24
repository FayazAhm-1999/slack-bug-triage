# Slack Bug Triage Assistant

Converts Slack bug reports into structured GitHub issues using Claude AI. Supports two intake workflows: a dedicated bug channel and a human-in-the-loop reaction-based flow for general channels.

---

## Problem Statement

Engineering teams receive bug reports scattered across Slack — sometimes in dedicated channels, sometimes buried in general conversation. Manually converting these into well-structured GitHub issues is tedious, inconsistent, and easy to drop. Duplicate issues accumulate, and reporters rarely include all the information needed to reproduce a bug.

This application automates the pipeline: Slack thread → Claude extraction → quality validation → duplicate check → GitHub issue.

---

## Architecture

```
                        SLACK WORKSPACE
                               |
              ┌────────────────┴─────────────────┐
              │ Event: message (BUG_CHANNEL_ID)  │
              │ Event: reaction_added (bug emoji) │
              └────────────────┬─────────────────┘
                               |  POST /slack/events
                               ▼
                    ┌─────────────────────┐
                    │   FastAPI App       │
                    │  Signature Verify   │ ← HMAC-SHA256
                    │  Respond 200 fast   │ ← asyncio.create_task
                    │  Background task    │
                    └──────────┬──────────┘
              Path 1           │           Path 2
          (message in          │      (🐛 reaction by
          bug channel)         │       authorized user)
                    ┌──────────▼──────────┐
                    │  slack_client.py    │
                    │  Fetch msg + thread │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  claude_client.py   │
                    │  Extract BugReport  │ ← structured JSON extraction
                    └──────────┬──────────┘
                               │
              Path 1           │           Path 2 only
           (skip gate)         │      ┌─────────────────┐
                               │      │  validator.py   │
                               │      │  Score >= 80?   │
                               │      └──┬──────────┬───┘
                               │    pass │          │ fail
                    ┌──────────▼─────────▼──┐       │
                    │  duplicate_detector   │  Post quality
                    │  TF-IDF cosine sim    │  feedback to Slack
                    │  vs last 50 issues    │
                    └──────────┬────────────┘
                         dup   │   new
                    ┌──────────┤
                    │          │
          Comment on     Create GitHub
          existing issue    issue + labels
                    │          │
                    └────┬─────┘
                         │
                Post Slack confirmation
```

---

## Module Overview

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, signature verification, event routing, two pipeline flows |
| `app/config.py` | Env var loading via pydantic-settings, fail-fast at startup |
| `app/models.py` | Pydantic data models — shared contracts, no business logic |
| `app/slack_client.py` | Async Slack API wrapper (conversations.replies, chat.postMessage, etc.) |
| `app/github_client.py` | Async GitHub REST API wrapper + issue body formatter |
| `app/claude_client.py` | Claude extraction with retry on malformed JSON |
| `app/bug_extractor.py` | Thin adapter: assembles thread text, delegates to claude_client |
| `app/validator.py` | Pure quality scoring function — no I/O, fully deterministic |
| `app/duplicate_detector.py` | TF-IDF cosine similarity against recent open issues |

---

## Required Slack Bot OAuth Scopes

Configure these in your Slack App settings under **OAuth & Permissions → Bot Token Scopes**:

| Scope | Purpose |
|---|---|
| `channels:history` | Read messages and thread replies |
| `channels:read` | Resolve channel IDs |
| `chat:write` | Post confirmation and feedback messages |
| `reactions:read` | Receive reaction_added events |
| `reactions:write` | Add :eyes: and :white_check_mark: reactions to messages |

Also enable **Event Subscriptions** and subscribe to:
- `message.channels`
- `reaction_added`

---

## Setup

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in all values in .env
```

### 4. Run locally

```bash
uv run uvicorn app.main:app --reload --port 8000
```

### 5. Expose to Slack (development)

```bash
ngrok http 8000
```

In your Slack App settings, set the **Request URL** to:
```
https://<your-ngrok-id>.ngrok.io/slack/events
```

---

## Environment Variables

| Variable | Required | Description | Example |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot token from OAuth installation | `xoxb-...` |
| `SLACK_SIGNING_SECRET` | Yes | Used to verify request authenticity | `abc123...` |
| `ANTHROPIC_API_KEY` | Yes | Claude API key | `sk-ant-...` |
| `GITHUB_TOKEN` | Yes | Personal access token with `repo` scope | `ghp_...` |
| `GITHUB_REPO` | Yes | Target repository | `my-org/my-repo` |
| `AUTHORIZED_SLACK_USERS` | Yes | Comma-separated Slack user IDs for reaction flow | `U01234,U05678` |
| `BUG_CHANNEL_ID` | Yes | Channel ID for dedicated bug intake | `C01234567` |
| `DUPLICATE_THRESHOLD` | No | Cosine similarity threshold (default: 0.85) | `0.85` |

---

## How It Works

### Path 1: Dedicated Bug Channel

Messages posted to `BUG_CHANNEL_ID` are always processed:

1. Claude extracts a structured bug report from the message + thread.
2. Duplicate detection runs against the last 50 open GitHub issues.
3. **If duplicate**: a comment is added to the existing issue with the Slack thread link and a summary. No new issue is created.
4. **If new**: a GitHub issue is created with `bug` and `severity:<level>` labels.
5. A confirmation is posted back to the Slack thread.

No quality gate — users in the bug channel have already indicated intent.

### Path 2: General Channels (Reaction-Based)

1. An authorized triager adds a 🐛 reaction to any Slack message.
2. Claude extracts a structured bug report.
3. **Quality gate**: the report is scored (max 100, threshold 80). Missing fields are worth 20 points each. If the score is below 80, feedback is posted to the thread explaining what's missing.
4. Duplicate detection runs as in Path 1.
5. If the report passes both gates: a GitHub issue is created.

This is a *human-in-the-loop* pattern — a triager acts as a lightweight filter, and the quality gate enforces minimum information requirements before opening an issue.

---

## Tradeoffs & Design Decisions

### Reaction-based triggering (cost optimization)

Processing every Slack message with Claude would be extremely expensive. The 🐛 reaction pattern means Claude is only called when a human has already decided the message is worth investigating. This dramatically reduces API costs while preserving full automation for the bug channel.

### Dedicated bug channel (no quality gate)

Users who post in the dedicated bug channel have already made a conscious decision to report a bug. Blocking them with a quality gate would be frustrating and would undermine trust in the system. The tradeoff is that some low-quality issues may be created — but they can be triaged in GitHub.

### Claude semantic comparison over TF-IDF (duplicate detection)

TF-IDF was replaced because it is lexical — it counts shared terms. Paraphrases like *"the bot should react after parsing a ticket"* and *"the bot should acknowledge ticket creation with a reaction"* share almost no non-stopword terms after IDF weighting, so TF-IDF assigns them near-zero similarity despite describing the same problem. Claude understands semantic equivalence regardless of surface wording.

Each duplicate check sends a single Claude Haiku 4.5 call (~2,850 input tokens, ~50 output tokens):

| Volume | Cost per check | Daily | Monthly |
|---|---|---|---|
| 10 reports/day | ~$0.003 | ~$0.03 | ~$1 |
| 100 reports/day | ~$0.003 | ~$0.30 | ~$9 |
| 1,000 reports/day | ~$0.003 | ~$3.00 | ~$90 |
| 10,000 reports/day | ~$0.003 | ~$30.00 | ~$900 |

The cost is **linear with incoming report volume** because every new report triggers one Claude call regardless of corpus size. This is fine at demo scale; it becomes the dominant cost at high volume.

**Upgrade path for production**: pre-compute an embedding for each issue once (at creation time) and store it in a vector database (pgvector, Chroma). A duplicate check becomes one embedding call for the new bug plus a fast vector search — cost drops ~100× and no longer scales with report volume. The `DuplicateResult` interface in `duplicate_detector.py` is unchanged; only the similarity computation swaps out.

Embedding options (no Anthropic embeddings API exists):
- **sentence-transformers `all-MiniLM-L6-v2`** — free, local, ~80MB download (+ torch). Best for self-hosted.
- **OpenAI `text-embedding-3-small`** — $0.02/MTok (~$0.000004 per bug report). Requires a second API key.
- **Voyage AI `voyage-3-lite`** — $0.02/MTok, strong on technical text.

### Brute-force comparison (no vector database)

With only 50 issues, sending all titles and snippets to Claude in one prompt is trivially fast. A vector database only pays off when the corpus grows to thousands of items and you need approximate nearest-neighbour search at low latency. Adding one here would obscure the actual logic without measurable benefit at this scale.

### Scaling path

When this needs to handle higher volume:

1. **Replace `asyncio.create_task` with a message queue** (Redis + Celery, or AWS SQS + Lambda). The Slack endpoint enqueues the event payload; workers process it independently. This decouples ingestion from processing, enables retries, and provides backpressure.
2. **Switch duplicate detection to pre-computed embeddings** stored in a vector database (pgvector, Chroma). See the upgrade path above — eliminates the linear cost growth.
3. **Increase duplicate detection corpus** by caching recent issues in Redis with a TTL, rather than fetching 50 on every request.
4. **Add a database** to track which Slack messages have been processed, preventing re-processing after restarts.

---

## Running Tests

```bash
uv run pytest
```

The test suite covers:
- `validator.py` scoring logic (pure functions, no mocks needed)
- Slack signature verification
- GitHub issue body formatting
