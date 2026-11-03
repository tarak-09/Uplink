"""Muse Spark turns fact sheets into short WhatsApp messages; templates stand behind it.

The model never decides what is true. It receives facts that the correlator and tracker
already settled, writes them in the reader's language, and its draft is checked before it
is sent: no invented numbers, no links, a sane length. A draft that fails gets one rewrite;
after that the hand-written template goes out instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

from . import templates
from .muse import ModelAPIError

LANGUAGE_NAMES = {"english": "English", "telugu": "Telugu", "hindi": "Hindi"}
DEFAULT_MAX_CHARS = 700

STYLE_RULES = """\
Rules:
- Use only the facts given. Never add names, numbers, times, places or causes that are not in them.
- Copy node names exactly as written, in Latin letters, even when writing {language}.
- Plain WhatsApp text: *bold* is fine; no headings, tables, links or hashtags; at most one emoji.
- Stay under {max_chars} characters."""

PROMPTS = {
    "outage": """\
You write WhatsApp alerts for the volunteers who keep {network} running, a free community Wi-Fi mesh. \
The user message is a JSON fact sheet about one outage.

Write the alert in {language}. Start with one short line in *bold* that says what broke. Then two to four \
short lines: how many nodes and people are affected and since when, the most likely cause and the evidence \
for it, and the one thing to check first.

"""
    + STYLE_RULES,
    "resolved": """\
You write WhatsApp updates for the volunteers who keep {network} running, a free community Wi-Fi mesh. \
The user message is a JSON fact sheet about an outage that has just ended.

Write a short "back online" message in {language}: one line in *bold* saying what recovered, then one or two \
lines with how long it lasted, how many nodes are back, anything still down, and whether it looks like a \
firmware update.

"""
    + STYLE_RULES,
    "answer": """\
You are Uplink, a WhatsApp helper for the volunteers who run {network}, a free community Wi-Fi mesh. \
The next message is the network's current state as JSON; then comes the volunteer's question.

Answer from the JSON only. If the answer is not in it, say you don't know and suggest sending "status". \
Reply in the language of the question (English, Telugu or Hindi); if unsure, use {language}. Be brief and concrete.

"""
    + STYLE_RULES,
}


class ChatModel(Protocol):
    def chat(self, messages: Sequence[Mapping[str, str]]) -> str: ...


@dataclass(frozen=True)
class Draft:
    text: str
    source: Literal["muse", "template"]
    note: str = ""  # why the template was used


class Writer:
    def __init__(
        self, model: ChatModel | None, *, language: str, network: str, max_chars: int = DEFAULT_MAX_CHARS
    ) -> None:
        self.model = model
        self.language = language
        self.network = network
        self.max_chars = max_chars

    def write(self, facts: Mapping[str, Any]) -> Draft:
        """A message for an outage, a recovery, or a feed problem."""
        fallback = templates.render(facts, self.language)
        prompt = PROMPTS.get(facts["event"])
        if self.model is None or prompt is None:
            return Draft(fallback, "template", "" if prompt else "template-only message")
        messages = [
            {"role": "developer", "content": self._format(prompt, self.language)},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, indent=1)},
        ]
        return self._ask(messages, facts, fallback)

    def answer(self, question: str, state: Mapping[str, Any], fallback: str, language: str | None = None) -> Draft:
        """Answer a volunteer's question from the network state; `fallback` covers failures."""
        if self.model is None:
            return Draft(fallback, "template", "no model configured")
        messages = [
            {"role": "developer", "content": self._format(PROMPTS["answer"], language or self.language)},
            {"role": "developer", "content": "Network state:\n" + json.dumps(state, ensure_ascii=False, indent=1)},
            {"role": "user", "content": question},
        ]
        return self._ask(messages, {"state": state, "question": question}, fallback)

    def _ask(self, messages: list[dict[str, str]], facts: Mapping[str, Any], fallback: str) -> Draft:
        assert self.model is not None
        problem = ""
        for _ in range(2):
            try:
                draft = tidy(self.model.chat(messages))
            except ModelAPIError as exc:
                return Draft(fallback, "template", f"model unavailable: {exc}")
            problem = check_draft(draft, facts, self.max_chars)
            if not problem:
                return Draft(draft, "muse")
            messages = messages + [
                {"role": "assistant", "content": draft},
                {"role": "user", "content": f"That draft {problem}. Rewrite it and follow every rule."},
            ]
        return Draft(fallback, "template", f"model draft rejected: {problem}")

    def _format(self, prompt: str, language: str) -> str:
        return prompt.format(
            network=self.network, language=LANGUAGE_NAMES.get(language, "English"), max_chars=self.max_chars
        )


def tidy(draft: str) -> str:
    """Undo common markdown habits: **bold** becomes WhatsApp's *bold*, headings lose their #."""
    draft = draft.strip()
    if len(draft) >= 2 and draft[0] == draft[-1] == '"':
        draft = draft[1:-1].strip()
    draft = re.sub(r"\*\*(.+?)\*\*", r"*\1*", draft)
    draft = re.sub(r"(?m)^#{1,6}\s*", "", draft)
    return draft.strip()


def check_draft(draft: str, facts: Mapping[str, Any], max_chars: int) -> str:
    """Return what is wrong with a draft, or "" if it may be sent."""
    if not draft:
        return "was empty"
    if len(draft) > max_chars:
        return f"was {len(draft)} characters long; the limit is {max_chars}"
    if re.search(r"https?://", draft, re.IGNORECASE):
        return "contained a link"
    unknown = sorted(numbers_in(draft) - allowed_numbers(facts))
    if unknown:
        return "used numbers that are not in the facts: " + ", ".join(map(str, unknown[:5]))
    return ""


def numbers_in(text: str) -> set[int]:
    """Every number written in the text, in any script ("16:15", "1,234", "౧౬")."""
    joined = re.sub(r"(?<=\d)[,.](?=\d{3}\b)", "", text)
    return {int(match) for match in re.findall(r"\d+", joined)}


def allowed_numbers(facts: Mapping[str, Any]) -> set[int]:
    """Numbers the facts contain, plus the 12-hour form of each clock time."""
    serialized = json.dumps(facts, ensure_ascii=False)
    allowed = {int(match) for match in re.findall(r"\d+", serialized)}
    for hour in re.findall(r"\b(\d{1,2}):\d{2}\b", serialized):
        allowed.add(int(hour) % 12 or 12)
    return allowed
