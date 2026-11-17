"""E-Mail-Versand der Abrechnungen (SMTP, Zugangsdaten aus .env)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from .config import config


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(config.smtp_host and config.smtp_from)


LOGO = Path(__file__).parent / "static" / "icon-192.png"


def render_html(**ctx) -> str:
    """HTML-Mail aus ``templates/mail/base.html`` (responsive, Inline-Styles). ``logo_cid`` setzt send_mail."""
    from .render import templates

    ctx.setdefault("logo_cid", "{{LOGO_CID}}")
    return templates.get_template("mail/base.html").render(**ctx)


def send_mail(to: list[str], subject: str, body: str, attachments: list[tuple[str, bytes]],
              bcc: list[str] | None = None, html: Optional[str] = None) -> None:
    """Text-Mail bzw. – mit ``html`` – multipart/alternative (Text + HTML mit eingebettetem Logo) + PDF-Anhänge."""
    if not configured():
        raise MailError("SMTP ist nicht konfiguriert (SMTP_HOST / SMTP_FROM in .env).")
    if not to:
        raise MailError("Keine E-Mail-Adresse hinterlegt.")
    msg = EmailMessage()
    msg["From"] = config.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    if html:
        cid = make_msgid(domain="nebenkosten.local")
        use_logo = "{{LOGO_CID}}" in html and LOGO.exists()
        msg.add_alternative(html.replace("{{LOGO_CID}}", cid[1:-1]), subtype="html")
        if use_logo:
            msg.get_payload()[1].add_related(LOGO.read_bytes(), maintype="image", subtype="png", cid=cid,
                                             filename="logo.png", disposition="inline")
    for name, data, *mime in attachments:  # (Name, Daten[, "typ/subtyp"]) – Standard PDF
        maintype, subtype = (mime[0] if mime else "application/pdf").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    recipients = to + (bcc or [])
    ctx = ssl.create_default_context()
    if config.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
    with server:
        if config.smtp_security == "starttls":
            server.starttls(context=ctx)
        if config.smtp_user:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg, to_addrs=recipients)


def explain(e: Exception) -> str:
    """Typische SMTP-Fehler verständlich machen."""
    text = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return f"Anmeldung abgelehnt – SMTP_USER/SMTP_PASSWORD prüfen ({text})"
    if isinstance(e, smtplib.SMTPSenderRefused):
        return (f"Absender abgelehnt – SMTP_FROM muss die Adresse des Postfachs (oder ein Alias davon) sein, "
                f"mit dem du dich anmeldest ({text})")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"Empfänger abgelehnt ({text})"
    if isinstance(e, (ConnectionRefusedError, TimeoutError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return (f"Server nicht erreichbar – SMTP_HOST/SMTP_PORT/SMTP_SECURITY prüfen "
                f"(587 = starttls, 465 = ssl) ({text})")
    return text
