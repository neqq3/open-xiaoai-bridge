"""OpenAI-compatible continuous conversation controller."""

from core.openai import OpenAIManager
from core.streaming_conversation import StreamingConversationController


class OpenAIConversationController(StreamingConversationController):
    """Generic OpenAI conversation; streaming is optional and progress-free."""

    CONFIG_PREFIX = "openai"
    BACKEND_NAME = "OpenAI"
    LOG_MODULE = "OpenAI Conv"
    WAKEUP_SOURCE = "openai"
    MANAGER = OpenAIManager
    STREAMING_DEFAULT = False
