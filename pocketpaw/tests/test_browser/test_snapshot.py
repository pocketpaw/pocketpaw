# Browser snapshot generator tests
# Changes: Initial creation with comprehensive test cases
"""Tests for browser accessibility tree snapshot generator."""

from pocketpaw.browser.snapshot import (
    AccessibilityNode,
    RefMap,
    SnapshotGenerator,
)


class TestRefMap:
    """Tests for the RefMap dataclass."""

    def test_create_empty_refmap(self):
        """Should create an empty RefMap."""
        refmap = RefMap()
        assert refmap.refs == {}
        assert refmap.next_ref == 1

    def test_add_ref(self):
        """Should add a reference and return the ref number."""
        refmap = RefMap()
        ref = refmap.add("button#submit")
        assert ref == 1
        assert refmap.refs[1] == "button#submit"
        assert refmap.next_ref == 2

    def test_add_multiple_refs(self):
        """Should track multiple references with incrementing numbers."""
        refmap = RefMap()
        ref1 = refmap.add("input#username")
        ref2 = refmap.add("input#password")
        ref3 = refmap.add("button#login")

        assert ref1 == 1
        assert ref2 == 2
        assert ref3 == 3
        assert refmap.refs[1] == "input#username"
        assert refmap.refs[2] == "input#password"
        assert refmap.refs[3] == "button#login"

    def test_get_selector(self):
        """Should retrieve selector by ref number."""
        refmap = RefMap()
        refmap.add("div.container")
        selector = refmap.get_selector(1)
        assert selector == "div.container"

    def test_get_selector_missing(self):
        """Should return None for missing ref."""
        refmap = RefMap()
        assert refmap.get_selector(999) is None


class TestAccessibilityNode:
    """Tests for AccessibilityNode dataclass."""

    def test_create_simple_node(self):
        """Should create a simple accessibility node."""
        node = AccessibilityNode(
            role="button",
            name="Submit",
        )
        assert node.role == "button"
        assert node.name == "Submit"
        assert node.children == []
        assert node.properties == {}

    def test_create_node_with_properties(self):
        """Should create node with extra properties."""
        node = AccessibilityNode(role="heading", name="Welcome", properties={"level": 1})
        assert node.properties["level"] == 1

    def test_create_node_with_children(self):
        """Should create node with child nodes."""
        child = AccessibilityNode(role="text", name="Hello")
        parent = AccessibilityNode(role="paragraph", name="", children=[child])
        assert len(parent.children) == 1
        assert parent.children[0].name == "Hello"


class TestSnapshotGenerator:
    """Tests for SnapshotGenerator class."""

    def test_generate_empty_tree(self):
        """Should handle empty accessibility tree."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(role="WebArea", name="", children=[])

        snapshot, refmap = generator.generate(node)

        assert "Page:" in snapshot or snapshot.strip() == ""
        assert isinstance(refmap, RefMap)

    def test_generate_simple_heading(self):
        """Should render heading with level attribute."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Test Page",
            children=[
                AccessibilityNode(role="heading", name="Welcome to Test", properties={"level": 1})
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "heading" in snapshot
        assert "Welcome to Test" in snapshot
        assert "[level=1]" in snapshot

    def test_generate_interactive_elements_get_refs(self):
        """Should assign refs to interactive elements."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Form Page",
            children=[
                AccessibilityNode(role="textbox", name="Username"),
                AccessibilityNode(role="textbox", name="Password"),
                AccessibilityNode(role="button", name="Submit"),
            ],
        )

        snapshot, refmap = generator.generate(node)

        # All interactive elements should have refs
        assert "[ref=1]" in snapshot
        assert "[ref=2]" in snapshot
        assert "[ref=3]" in snapshot
        assert len(refmap.refs) == 3

    def test_generate_non_interactive_elements_no_refs(self):
        """Should NOT assign refs to non-interactive elements."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Static Page",
            children=[
                AccessibilityNode(role="heading", name="Title", properties={"level": 1}),
                AccessibilityNode(role="paragraph", name="Some text"),
                AccessibilityNode(role="StaticText", name="More text"),
            ],
        )

        snapshot, refmap = generator.generate(node)

        # Static elements should not have refs
        assert "[ref=" not in snapshot
        assert len(refmap.refs) == 0

    def test_generate_link_elements(self):
        """Should assign refs to links."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Links Page",
            children=[
                AccessibilityNode(role="link", name="Click here"),
                AccessibilityNode(role="link", name="Learn more"),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "[ref=1]" in snapshot
        assert "[ref=2]" in snapshot
        assert "link" in snapshot
        assert "Click here" in snapshot

    def test_generate_nested_structure(self):
        """Should handle nested elements correctly."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Form",
            children=[
                AccessibilityNode(
                    role="form",
                    name="Login Form",
                    children=[
                        AccessibilityNode(role="textbox", name="Email"),
                        AccessibilityNode(role="textbox", name="Password"),
                        AccessibilityNode(role="button", name="Sign In"),
                    ],
                )
            ],
        )

        snapshot, refmap = generator.generate(node)

        # Should show hierarchy through indentation
        assert "form" in snapshot
        assert "textbox" in snapshot
        assert "button" in snapshot
        assert "[ref=1]" in snapshot
        assert "[ref=2]" in snapshot
        assert "[ref=3]" in snapshot

    def test_generate_focused_element(self):
        """Should mark focused element."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Form",
            children=[
                AccessibilityNode(role="textbox", name="Username", properties={"focused": True}),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "[focused]" in snapshot

    def test_generate_disabled_element(self):
        """Should mark disabled element."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Form",
            children=[
                AccessibilityNode(role="button", name="Submit", properties={"disabled": True}),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "[disabled]" in snapshot

    def test_generate_password_field(self):
        """Should indicate password type for password fields."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Login",
            children=[
                AccessibilityNode(role="textbox", name="Password", properties={"type": "password"}),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "[type=password]" in snapshot

    def test_generate_checkbox(self):
        """Should render checkbox with checked state."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Settings",
            children=[
                AccessibilityNode(
                    role="checkbox", name="Remember me", properties={"checked": True}
                ),
                AccessibilityNode(
                    role="checkbox", name="Newsletter", properties={"checked": False}
                ),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "[checked]" in snapshot
        assert "Remember me" in snapshot

    def test_generate_combobox(self):
        """Should handle combobox/dropdown elements."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Form",
            children=[
                AccessibilityNode(role="combobox", name="Country", properties={"expanded": False}),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "combobox" in snapshot
        assert "[ref=1]" in snapshot

    def test_generate_with_page_title_and_url(self):
        """Should include page title and URL in snapshot."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="GitHub Login",
            children=[
                AccessibilityNode(role="heading", name="Sign in", properties={"level": 1}),
            ],
        )

        snapshot, refmap = generator.generate(
            node, title="GitHub Login", url="https://github.com/login"
        )

        assert "Page: GitHub Login" in snapshot
        assert "URL: https://github.com/login" in snapshot

    def test_selector_generation_for_button(self):
        """Should generate appropriate selector for button."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Page",
            children=[
                AccessibilityNode(role="button", name="Submit Form"),
            ],
        )

        snapshot, refmap = generator.generate(node)

        selector = refmap.get_selector(1)
        assert selector is not None
        # Selector should contain role and name info for finding the element
        assert "button" in selector.lower() or "Submit" in selector

    def test_generate_list_structure(self):
        """Should handle list elements."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Menu",
            children=[
                AccessibilityNode(
                    role="list",
                    name="Navigation",
                    children=[
                        AccessibilityNode(role="listitem", name="Home"),
                        AccessibilityNode(role="listitem", name="About"),
                        AccessibilityNode(role="listitem", name="Contact"),
                    ],
                )
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "list" in snapshot
        assert "Home" in snapshot
        assert "About" in snapshot

    def test_generate_image_with_alt(self):
        """Should include image alt text."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Gallery",
            children=[
                AccessibilityNode(role="img", name="Profile picture of John"),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "img" in snapshot or "image" in snapshot
        assert "Profile picture of John" in snapshot

    def test_skip_hidden_elements(self):
        """Should skip hidden elements."""
        generator = SnapshotGenerator()
        node = AccessibilityNode(
            role="WebArea",
            name="Page",
            children=[
                AccessibilityNode(role="button", name="Visible"),
                AccessibilityNode(role="button", name="Hidden", properties={"hidden": True}),
            ],
        )

        snapshot, refmap = generator.generate(node)

        assert "Visible" in snapshot
        assert "Hidden" not in snapshot

    def test_truncate_long_names(self):
        """Should truncate very long element names."""
        generator = SnapshotGenerator()
        long_name = "A" * 500  # Very long name
        node = AccessibilityNode(
            role="WebArea",
            name="Page",
            children=[
                AccessibilityNode(role="paragraph", name=long_name),
            ],
        )

        snapshot, refmap = generator.generate(node)

        # Name should be truncated to reasonable length
        assert len(snapshot) < len(long_name)


class TestSnapshotFromPlaywrightTree:
    """Tests for converting Playwright accessibility tree format."""

    def test_from_playwright_dict(self):
        """Should convert Playwright accessibility tree dict to AccessibilityNode."""
        playwright_tree = {
            "role": "WebArea",
            "name": "Test Page",
            "children": [
                {"role": "heading", "name": "Title", "level": 1},
                {"role": "button", "name": "Click Me"},
            ],
        }

        node = AccessibilityNode.from_playwright_dict(playwright_tree)

        assert node.role == "WebArea"
        assert node.name == "Test Page"
        assert len(node.children) == 2
        assert node.children[0].role == "heading"
        assert node.children[0].properties.get("level") == 1

    def test_from_playwright_dict_nested(self):
        """Should handle deeply nested Playwright tree."""
        playwright_tree = {
            "role": "WebArea",
            "name": "Page",
            "children": [
                {
                    "role": "navigation",
                    "name": "Main Nav",
                    "children": [
                        {
                            "role": "list",
                            "name": "",
                            "children": [
                                {"role": "link", "name": "Home"},
                                {"role": "link", "name": "About"},
                            ],
                        }
                    ],
                }
            ],
        }

        node = AccessibilityNode.from_playwright_dict(playwright_tree)

        assert node.children[0].role == "navigation"
        nav_list = node.children[0].children[0]
        assert nav_list.role == "list"
        assert len(nav_list.children) == 2
