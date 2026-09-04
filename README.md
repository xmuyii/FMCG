# Marketplace WhatsApp bot

Backend for the informal-market marketplace: a FastAPI webhook that lets
sellers list goods over WhatsApp (via GOWA) and buyers browse them on the
website, all backed by Supabase.

## Deploying (two separate Railway services)

**1. GOWA (WhatsApp connection)**
- Deploy via the official template: https://railway.com/deploy/gowa-whatsapp-web-multidevice
- After it's up, open its dashboard and scan the QR code with the middleman's
  WhatsApp (Linked Devices) to connect it.
- In GOWA's settings, set `WEBHOOK_URL` to this app's `/webhook` URL (once
  deployed below), and set `WHATSAPP_AUTO_DOWNLOAD_MEDIA=false` so images
  arrive as fetchable URLs rather than local file paths.
- Note the basic-auth username/password Railway generates for it.

**2. This app (bot logic, image processing, and the website)**
- Push this folder to a GitHub repo, then "Deploy from GitHub repo" in Railway.
- Set the environment variables from `.env.example`:
  - `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_IMAGES_BUCKET`
  - `GOWA_BASE_URL` (the Railway URL from step 1), `GOWA_BASIC_AUTH_USER`, `GOWA_BASIC_AUTH_PASS`
  - `LISTING_IMAGE_SIZE`, `MAX_UPLOAD_MB`, `ADMIN_PHONES`
- Railway auto-detects `railway.toml` and runs the app.
- Once deployed, this single Railway URL serves **both**: the buyer website
  at `/`, and the bot's webhook at `/webhook` (point GOWA's webhook setting
  here). `/docs` still shows FastAPI's auto-generated API docs — that's
  normal and harmless, just not the site itself.
- Before this is live for real buyers, edit `webapp/index.html`'s `CONFIG`
  block: set `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `MIDDLEMAN_PHONE`, and set
  `DEMO_MODE` to `false`.

**3. Supabase setup**
- Run `schema.sql` in the Supabase SQL editor.
- Create a public Storage bucket named `listing-images` (Storage > New bucket).

## Verifying before relying on it

GOWA's exact webhook JSON shape (particularly the `image` field, which
differs depending on `WHATSAPP_AUTO_DOWNLOAD_MEDIA`) should be confirmed by
sending a real test photo through once deployed, and compared against
`docs/webhook-payload.md` in the GOWA repo. `get_incoming_media_url()` in
`main.py` is the one place to adjust if the real payload differs.

## Seller onboarding & trust

Anyone can text `SELL`, but a new seller's listings stay hidden (`pending_approval`)
until the middleman verifies them and approves — this is what keeps buyers
trusting the site, since a broker-model marketplace only works if the
middleman actually knows who they're vouching for.

Approval happens entirely over WhatsApp: the middleman (a number listed in
`ADMIN_PHONES`) texts:
```
APPROVE 2348012345678
```
That flips the seller to approved and immediately publishes any of their
listings that already have a photo attached.

To see who's waiting, the middleman texts:
```
PENDING
```
which lists each unapproved seller's phone number, what they've listed so
far, and how many of those listings already have a photo ready to go live.

## Testing the SELL flow

Text the middleman's WhatsApp number, as a photo with caption:
```
SELL Rice, 50kg, 20000, Grains, Sabo market
```
The bot should reply confirming the listing is live (or pending verification,
if it's a new seller), with a `#id` you can reference later — buyers reply
mentioning `#id` to trigger a lead; sellers resend a photo with `#id` as
caption if the listing was saved without one.
