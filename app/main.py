"""FastAPI application entry point.

Two Slack event paths:
  - Path 1: message in BUG_CHANNEL_ID  → always create GitHub issue (no quality gate)
  - Path 2: 🐛 reaction by authorized user → quality gate, then create issue

Key design decisions:
  - Respond 200 immediately, do all work in a background asyncio task.
    Slack retries if it doesn't receive 2xx within 3 seconds.
  - Deduplicate Slack retries by checking X-Slack-Retry-Num header.
  - Verify HMAC-SHA256 signature on every request to prevent spoofing.
"""

import asyncio
import hashlib
import hmac
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from app import bug_extractor, duplicate_detector, github_client, slack_client
from app.config import settings
from app.models import MessageEvent, ReactionEvent, SlackEventPayload
from app.validator import score_bug_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Slack Bug Triage Assistant")


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

    messages = await slack_client.get_thread_messages(event.channel, event.ts)
    permalink = await slack_client.get_message_permalink(event.channel, event.ts)

    from app.models import SlackThreadContext
    context = SlackThreadContext(
        channel_id=event.channel,
        message_ts=event.ts,
        thread_ts=event.ts,
        original_message=event.text,
        thread_replies=[m.get("text", "") for m in messages[1:]],
        permalink=permalink,
    )

    bug = await bug_extractor.extract_from_thread(context)
    if bug is None:
        logger.warning("Extraction failed for message %s — skipping", event.ts)
        return

    dup = await duplicate_detector.check_duplicate(bug)
    if dup.is_duplicate:
        comment = _build_duplicate_comment(bug, permalink)
        await github_client.add_comment(dup.existing_issue_number, comment)
        await slack_client.post_message(
            event.channel,
            event.ts,
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
        logger.exception("GitHub issue creation failed for message %s", event.ts)
        await slack_client.post_message(
            event.channel, event.ts,
            ":warning: Failed to create GitHub issue. Please file it manually.",
        )
        return

    await slack_client.post_message(
        event.channel,
        event.ts,
        f":white_check_mark: GitHub issue created: {issue['html_url']}",
    )
    logger.info("Created issue #%s for message %s", issue["number"], event.ts)


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
    if event.user not in settings.authorized_user_ids:
        logger.info("Unauthorized reaction from user %s — ignoring", event.user)
        return

    channel_id = event.item.channel
    message_ts = event.item.ts

    logger.info("Path 2: processing reaction by %s on message %s", event.user, message_ts)

    messages = await slack_client.get_thread_messages(channel_id, message_ts)
    permalink = await slack_client.get_message_permalink(channel_id, message_ts)

    original_text = messages[0].get("text", "") if messages else ""

    from app.models import SlackThreadContext
    context = SlackThreadContext(
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=message_ts,
        original_message=original_text,
        thread_replies=[m.get("text", "") for m in messages[1:]],
        permalink=permalink,
    )

    bug = await bug_extractor.extract_from_thread(context)
    if bug is None:
        logger.warning("Extraction failed for message %s — skipping", message_ts)
        return

    # Quality gate: Path 2 requires a minimum score before creating an issue
    quality = score_bug_report(bug)
    logger.info("Quality score for %s: %d/100 (passed=%s)", message_ts, quality.quality_score, quality.passed)

    if not quality.passed:
        feedback = _build_quality_feedback(quality)
        await slack_client.post_message(channel_id, message_ts, feedback)
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
