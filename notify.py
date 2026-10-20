#!/usr/bin/env python3
"""
notify.py  --  Delivery layer for the nightly digest.

One function matters to the rest of the program:

    send_all(subject, short_body, long_body, ok) -> list[str]

It fans the digest out to every channel listed in DIGEST_CHANNELS and returns
a list of human-readable errors (empty list == everything delivered).

WHY THERE ARE TWO BODIES
------------------------
Email can carry the full per-store, per-metric breakdown. A text message
cannot -- SMS is 160 characters in GSM-7, or 153 each once a message has to be
split into segments, and every segment is billed separately. So the caller
builds two registers of the same message and each channel picks the one it can
actually carry.

CHANNELS, CHEAPEST-TO-START FIRST
---------------------------------
  telegram        Free, no carrier or SMTP involved, about two minutes to set
                  up, and the only channel with no length limit worth caring
                  about -- so it gets the FULL breakdown rather than the short
                  form. Best default for an always-on homelab box.
  smtp            Email. Works today, free, no registration.
  sms_gateway     Text, via your carrier's free email-to-SMS bridge. Also
                  works today and needs no account -- it is just email sent to
                  a magic address. Carriers are slowly deprecating these, and
                  delivery is best-effort with no receipt, so it is the right
                  default but not the right long-term answer.
  twilio_sms      Real SMS. Costs a fraction of a cent per segment. US long
                  codes require A2P 10DLC brand/campaign registration, which
                  takes days to clear; unregistered traffic gets filtered by
                  the carriers rather than rejected, so it fails silently.
  twilio_whatsapp WhatsApp. See the long note on send_twilio_whatsapp below --
                  this one has a constraint that will bite you at 3am if you
                  do not read it.

Stdlib only. Twilio is a plain form-encoded POST, so pulling in their SDK to
make one HTTP call would not earn its keep.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import re
import smtplib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage

log = logging.getLogger("silver.notify")

TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_LIMIT = 4096          # hard cap on a single sendMessage text
HTTP_TIMEOUT = 30


# ===========================================================================
#  HELPERS
# ===========================================================================

def _env_list(key: str) -> list[str]:
    """Read a comma-separated env var into a list of non-empty items."""
    return [x.strip() for x in os.environ.get(key, "").split(",") if x.strip()]


def sanitize_param(text: str, limit: int = 640) -> str:
    """
    Flatten text so it is safe to use as a WhatsApp template variable.

    WhatsApp rejects template parameters containing newlines, tabs, or runs of
    four or more spaces. Our digest bodies are multi-line by design, so every
    value going into a template has to be collapsed to a single line first.
    This is the single most common cause of a template send being rejected.
    """
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:limit]


def store_count() -> int:
    """
    How many stores are configured. Used to pin the WhatsApp template's
    variable count, which has to be identical on every send.
    """
    return sum(1 for i in range(1, 6) if os.environ.get(f"STORE{i}_USER"))


def sms_segments(text: str) -> int:
    """
    Estimate how many SMS segments a body will cost.

    Anything outside the GSM-7 alphabet (curly quotes and em dashes are the
    usual culprits) forces the whole message into UCS-2, which cuts capacity
    from 160 to 70 characters. Worth logging so a stray character does not
    quietly triple the bill.
    """
    gsm = set(
        "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
        "^{}\\[~]|€"
    )
    unicode_mode = any(c not in gsm for c in text)
    single, multi = (70, 67) if unicode_mode else (160, 153)
    n = len(text)
    return 1 if n <= single else -(-n // multi)


# ===========================================================================
#  EMAIL  (used by both the smtp and sms_gateway channels)
# ===========================================================================

def _send_mail(to: list[str], subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not (host and user and password):
        raise RuntimeError("SMTP_HOST / SMTP_USER / SMTP_PASS not set")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    port = int(os.environ.get("SMTP_PORT", "587"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


def send_smtp(subject: str, short_body: str, long_body: str, ok: bool) -> None:
    """Full digest by email."""
    to = _env_list("DIGEST_TO")
    if not to:
        raise RuntimeError("DIGEST_TO not set")
    _send_mail(to, subject, long_body)
    log.info("email sent to %s", ", ".join(to))


def send_sms_gateway(subject: str, short_body: str, long_body: str, ok: bool) -> None:
    """
    Text message via the carrier's email-to-SMS gateway.

    The subject line is deliberately dropped into the body: most gateways
    either discard the subject or prepend it in an ugly way, so we control the
    formatting ourselves and send a body-only message.
    """
    to = _env_list("SMS_GATEWAY_TO")
    if not to:
        raise RuntimeError("SMS_GATEWAY_TO not set")
    body = f"{subject}\n{short_body}"
    _send_mail(to, "", body)
    log.info("sms gateway message sent to %s (%d segment(s))",
             ", ".join(to), sms_segments(body))


# ===========================================================================
#  TWILIO
# ===========================================================================

def _twilio_post(payload: dict[str, str]) -> dict:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")

    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(TWILIO_API.format(sid=sid), data=data)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        # Twilio returns a JSON body with a specific error code -- surfacing it
        # verbatim turns "it didn't work" into something you can actually look up.
        try:
            j = json.loads(detail)
            raise RuntimeError(
                f"Twilio {e.code} [{j.get('code')}] {j.get('message')} "
                f"-- {j.get('more_info', '')}"
            ) from None
        except (ValueError, AttributeError):
            raise RuntimeError(f"Twilio HTTP {e.code}: {detail[:300]}") from None


def send_twilio_sms(subject: str, short_body: str, long_body: str, ok: bool) -> None:
    """Real SMS through Twilio."""
    to = _env_list("TWILIO_SMS_TO")
    frm = os.environ.get("TWILIO_SMS_FROM")
    if not to or not frm:
        raise RuntimeError("TWILIO_SMS_TO / TWILIO_SMS_FROM not set")

    body = f"{subject}\n{short_body}"
    log.info("twilio sms: %d segment(s) per recipient", sms_segments(body))
    for number in to:
        r = _twilio_post({"To": number, "From": frm, "Body": body})
        log.info("twilio sms queued to %s (sid=%s)", number, r.get("sid"))


def send_twilio_whatsapp(subject: str, short_body: str, long_body: str, ok: bool) -> None:
    """
    WhatsApp through Twilio.

    READ THIS BEFORE YOU RELY ON IT
    -------------------------------
    WhatsApp only lets a business send a free-form message inside a 24-hour
    "customer service window", and that window is opened by the CUSTOMER
    messaging the business first. A digest that fires at 3:15am is by
    definition outside that window, so it MUST be sent as a pre-approved
    template. Since April 2025 a template has to be sent using ContentSid plus
    ContentVariables -- passing the template text in Body fails outright.

    The trap: Twilio's WhatsApp *sandbox* gives you a 24-hour window as soon as
    you send it the "join <code>" phrase, so a free-form send tests green,
    then stops working the next day once the window lapses. That is why this
    function refuses to send free-form and always uses a template.

    Set up, in order:
      1. Get a WhatsApp sender approved in Twilio (Meta business verification).
      2. Create a template with exactly (number of stores + 2) variables --
         one headline, one per store, one for the combined total. With two
         stores that is four:
             {{1}}
             {{2}}
             {{3}}
             {{4}}
      3. Put its Content SID (starts with HX) in TWILIO_WHATSAPP_TEMPLATE_SID.

    ADDING A STORE LATER MEANS GETTING A NEW TEMPLATE APPROVED. The variable
    count is baked into the approved template, so a third store needs a
    five-variable version. The log line below prints the count actually sent,
    which is the fastest way to confirm a mismatch.

    Variables are populated from the short digest, one line per variable, each
    flattened to a single line because WhatsApp rejects newlines inside a
    parameter. A failed capture is carried in the same template rather than a
    second one -- the [CHECK] prefix rides along in variable 1.
    """
    to = _env_list("TWILIO_WHATSAPP_TO")
    frm = os.environ.get("TWILIO_WHATSAPP_FROM")
    template = os.environ.get("TWILIO_WHATSAPP_TEMPLATE_SID")

    if not to or not frm:
        raise RuntimeError("TWILIO_WHATSAPP_TO / TWILIO_WHATSAPP_FROM not set")
    if not template:
        raise RuntimeError(
            "TWILIO_WHATSAPP_TEMPLATE_SID not set. A nightly digest is always "
            "outside WhatsApp's 24-hour window, so it can only be delivered as "
            "an approved template. See the docstring in notify.py."
        )

    # Variable 1 is the headline (carries [CHECK] when something broke),
    # variables 2..n are the body lines, each flattened.
    #
    # The count MUST be identical on every single send. Meta rejects a template
    # whose parameter count does not match the approved definition, and it
    # rejects empty-string parameters too. The digest's line count is NOT
    # naturally fixed -- the TOTAL line disappears when fewer than two stores
    # report, so the night every store fails would produce fewer variables and
    # the alert you most need would be the one that never arrives. So we pin
    # the count to (stores + 1) body slots and pad short with a placeholder.
    slots = store_count() + 1
    lines = [ln for ln in short_body.splitlines() if ln.strip()]
    if len(lines) > slots:
        # Fold the overflow into the last slot rather than dropping it.
        lines = lines[:slots - 1] + [" / ".join(lines[slots - 1:])]
    lines += ["-"] * (slots - len(lines))

    variables = {"1": sanitize_param(subject)}
    for i, line in enumerate(lines, start=2):
        variables[str(i)] = sanitize_param(line) or "-"

    log.info("whatsapp template %s: %d variables", template, len(variables))
    payload_vars = json.dumps(variables)
    for number in to:
        prefix = "" if number.startswith("whatsapp:") else "whatsapp:"
        r = _twilio_post({
            "To": f"{prefix}{number}",
            "From": frm if frm.startswith("whatsapp:") else f"whatsapp:{frm}",
            "ContentSid": template,
            "ContentVariables": payload_vars,
        })
        log.info("whatsapp template queued to %s (sid=%s)", number, r.get("sid"))


# ===========================================================================
#  TELEGRAM
# ===========================================================================

def _html_escape(text: str) -> str:
    """Escape for Telegram's HTML parse mode. Ampersand FIRST, or you double-escape."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def telegram_call(method: str, params: dict[str, str],
                  timeout: int | None = None) -> dict:
    """
    One Telegram Bot API call. Raises RuntimeError with Telegram's own text.

    `timeout` overrides the socket timeout, which long-polling needs: getUpdates
    asks Telegram to HOLD the connection open for up to POLL_TIMEOUT seconds, so
    a socket timeout shorter than that aborts every poll on schedule and turns
    the long-poll into a noisy retry loop.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    url = TELEGRAM_API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data=data,
                                    timeout=timeout or HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            j = json.loads(detail)
            raise RuntimeError(
                f"Telegram {e.code}: {j.get('description', detail[:200])}"
            ) from None
        except ValueError:
            raise RuntimeError(f"Telegram HTTP {e.code}: {detail[:200]}") from None

    if not body.get("ok"):
        raise RuntimeError(f"Telegram refused: {body.get('description')}")
    return body


def telegram_send_document(chat_id: str, path: pathlib.Path,
                           caption: str = "") -> dict:
    """Upload one file (sendDocument). Hand-rolled multipart: no extra deps."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    ctype = {".xlsx": "application/vnd.openxmlformats-officedocument."
                      "spreadsheetml.sheet"}.get(path.suffix, "text/csv")
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in (("chat_id", chat_id), ("caption", caption)):
        if v:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                         f'name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                 f'name="document"; filename="{path.name}"\r\n'
                 f'Content-Type: {ctype}\r\n\r\n'.encode()
                 + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        TELEGRAM_API.format(token=token, method="sendDocument"),
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT * 2) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram HTTP {e.code}: "
                           f"{e.read().decode(errors='replace')[:200]}") from None
    if not body.get("ok"):
        raise RuntimeError(f"Telegram refused: {body.get('description')}")
    return body


def _telegram_chunks(subject: str, body: str) -> list[str]:
    """
    Split the digest into <=4096-char HTML messages, breaking on line
    boundaries so a store's numbers never get cut in half mid-row.

    The body goes inside <pre> because the long digest is column-aligned and
    Telegram's proportional font would otherwise turn it into mush.
    """
    header = f"<b>{_html_escape(subject)}</b>\n"
    open_tag, close_tag = "<pre>", "</pre>"
    budget = TELEGRAM_LIMIT - len(header) - len(open_tag) - len(close_tag) - 16

    out: list[str] = []
    current: list[str] = []
    size = 0
    for line in body.splitlines():
        esc = _html_escape(line)
        if size + len(esc) + 1 > budget and current:
            out.append(header + open_tag + "\n".join(current) + close_tag)
            current, size = [], 0
        current.append(esc)
        size += len(esc) + 1
    if current or not out:
        out.append(header + open_tag + "\n".join(current) + close_tag)
    return out


def send_telegram(subject: str, short_body: str, long_body: str, ok: bool) -> None:
    """
    Telegram gets the FULL breakdown, not the short form.

    Unlike SMS there is no per-message cost and the limit is 4096 characters,
    so there is no reason to send the abbreviated version -- you may as well
    have the whole report on your phone.
    """
    chats = _env_list("TELEGRAM_CHAT_ID")
    if not chats:
        raise RuntimeError("TELEGRAM_CHAT_ID not set (run --telegram-setup to find it)")

    messages = _telegram_chunks(subject, long_body)
    for chat in chats:
        for i, text in enumerate(messages, 1):
            telegram_call("sendMessage", {
                "chat_id": chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
        log.info("telegram sent to %s (%d message(s))", chat, len(messages))


# ===========================================================================
#  DISPATCH
# ===========================================================================

CHANNELS = {
    "telegram": send_telegram,
    "smtp": send_smtp,
    "sms_gateway": send_sms_gateway,
    "twilio_sms": send_twilio_sms,
    "twilio_whatsapp": send_twilio_whatsapp,
}


def configured_channels() -> list[str]:
    """
    Which channels to use. Explicit DIGEST_CHANNELS wins; otherwise we infer
    from what is filled in, so an existing .env keeps working untouched.
    """
    explicit = _env_list("DIGEST_CHANNELS")
    if explicit:
        return explicit

    inferred = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        inferred.append("telegram")
    if os.environ.get("SMTP_HOST") and os.environ.get("DIGEST_TO"):
        inferred.append("smtp")
    if os.environ.get("SMS_GATEWAY_TO"):
        inferred.append("sms_gateway")
    return inferred


def send_all(subject: str, short_body: str, long_body: str, ok: bool) -> list[str]:
    """
    Deliver the digest everywhere. Returns a list of error strings.

    Each channel is attempted independently: WhatsApp being misconfigured must
    never suppress the email, because the whole contract of this program is
    that silence never means success.
    """
    names = configured_channels()
    if not names:
        # Print it so the run is not a total loss, but return an ERROR: a job
        # that delivered nothing must never report success, or a typo'd .env
        # turns into weeks of silence that looks exactly like weeks of good news.
        log.error("no delivery channels configured -- printing digest instead")
        print("\n" + "=" * 60)
        print(subject)
        print("=" * 60)
        print(long_body)
        return ["(config): no delivery channels configured -- nothing was sent"]

    errors: list[str] = []
    for name in names:
        fn = CHANNELS.get(name)
        if fn is None:
            errors.append(f"{name}: unknown channel (valid: {', '.join(CHANNELS)})")
            log.error("unknown channel %r", name)
            continue
        try:
            fn(subject, short_body, long_body, ok)
        except Exception as e:
            msg = f"{name}: {type(e).__name__}: {e}"
            errors.append(msg)
            log.error("delivery failed -- %s", msg)

    if errors and len(errors) == len(names):
        log.critical("EVERY delivery channel failed -- digest was not delivered")
    return errors
