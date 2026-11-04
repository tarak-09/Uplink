"""Answers volunteers' questions ("what is down?", "is Daniel5 up?", "why?") from Uplink's latest state.

The facts of the last message Uplink sent (kept on disk for 24 hours) come along as last_alert, so a
short reply to an alert such as "why?" or "say it in Hindi" is about that alert. Each chat's last
question and answer are kept for ten minutes as previous_exchange, so a follow-up such as "can you say
that in Telugu?" can build on them. They stay in memory only: message text is never written to disk.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import templates
from .config import Settings
from .facts import network_facts
from .incidents import Incident
from .store import Store
from .writer import Draft, Writer

HELP_WORDS = {"help", "hi", "hello", "start", "?", "సహాయం", "హాయ్", "मदद", "नमस्ते"}
STATUS_WORDS = {"status", "stat", "s", "స్థితి", "స్టేటస్", "स्थिति", "स्टेटस"}
FOLLOW_UP_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class Exchange:
    question: str
    answer: str
    at: datetime


class Assistant:
    def __init__(
        self,
        *,
        store: Store,
        settings: Settings,
        writer: Writer,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.writer = writer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last: dict[str, Exchange] = {}
        self._lock = threading.Lock()

    def reply(self, question: str, chat: str = "") -> Draft:
        """Answer `question`. In a `chat`, the chat's last exchange, if recent, comes along as context."""
        draft = self._answer(question, self._recent(chat))
        if chat:
            with self._lock:
                self._last[chat] = Exchange(question, draft.text, self._clock())
        return draft

    def _recent(self, chat: str) -> Exchange | None:
        with self._lock:
            exchange = self._last.get(chat) if chat else None
        if exchange is None or self._clock() - exchange.at > FOLLOW_UP_WINDOW:
            return None
        return exchange

    def _answer(self, question: str, earlier: Exchange | None) -> Draft:
        language = templates.detect_language(question) or self.settings.language
        command = re.sub(r"[^\w?]+", " ", question.lower()).strip()
        if command in HELP_WORDS or not command:
            return Draft(templates.text(language, "help"), "template")
        snapshot = self.store.load_snapshot()
        if snapshot is None:
            return Draft(templates.text(language, "no_data"), "template")
        state = self.store.load_state().get("tracker", {})
        open_incidents = [Incident.from_dict(i) for i in state.get("open", ())]
        recent = [Incident.from_dict(i) for i in state.get("closed", ())]
        facts = network_facts(snapshot, open_incidents, recent, self.settings, question)
        summary = templates.render_status(facts, language)
        if command in STATUS_WORDS:
            return Draft(summary, "template")
        fallback = templates.text(language, "no_ai") + "\n" + summary
        last_alert = self.store.load_last_alert()
        if last_alert is not None:
            facts["last_alert"] = last_alert
        if earlier:
            facts["previous_exchange"] = {"question": earlier.question, "answer": earlier.answer}
        context = (earlier.question, earlier.answer) if earlier else None
        return self.writer.answer(question, facts, fallback, language, earlier=context)
