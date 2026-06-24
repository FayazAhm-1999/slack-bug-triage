"""FastAPI application entry point.

Two Slack event paths:
  - Path 1: message in BUG_CHANNEL_ID  → always create GitHub issue (no quality gate)
  - Path 2: 🐛 reaction by authorized user → quality gate, then create issue

Key design decisions:
  - Respond 200 immediately, do all work in a background asyncio task.
    Slack retries if it doesn't receive 2xx within 3 seconds.
  - Deduplicate Slack retries by checking X-Slack-Retry-Num header AND
    an in-memory event-key set keyed on (channel, ts).
  - Verify HMAC-SHA256 signature on every request to prevent spoofing.
  - Shared HTTP clients (slack_client, github_client) are closed on shutdown.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from app import bug_extractor, duplicate_detector, github_client, slack_client
from app.config import AUTHORIZED_USER_IDS, settings
from app.models import MessageEvent, ReactionEvent, SlackEventPayload, SlackThreadContext
from app.validator import score_bug_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory event deduplication
# ---------------------------------------------------------------------------

class _ExpiringSet:
    """Bounded TTL set for deduplicating Slack event deliveries.

    Entries expire after `ttl_seconds`. The internal dict is compacted when it
    grows beyond `_MAX_SIZE` to prevent unbounded memory growth.
    """

    _MAX_SIZE = 2000

    def __init__(self, ttl_seconds: int = 600) -> None:
        self._data: dict[str, float] = {}
        self._ttl = ttl_seconds

    def seen(self, key: str) -> bool:
        """Return True if key was seen recently. Registers the key as seen."""
        now = time.monotonic()
        if len(self._data) >= self._MAX_SIZE:
            self._data = {k: v for k, v in self._data.items() if now - v < self._ttl}
        if key in self._data and now - self._data[key] < self._ttl:
            return True
        self._data[key] = now
        return False


_processed_events = _ExpiringSet()


# ---------------------------------------------------------------------------
# App lifespan — close shared HTTP clients on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await slack_client.close()
    await github_client.close()


app = FastAPI(title="Slack Bug Triage Assistant", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------

def verify_slack_signature(request: Request, body: bytes) -> bool:
    """HMAC-SHA256 verification per Slack signing-secret spec.

    Rejects requests older than 5 minutes to prevent replay attacks.
    """
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "0")
    slack_sig = request.headers.get("X-Slack-Signature", "")

    # Reject stale requests
    if abs(time.time() - int(timestamp)) > 300:
        logger.warning("Rejected stale Slack request (timestamp %s)", timestamp)
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    mac = hmac.new(
        settings.slack_signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    )
    expected = f"v0={mac.hexdigest()}"
    return hmac.compare_digest(expected, slack_sig)


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()

    if not verify_slack_signature(request, body):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")

    # Slack retries requests that don't receive a 2xx within 3 seconds.
    # Return 200 immediately on retries — the background task from the first
    # delivery is already running.
    if request.headers.get("X-Slack-Retry-Num"):
        return {"ok": True}

    try:
        payload = SlackEventPayload.model_validate_json(body)
    except ValidationError as exc:
        logger.error("Failed to parse Slack payload: %s", exc)
        raise HTTPException(status_code=400, detail="Bad payload")

    # Slack sends this once when you first configure the Events API URL.
    if payload.type == "url_verification":
        return {"challenge": payload.challenge}

    if payload.type == "event_callback" and payload.event:
        event_type = payload.event.get("type")
        channel = payload.event.get("channel", "")
        ts = payload.event.get("ts", payload.event.get("item", {}).get("ts", ""))
        event_key = f"{channel}:{ts}"

        if _processed_events.seen(event_key):
            logger.debug("Duplicate event suppressed: %s", event_key)
            return {"ok": True}

        # Fire and forget — we must return 200 before Slack's 3-second timeout.
        asyncio.create_task(handle_event(event_type, payload.event))

    return {"ok": True}


# ---------------------------------------------------------------------------
# Event dispatcher
# ---------------------------------------------------------------------------

async def handle_event(event_type: str | None, event: dict) -> None:
    try:
        if event_type == "message":
            await handle_message_event(MessageEvent.model_validate(event))
        elif event_type == "reaction_added":
            await handle_reaction_event(ReactionEvent.model_validate(event))
        else:
            logger.debug("Ignoring event type: %s", event_type)
    except Exception:
        logger.exception("Unhandled error processing event type=%s", event_type)


# ---------------------------------------------------------------------------
# Path 1: Dedicated bug channel message
# ---------------------------------------------------------------------------

async def handle_message_event(event: MessageEvent) -> None:
    # Only process messages in the configured bug intake channel
    if event.channel != settings.bug_channel_id:
        return

    # Ignore bot messages, edits, and deletions
    if event.subtype in ("bot_message", "message_changed", "message_deleted"):
        return

    # Ignore replies; only process new top-level messages
    if event.thread_ts and event.thread_ts != event.ts:
        return

    logger.info("Path 1: processing bug channel message ts=%s", event.ts)
    await _process_thread(event.channel, event.ts, run_quality_gate=False)


# ---------------------------------------------------------------------------
# Path 2: Bug emoji reaction on general channel message
# ---------------------------------------------------------------------------

async def handle_reaction_event(event: ReactionEvent) -> None:
    # Only handle the bug emoji
    if event.reaction != "bug":
        return

    # Only handle reactions on messages (not files)
    if event.item.type != "message":
        return

    # Authorization: only listed users may trigger issue creation
    if event.user not in AUTHORIZED_USER_IDS:
        logger.info("Unauthorized reaction from user %s — ignoring", event.user)
        return

    logger.info("Path 2: processing reaction by %s on message %s", event.user, event.item.ts)
    await _process_thread(event.item.channel, event.item.ts, run_quality_gate=True)


# ---------------------------------------------------------------------------
# Shared pipeline: fetch thread → extract → (quality gate) → dup check → issue
# ---------------------------------------------------------------------------

async def _process_thread(
    channel_id: str,
    message_ts: str,
    *,
    run_quality_gate: bool,
) -> None:
    """Run the full triage pipeline for a single Slack message thread.

    Args:
        channel_id: The Slack channel the message lives in.
        message_ts: The timestamp of the original message (also used as thread_ts).
        run_quality_gate: If True, reject low-quality reports with user feedback
                          (Path 2 only).
    """
    await slack_client.add_reaction(channel_id, message_ts, "eyes")

    messages = await slack_client.get_thread_messages(channel_id, message_ts)
    permalink = await slack_client.get_message_permalink(channel_id, message_ts)

    original_message = messages[0].get("text", "") if messages else ""

    context = SlackThreadContext(
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=message_ts,
        original_message=original_message,
        thread_replies=[m.get("text", "") for m in messages[1:]],
        permalink=permalink,
    )

    bug = await bug_extractor.extract_from_thread(context)
    if bug is None:
        logger.warning("Extraction failed for message %s — skipping", message_ts)
        return

    if run_quality_gate:
        quality = score_bug_report(bug)
        logger.info(
            "Quality score for %s: %d/100 (passed=%s)",
            message_ts, quality.quality_score, quality.passed,
        )
        if not quality.passed:
            await slack_client.post_message(
                channel_id, message_ts, _build_quality_feedback(quality)
            )
            return

    dup = await duplicate_detector.check_duplicate(bug)
    if dup.is_duplicate:
        comment = _build_duplicate_comment(bug, permalink)
        await github_client.add_comment(dup.existing_issue_number, comment)
        await slack_client.post_message(
            channel_id,
            message_ts,
            f":link: This looks like a duplicate of {dup.existing_issue_url} "
            f"(similarity: {dup.similarity_score:.0%}). I've added context to the existing issue.",
        )
        logger.info("Duplicate detected — commented on issue #%s", dup.existing_issue_number)
        return

    try:
        body = github_client.format_issue_body(bug, permalink)
        labels = github_client.build_labels(bug.severity)
        issue = await github_client.create_issue(bug.title, body, labels)
    except Exception:
        logger.exception("GitHub issue creation failed for message %s", message_ts)
        await slack_client.post_message(
            channel_id, message_ts,
            ":warning: Failed to create GitHub issue. Please file it manually.",
        )
        return

    await slack_client.add_reaction(channel_id, message_ts, "white_check_mark")
    await slack_client.post_message(
        channel_id,
        message_ts,
        f":white_check_mark: GitHub issue created: {issue['html_url']}",
    )
    logger.info("Created issue #%s for message %s", issue["number"], message_ts)


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _build_quality_feedback(quality) -> str:
    missing = ", ".join(quality.missing_fields)
    return (
        f":memo: This bug report scored {quality.quality_score}/100 "
        f"(minimum 80 required to create a GitHub issue).\n"
        f"*Missing information:* {missing}\n"
        f"Please add these details and ask an authorized triager to re-add the :bug: reaction."
    )


def _build_duplicate_comment(bug, slack_url: str | None) -> str:
    source = slack_url or "_Slack link unavailable_"
    return (
        f"**Potential duplicate report received from Slack.**\n\n"
        f"Source thread: {source}\n\n"
        f"**Summary:**\n{bug.summary}"
    )
