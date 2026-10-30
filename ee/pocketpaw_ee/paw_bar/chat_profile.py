"""chat_profile.py: the card profile Paw OS chat cards are checked and written
against. It is the ripple concierge profile with chat's one host event and the
navigate/api actions the chat renderer runs."""

from dataclasses import replace

from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE

CHAT_PROFILE = replace(
    RIPPLE_PROFILE,
    name="chat",
    host_events=("chat.send",),
    actions=RIPPLE_PROFILE.actions | frozenset({"navigate", "api"}),
)
