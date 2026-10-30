import re

from pocketpaw.uploads.keys import new_storage_key, sanitize_ext


def test_new_storage_key_shape():
    key = new_storage_key("chat", ".png")
    assert re.match(r"^chat/\d{6}/[a-f0-9]{32}\.png$", key), key


def test_default_kind_is_chat():
    key = new_storage_key(ext=".pdf")
    assert key.startswith("chat/")


def test_no_ext_produces_key_without_extension():
    key = new_storage_key("chat", "")
    assert re.match(r"^chat/\d{6}/[a-f0-9]{32}$", key)


def test_1000_keys_are_unique():
    keys = {new_storage_key("chat", ".bin") for _ in range(1000)}
    assert len(keys) == 1000


def test_sanitize_ext_lowercases():
    assert sanitize_ext(".PNG") == ".png"


def test_sanitize_ext_strips_non_alnum():
    assert sanitize_ext(".p!ng") == ".png"


def test_sanitize_ext_caps_length():
    assert sanitize_ext(".abcdefghijklmno") == ".abcdefgh"  # 8 chars max


def test_sanitize_ext_empty_returns_empty():
    assert sanitize_ext("") == ""
    assert sanitize_ext(".") == ""


def test_sanitize_ext_adds_leading_dot():
    assert sanitize_ext("png") == ".png"
