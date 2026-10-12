"""Web server: WhatsApp webhook + a simple staff dashboard for tickets.

Run:  uvicorn agent.server:app --host 0.0.0.0 --port 8000
"""

import html
import json
import logging
import secrets
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .config import get_settings
from .core import NON_TEXT_TEXT, RentalAgent
from .store import DEPARTMENTS, TICKET_STATUSES
from .whatsapp import parse_webhook, send_text, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

settings = get_settings()
app = FastAPI(title="Atwar rental WhatsApp agent")
_agent: RentalAgent | None = None


def agent() -> RentalAgent:
    global _agent
    if _agent is None:
        _agent = RentalAgent(settings)
    return _agent


# --- WhatsApp webhook ---


@app.get("/webhook")
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    if mode == "subscribe" and settings.wa_verify_token and secrets.compare_digest(token, settings.wa_verify_token):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not verify_signature(settings.wa_app_secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401)
    for msg in parse_webhook(json.loads(body or b"{}")):
        if agent().store.mark_processed(msg.message_id):
            background.add_task(_process, msg.phone, msg.kind, msg.text, msg.profile_name)
    # Answer Meta right away; replies are sent from the background task.
    return {"ok": True}


def _process(phone: str, kind: str, text: str | None, profile_name: str | None) -> None:
    if not text:
        text = NON_TEXT_TEXT.format(kind=kind)
    try:
        reply = agent().handle_message(phone, text, profile_name)
        if reply:
            send_text(settings.wa_token, settings.wa_phone_number_id, settings.graph_api_version, phone, reply)
    except Exception:
        log.exception("failed to handle message from %s", phone)


# --- Staff dashboard ---


def _check_admin(token: str) -> None:
    if not settings.admin_token or not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=403)


_STYLE = """
body{font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#1d2330}
h1{font-size:20px} h2{font-size:16px;margin-top:28px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:12px;margin:10px 0}
.meta{color:#5b6472;font-size:13px} .tag{display:inline-block;padding:2px 8px;border-radius:99px;background:#eef1f5;font-size:12px;margin-left:4px}
.طارئ{background:#fde2e1;color:#a4161a}.عاجل{background:#fff1d6;color:#8a5a00}
form.inline{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px} select,input,button{font:inherit;padding:6px 8px;border-radius:6px;border:1px solid #cfd5dc}
button{background:#2f5bea;color:#fff;border:0} a{color:#2f5bea}
"""


@app.get("/admin", response_class=HTMLResponse)
def dashboard(token: str = "", status: str = "", department: str = ""):
    _check_admin(token)
    store = agent().store
    tickets = store.list_tickets(status or None, department or None)
    handed = store.handed_off_conversations()
    e = html.escape

    def opts(values, current):
        return "".join(f'<option value="{e(v)}"{" selected" if v == current else ""}>{e(v)}</option>' for v in values)

    parts = [
        f"<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>تذاكر التأجير</title>"
        f"<style>{_STYLE}</style><body><h1>تذاكر وكيل التأجير</h1>",
        f"<form class='inline' method='get'><input type='hidden' name='token' value='{e(token)}'>"
        f"<select name='status'><option value=''>كل الحالات</option>{opts(TICKET_STATUSES, status)}</select>"
        f"<select name='department'><option value=''>كل الأقسام</option>{opts(DEPARTMENTS, department)}</select>"
        f"<button>عرض</button></form>",
    ]

    parts.append(f"<h2>محادثات محوّلة لموظف ({len(handed)})</h2>")
    for c in handed:
        parts.append(
            f"<div class='card'><b dir='ltr'>+{e(c['phone'])}</b> <span class='meta'>{e(c['last_activity'])}</span>"
            f"<div>{e(c['handoff_reason'] or '')}</div>"
            f"<form class='inline' method='post' action='/admin/resume'>"
            f"<input type='hidden' name='token' value='{e(token)}'><input type='hidden' name='phone' value='{e(c['phone'])}'>"
            f"<button>رجّع المحادثة للوكيل</button></form>"
            f"<form class='inline' method='post' action='/admin/reply'>"
            f"<input type='hidden' name='token' value='{e(token)}'><input type='hidden' name='phone' value='{e(c['phone'])}'>"
            f"<input name='text' placeholder='اكتب ردك للعميل' required style='flex:1;min-width:200px'>"
            f"<button>إرسال</button></form></div>"
        )

    parts.append(f"<h2>التذاكر ({len(tickets)})</h2>")
    for t in tickets:
        parts.append(
            f"<div class='card'><b>#{t['id']}</b> <span class='tag'>{e(t['ticket_type'])}</span>"
            f"<span class='tag'>{e(t['department'])}</span><span class='tag {e(t['urgency'])}'>{e(t['urgency'])}</span>"
            f"<span class='tag'>{e(t['status'])}</span>"
            f"<div class='meta'>{e(t['customer_name'] or 'بدون اسم')} · <span dir='ltr'>+{e(t['phone'])}</span>"
            f" · {e(t['unit_ref'] or '')} · {e(t['created_at'])}</div>"
            f"<p>{e(t['summary'])}</p>"
            f"<form class='inline' method='post' action='/admin/ticket'>"
            f"<input type='hidden' name='token' value='{e(token)}'><input type='hidden' name='ticket_id' value='{t['id']}'>"
            f"<select name='status'>{opts(TICKET_STATUSES, t['status'])}</select>"
            f"<input name='staff_note' placeholder='ملاحظة للعميل (يشوفها الوكيل)' value='{e(t['staff_note'] or '')}'>"
            f"<button>حفظ</button></form></div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


@app.post("/admin/ticket")
def update_ticket(token: str = Form(...), ticket_id: int = Form(...), status: str = Form(...), staff_note: str = Form("")):
    _check_admin(token)
    if not agent().store.update_ticket(ticket_id, status, staff_note or None):
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/admin?token={quote(token)}", status_code=303)


@app.post("/admin/resume")
def resume(token: str = Form(...), phone: str = Form(...)):
    _check_admin(token)
    agent().store.resume_bot(phone)
    return RedirectResponse(f"/admin?token={quote(token)}", status_code=303)


@app.post("/admin/reply")
def staff_reply(token: str = Form(...), phone: str = Form(...), text: str = Form(...)):
    """A staff member answers a handed-off customer from the dashboard."""
    _check_admin(token)
    send_text(settings.wa_token, settings.wa_phone_number_id, settings.graph_api_version, phone, text)
    agent().record_staff_reply(phone, text)
    return RedirectResponse(f"/admin?token={quote(token)}", status_code=303)


@app.get("/health")
def health():
    return {"ok": True}
