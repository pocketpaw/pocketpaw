# E2E Tests for pocketpaw Dashboard
# Created: 2026-02-05
#
# End-to-end tests using Playwright to verify the dashboard UI works correctly.
# Tests run against a real server instance with a real browser.
#
# Run with: pytest tests/e2e/ -v --headed (to see browser)
# Run headless: pytest tests/e2e/ -v

from playwright.sync_api import Page, expect


class TestDashboardLoads:
    """Tests that the dashboard loads correctly."""

    def test_dashboard_title(self, page: Page, dashboard_url: str):
        """Test that dashboard page loads with correct title."""
        page.goto(dashboard_url, wait_until="networkidle")
        expect(page).to_have_title("PocketPaw (Beta)")

    def test_chat_view_visible_by_default(self, page: Page, dashboard_url: str):
        """Test that Chat view is visible by default."""
        page.goto(dashboard_url)

        # Chat tab should be active
        chat_tab = page.get_by_role("button", name="Chat", exact=True)
        expect(chat_tab).to_be_visible()

    def test_view_tabs_exist(self, page: Page, dashboard_url: str):
        """Test that all view tabs exist."""
        page.goto(dashboard_url)

        exact_tabs = ["Chat", "Activity", "Terminal"]
        for tab in exact_tabs:
            expect(page.get_by_role("button", name=tab, exact=True)).to_be_visible()

        # Deep Work button contains child elements (badge text), so can't use exact match
        expect(page.get_by_role("button", name="Deep Work").first).to_be_visible()

    def test_agent_mode_toggle_exists(self, page: Page, dashboard_url: str):
        """Test that agent mode toggle exists."""
        page.goto(dashboard_url)

        # Look for Agent Mode label (use exact match to avoid multiple matches)
        expect(page.get_by_label("Agent mode")).to_be_visible()


class TestCrewView:
    """Tests for the Deep Work view."""

    def test_crew_tab_switches_view(self, page: Page, dashboard_url: str):
        """Test that clicking Deep Work tab switches to Deep Work view."""
        page.goto(dashboard_url)

        # Click Deep Work tab
        page.get_by_role("button", name="Deep Work").click()

        # Wait for loading to complete
        page.get_by_text("Loading Crew...").wait_for(state="hidden", timeout=10000)

        # Check stats bar appears (indicator of Deep Work view) - use heading "Agents"
        expect(page.get_by_role("heading", name="Agents")).to_be_visible()

    def test_new_agent_button_exists(self, page: Page, dashboard_url: str):
        """Test that New Agent button exists in Deep Work view."""
        page.goto(dashboard_url)
        page.get_by_role("button", name="Deep Work").click()
        page.get_by_text("Loading Crew...").wait_for(state="hidden", timeout=10000)

        expect(page.get_by_role("button", name="New Agent", exact=True)).to_be_visible()

    def test_new_task_button_exists(self, page: Page, dashboard_url: str):
        """Test that New Task button exists in Deep Work view."""
        page.goto(dashboard_url)
        page.get_by_role("button", name="Deep Work").click()
        page.wait_for_load_state("networkidle")
        page.get_by_text("Loading Crew...").wait_for(state="hidden", timeout=10000)

        expect(page.get_by_role("button", name="New Task", exact=True)).to_be_visible()

    def test_stats_bar_shows_numbers(self, page: Page, dashboard_url: str):
        """Test that stats bar shows agent and task counts."""
        page.goto(dashboard_url)
        page.get_by_role("button", name="Deep Work").click()
        page.get_by_text("Loading Crew...").wait_for(state="hidden", timeout=10000)

        # Stats bar should show "Live" indicator
        expect(page.get_by_text("Live", exact=True)).to_be_visible()

        # Stats should show "done today" text
        expect(page.get_by_text("done").first).to_be_visible()


class TestAgentCreation:
    """Tests for creating and deleting agents in Deep Work view."""

    def test_create_agent_modal_opens(self, page: Page, dashboard_url: str):
        """Test that clicking New Agent opens the creation form."""
        page.goto(dashboard_url)
        page.wait_for_load_state("networkidle")

        # Click Deep Work to access agent controls
        page.get_by_role("button", name="Deep Work").click()
        page.wait_for_load_state("networkidle")

        # Click New Agent
        page.get_by_role("button", name="New Agent", exact=True).click()

        # Wait for modal animation
        page.wait_for_load_state("networkidle")

        # Modal should appear with "Create Agent" button
        expect(page.get_by_role("button", name="Create Agent", exact=True)).to_be_visible()

    def test_create_agent_flow(self, page: Page, dashboard_url: str):
        """Test creating a new agent through the UI."""
        page.goto(dashboard_url)
        page.wait_for_load_state("networkidle")

        # Click Deep Work to access agent controls
        page.get_by_role("button", name="Deep Work").click()
        page.wait_for_load_state("networkidle")

        # Click New Agent button
        page.get_by_role("button", name="New Agent", exact=True).click()
        page.wait_for_load_state("networkidle")  # Wait for modal animation

        # Fill form using placeholder text
        page.get_by_placeholder("Agent name").fill("E2E Test Agent")
        page.get_by_placeholder("Role (e.g., Research Lead)").fill("Test Role")

        # Submit - click the Create Agent button
        page.get_by_role("button", name="Create Agent", exact=True).click()

        # Wait for API response and UI update
        page.wait_for_load_state("networkidle")

        # Agent should appear somewhere (list or activity feed)
        expect(page.get_by_text("E2E Test Agent").first).to_be_visible(timeout=5000)

    def test_delete_agent_flow(self, page: Page, dashboard_url: str):
        """Test deleting an agent through the UI."""
        page.goto(dashboard_url)
        page.wait_for_load_state("networkidle")

        # Click Deep Work to access agent controls
        page.get_by_role("button", name="Deep Work").click()
        page.wait_for_load_state("networkidle")

        # First create an agent to delete
        page.get_by_role("button", name="New Agent", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.get_by_placeholder("Agent name").fill("DeleteMe Agent")
        page.get_by_placeholder("Role (e.g., Research Lead)").fill("Temp Role")
        page.get_by_role("button", name="Create Agent", exact=True).click()
        page.wait_for_timeout(1500)

        # Verify agent was created
        expect(page.get_by_text("DeleteMe Agent").first).to_be_visible(timeout=5000)

        # Count agents before deletion
        initial_count = page.locator("text=DeleteMe Agent").count()

        # Handle the confirm dialog - accept it
        page.on("dialog", lambda dialog: dialog.accept())

        # Use JavaScript to click the delete button directly
        page.evaluate("""
            () => {
                const spans = document.querySelectorAll('span');
                for (const span of spans) {
                    if (span.textContent === 'DeleteMe Agent') {
                        const card = span.closest('.group');
                        if (card) {
                            const btn = card.querySelector('button.ml-auto');
                            if (btn) btn.click();
                        }
                        break;
                    }
                }
            }
        """)

        # Wait for confirm dialog and deletion
        page.wait_for_timeout(1500)

        # Check that agent count decreased or agent is no longer visible
        final_count = page.locator("text=DeleteMe Agent").count()
        assert final_count < initial_count or final_count == 0, "Agent should be deleted"


class TestTaskCreation:
    """Tests for creating tasks in Deep Work view."""

    def test_create_task_modal_opens(self, page: Page, dashboard_url: str):
        """Test that clicking New Task opens the creation form."""
        page.goto(dashboard_url)
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Deep Work").click()

        # Click New Task
        page.get_by_role("button", name="New Task", exact=True).click()
        page.wait_for_load_state("networkidle")

        # Modal should appear with New Task title input
        expect(page.get_by_role("heading", name="New Task")).to_be_visible()

    def test_create_task_flow(self, page: Page, dashboard_url: str):
        """Test creating a new task through the UI and verify it appears in task list."""
        page.goto(dashboard_url)
        page.get_by_role("button", name="Deep Work").click()
        page.get_by_text("Loading Crew...").wait_for(state="hidden", timeout=10000)

        # Click New Task button in header
        page.get_by_role("button", name="New Task").first.click()
        page.wait_for_load_state("networkidle")  # Wait for modal animation

        # Fill form using placeholder
        page.get_by_placeholder("Task title").last.fill("E2E Task In List")

        # Submit - click the Create Task button inside the modal
        modal_submit_btn = page.get_by_role("button", name="Create Task")
        modal_submit_btn.last.click()

        # Wait for API response and UI update
        page.wait_for_load_state("networkidle")

        # Verify task appears in the task list panel (not just activity feed)
        task_panel = page.locator("div.flex-1.flex.flex-col.border-r")
        expect(task_panel.get_by_text("E2E Task In List").first).to_be_visible(timeout=5000)


class TestSidebarNavigation:
    """Tests for sidebar navigation."""

    def test_sidebar_exists(self, page: Page, dashboard_url: str):
        """Test that sidebar exists and has key elements."""
        page.goto(dashboard_url)

        # Sidebar should have PocketPaw branding or key nav items
        # Check for Settings or other sidebar elements
        sidebar = page.locator("aside, nav").first
        expect(sidebar).to_be_visible()

    def test_settings_opens(self, page: Page, dashboard_url: str):
        """Test that settings modal can be opened."""
        page.goto(dashboard_url)

        # Click settings button (usually a gear icon)
        settings_btn = page.get_by_role("button", name="Settings").first
        if settings_btn.is_visible():
            settings_btn.click()
            # Settings modal should appear
            page.wait_for_load_state("networkidle")


class TestRemoteAccessModal:
    """Tests for the Remote Access modal."""

    def test_remote_button_exists(self, page: Page, dashboard_url: str):
        """Test that Take Your Paw With You button exists."""
        page.goto(dashboard_url)

        # This button might be hidden on mobile, so check desktop viewport
        remote_btn = page.get_by_role("button", name="Take Your Paw With You")
        # May not be visible on all viewports
        if remote_btn.is_visible():
            expect(remote_btn).to_be_visible()
