"""Transactional email builders and SMTP delivery helpers with dev log fallback."""

from __future__ import annotations

import html
import logging
import smtplib
import sys
import urllib.parse
from email.message import EmailMessage

from trip_api.config import Settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _log_smtp_fallback(message: str, *args: object) -> None:
    """Emit INFO log to trip_api.email while guaranteeing delivery under uvicorn or pytest."""
    logger.info(message, *args)
    if not logger.hasHandlers():
        uvicorn_logger = logging.getLogger("uvicorn.error")
        if uvicorn_logger.handlers:
            uvicorn_logger.info(message, *args)
        else:
            sys.stderr.write((message % args) + "\n")


def _send_email_message(
    settings: Settings,
    *,
    recipient_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = recipient_email
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    if settings.smtp_use_tls and settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def build_verification_url(public_base_url: str, raw_token: str) -> str:
    """Build the frontend verification URL carrying the one-time opaque token."""
    encoded_token = urllib.parse.quote(raw_token, safe="")
    return f"{public_base_url.rstrip('/')}/verify-email?token={encoded_token}"


def build_password_reset_url(public_base_url: str, raw_token: str) -> str:
    """Build the frontend password reset URL carrying the one-time opaque token."""
    encoded_token = urllib.parse.quote(raw_token, safe="")
    return f"{public_base_url.rstrip('/')}/reset-password?token={encoded_token}"


def send_verification_email(
    settings: Settings,
    recipient_email: str,
    display_name: str,
    verify_url: str,
) -> None:
    """Send an account verification email via SMTP or log the link when SMTP is unconfigured."""
    if not settings.smtp_host.strip():
        _log_smtp_fallback(
            "SMTP not configured; email verification link for %s: %s",
            recipient_email,
            verify_url,
        )
        return

    greeting_name = display_name.strip() or recipient_email
    ttl_hours = settings.email_verification_ttl_hours
    plain_body = (
        f"Hello {greeting_name},\n\n"
        "Please verify your email address to activate your Trip Animator account:\n\n"
        f"{verify_url}\n\n"
        f"This verification link expires in {ttl_hours} hours.\n"
    )
    escaped_name = html.escape(greeting_name)
    escaped_url = html.escape(verify_url, quote=True)
    html_body = (
        "<!doctype html>"
        "<html><body>"
        f"<p>Hello {escaped_name},</p>"
        "<p>Please verify your email address to activate your Trip Animator account:</p>"
        f'<p><a href="{escaped_url}">Verify email address</a></p>'
        "<p>If the button does not work, copy and paste this link into your browser:<br>"
        f"{escaped_url}</p>"
        f"<p>This verification link expires in {ttl_hours} hours.</p>"
        "</body></html>"
    )
    _send_email_message(
        settings,
        recipient_email=recipient_email,
        subject="Verify your Trip Animator email address",
        plain_body=plain_body,
        html_body=html_body,
    )


def send_password_reset_email(
    settings: Settings,
    recipient_email: str,
    display_name: str,
    reset_url: str,
) -> None:
    """Send a password reset email via SMTP or log the link when SMTP is unconfigured."""
    if not settings.smtp_host.strip():
        _log_smtp_fallback(
            "SMTP not configured; password reset link for %s: %s",
            recipient_email,
            reset_url,
        )
        return

    greeting_name = display_name.strip() or recipient_email
    ttl_minutes = settings.password_reset_ttl_minutes
    plain_body = (
        f"Hello {greeting_name},\n\n"
        "We received a request to reset the password for your Trip Animator account.\n"
        "Open the link below to choose a new password:\n\n"
        f"{reset_url}\n\n"
        f"This password reset link expires in {ttl_minutes} minutes.\n"
        "If you did not request a password reset, you can safely ignore this email.\n"
    )
    escaped_name = html.escape(greeting_name)
    escaped_url = html.escape(reset_url, quote=True)
    html_body = (
        "<!doctype html>"
        "<html><body>"
        f"<p>Hello {escaped_name},</p>"
        "<p>We received a request to reset the password for your Trip Animator account.</p>"
        f'<p><a href="{escaped_url}">Reset your password</a></p>'
        "<p>If the button does not work, copy and paste this link into your browser:<br>"
        f"{escaped_url}</p>"
        f"<p>This password reset link expires in {ttl_minutes} minutes.</p>"
        "<p>If you did not request a password reset, you can safely ignore this email.</p>"
        "</body></html>"
    )
    _send_email_message(
        settings,
        recipient_email=recipient_email,
        subject="Reset your Trip Animator password",
        plain_body=plain_body,
        html_body=html_body,
    )
