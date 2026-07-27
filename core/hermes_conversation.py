"""Hermes Agent continuous conversation controller."""

from core.external_conversation import ExternalConversationController
from core.hermes import HermesManager


class HermesConversationController(ExternalConversationController):
    """Hermes conversation entry, independent from generic OpenAI routing."""

    CONFIG_PREFIX = "hermes"
    BACKEND_NAME = "Hermes"
    LOG_MODULE = "Hermes Conv"
    WAKEUP_SOURCE = "hermes"
    MANAGER = HermesManager
