"""Unit tests for ee.cloud.uploads.paths."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.uploads.paths import (
    basename,
    is_subpath,
    join_path,
    normalize_path,
    parent_of,
)


class TestNormalizePath:
    def test_root(self):
        assert normalize_path("/") == "/"
        assert normalize_path("") == "/"
        assert normalize_path(None) == "/"

    def test_simple(self):
        assert normalize_path("/foo") == "/foo"
        assert normalize_path("/foo/bar") == "/foo/bar"

    def test_trailing_slash_stripped(self):
        assert normalize_path("/foo/") == "/foo"
        assert normalize_path("/foo/bar/") == "/foo/bar"

    def test_repeated_slashes(self):
        assert normalize_path("//foo//bar///") == "/foo/bar"

    def test_dot_stripped(self):
        assert normalize_path("/foo/./bar") == "/foo/bar"
        assert normalize_path("/./foo") == "/foo"

    def test_dotdot_rejected(self):
        with pytest.raises(ValueError):
            normalize_path("/foo/../bar")
        with pytest.raises(ValueError):
            normalize_path("/..")

    def test_relative_rejected(self):
        with pytest.raises(ValueError):
            normalize_path("foo/bar")

    def test_null_byte_rejected(self):
        with pytest.raises(ValueError):
            normalize_path("/foo\x00bar")

    def test_control_char_rejected(self):
        with pytest.raises(ValueError):
            normalize_path("/foo\x01bar")

    def test_backslash_in_segment_rejected(self):
        with pytest.raises(ValueError):
            normalize_path("/foo\\bar")

    def test_long_path_rejected(self):
        long = "/" + "a" * 2000
        with pytest.raises(ValueError):
            normalize_path(long)

    def test_long_segment_rejected(self):
        seg = "a" * 300
        with pytest.raises(ValueError):
            normalize_path("/" + seg)

    def test_unicode_name(self):
        assert normalize_path("/报告/2026") == "/报告/2026"


class TestIsSubpath:
    def test_equal(self):
        assert is_subpath("/a", "/a")

    def test_strict_descendant(self):
        assert is_subpath("/a", "/a/b")
        assert is_subpath("/a", "/a/b/c")

    def test_root_ancestor(self):
        assert is_subpath("/", "/a")
        assert is_subpath("/", "/")

    def test_sibling_is_not_subpath(self):
        assert not is_subpath("/a", "/ab")
        assert not is_subpath("/a", "/b")


class TestJoinPath:
    def test_root_join(self):
        assert join_path("/", "foo") == "/foo"

    def test_nested_join(self):
        assert join_path("/foo", "bar") == "/foo/bar"

    def test_rejects_slash_in_name(self):
        with pytest.raises(ValueError):
            join_path("/foo", "bar/baz")

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            join_path("/foo", "..")

    def test_normalizes_parent(self):
        assert join_path("/foo/", "bar") == "/foo/bar"


class TestParentAndBasename:
    def test_parent_of(self):
        assert parent_of("/") == "/"
        assert parent_of("/a") == "/"
        assert parent_of("/a/b") == "/a"
        assert parent_of("/a/b/c") == "/a/b"

    def test_basename(self):
        assert basename("/") == ""
        assert basename("/a") == "a"
        assert basename("/a/b") == "b"
