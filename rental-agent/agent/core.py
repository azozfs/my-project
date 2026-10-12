"""The agent loop: one customer message in, one WhatsApp reply out."""

import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import anthropic

from .config import Settings
from .prompt import build_system_prompt
from .store import Store
from .tools import TOOLS, ToolContext, run_tool
from .units import Inventory

log = logging.getLogger(__name__)

MAX_STEPS = 6
RIYADH = timezone(timedelta(hours=3))

SORRY_TEXT = "المعذرة، صار عندي خلل بسيط. حوّلت محادثتك لأحد موظفي قسم التأجير وبيرد عليك في أقرب وقت 🙏"
NON_TEXT_TEXT = "[أرسل العميل {kind} — الوكيل يقرأ الرسائل النصية فقط حاليًا]"


def _echo_content(content: list) -> list[dict]:
    """Assistant content to store and send back next turn.

    After a mid-output server-side fallback, only text blocks before the last
    `fallback` marker are kept; everything after it is echoed as-is.
    """
    blocks = [b.model_dump(exclude_none=True) for b in content]
    cut = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if cut is None:
        return blocks
    return [b for b in blocks[:cut] if b.get("type") == "text"] + blocks[cut + 1 :]


def _text_of(content: list) -> str:
    return "\n\n".join(b.text for b in content if b.type == "text" and b.text.strip()).strip()


class RentalAgent:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None, store: Store | None = None):
        self.settings = settings
        self.client = client or anthropic.Anthropic()
        self.store = store or Store(settings.db_path)
        self.inventory = Inventory(settings.units_path)
        self.system = build_system_prompt(settings.policies_path)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def _call(self, messages: list):
        return self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=8000,
            system=self.system,
            tools=TOOLS,
            messages=messages,
            cache_control={"type": "ephemeral"},
            output_config={"effort": self.settings.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def handle_message(self, phone: str, text: str, profile_name: str | None = None) -> str | None:
        """Process one incoming message. Returns the reply, or None when staff own the chat."""
        with self._locks[phone]:
            return self._handle(phone, text, profile_name)

    def record_staff_reply(self, phone: str, text: str) -> None:
        """Keep a staff member's reply in the history so the agent has it after resuming."""
        with self._locks[phone]:
            conv = self.store.current_conversation(phone, self.settings.session_gap_hours)
            messages = conv["messages"]
            if not messages:
                messages.append({"role": "user", "content": "<context>بداية محادثة من موظف.</context>"})
            messages.append({"role": "assistant", "content": [{"type": "text", "text": f"[رد موظف التأجير]: {text}"}]})
            self.store.save_messages(conv["id"], messages)

    def _handle(self, phone: str, text: str, profile_name: str | None) -> str | None:
        conv = self.store.current_conversation(phone, self.settings.session_gap_hours)
        messages = conv["messages"]

        now = datetime.now(RIYADH).strftime("%Y-%m-%d %H:%M (%A)")
        context = f"<context>وقت الرسالة: {now} بتوقيت السعودية."
        if profile_name:
            context += f" اسم العميل في الواتساب: {profile_name}."
        context += "</context>"
        clean = re.sub(r"</?context>", "", text)
        messages.append({"role": "user", "content": [{"type": "text", "text": context}, {"type": "text", "text": clean}]})

        if conv["handed_off"]:
            # A human owns this chat; keep the message on record for when the bot resumes.
            self.store.save_messages(conv["id"], messages)
            return None

        ctx = ToolContext(phone=phone, conversation_id=conv["id"], store=self.store, inventory=self.inventory)
        reply = ""
        try:
            for _ in range(MAX_STEPS):
                response = self._call(messages)
                if response.stop_reason == "refusal":
                    log.warning("refusal for %s: %s", phone, response.stop_details)
                    reply = ""
                    break
                messages.append({"role": "assistant", "content": _echo_content(response.content)})
                reply = _text_of(response.content) or reply
                if response.stop_reason == "pause_turn":
                    continue
                if response.stop_reason != "tool_use":
                    break
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output, is_error = run_tool(block.name, block.input, ctx)
                    log.info("tool %s -> %s", block.name, output[:200])
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
                    )
                messages.append({"role": "user", "content": results})
        except anthropic.APIError:
            log.exception("Claude API error for %s", phone)
            reply = ""
        finally:
            self.store.save_messages(conv["id"], messages)

        if not reply:
            self.store.set_handoff(conv["id"], True, "خلل تقني في الوكيل")
            return SORRY_TEXT
        return reply
