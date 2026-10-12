"""Tools the agent can call. Each call is validated with pydantic before it runs."""

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from .store import DEPARTMENTS, TICKET_TYPES, Store
from .units import Inventory

Category = Literal["سكني", "تجاري", "إداري"]
Urgency = Literal["عادي", "عاجل", "طارئ"]


def _nullable(schema: dict) -> dict:
    return {**schema, "type": [schema["type"], "null"]}


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool(
        "search_units",
        "يبحث في الوحدات الشاغرة المتاحة للإيجار. استخدمه قبل ما تذكر أي وحدة أو سعر. "
        "كل الفلاتر اختيارية؛ أرسل null للي ما حدده العميل.",
        {
            "category": _nullable({"type": "string", "enum": ["سكني", "تجاري", "إداري"]}),
            "district": _nullable({"type": "string", "description": "اسم الحي، مثل: الروضة"}),
            "max_annual_rent": _nullable({"type": "number", "description": "أعلى إيجار سنوي بالريال"}),
            "min_rooms": _nullable({"type": "integer", "description": "أقل عدد غرف نوم"}),
        },
    ),
    _tool(
        "create_ticket",
        "يسجّل طلب أو شكوى أو مقترح ويحوّله للقسم المختص، ويرجع رقم التذكرة. "
        "سجّل تذكرة وحدة لكل موضوع، بعد ما تجمع الاسم ورقم الوحدة والتفاصيل الكافية.",
        {
            "ticket_type": {"type": "string", "enum": TICKET_TYPES},
            "department": {"type": "string", "enum": DEPARTMENTS},
            "urgency": {
                "type": "string",
                "enum": ["عادي", "عاجل", "طارئ"],
                "description": "طارئ: خطر على الناس أو الممتلكات (تسرب غاز، حريق، تماس كهربائي، تسرب ماء كبير). "
                "عاجل: يعطّل السكن (لا ماء، لا كهرباء، مكيف خربان في الصيف، باب ما ينقفل). غير كذا: عادي.",
            },
            "summary": {"type": "string", "description": "ملخص واضح للموظف: وش المشكلة أو الطلب بالتفصيل"},
            "customer_name": _nullable({"type": "string"}),
            "unit_ref": _nullable({"type": "string", "description": "اسم العمارة أو الحي ورقم الوحدة"}),
        },
    ),
    _tool(
        "check_my_tickets",
        "يعرض آخر التذاكر المسجلة لرقم العميل الحالي وحالتها.",
        {},
    ),
    _tool(
        "handoff_to_staff",
        "يحوّل المحادثة لموظف بشري ويوقف ردودك الآلية على هذا العميل لين يرجّعك الموظف.",
        {"reason": {"type": "string", "description": "سبب التحويل باختصار للموظف"}},
    ),
]


class SearchUnitsInput(BaseModel):
    category: Category | None
    district: str | None
    max_annual_rent: float | None
    min_rooms: int | None


class CreateTicketInput(BaseModel):
    ticket_type: Literal[tuple(TICKET_TYPES)]  # type: ignore[valid-type]
    department: Literal[tuple(DEPARTMENTS)]  # type: ignore[valid-type]
    urgency: Urgency
    summary: str
    customer_name: str | None
    unit_ref: str | None


class HandoffInput(BaseModel):
    reason: str


UNIT_FIELDS = (
    "category", "building", "district", "unit_no", "floor", "rooms", "halls", "bathrooms", "ac",
    "kitchen", "area_m2", "annual_rent", "monthly_rent", "insurance", "services_fee", "commission",
    "payments", "notes", "ad_link",
)


@dataclass
class ToolContext:
    phone: str
    conversation_id: int
    store: Store
    inventory: Inventory
    handed_off: bool = False


def run_tool(name: str, raw_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Run one tool. Returns (result text, is_error)."""
    try:
        if name == "search_units":
            args = SearchUnitsInput.model_validate(raw_input)
            units = ctx.inventory.search(**args.model_dump())
            if not units:
                return json.dumps(
                    {"results": [], "available_districts": ctx.inventory.districts()}, ensure_ascii=False
                ), False
            slim = [{k: u.get(k) for k in UNIT_FIELDS if u.get(k) not in (None, "")} for u in units]
            return json.dumps({"results": slim}, ensure_ascii=False), False

        if name == "create_ticket":
            args = CreateTicketInput.model_validate(raw_input)
            ticket_id = ctx.store.create_ticket(phone=ctx.phone, **args.model_dump())
            return json.dumps({"ticket_id": ticket_id, "department": args.department}, ensure_ascii=False), False

        if name == "check_my_tickets":
            tickets = ctx.store.tickets_for_phone(ctx.phone)
            slim = [
                {k: t[k] for k in ("id", "created_at", "ticket_type", "department", "status", "summary", "staff_note")}
                for t in tickets
            ]
            return json.dumps({"tickets": slim}, ensure_ascii=False), False

        if name == "handoff_to_staff":
            args = HandoffInput.model_validate(raw_input)
            ctx.store.set_handoff(ctx.conversation_id, True, args.reason)
            ctx.handed_off = True
            return json.dumps({"ok": True}), False

        return f"unknown tool: {name}", True
    except ValidationError as e:
        return f"INVALID_INPUT: {e.errors(include_url=False)}", True
