"""Answers volunteers' questions ("what is down?", "is Daniel5 up?") from Uplink's latest state."""

from __future__ import annotations

import re

from . import templates
from .config import Settings
from .facts import network_facts
from .incidents import Incident
from .store import Store
from .writer import Draft, Writer

HELP_WORDS = {"help", "hi", "hello", "start", "?", "సహాయం", "హాయ్", "मदद", "नमस्ते"}
STATUS_WORDS = {"status", "stat", "s", "స్థితి", "స్టేటస్", "स्थिति", "स्टेटस"}


class Assistant:
    def __init__(self, *, store: Store, settings: Settings, writer: Writer) -> None:
        self.store = store
        self.settings = settings
        self.writer = writer

    def reply(self, question: str) -> Draft:
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
        return self.writer.answer(question, facts, fallback, language)
