"""Assembles Uplink's parts from Settings, so the CLI and the tests wire them the same way."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import __version__
from .assistant import Assistant
from .config import Settings
from .feed import Feed, open_feed
from .monitor import ConsoleSender, Monitor, Sender, WhatsAppSender
from .muse import MetaModelAPI
from .server import WebhookApp
from .store import Store
from .transport import Transport
from .twilio import TwilioWhatsApp
from .whatsapp import Messenger, WhatsAppClient
from .writer import Writer


def make_transport(settings: Settings) -> Transport:
    contact = f"; contact: {settings.contact}" if settings.contact else ""
    return Transport(user_agent=f"Uplink/{__version__} (community mesh outage alerts{contact})")


def make_model(settings: Settings, transport: Transport) -> MetaModelAPI | None:
    if not settings.has_model:
        return None
    return MetaModelAPI(
        api_key=settings.model_api_key,
        transport=transport,
        base_url=settings.model_base_url,
        model=settings.model_name,
        asr_model=settings.asr_model,
        reasoning_effort=settings.reasoning_effort,
    )


def make_whatsapp(settings: Settings, transport: Transport) -> Messenger | None:
    """The configured WhatsApp provider, or None while its credentials are missing."""
    if settings.uses_twilio:
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            return None
        return TwilioWhatsApp(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            transport=transport,
            sender=settings.twilio_whatsapp_from,
            base_url=settings.twilio_base_url,
        )
    if not (settings.whatsapp_token and settings.whatsapp_phone_number_id):
        return None
    return WhatsAppClient(
        token=settings.whatsapp_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        transport=transport,
        api_version=settings.whatsapp_api_version,
        base_url=settings.graph_base_url,
    )


def make_writer(settings: Settings, model: MetaModelAPI | None) -> Writer:
    return Writer(model, language=settings.language, network=settings.network_name)


def make_sender(settings: Settings, whatsapp: Messenger | None, *, dry_run: bool) -> Sender:
    if dry_run or whatsapp is None or not settings.whatsapp_to:
        return ConsoleSender()
    return WhatsAppSender(
        whatsapp,
        settings.whatsapp_to,
        template="" if settings.uses_twilio else settings.whatsapp_alert_template,
        template_language=settings.whatsapp_template_language,
    )


def make_monitor(
    settings: Settings,
    *,
    dry_run: bool = False,
    feed: Feed | None = None,
    store: Store | None = None,
    transport: Transport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Monitor:
    transport = transport or make_transport(settings)
    return Monitor(
        settings=settings,
        feed=feed or open_feed(settings.feed_url, transport),
        store=store or Store(settings.data_dir),
        writer=make_writer(settings, make_model(settings, transport)),
        sender=make_sender(settings, make_whatsapp(settings, transport), dry_run=dry_run),
        clock=clock,
    )


def make_assistant(settings: Settings, store: Store, model: MetaModelAPI | None) -> Assistant:
    return Assistant(store=store, settings=settings, writer=make_writer(settings, model))


def make_webhook(settings: Settings, *, store: Store | None = None, transport: Transport | None = None) -> WebhookApp:
    transport = transport or make_transport(settings)
    whatsapp = make_whatsapp(settings, transport)
    if whatsapp is None:
        raise ValueError("the webhook needs " + ", ".join(settings.missing_for_webhook()))
    store = store or Store(settings.data_dir)
    model = make_model(settings, transport)
    return WebhookApp(
        settings=settings,
        whatsapp=whatsapp,
        assistant=make_assistant(settings, store, model),
        store=store,
        model=model,
    )
