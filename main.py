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

# Bare phone numbers (comma-separated, no "@s.whatsapp.net") allowed to run
# admin commands like APPROVE. This should just be the middleman's own
# number(s) -- e.g. "2348012345678,2348099999999".
ADMIN_PHONES = {
    p.strip() for p in os.environ.get("ADMIN_PHONES", "").split(",") if p.strip()
}

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI()

SELL_RE = re.compile(r"^\s*SELL\s+(.+)$", re.IGNORECASE)
LISTING_REF_RE = re.compile(r"#\s*(\d+)")
APPROVE_RE = re.compile(r"^\s*APPROVE\s+(\d+)\s*$", re.IGNORECASE)
PENDING_RE = re.compile(r"^\s*PENDING\s*$", re.IGNORECASE)


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
        requests.post(
            f"{GOWA_BASE_URL}/send/message",
            json={"phone": chat_id, "message": text},
            auth=(GOWA_BASIC_AUTH_USER, GOWA_BASIC_AUTH_PASS),
            timeout=15,
        )
    except requests.RequestException:
        # Reply delivery failing shouldn't crash webhook processing --
        # worth logging/alerting on in production.
        pass


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

def get_or_create_seller(phone: str) -> tuple[dict, bool]:
    """Returns (seller, is_newly_created)."""
    existing = supabase.table("sellers").select("*").eq("phone", phone).execute()
    if existing.data:
        return existing.data[0], False
    created = supabase.table("sellers").insert({"phone": phone, "approved": False}).execute()
    return created.data[0], True


def listing_status(seller_approved: bool, has_image: bool) -> str:
    if not seller_approved:
        return "pending_approval"
    return "active" if has_image else "pending_image"


def handle_sell_command(sender_jid: str, rest_of_message: str, media_url: str | None) -> str:
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

    seller, is_new = get_or_create_seller(phone_from_jid(sender_jid))

    status = listing_status(seller["approved"], has_image=False)
    listing = supabase.table("listings").insert({
        "seller_id": seller["id"],
        "item": item,
        "quantity": quantity,
        "price": float(price_digits),
        "category": category,
        "area": area,
        "status": status,
    }).execute().data[0]

    listing_id = listing["id"]
    image_note = ""

    if media_url:
        try:
            processed = process_listing_image(media_url)
            image_url = upload_listing_image(listing_id, processed)
            new_status = listing_status(seller["approved"], has_image=True)
            supabase.table("listings").update({
                "image_url": image_url,
                "status": new_status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", listing_id).execute()
            status = new_status
        except Exception:
            image_note = " (couldn't process the photo — resend it alone with caption " + f"#{listing_id})"

    if is_new or not seller["approved"]:
        return (
            f"Thanks! '{item}' (#{listing_id}) is saved{image_note}, but new sellers need to be "
            f"verified before listings go live. We'll confirm once that's done — usually the same day."
        )

    if status == "pending_image":
        return (
            f"Got the details for '{item}' (#{listing_id}), but I need a photo "
            f"before it can go live. Please resend as an image with caption: #{listing_id}"
        )

    return f"Listed: {item} - {quantity} - \u20a6{float(price_digits):,.0f} (#{listing_id}). It's live on the site!"


def handle_approve_command(sender_jid: str, phone_to_approve: str) -> str:
    if phone_from_jid(sender_jid) not in ADMIN_PHONES:
        return "Sorry, that command isn't available."

    existing = supabase.table("sellers").select("*").eq("phone", phone_to_approve).execute()
    if not existing.data:
        return f"No seller found with number {phone_to_approve}."

    seller = existing.data[0]
    supabase.table("sellers").update({"approved": True}).eq("id", seller["id"]).execute()

    # Anything they already listed with a photo can go live immediately.
    activated = supabase.table("listings").update({
        "status": "active",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("seller_id", seller["id"]).eq("status", "pending_approval").not_.is_("image_url", "null").execute()

    count = len(activated.data or [])
    return f"Approved {phone_to_approve}. {count} listing(s) with photos are now live."


def handle_pending_command(sender_jid: str) -> str:
    if phone_from_jid(sender_jid) not in ADMIN_PHONES:
        return "Sorry, that command isn't available."

    pending_sellers = (
        supabase.table("sellers")
        .select("*")
        .eq("approved", False)
        .order("created_at")
        .execute()
        .data
    )
    if not pending_sellers:
        return "No sellers waiting on approval right now."

    lines = ["Sellers waiting on approval:"]
    for seller in pending_sellers:
        listings = (
            supabase.table("listings")
            .select("id,item,image_url")
            .eq("seller_id", seller["id"])
            .eq("status", "pending_approval")
            .execute()
            .data
        )
        ready = sum(1 for l in listings if l.get("image_url"))
        items = ", ".join(l["item"] for l in listings) or "no items yet"
        lines.append(f"- {seller['phone']}: {items} ({ready}/{len(listings)} with photo)")

    lines.append("\nApprove with: APPROVE <phone>")
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
    if not listing_res.data or listing_res.data[0]["status"] not in ("pending_image", "pending_approval"):
        return f"Listing #{listing_id} isn't waiting on a photo."

    listing = listing_res.data[0]
    seller = supabase.table("sellers").select("*").eq("id", listing["seller_id"]).execute().data[0]

    try:
        processed = process_listing_image(media_url)
        image_url = upload_listing_image(listing_id, processed)
        new_status = listing_status(seller["approved"], has_image=True)
        supabase.table("listings").update({
            "image_url": image_url,
            "status": new_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", listing_id).execute()
    except Exception:
        return "Couldn't process that photo — please try resending it."

    if new_status == "pending_approval":
        return f"Photo added to #{listing_id}. It'll go live once your account is verified."

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
    approve_match = APPROVE_RE.match(message)
    listing_ref_match = LISTING_REF_RE.search(message)

    if approve_match:
        reply_text = handle_approve_command(sender_jid, approve_match.group(1))
    elif PENDING_RE.match(message):
        reply_text = handle_pending_command(sender_jid)
    elif sell_match:
        reply_text = handle_sell_command(sender_jid, sell_match.group(1), media_url)
    elif media_url and listing_ref_match:
        reply_text = handle_pending_image_followup(
            sender_jid, message, media_url, int(listing_ref_match.group(1))
        )
    elif listing_ref_match:
        reply_text = handle_buyer_inquiry(sender_jid, message, int(listing_ref_match.group(1)))
    else:
        reply_text = (
            "Hi! To list an item, send a photo with caption:\n"
            "SELL <item>, <quantity>, <price>, <category>, <area>\n\n"
            "To ask about something on the site, just mention its # number."
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
