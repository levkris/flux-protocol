import time
from collections import defaultdict

from .constants import (
    MAX_MSGS_PER_MINUTE, MIN_CONTENT_LEN, URL_COUNT_THRESHOLD,
    REPETITION_RATIO, SPAM_SCORE_THRESHOLD,
    SPAM_KEYWORDS, SPAM_URL_RE, SPAM_SUBJECT_CAPS_RE, SPAM_SUBJECT_PUNCT_RE,
)

_rate_window: dict[str, list[int]] = defaultdict(list)


def is_spam(msg: dict) -> dict:
    """Returns {"spam": bool, "reason": str | None, "score": int}."""
    content: str = msg.get("content", "") or ""
    subject: str = msg.get("subject", "") or ""
    sender: str = msg.get("from", "") or ""

    if _check_rate_limit(sender):
        return {"spam": True, "reason": f"rate limit exceeded for {sender[:16]}…", "score": 99}

    if len(content.strip()) < MIN_CONTENT_LEN:
        return {"spam": True, "reason": "empty content", "score": 1}

    if len(content) > 10:
        most_common = max(set(content), key=content.count)
        if content.count(most_common) / len(content) > REPETITION_RATIO:
            return {"spam": True, "reason": "repetitive content", "score": 3}

    # Normalise URL density against content length so short messages aren't unfairly penalised
    url_density = len(SPAM_URL_RE.findall(content)) / (max(len(content), 1) / 200)
    if url_density > URL_COUNT_THRESHOLD:
        return {"spam": True, "reason": "excessive URLs", "score": 3}

    score = _keyword_score(content.lower() + " " + subject.lower())
    if score >= SPAM_SCORE_THRESHOLD:
        return {"spam": True, "reason": f"spam keyword score {score}", "score": score}

    if subject:
        if SPAM_SUBJECT_CAPS_RE.search(subject):
            score += 1
        if SPAM_SUBJECT_PUNCT_RE.search(subject):
            score += 1
        if score >= SPAM_SCORE_THRESHOLD:
            return {"spam": True, "reason": "abusive subject line", "score": score}

    return {"spam": False, "reason": None, "score": score}


def _check_rate_limit(sender: str) -> bool:
    if not sender:
        return False
    now = int(time.time() * 1000)
    window_start = now - 60_000
    _rate_window[sender] = [t for t in _rate_window[sender] if t > window_start]
    _rate_window[sender].append(now)
    return len(_rate_window[sender]) > MAX_MSGS_PER_MINUTE


def _keyword_score(text: str) -> int:
    return sum(w for kw, w in SPAM_KEYWORDS if kw in text)


def reset_rate_limit(sender: str):
    _rate_window.pop(sender, None)


def spam_stats() -> dict:
    now = int(time.time() * 1000)
    window_start = now - 60_000
    active = {addr: len([t for t in ts if t > window_start]) for addr, ts in _rate_window.items()}
    return {"active_senders": len(active), "counts": active}