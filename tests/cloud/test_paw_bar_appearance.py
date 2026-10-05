# tests/cloud/test_paw_bar_appearance.py — the owner's Paw Bar appearance.
#
# Three things are pinned here:
#
# (1) Follow the site. The bar layers bar defaults < the host site's detected
#     theme < owner ``tokens`` < ``tokensDark``, so any emitted token overrides
#     the site. An untouched appearance (accent "", font "site", radius None,
#     every colour "") must therefore emit no look token at all.
# (2) The token names are the ones the bar reads (paw-bar lib/bar-themes.ts,
#     lib/site-theme.ts), and tokens the bar never reads (hero, motion, unread,
#     line/wash strength, the old surface scale) are no longer emitted.
# (3) Every value becomes the RIGHT-HAND SIDE of a CSS custom property inside a
#     document the widget serves. An unvalidated value is a style injection and
#     a URL field is an exfiltration channel, so the validators are attacked here
#     rather than trusted.

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


def test_defaults_follow_the_site():
    """A fresh appearance follows the website: no accent, font or radius token,
    so the site theme the loader detected is what the bar wears. Only blur, a
    bar-only facet the site has no say in, is emitted."""
    look = ConciergeAppearance()

    assert look.surface_mode == "auto"
    assert look.accent == ""
    assert look.font == "site"
    assert look.radius is None
    assert look.tokens() == {"--pawbar-blur": "28px"}
    assert look.tokens_dark() == {"--pawbar-blur": "28px"}


def test_every_font_stack_is_still_settable():
    for key, stack in FONT_STACKS.items():
        look = ConciergeAppearance(font=key)
        assert look.font == key
        assert look.tokens()["--pawbar-font"] == stack


def test_a_radius_of_zero_is_an_override_not_follow_the_site():
    """None follows the site; 0 is square corners the owner asked for."""
    assert ConciergeAppearance(radius=0).tokens()["--pawbar-radius"] == "0px"
    assert "--pawbar-radius" not in ConciergeAppearance(radius=None).tokens()


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


def test_a_safe_hero_image_is_stored_but_renders_nothing():
    """The bar has no hero, so the field is kept for the editor and emits no token."""
    url = "https://cdn.example.test/hero.jpg"
    look = ConciergeAppearance(hero=HeroAppearance(style="image", image_url=url))

    assert look.hero.image_url == url
    assert not any(k.startswith("--pawbar-hero") for k in look.tokens())


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


def test_an_unknown_font_falls_back_to_following_the_site():
    """The family is looked up in a fixed table by key, never accepted as a
    string — which is what stops a font field from being a CSS grammar."""
    look = ConciergeAppearance(font="'; background: red; font-family: 'x")
    assert look.font == "site"
    assert "--pawbar-font" not in look.tokens()


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
# Tokens the bar never reads are not emitted
# --------------------------------------------------------------------------- #


def test_dead_tokens_are_not_emitted_even_when_their_fields_are_set():
    """Hero, motion, unread, line/wash strength and the surface scale have no
    surface in the bar. The fields still validate (paw-enterprise writes them)
    but nothing renders from them."""
    look = ConciergeAppearance(
        hero=HeroAppearance(style="solid", from_color="#123456", to_color="#abcdef"),
        motion=MotionAppearance(preset="expressive"),
        colors=ColorAppearance(
            surface="#f7f7fb", ink="#111111", user_bubble="#e2662a", unread="#ff0044"
        ),
    )
    for tokens in (look.tokens(), look.tokens_dark()):
        for dead in (
            "--pawbar-hero-from",
            "--pawbar-hero-to",
            "--pawbar-hero-height",
            "--pawbar-duration",
            "--pawbar-ease-emphasis",
            "--pawbar-motion-scale",
            "--pawbar-unread",
            "--pawbar-line-strength",
            "--pawbar-wash-strength",
            "--pawbar-surface",
            "--pawbar-surface-strong",
            "--pawbar-ink",
            "--pawbar-user-bubble",
            "--pawbar-owner-bubble",
        ):
            assert dead not in tokens, dead


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
    assert "--pawbar-radius" not in config["tokens"]
    assert "--pawbar-accent" not in config["tokens"]
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


def test_unset_colours_are_absent_so_the_site_shows_through():
    """An emitted colour overrides the site's detected one, so a colour the owner
    did not set must not be emitted at all."""
    assert ColorAppearance().tokens() == {}
    assert ColorAppearance().tokens("dark") == {}


def _channels(value: str) -> tuple[int, int, int]:
    inner = value[value.index("(") + 1 : value.index(")")]
    r, g, b = (int(p.strip()) for p in inner.split(",")[:3])
    return r, g, b


def test_a_surface_paints_the_pill_and_the_frame_at_the_bar_glass_alpha():
    """``colors.surface`` lands on the names the bar reads, at the alpha the bar
    gives a detected site background (paw-bar lib/site-theme.ts): the pill at
    0.78, the frame at 0.82 light and 0.55 dark."""
    light = ColorAppearance(surface="#f7f7fb").tokens("light")
    dark = ColorAppearance(surface="#f7f7fb").tokens("dark")

    assert light["--pawbar-bg"] == "rgba(247, 247, 251, 0.78)"
    assert light["--pawbar-frame-bg"] == "rgba(247, 247, 251, 0.82)"
    assert dark["--pawbar-bg"] == "rgba(247, 247, 251, 0.78)"
    assert dark["--pawbar-frame-bg"] == "rgba(247, 247, 251, 0.55)"


def test_one_surface_colour_produces_legible_type():
    """A light surface under the bar's own light type is white on white, so a
    surface with no ink derives one that reads on it."""
    light = ColorAppearance(surface="#f7f7fb").tokens()
    dark = ColorAppearance(surface="#101018").tokens()

    for tokens in (light, dark):
        assert tokens["--pawbar-fg"] == tokens["--pawbar-frame-fg"]
    assert sum(_channels(light["--pawbar-fg"])) < 200, "dark type on a light ground"
    assert sum(_channels(dark["--pawbar-fg"])) > 550, "light type on a dark ground"


def test_an_explicit_ink_beats_the_derived_one():
    tokens = ColorAppearance(surface="#101018", ink="#c8b8a0").tokens()
    assert tokens["--pawbar-fg"] == "rgba(200, 184, 160, 1)"
    assert tokens["--pawbar-frame-fg"] == "rgba(200, 184, 160, 1)"


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


def test_named_colours_reach_the_names_the_bar_reads():
    tokens = ColorAppearance(
        user_bubble="#e2662a",
        owner_bubble="#123",
        assistant_bubble="#eeeeee",
        accent_fg="#ffffff",
        ring="#00ff00",
        danger="#ff0000",
    ).tokens()

    assert tokens["--pawbar-bubble-bg"] == "rgba(226, 102, 42, 1)"
    # Three-digit hex expands rather than being echoed.
    assert tokens["--pawbar-owner-bubble-bg"] == "rgba(17, 34, 51, 1)"
    assert tokens["--pawbar-assistant-bubble"] == "rgba(238, 238, 238, 1)"
    assert tokens["--pawbar-accent-fg"] == "rgba(255, 255, 255, 1)"
    assert tokens["--pawbar-ring"] == "rgba(0, 255, 0, 1)"
    assert tokens["--pawbar-danger"] == "rgba(255, 0, 0, 1)"


def test_a_visitor_bubble_colour_brings_its_own_legible_text():
    """The bar's bubble text flips with the scheme, so an owner bubble colour
    gets text chosen for it rather than one that vanishes in light or dark."""
    pale = ColorAppearance(user_bubble="#fafafa").tokens()
    deep = ColorAppearance(user_bubble="#1c1c21").tokens()
    assert sum(_channels(pale["--pawbar-bubble-fg"])) < 200
    assert sum(_channels(deep["--pawbar-bubble-fg"])) > 550


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
    assert look.tokens()["--pawbar-bubble-bg"] == "rgba(226, 102, 42, 1)"


# --------------------------------------------------------------------------- #
# Launcher style, logo, and the dark palette
# --------------------------------------------------------------------------- #

_PNG_DATA = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def test_a_doc_that_never_set_the_new_fields_is_unchanged():
    """A Site saved before these fields existed loads without them and renders
    the same bar: docked, no logo, and a dark palette identical to the light."""
    stored = {"accent": "#ff0055", "colors": {"ink": "#222222", "line_strength": 14}}
    look = ConciergeAppearance.model_validate(stored)

    assert look.launcher.style == "bar"
    assert look.logo_url == ""
    assert look.accent_dark == ""
    assert look.colors_dark == ColorAppearance()
    assert look.tokens_dark() == look.tokens()


@pytest.mark.parametrize(
    ("given", "expected"), [("icon", "icon"), ("bar", "bar"), ("pill", "bar"), ("", "bar")]
)
def test_the_launcher_style_falls_back_to_bar(given, expected):
    assert LauncherAppearance(style=given).style == expected


def test_a_raster_data_url_survives():
    for kind in ("png", "jpeg", "webp", "gif"):
        url = _PNG_DATA.replace("image/png", f"image/{kind}")
        assert ConciergeAppearance(logo_url=url).logo_url == url
        assert ConciergeAppearance(agent_avatar_url=url).agent_avatar_url == url
        assert LauncherAppearance(icon_url=url).icon_url == url


def test_an_oversized_data_url_is_dropped():
    """Inline images live on the Site doc and in the frame HTML, so they are
    capped at 200k characters. At the cap it survives; one over it does not."""
    head = "data:image/png;base64,"
    at_cap = head + "A" * (200_000 - len(head))
    assert ConciergeAppearance(logo_url=at_cap).logo_url == at_cap
    assert ConciergeAppearance(logo_url=at_cap + "A").logo_url == ""
    assert HeroAppearance(image_url=at_cap + "A").image_url == ""


@pytest.mark.parametrize(
    "hostile",
    [
        "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+",
        "data:image/svg+xml,<svg onload=alert(1)>",
        "data:image/png,rawbytes",
        "data:image/png;base64,abc)def",
        'data:image/png;base64,abc");x:url("https://evil.test/',
        "data:image/bmp;base64,Qk0=",
        "data:text/html;base64,PHNjcmlwdD4=",
    ],
)
def test_an_svg_or_non_base64_data_url_is_dropped(hostile: str):
    """SVG is a document that can run script, and anything outside the base64
    alphabet could close the url() token."""
    assert ConciergeAppearance(logo_url=hostile).logo_url == ""
    assert ConciergeAppearance(agent_avatar_url=hostile).agent_avatar_url == ""


def test_https_urls_are_unchanged_by_the_data_url_rules():
    url = "https://cdn.example.test/logo.png"
    assert ConciergeAppearance(logo_url=url).logo_url == url
    assert ConciergeAppearance(logo_url="http://x.test/l.png").logo_url == ""


def test_the_dark_accent_is_hex_only():
    assert ConciergeAppearance(accent_dark="red; x: y").accent_dark == ""
    assert ConciergeAppearance(accent_dark="#abc").accent_dark == "#abc"


def test_dark_fields_that_are_set_reach_tokens_dark():
    look = ConciergeAppearance(
        accent="#3b6fe0",
        accent_dark="#88aaff",
        radius=12,
        colors=ColorAppearance(surface="#f7f7fb"),
        colors_dark=ColorAppearance(surface="#101018"),
    )
    light, dark = look.tokens(), look.tokens_dark()

    assert light["--pawbar-accent"] == "#3b6fe0"
    assert dark["--pawbar-accent"] == "#88aaff"
    assert dark["--pawbar-bg"] == "rgba(16, 16, 24, 0.78)" != light["--pawbar-bg"]
    assert dark["--pawbar-frame-bg"] == "rgba(16, 16, 24, 0.55)"
    # Everything that is not a colour is the same map.
    assert dark["--pawbar-radius"] == light["--pawbar-radius"] == "12px"


def test_tokens_dark_falls_back_to_the_light_value():
    """A dark field left unset takes the light value, so an owner who customised
    one set does not get a half-default bar on a dark page."""
    look = ConciergeAppearance(
        accent="#ff0055",
        colors=ColorAppearance(user_bubble="#e2662a", ring="#123456"),
        colors_dark=ColorAppearance(ring="#654321"),
    )
    dark = look.tokens_dark()

    assert dark["--pawbar-accent"] == "#ff0055"
    assert dark["--pawbar-bubble-bg"] == "rgba(226, 102, 42, 1)"
    assert dark["--pawbar-ring"] == "rgba(101, 67, 33, 1)"


def test_a_following_light_accent_keeps_dark_following_too():
    """``accent_dark`` "" means "same as light", so a light accent that follows
    the site leaves the dark one following too."""
    assert "--pawbar-accent" not in ConciergeAppearance(accent="").tokens_dark()
    dark = ConciergeAppearance(accent_dark="#88aaff").tokens_dark()
    assert dark["--pawbar-accent"] == "#88aaff"


def test_the_launcher_style_side_and_logo_reach_the_frame():
    look = ConciergeAppearance(
        logo_url="https://cdn.example.test/logo.png",
        launcher=LauncherAppearance(style="icon", position="bottom-left"),
        accent_dark="#88aaff",
    )
    config = _config(appearance=look)

    assert config["launcher"] == "icon"
    assert config["side"] == "left"
    assert config["logo"] == "https://cdn.example.test/logo.png"
    assert config["tokensDark"]["--pawbar-accent"] == "#88aaff"
    # ``tokens`` keeps its meaning: the light (or pinned) palette, which here
    # follows the site.
    assert "--pawbar-accent" not in config["tokens"]


def test_a_site_with_no_appearance_boots_docked_on_the_right():
    config = _config(appearance=None)

    assert config["launcher"] == "bar"
    assert config["side"] == "right"
    assert config["logo"] == ""
    assert config["tokensDark"] == config["tokens"]
