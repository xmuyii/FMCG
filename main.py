"""
WhatsApp -> marketplace bot, built against GOWA (go-whatsapp-web-multidevice),
a self-hosted open-source WhatsApp REST API + webhook server:
https://github.com/aldinokemal/go-whatsapp-web-multidevice

Architecture: GOWA links to WhatsApp Web once via QR code and runs headless
(no phone needed after that). When a message arrives, GOWA POSTs a webhook
to this app's /webhook endpoint. Unlike a simple auto-reply app, GOWA does
NOT treat the HTTP response of that webhook as a reply -- this app must make
a SEPARATE call back to GOWA's own REST API (/send/message) to actually
message the user back. That round trip is handled by send_whatsapp_message()
below.

GOWA's documented webhook envelope looks like:

    {
      "event": "message",
      "device_id": "628987654321@s.whatsapp.net",
      "payload": {
        "id": "...",
        "chat_id": "628123456789@s.whatsapp.net",
        "from": "628123456789@s.whatsapp.net",
        "from_name": "John Doe",
        "timestamp": "...",
        "body": "SELL Rice, 50kg, 20000",
        "image": "https://.../file.jpg"   # or {"url": ...} / {"path": ..., "caption": ...}
                                            # depending on WHATSAPP_AUTO_DOWNLOAD_MEDIA
      }
    }

Confirm the exact "image" shape against docs/webhook-payload.md once GOWA is
deployed -- get_incoming_media_url() below is the one place to adjust if it
differs. Set WHATSAPP_AUTO_DOWNLOAD_MEDIA=false in GOWA's config so "image"
is a fetchable URL rather than a local file path on GOWA's own container.
"""

import io
import os
import re
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
IMAGES_BUCKET = os.environ.get("SUPABASE_IMAGES_BUCKET", "listing-images")
IMAGE_SIZE = int(os.environ.get("LISTING_IMAGE_SIZE", "1000"))
MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "10"))

GOWA_BASE_URL = os.environ["GOWA_BASE_URL"]  # e.g. https://your-gowa.up.railway.app
GOWA_BASIC_AUTH_USER = os.environ["GOWA_BASIC_AUTH_USER"]
GOWA_BASIC_AUTH_PASS = os.environ["GOWA_BASIC_AUTH_PASS"]

# Admins now live in the `admins` table (with roles), not a fixed list --
# this lets a super admin add/remove other admins from WhatsApp itself.
# SUPER_ADMIN_PHONES only matters on first boot: any phone listed here is
# seeded as a super admin if the admins table doesn't already have them,
# solving the chicken-and-egg problem of needing an admin to create the
# first admin. Safe to leave set permanently -- it only ever adds, never
# overrides a role someone has already been given via ADD ADMIN.
SUPER_ADMIN_PHONES = {
    p.strip() for p in os.environ.get("SUPER_ADMIN_PHONES", "").split(",") if p.strip()
}

ADMIN_ROLES = {"super", "verifier"}

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI()


@app.on_event("startup")
async def bootstrap_admins():
    for phone in SUPER_ADMIN_PHONES:
        existing = supabase.table("admins").select("*").eq("phone", phone).execute()
        if not existing.data:
            supabase.table("admins").insert({"phone": phone, "role": "super"}).execute()


SELL_RE = re.compile(r"^\s*SELL\b(.*)$", re.IGNORECASE | re.DOTALL)
LISTING_REF_RE = re.compile(r"#\s*(\d+)")
HELP_RE = re.compile(r"^\s*HELP\s*$", re.IGNORECASE)
EDIT_RE = re.compile(r"^\s*EDIT\s+#?(\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
APPLICATION_HINT_RE = re.compile(r"NAME\s*:", re.IGNORECASE)

# Admin-only commands
APPROVE_SELLER_RE = re.compile(r"^\s*APPROVE\s+(\d{6,})\s*$", re.IGNORECASE)
REJECT_SELLER_RE = re.compile(r"^\s*REJECT\s+(\d{6,})\s*$", re.IGNORECASE)
APPROVE_EDIT_RE = re.compile(r"^\s*APPROVE\s+EDIT\s+#?(\d+)\s*$", re.IGNORECASE)
REJECT_EDIT_RE = re.compile(r"^\s*REJECT\s+EDIT\s+#?(\d+)\s*$", re.IGNORECASE)
REMOVE_RE = re.compile(r"^\s*REMOVE\s+#?(\d+)\s*$", re.IGNORECASE)
APPLICATIONS_RE = re.compile(r"^\s*(APPLICATIONS|PENDING)\s*$", re.IGNORECASE)

# Super-admin-only: managing other admins
ADD_ADMIN_RE = re.compile(r"^\s*ADD\s+ADMIN\s+(\d{6,})\s+(SUPER|VERIFIER)\s*$", re.IGNORECASE)
REMOVE_ADMIN_RE = re.compile(r"^\s*REMOVE\s+ADMIN\s+(\d{6,})\s*$", re.IGNORECASE)
ADMINS_RE = re.compile(r"^\s*ADMINS\s*$", re.IGNORECASE)

# Category management (super admin only to add/remove; anyone can list)
ADD_CATEGORY_RE = re.compile(r"^\s*ADD\s+CATEGORY\s+(.+)$", re.IGNORECASE)
REMOVE_CATEGORY_RE = re.compile(r"^\s*REMOVE\s+CATEGORY\s+(.+)$", re.IGNORECASE)
CATEGORIES_LIST_RE = re.compile(r"^\s*CATEGORIES\s*$", re.IGNORECASE)
UNCATEGORIZED = "Uncategorized"

# Seller performance tracking (part of ongoing lifecycle management, not just
# onboarding) -- either admin role can rate, matching who can vet sellers.
RATE_RE = re.compile(r"^\s*RATE\s+(\d{6,})\s+(.+)$", re.IGNORECASE | re.DOTALL)
SELLER_INFO_RE = re.compile(r"^\s*SELLER\s+(\d{6,})\s*$", re.IGNORECASE)

FORM_TEMPLATE = (
    "Welcome! Before you can start selling, reply with your details in "
    "exactly this format (fill in the blanks):\n\n"
    "NAME: \n"
    "BUSINESS: \n"
    "MARKET: \n"
    "YEARS: \n"
    "CATEGORIES: \n\n"
    "Example:\n"
    "NAME: Musa Ibrahim\n"
    "BUSINESS: Ibrahim Grains\n"
    "MARKET: Sabo market\n"
    "YEARS: 5\n"
    "CATEGORIES: Grains, produce"
)

APPLICATION_FIELD_PATTERNS = {
    "full_name": re.compile(r"NAME\s*:\s*(.+)", re.IGNORECASE),
    "business_name": re.compile(r"BUSINESS\s*:\s*(.+)", re.IGNORECASE),
    "market_location": re.compile(r"MARKET\s*:\s*(.+)", re.IGNORECASE),
    "years_trading": re.compile(r"YEARS\s*:\s*(.+)", re.IGNORECASE),
    "categories": re.compile(r"CATEGORIES\s*:\s*(.+)", re.IGNORECASE),
}
FIELD_LABELS = {
    "full_name": "NAME", "business_name": "BUSINESS", "market_location": "MARKET",
    "years_trading": "YEARS", "categories": "CATEGORIES",
}

HELP_TEXT = (
    "Here's how this works:\n\n"
    "SELLERS\n"
    "- New here? Text SELL to start registration.\n"
    "- Once approved: send a photo with caption\n"
    "  SELL <item>, <quantity>, <price>, <category>, <area>\n"
    "- To change a listing: EDIT #<id> field=value\n"
    "  e.g. EDIT #42 price=25000, quantity=45kg\n"
    "  Changes need approval before they go live.\n\n"
    "BUYERS\n"
    "- Mention a listing's # number to ask about it.\n\n"
    "Text HELP any time to see this again."
)

EDITABLE_FIELDS = {"item", "quantity", "price", "category", "area"}


# ---------------------------------------------------------------------------
# GOWA adapters -- adjust get_incoming_media_url() if the real "image" field
# shape differs from what's documented (see module docstring above).
# ---------------------------------------------------------------------------

def get_incoming_text(payload: dict) -> str:
    message = payload.get("payload", {})
    return (message.get("body") or "").strip()


def get_incoming_sender(payload: dict) -> str:
    """Returns the WhatsApp JID (e.g. '628123456789@s.whatsapp.net'), which
    doubles as the chat_id GOWA expects when we send a reply."""
    message = payload.get("payload", {})
    return (message.get("chat_id") or message.get("from") or "").strip()


def get_incoming_media_url(payload: dict) -> str | None:
    image = payload.get("payload", {}).get("image")
    if not image:
        return None
    if isinstance(image, str):
        return image if image.startswith("http") else None
    if isinstance(image, dict):
        return image.get("url")
    return None


def phone_from_jid(jid: str) -> str:
    """'628123456789@s.whatsapp.net' -> '628123456789', for storage/display."""
    return jid.split("@")[0]


def send_whatsapp_message(chat_id: str, text: str) -> None:
    try:
        resp = requests.post(
            f"{GOWA_BASE_URL}/send/message",
            json={"phone": chat_id, "message": text},
            auth=(GOWA_BASIC_AUTH_USER, GOWA_BASIC_AUTH_PASS),
            timeout=15,
        )
        if not resp.ok:
            print(f"[send_whatsapp_message] GOWA returned {resp.status_code}: {resp.text[:300]}")
    except requests.RequestException as exc:
        print(f"[send_whatsapp_message] failed to reach GOWA at {GOWA_BASE_URL}: {exc}")


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

def process_listing_image(source_url: str) -> bytes:
    """
    Download an image and return JPEG bytes cropped to a centered square of
    IMAGE_SIZE x IMAGE_SIZE, regardless of the original's dimensions or
    aspect ratio, so every listing looks visually uniform on the site.
    """
    resp = requests.get(source_url, timeout=20)
    resp.raise_for_status()

    if len(resp.content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"Image exceeds {MAX_UPLOAD_MB}MB limit")

    img = Image.open(io.BytesIO(resp.content))
    img = ImageOps.exif_transpose(img)  # respect phone camera orientation
    img = img.convert("RGB")

    # Center-crop to square, then resize to the canonical size.
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue()


def upload_listing_image(listing_id: int, image_bytes: bytes) -> str:
    path = f"{listing_id}.jpg"
    supabase.storage.from_(IMAGES_BUCKET).upload(
        path,
        image_bytes,
        {"content-type": "image/jpeg", "upsert": "true"},
    )
    return supabase.storage.from_(IMAGES_BUCKET).get_public_url(path)


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

def get_seller_by_phone(phone: str) -> dict | None:
    result = supabase.table("sellers").select("*").eq("phone", phone).execute()
    return result.data[0] if result.data else None


def get_admin_role(phone: str) -> str | None:
    result = supabase.table("admins").select("*").eq("phone", phone).execute()
    return result.data[0]["role"] if result.data else None


def handle_add_admin_command(sender_jid: str, phone_to_add: str, role: str) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) != "super":
        return "Sorry, that command isn't available."

    role = role.lower()
    existing = supabase.table("admins").select("*").eq("phone", phone_to_add).execute()
    if existing.data:
        supabase.table("admins").update({"role": role}).eq("phone", phone_to_add).execute()
        return f"Updated {phone_to_add} to {role} admin."

    supabase.table("admins").insert({
        "phone": phone_to_add, "role": role, "added_by": phone_from_jid(sender_jid),
    }).execute()
    return f"Added {phone_to_add} as a {role} admin."


def handle_remove_admin_command(sender_jid: str, phone_to_remove: str) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) != "super":
        return "Sorry, that command isn't available."
    if phone_to_remove == phone_from_jid(sender_jid):
        return "You can't remove yourself as admin."

    existing = supabase.table("admins").select("*").eq("phone", phone_to_remove).execute()
    if not existing.data:
        return f"{phone_to_remove} isn't an admin."

    supabase.table("admins").delete().eq("phone", phone_to_remove).execute()
    return f"Removed {phone_to_remove} as admin."


def handle_list_admins_command(sender_jid: str) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) != "super":
        return "Sorry, that command isn't available."

    admins = supabase.table("admins").select("*").order("created_at").execute().data
    if not admins:
        return "No admins configured."

    lines = ["Admins:"]
    for a in admins:
        lines.append(f"- {a['phone']} ({a['role']})")
    lines.append(
        "\nAdd with: ADD ADMIN <phone> SUPER|VERIFIER"
        "\nRemove with: REMOVE ADMIN <phone>"
    )
    return "\n".join(lines)


def create_new_contact(phone: str) -> dict:
    return supabase.table("sellers").insert({
        "phone": phone, "approved": False, "application_status": "awaiting_form",
    }).execute().data[0]


def parse_application(message: str) -> dict:
    parsed = {}
    for field, pattern in APPLICATION_FIELD_PATTERNS.items():
        match = pattern.search(message)
        if match and match.group(1).strip():
            parsed[field] = match.group(1).strip()
    return parsed


def handle_application_submission(sender_jid: str, message: str) -> str:
    seller = get_seller_by_phone(phone_from_jid(sender_jid))
    parsed = parse_application(message)
    missing = [label for key, label in FIELD_LABELS.items() if key not in parsed]

    if missing:
        return (
            f"A few details are missing: {', '.join(missing)}.\n"
            f"Please resend the full form:\n\n{FORM_TEMPLATE}"
        )

    supabase.table("sellers").update({
        **parsed, "application_status": "submitted",
    }).eq("id", seller["id"]).execute()

    return "Thanks! Your application has been submitted for review. We'll let you know once it's approved."


def handle_sell_command(sender_jid: str, rest_of_message: str, media_url: str | None) -> str:
    phone = phone_from_jid(sender_jid)
    seller = get_seller_by_phone(phone)

    if seller is None:
        create_new_contact(phone)
        return FORM_TEMPLATE
    if seller["application_status"] == "awaiting_form":
        return f"Please complete your registration first:\n\n{FORM_TEMPLATE}"
    if seller["application_status"] == "submitted":
        return "Your application is still under review — we'll notify you once it's approved."
    if seller["application_status"] == "rejected":
        return f"Your previous application wasn't approved. Reply with the form below to reapply:\n\n{FORM_TEMPLATE}"

    # From here on, seller["application_status"] == "approved".
    parts = [p.strip() for p in rest_of_message.split(",")]
    if len(parts) != 5:
        return (
            "To list an item, send a photo with this caption:\n"
            "SELL <item>, <quantity>, <price>, <category>, <area>\n"
            "Example: SELL Rice, 50kg, 20000, Grains, Sabo market"
        )

    item, quantity, price_raw, category, area = parts
    price_digits = re.sub(r"[^\d.]", "", price_raw)
    if not price_digits:
        return "I couldn't read the price. Please end with a plain number, e.g. 20000."

    listing = supabase.table("listings").insert({
        "seller_id": seller["id"],
        "item": item,
        "quantity": quantity,
        "price": float(price_digits),
        "category": category,
        "area": area,
        "status": "pending_image",
    }).execute().data[0]

    listing_id = listing["id"]

    if not media_url:
        return (
            f"Got the details for '{item}' (#{listing_id}), but I need a photo "
            f"before it can go live. Please resend as an image with caption: #{listing_id}"
        )

    try:
        processed = process_listing_image(media_url)
        image_url = upload_listing_image(listing_id, processed)
        supabase.table("listings").update({
            "image_url": image_url,
            "status": "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", listing_id).execute()
    except Exception:
        return f"Saved '{item}' (#{listing_id}) but couldn't process that photo. Resend it alone with caption #{listing_id}"

    return f"Listed: {item} - {quantity} - \u20a6{float(price_digits):,.0f} (#{listing_id}). It's live on the site!"


def format_field_value(field: str, value) -> str:
    if field == "price":
        return f"\u20a6{float(value):,.0f}"
    return str(value)


def handle_edit_command(sender_jid: str, listing_id: int, changes_raw: str) -> str:
    seller = get_seller_by_phone(phone_from_jid(sender_jid))
    if not seller or seller["application_status"] != "approved":
        return "Only approved sellers can edit listings."

    listing_res = supabase.table("listings").select("*").eq("id", listing_id).execute()
    if not listing_res.data:
        return f"I couldn't find listing #{listing_id}."
    listing = listing_res.data[0]

    if listing["seller_id"] != seller["id"]:
        return "You can only edit your own listings."

    changes = {}
    for pair in changes_raw.split(","):
        if "=" not in pair:
            continue
        field, value = pair.split("=", 1)
        field = field.strip().lower()
        value = value.strip()
        if field not in EDITABLE_FIELDS or not value:
            continue
        if field == "price":
            digits = re.sub(r"[^\d.]", "", value)
            if not digits:
                return "I couldn't read that price. Use a plain number, e.g. price=25000."
            value = digits
        changes[field] = value

    if not changes:
        return (
            "I couldn't read any changes. Format: EDIT #<id> field=value\n"
            f"Editable fields: {', '.join(sorted(EDITABLE_FIELDS))}\n"
            "Example: EDIT #42 price=25000, quantity=45kg"
        )

    supabase.table("listings").update({
        "pending_changes": changes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", listing_id).execute()

    diff_lines = [
        f"{field}: {format_field_value(field, listing.get(field))} \u2192 {format_field_value(field, new_value)}"
        for field, new_value in changes.items()
    ]
    diff = "\n".join(diff_lines)
    return (
        f"Change submitted for #{listing_id}:\n{diff}\n\n"
        f"It'll apply once approved — the live listing is unchanged until then."
    )


def handle_approve_edit_command(sender_jid: str, listing_id: int) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) != "super":
        return "Sorry, that command isn't available."

    listing_res = supabase.table("listings").select("*").eq("id", listing_id).execute()
    if not listing_res.data or not listing_res.data[0].get("pending_changes"):
        return f"No pending edit for #{listing_id}."

    listing = listing_res.data[0]
    changes = listing["pending_changes"]
    diff_lines = [
        f"{field}: {format_field_value(field, listing.get(field))} \u2192 {format_field_value(field, new_value)}"
        for field, new_value in changes.items()
    ]

    if "price" in changes:
        changes["price"] = float(changes["price"])

    supabase.table("listings").update({
        **changes, "pending_changes": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", listing_id).execute()

    return f"Edit approved for #{listing_id}:\n" + "\n".join(diff_lines)


def handle_reject_edit_command(sender_jid: str, listing_id: int) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) != "super":
        return "Sorry, that command isn't available."

    supabase.table("listings").update({"pending_changes": None}).eq("id", listing_id).execute()
    return f"Edit for #{listing_id} rejected — listing unchanged."


def handle_remove_command(sender_jid: str, listing_id: int) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) != "super":
        return "Sorry, that command isn't available."

    supabase.table("listings").update({
        "status": "removed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", listing_id).execute()
    return f"Removed #{listing_id}."


def handle_approve_seller_command(sender_jid: str, phone_to_approve: str) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) not in ("super", "verifier"):
        return "Sorry, that command isn't available."

    seller = get_seller_by_phone(phone_to_approve)
    if not seller:
        return f"No seller found with number {phone_to_approve}."

    supabase.table("sellers").update({
        "approved": True, "application_status": "approved",
    }).eq("id", seller["id"]).execute()

    send_whatsapp_message(
        f"{phone_to_approve}@s.whatsapp.net",
        "You're approved! You can now list items — text HELP to see how.",
    )
    return f"Approved {phone_to_approve}."


def handle_reject_seller_command(sender_jid: str, phone_to_reject: str) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) not in ("super", "verifier"):
        return "Sorry, that command isn't available."

    seller = get_seller_by_phone(phone_to_reject)
    if not seller:
        return f"No seller found with number {phone_to_reject}."

    supabase.table("sellers").update({"application_status": "rejected"}).eq("id", seller["id"]).execute()
    return f"Rejected {phone_to_reject}. They can text SELL to reapply."


def handle_applications_command(sender_jid: str) -> str:
    if get_admin_role(phone_from_jid(sender_jid)) not in ("super", "verifier"):
        return "Sorry, that command isn't available."

    submitted = (
        supabase.table("sellers").select("*").eq("application_status", "submitted")
        .order("created_at").execute().data
    )
    if not submitted:
        return "No applications waiting on review."

    lines = ["Applications waiting on review:"]
    for s in submitted:
        lines.append(
            f"\n{s['phone']}\n"
            f"  Name: {s.get('full_name', '-')}\n"
            f"  Business: {s.get('business_name', '-')}\n"
            f"  Market: {s.get('market_location', '-')}\n"
            f"  Years: {s.get('years_trading', '-')}\n"
            f"  Categories: {s.get('categories', '-')}"
        )
    lines.append("\nApprove with: APPROVE <phone>\nReject with: REJECT <phone>")
    return "\n".join(lines)


def handle_buyer_inquiry(sender_jid: str, message: str, listing_id: int) -> str:
    listing = supabase.table("listings").select("*").eq("id", listing_id).execute()
    if not listing.data:
        return f"I couldn't find listing #{listing_id} — it may have been removed."

    supabase.table("buyer_requests").insert({
        "buyer_phone": phone_from_jid(sender_jid),
        "listing_id": listing_id,
        "message": message,
    }).execute()

    item = listing.data[0]["item"]
    return f"Thanks! I've noted your interest in #{listing_id} ({item}). I'll follow up shortly."


def handle_pending_image_followup(sender_jid: str, message: str, media_url: str, listing_id: int) -> str:
    listing_res = supabase.table("listings").select("*").eq("id", listing_id).execute()
    if not listing_res.data or listing_res.data[0]["status"] != "pending_image":
        return f"Listing #{listing_id} isn't waiting on a photo."

    listing = listing_res.data[0]
    seller = get_seller_by_phone(phone_from_jid(sender_jid))
    if not seller or listing["seller_id"] != seller["id"]:
        return "You can only add a photo to your own listing."

    try:
        processed = process_listing_image(media_url)
        image_url = upload_listing_image(listing_id, processed)
        supabase.table("listings").update({
            "image_url": image_url,
            "status": "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", listing_id).execute()
    except Exception:
        return "Couldn't process that photo — please try resending it."

    return f"Photo added to #{listing_id}. It's now live on the site!"


# ---------------------------------------------------------------------------
# Webhook entrypoint
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()

    if payload.get("event") not in (None, "message"):
        # Ignore non-message events (delivery receipts, presence, etc.)
        return {"status": "ignored"}

    sender_jid = get_incoming_sender(payload)
    message = get_incoming_text(payload)
    media_url = get_incoming_media_url(payload)

    if not sender_jid:
        return {"status": "ignored"}

    sell_match = SELL_RE.match(message)
    edit_match = EDIT_RE.match(message)
    listing_ref_match = LISTING_REF_RE.search(message)

    add_admin_match = ADD_ADMIN_RE.match(message)
    remove_admin_match = REMOVE_ADMIN_RE.match(message)
    approve_edit_match = APPROVE_EDIT_RE.match(message)
    reject_edit_match = REJECT_EDIT_RE.match(message)
    remove_match = REMOVE_RE.match(message)
    approve_seller_match = APPROVE_SELLER_RE.match(message)
    reject_seller_match = REJECT_SELLER_RE.match(message)

    seller_for_application = None
    if APPLICATION_HINT_RE.search(message):
        seller_for_application = get_seller_by_phone(phone_from_jid(sender_jid))

    if add_admin_match:
        reply_text = handle_add_admin_command(sender_jid, add_admin_match.group(1), add_admin_match.group(2))
    elif remove_admin_match:
        reply_text = handle_remove_admin_command(sender_jid, remove_admin_match.group(1))
    elif ADMINS_RE.match(message):
        reply_text = handle_list_admins_command(sender_jid)
    elif approve_edit_match:
        reply_text = handle_approve_edit_command(sender_jid, int(approve_edit_match.group(1)))
    elif reject_edit_match:
        reply_text = handle_reject_edit_command(sender_jid, int(reject_edit_match.group(1)))
    elif remove_match:
        reply_text = handle_remove_command(sender_jid, int(remove_match.group(1)))
    elif approve_seller_match:
        reply_text = handle_approve_seller_command(sender_jid, approve_seller_match.group(1))
    elif reject_seller_match:
        reply_text = handle_reject_seller_command(sender_jid, reject_seller_match.group(1))
    elif APPLICATIONS_RE.match(message):
        reply_text = handle_applications_command(sender_jid)
    elif HELP_RE.match(message):
        reply_text = HELP_TEXT
    elif seller_for_application and seller_for_application["application_status"] in ("awaiting_form", "rejected"):
        reply_text = handle_application_submission(sender_jid, message)
    elif sell_match:
        reply_text = handle_sell_command(sender_jid, sell_match.group(1).strip(), media_url)
    elif edit_match:
        reply_text = handle_edit_command(sender_jid, int(edit_match.group(1)), edit_match.group(2))
    elif media_url and listing_ref_match:
        reply_text = handle_pending_image_followup(
            sender_jid, message, media_url, int(listing_ref_match.group(1))
        )
    elif listing_ref_match:
        reply_text = handle_buyer_inquiry(sender_jid, message, int(listing_ref_match.group(1)))
    else:
        reply_text = (
            "Hi! To list an item, text SELL to get started.\n"
            "To ask about something on the site, mention its # number.\n"
            "Text HELP any time for more."
        )

    send_whatsapp_message(sender_jid, reply_text)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}


# Serve the buyer-facing web app (webapp/index.html and its assets) at "/".
# This MUST be the last route registered -- Starlette matches routes in the
# order they're added, so /webhook and /health above still take priority
# over this catch-all mount.
app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")