# Browser accessibility tree snapshot generator
# Changes: Initial creation with RefMap, AccessibilityNode, and SnapshotGenerator
#
# Converts Playwright's accessibility tree into a semantic text format
# with [ref=N] markers for LLM-based browser control.
"""Accessibility tree to semantic text snapshot converter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RefMap:
    """Maps reference numbers to element selectors.

    The LLM uses [ref=N] in the snapshot to identify elements,
    and we use this mapping to find the actual element for interaction.
    """

    refs: dict[int, str] = field(default_factory=dict)
    next_ref: int = 1

    def add(self, selector: str) -> int:
        """Add a selector and return its reference number."""
        ref = self.next_ref
        self.refs[ref] = selector
        self.next_ref += 1
        return ref

    def get_selector(self, ref: int) -> str | None:
        """Get selector by reference number."""
        return self.refs.get(ref)


@dataclass
class AccessibilityNode:
    """Represents a node in the accessibility tree.

    This is our internal representation, converted from Playwright's
    accessibility tree format.
    """

    role: str
    name: str
    children: list[AccessibilityNode] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_playwright_dict(cls, data: dict[str, Any]) -> AccessibilityNode:
        """Convert Playwright accessibility tree dict to AccessibilityNode.

        Playwright returns accessibility tree as nested dicts with keys like:
        - role: string
        - name: string
        - children: list of child dicts
        - Various properties: level, focused, disabled, checked, etc.
        """
        role = data.get("role", "")
        name = data.get("name", "")

        # Extract known properties
        properties: dict[str, Any] = {}
        property_keys = [
            "level",
            "focused",
            "disabled",
            "checked",
            "expanded",
            "selected",
            "pressed",
            "required",
            "readonly",
            "hidden",
            "type",
            "value",
            "valuetext",
            "description",
        ]
        for key in property_keys:
            if key in data:
                properties[key] = data[key]

        # Convert children recursively
        children = []
        for child_data in data.get("children", []):
            children.append(cls.from_playwright_dict(child_data))

        return cls(role=role, name=name, children=children, properties=properties)


class SnapshotGenerator:
    """Generates semantic text snapshots from accessibility trees.

    Converts the accessibility tree into a text format that an LLM can
    understand and use to control the browser. Interactive elements get
    [ref=N] markers for identification.
    """

    # Roles that represent interactive elements (get refs)
    INTERACTIVE_ROLES = {
        "button",
        "link",
        "textbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "switch",
        "slider",
        "spinbutton",
        "searchbox",
        "tab",
        "treeitem",
    }

    # Roles to skip entirely (not meaningful for LLM)
    SKIP_ROLES = {"none", "presentation", "generic"}

    # Maximum length for element names before truncation
    MAX_NAME_LENGTH = 100

    def __init__(self) -> None:
        self._refmap: RefMap = RefMap()
        self._lines: list[str] = []

    def generate(
        self, tree: AccessibilityNode, title: str | None = None, url: str | None = None
    ) -> tuple[str, RefMap]:
        """Generate a semantic snapshot from an accessibility tree.

        Args:
            tree: The root AccessibilityNode (usually WebArea)
            title: Optional page title
            url: Optional page URL

        Returns:
            Tuple of (snapshot_text, refmap)
        """
        self._refmap = RefMap()
        self._lines = []

        # Add page header
        if title:
            self._lines.append(f"Page: {title}")
        if url:
            self._lines.append(f"URL: {url}")
        if title or url:
            self._lines.append("")

        # Process the tree
        self._process_node(tree, indent=0)

        return "\n".join(self._lines), self._refmap

    def _process_node(self, node: AccessibilityNode, indent: int) -> None:
        """Recursively process a node and its children."""
        # Skip hidden elements
        if node.properties.get("hidden"):
            return

        # Skip meaningless roles
        if node.role.lower() in self.SKIP_ROLES:
            # Still process children
            for child in node.children:
                self._process_node(child, indent)
            return

        # Handle the WebArea (root) specially - just process children
        if node.role == "WebArea":
            for child in node.children:
                self._process_node(child, indent)
            return

        # Build the line for this node
        line_parts = []
        prefix = "  " * indent + "- "

        # Add role
        line_parts.append(node.role)

        # Add name (truncated if needed)
        if node.name:
            name = self._truncate_name(node.name)
            line_parts.append(f'"{name}"')

        # Check if interactive (needs ref)
        is_interactive = node.role.lower() in self.INTERACTIVE_ROLES

        # Generate selector and add ref for interactive elements
        if is_interactive:
            selector = self._generate_selector(node)
            ref = self._refmap.add(selector)
            line_parts.append(f"[ref={ref}]")

        # Add relevant properties as attributes
        attrs = self._format_properties(node)
        if attrs:
            line_parts.extend(attrs)

        # Assemble and add line
        line = prefix + " ".join(line_parts)
        self._lines.append(line)

        # Process children with increased indent
        for child in node.children:
            self._process_node(child, indent + 1)

    def _truncate_name(self, name: str) -> str:
        """Truncate long names with ellipsis."""
        if len(name) > self.MAX_NAME_LENGTH:
            return name[: self.MAX_NAME_LENGTH - 3] + "..."
        return name

    def _generate_selector(self, node: AccessibilityNode) -> str:
        """Generate a selector string for finding this element.

        We use ARIA role and name attributes which Playwright can query.
        Format: role=<role>[name="<name>"]
        """
        selector_parts = [f"role={node.role}"]
        if node.name:
            # Escape quotes in name
            escaped_name = node.name.replace('"', '\\"')
            selector_parts.append(f'[name="{escaped_name}"]')
        return "".join(selector_parts)

    def _format_properties(self, node: AccessibilityNode) -> list[str]:
        """Format node properties as attribute strings."""
        attrs = []
        props = node.properties

        # Level for headings
        if "level" in props:
            attrs.append(f"[level={props['level']}]")

        # Boolean states
        if props.get("focused"):
            attrs.append("[focused]")
        if props.get("disabled"):
            attrs.append("[disabled]")
        if props.get("checked"):
            attrs.append("[checked]")
        if props.get("expanded") is not None:
            attrs.append(f"[expanded={str(props['expanded']).lower()}]")
        if props.get("selected"):
            attrs.append("[selected]")
        if props.get("pressed"):
            attrs.append("[pressed]")
        if props.get("required"):
            attrs.append("[required]")
        if props.get("readonly"):
            attrs.append("[readonly]")

        # Type (e.g., password)
        if "type" in props:
            attrs.append(f"[type={props['type']}]")

        return attrs


__all__ = ["RefMap", "AccessibilityNode", "SnapshotGenerator"]
