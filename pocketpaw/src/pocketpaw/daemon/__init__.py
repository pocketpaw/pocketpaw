"""
PocketPaw Proactive Daemon Module

Transforms PocketPaw from a reactive chatbot into a proactive AI agent
that initiates actions based on user-defined "intentions" and various triggers.
"""

from .context import ContextHub
from .executor import IntentionExecutor
from .intentions import IntentionStore, get_intention_store
from .proactive import ProactiveDaemon, get_daemon
from .triggers import TriggerEngine

__all__ = [
    "IntentionStore",
    "get_intention_store",
    "TriggerEngine",
    "ContextHub",
    "IntentionExecutor",
    "ProactiveDaemon",
    "get_daemon",
]
