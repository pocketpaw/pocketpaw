# tests/cloud/test_paw_bar_appearance.py — the owner's Paw Bar appearance.
#
# Created 2026-08-19. Two things are being pinned, and the second matters more
# than the first.
#
# (1) The wire finally carries something. ``_pawbar_frame_config`` answered
#     ``"tokens": {}`` from the day the glass bar shipped — the widget read the
#     map and injected it as ``--pawbar-*`` custom properties, and nothing ever
#     filled it — and never emitted ``theme`` at all, which is why every bar was
#     dark regardless of what an owner wanted.
#
# (2) Every value in this model becomes the RIGHT-HAND SIDE of a CSS custom
#     property inside a document the widget serves. So an unvalidated value is a
#     style injection, and a URL field is an exfiltration channel — ``url(...)``
#     fires a request that carries a referrer. The validators are the boundary,
#     which makes them worth attacking in a test rather than trusting.

from __future__ import annotations

import pytest

from pocketpaw.paw_bar.appearance import (
    FONT_STACKS,
    ColorAppearance,
    ConciergeAppearance,
    HeroAppearance,
    LauncherAppearance,
    MotionAppearance,
)

# --------------------------------------------------------------------------- #
# Defaults — an unstyled site must be unchanged
# --------------------------------------------------------------------------- #


def test_defaults_reproduce_todays_look():
    """A Site nobody has styled renders the bar it always rendered. This is what
    makes the field additive and the migration unnecessary."""
    look = ConciergeAppearance()

    # "auto" — follow the customer's own site. It reads like a changed default
    # and is actually the SHIPPED behaviour finally being stated honestly: the
    # frame emitted this as ``theme``, the widget has read ``scheme`` since the
    # one-theme change, so no bar has ever received this field and every one of
    # them resolved light-or-dark off the host page. Defaulting to "auto" is
    # what keeps that true now that the frame really sends it.
    assert look.surface_mode == "auto"
    assert look.bar_resting == "compact"
    assert look.radius == 20
    assert look.blur == 28
    assert look.motion.preset == "lively"
    tokens = look.tokens()
    assert tokens["--pawbar-radius"] == "20px"
    assert tokens["--pawbar-blur"] == "28px"
    assert tokens["--pawbar-font"] == FONT_STACKS["system"]


def test_unset_optional_tokens_are_absent_rather_than_restated():
    """A token the owner did not set must NOT be emitted at its default value.

    The widget's own stylesheet is the source of the base look; restating it
    here would freeze every site to the values current at save time, so a later
    retune of the base would reach nobody.
    """
    look = ConciergeAppearance(accent="")
    assert "--pawbar-accent" not in look.tokens()


# --------------------------------------------------------------------------- #
# The validation boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hostile",
    [
        "red; background: url(https://evil.test/x)",
        "var(--anything)",
        "expression(alert(1))",
        "#12",
        "#gggggg",
        "url(https://evil.test/pixel.png)",
        "",
    ],
)
def test_a_colour_that_is_not_plain_hex_is_dropped(hostile: str):
    """Anything that is not ``#rgb`` / ``#rrggbb`` becomes "" and is therefore
    never emitted. A colour field that accepted general CSS would let an owner —
    or anyone who reached the settings endpoint — append a second declaration."""
    assert ConciergeAppearance(accent=hostile).accent == ""
    assert "--pawbar-accent" not in ConciergeAppearance(accent=hostile).tokens()


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "//evil.test/x.png",
        "http://insecure.test/x.png",  # mixed content — can only ever fail
        'https://evil.test/x.png"); background: url("https://evil.test/steal',
        "https://evil.test/a b.png",
    ],
)
def test_an_unsafe_image_url_is_dropped(hostile: str):
    """Only https:// and data:image/ survive, and neither may carry a character
    that could terminate the ``url()`` token and open a new declaration."""
    assert HeroAppearance(image_url=hostile).image_url == ""
    assert LauncherAppearance(icon_url=hostile).icon_url == ""
    assert ConciergeAppearance(agent_avatar_url=hostile).agent_avatar_url == ""
    assert ConciergeAppearance(team_avatar_urls=[hostile]).team_avatar_urls == []


def test_a_safe_image_url_survives_and_is_quoted_by_us():
    url = "https://cdn.example.test/hero.jpg"
    look = ConciergeAppearance(hero=HeroAppearance(style="image", image_url=url))

    assert look.tokens()["--pawbar-hero-image"] == f'url("{url}")'


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("radius", 9999, 32),
        ("radius", -40, 0),
        ("blur", 9999, 48),
        ("blur", -1, 0),
    ],
)
def test_lengths_are_clamped_not_echoed(field: str, value: int, expected: int):
    """A length is re-formatted from a clamped int, so a stored value can never
    be a string that carries anything but digits."""
    look = ConciergeAppearance(**{field: value})
    assert getattr(look, field) == expected
    assert look.tokens()[f"--pawbar-{field}"] == f"{expected}px"


def test_an_unknown_font_falls_back_rather_than_being_stored():
    """The family is looked up in a fixed table by key, never accepted as a
    string — which is what stops a font field from being a CSS grammar."""
    look = ConciergeAppearance(font="'; background: red; font-family: 'x")
    assert look.font == "system"
    assert look.tokens()["--pawbar-font"] == FONT_STACKS["system"]


@pytest.mark.parametrize(
    ("field", "cls", "hostile", "expected"),
    [
        ("surface_mode", ConciergeAppearance, "neon", "auto"),
        ("style", HeroAppearance, "iframe", "gradient"),
        ("position", LauncherAppearance, "middle-of-the-screen", "bottom-right"),
        ("preset", MotionAppearance, "seizure", "lively"),
    ],
)
def test_every_enum_field_falls_back_to_a_known_value(field, cls, hostile, expected):
    assert getattr(cls(**{field: hostile}), field) == expected


def test_reduced_motion_cannot_be_switched_off():
    """Not an owner's choice to make. A widget that ignores
    prefers-reduced-motion is an accessibility defect on somebody else's
    website, and the owner does not get to trade their visitors' setting away."""
    assert MotionAppearance(honor_reduced_motion=False).honor_reduced_motion is True


def test_team_avatars_are_capped_and_filtered():
    look = ConciergeAppearance(
        team_avatar_urls=[
            "https://a.test/1.png",
            "javascript:alert(1)",
            "https://a.test/2.png",
            "https://a.test/3.png",
            "https://a.test/4.png",
        ]
    )
    assert look.team_avatar_urls == [
        "https://a.test/1.png",
        "https://a.test/2.png",
        "https://a.test/3.png",
    ]


# --------------------------------------------------------------------------- #
# Motion presets
# --------------------------------------------------------------------------- #


def test_motion_presets_render_distinct_token_sets():
    calm = ConciergeAppearance(motion=MotionAppearance(preset="subtle")).tokens()
    loud = ConciergeAppearance(motion=MotionAppearance(preset="expressive")).tokens()

    assert calm["--pawbar-duration"] != loud["--pawbar-duration"]
    assert loud["--pawbar-motion-scale"] == "1.35"


def test_the_none_preset_stops_travel_without_stopping_state_changes():
    """``none`` zeroes duration and travel. Opacity transitions still resolve at
    0ms, so a state change is instant rather than invisible."""
    tokens = ConciergeAppearance(motion=MotionAppearance(preset="none")).tokens()

    assert tokens["--pawbar-duration"] == "0ms"
    assert tokens["--pawbar-motion-scale"] == "0"


def test_a_solid_hero_collapses_both_stops_to_one_colour():
    """One code path in the widget (a gradient) rather than a second background
    rule that has to be kept in sync with the first."""
    tokens = ConciergeAppearance(
        hero=HeroAppearance(style="solid", from_color="#123456", to_color="#abcdef")
    ).tokens()

    assert tokens["--pawbar-hero-from"] == "#123456"
    assert tokens["--pawbar-hero-to"] == "#123456"


# --------------------------------------------------------------------------- #
# The seam — the frame config that answered {} for a year
# --------------------------------------------------------------------------- #


def _config(**ov):
    from pocketpaw_ee.paw_bar.router import _pawbar_frame_config

    kwargs = dict(
        site_key="site_key_" + "a" * 24,
        widget_id="w-1",
        api_base="https://api.test/api/v1",
        parent_origin="https://brewco.com",
        greeting="",
    )
    kwargs.update(ov)
    return _pawbar_frame_config(**kwargs)


def test_the_frame_finally_emits_real_tokens():
    """The whole point. ``tokens`` was a hardcoded ``{}`` while the widget read
    it and injected it, so the white-label path was built end to end and dead."""
    look = ConciergeAppearance(accent="#ff0055", radius=4, surface_mode="light")

    config = _config(appearance=look)

    assert config["tokens"]["--pawbar-accent"] == "#ff0055"
    assert config["tokens"]["--pawbar-radius"] == "4px"
    # ``theme`` was never emitted at all, so the widget's `?? 'dark'` fallback
    # always won and a light bar was unreachable.
    assert config["theme"] == "light"


def test_a_site_with_no_appearance_still_frames():
    """A Site document written before this field exists deserializes without it,
    and the public frame must render for those rather than 500 the visitor."""
    config = _config(appearance=None)

    # See test_defaults_reproduce_todays_look: "auto" is what these sites have
    # effectively been getting all along.
    assert config["theme"] == "auto"
    assert config["scheme"] == "auto"
    assert config["tokens"]["--pawbar-radius"] == "20px"
    assert config["agentName"] == ""


def test_agent_identity_reaches_the_widget():
    look = ConciergeAppearance(
        agent_name="Fin",
        agent_subtitle="The team can also help",
        team_avatar_urls=["https://a.test/1.png"],
    )

    config = _config(appearance=look)

    assert config["agentName"] == "Fin"
    assert config["agentSubtitle"] == "The team can also help"
    assert config["avatars"] == ["https://a.test/1.png"]


def test_the_launcher_label_reaches_the_frame():
    """The resting pill says what the OWNER calls their own site.

    ``launcher.label`` has been stored and bound-checked since the appearance
    model landed, and the frame never emitted it, so the widget could not have
    rendered it however the owner set it. Absent emits "" rather than a
    server-side default: the fallback wording belongs to the surface that draws
    the pill, which is the only place that knows how much room it has."""
    look = ConciergeAppearance(launcher=LauncherAppearance(label="Ask about Ocean Supply"))

    assert _config(appearance=look)["launcherLabel"] == "Ask about Ocean Supply"
    assert _config(appearance=None)["launcherLabel"] == ""


def test_an_overlong_launcher_label_is_bounded_before_it_reaches_the_frame():
    """It renders inside a pill on somebody else's page. Unbounded, an owner
    could stretch the resting bar clear across their visitors' viewport."""
    look = ConciergeAppearance(launcher=LauncherAppearance(label="x" * 200))

    assert len(_config(appearance=look)["launcherLabel"]) == 40


def test_a_hostile_appearance_reaches_the_frame_defanged():
    """End to end: the validators run on construction, so what the frame emits
    is already safe rather than relying on a second scrub at render time."""
    look = ConciergeAppearance(
        accent="red; background: url(https://evil.test/x)",
        hero=HeroAppearance(style="image", image_url="javascript:alert(1)"),
    )

    tokens = _config(appearance=look)["tokens"]

    assert "--pawbar-accent" not in tokens
    assert "--pawbar-hero-image" not in tokens
    assert not any("evil.test" in v for v in tokens.values())


# --------------------------------------------------------------------------- #
# The light/dark choice reaches the widget at all (2026-08-22)
# --------------------------------------------------------------------------- #


def test_the_scheme_key_is_what_the_widget_actually_reads():
    """THE REGRESSION THIS EXISTS FOR, and it went unnoticed for months.

    The frame emitted the owner's light/dark choice as ``theme``. The widget
    stopped reading ``theme`` on 2026-08-19 — the one-theme change moved it to
    ``scheme`` and left ``theme`` explicitly ignored so older frame HTML would
    keep booting — and nothing ever sent ``scheme``. Both halves had tests.
    Both halves passed. The setting still did nothing, because no test on
    either side asserted that the key one wrote is the key the other reads.

    Mutation that must break this: drop the ``"scheme"`` line from
    ``_pawbar_frame_config``.
    """
    config = _config(appearance=ConciergeAppearance(surface_mode="light"))

    assert config["scheme"] == "light"
    # Still emitted alongside, for a bundle deployed before the rename that is
    # already sitting on a customer's page.
    assert config["theme"] == "light"


def test_the_resting_mode_reaches_the_widget():
    config = _config(appearance=ConciergeAppearance(bar_resting="full"))
    assert config["barResting"] == "full"


# --------------------------------------------------------------------------- #
# Colours — the whole palette, not just the accent
# --------------------------------------------------------------------------- #


def test_unset_colours_are_absent_so_the_stylesheet_still_owns_them():
    """The rule the whole token map follows. A colour we restate at its default
    freezes every site to the value current at save time, and a later retune of
    the base scale reaches nobody."""
    tokens = ColorAppearance().tokens()

    for absent in (
        "--pawbar-surface",
        "--pawbar-ink",
        "--pawbar-user-bubble",
        "--pawbar-ring",
        "--pawbar-unread",
    ):
        assert absent not in tokens

    # The two that ARE always emitted: numbers with a working default rather
    # than overrides, and the widget derives a second step from each.
    assert tokens["--pawbar-line-strength"] == "11%"
    assert tokens["--pawbar-wash-strength"] == "5%"


def test_one_surface_colour_produces_a_legible_widget():
    """Setting a light panel and nothing else used to be white type on a white
    panel — tokens.css documents that footgun, and a colour picker cannot warn
    anyone about it. So the ink is derived from the ground rather than left to
    the owner to work out."""
    light = ColorAppearance(surface="#f7f7fb").tokens()
    dark = ColorAppearance(surface="#101018").tokens()

    # Four surface steps and an ink, every time, from the one input.
    for token in (
        "--pawbar-surface",
        "--pawbar-surface-strong",
        "--pawbar-surface-raised",
        "--pawbar-surface-sunken",
        "--pawbar-ink",
    ):
        assert token in light and token in dark

    def _channels(value: str) -> tuple[int, int, int]:
        inner = value[value.index("(") + 1 : value.index(")")]
        r, g, b = (int(p.strip()) for p in inner.split(",")[:3])
        return r, g, b

    # The ink flips with the ground. This is the assertion that actually
    # prevents the bug: a light panel must not get light type.
    assert sum(_channels(light["--pawbar-ink"])) < 200, "dark ink on a light panel"
    assert sum(_channels(dark["--pawbar-ink"])) > 550, "light ink on a dark panel"


def test_an_explicit_ink_beats_the_derived_one():
    """Derivation is a floor, not a ceiling — an owner who wants warm-grey type
    on a near-black panel says so and wins."""
    tokens = ColorAppearance(surface="#101018", ink="#c8b8a0").tokens()
    assert tokens["--pawbar-ink"] == "rgba(200, 184, 160, 1)"


def test_every_colour_field_refuses_a_value_that_is_not_hex():
    """Each of these becomes the right-hand side of a CSS custom property in a
    document the widget serves, so a value that is not a colour is a style
    injection. Refused to "" — which means "the widget decides" — rather than
    stored and emitted.

    Mutation that must break this: widen _HEX_RE, or drop a field name from the
    validator's list.
    """
    hostile = "red; background-image: url(https://evil.test/x.png)"
    look = ColorAppearance(
        surface=hostile,
        ink=hostile,
        accent_fg=hostile,
        user_bubble=hostile,
        assistant_bubble=hostile,
        owner_bubble=hostile,
        ring=hostile,
        unread=hostile,
        danger=hostile,
    )
    assert look.surface == ""
    assert look.ink == ""
    tokens = look.tokens()
    assert not any("url(" in v or ";" in v for v in tokens.values())


def test_named_colours_reach_the_token_map():
    tokens = ColorAppearance(
        user_bubble="#e2662a",
        owner_bubble="#123",
        unread="#ff0044",
    ).tokens()

    assert tokens["--pawbar-user-bubble"] == "rgba(226, 102, 42, 1)"
    # Three-digit hex expands rather than being echoed.
    assert tokens["--pawbar-owner-bubble"] == "rgba(17, 34, 51, 1)"
    assert tokens["--pawbar-unread"] == "rgba(255, 0, 68, 1)"


@pytest.mark.parametrize(
    ("field", "given", "expected"),
    [
        ("surface_opacity", 5, 55),
        ("surface_opacity", 400, 100),
        ("line_strength", -3, 0),
        ("line_strength", 900, 30),
        ("wash_strength", 999, 20),
    ],
)
def test_numeric_colour_fields_are_clamped(field, given, expected):
    assert getattr(ColorAppearance(**{field: given}), field) == expected


def test_colours_ride_through_the_full_appearance():
    """The sub-model is wired into the appearance the frame actually renders,
    not merely present on the class."""
    look = ConciergeAppearance(colors=ColorAppearance(user_bubble="#e2662a"))
    assert look.tokens()["--pawbar-user-bubble"] == "rgba(226, 102, 42, 1)"
