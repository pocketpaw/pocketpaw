class UIStore {
  sidebarOpen = $state(true);
  searchFocusRequest = $state(0);
  sidebarDrawerOpen = $state(false);

  toggleSidebar(): void {
    this.sidebarOpen = !this.sidebarOpen;
  }

  requestSearchFocus(): void {
    if (!this.sidebarOpen) {
      this.sidebarOpen = true;
    }
    this.searchFocusRequest++;
  }

  openDrawer(): void {
    this.sidebarDrawerOpen = true;
  }

  closeDrawer(): void {
    this.sidebarDrawerOpen = false;
  }

  toggleDrawer(): void {
    this.sidebarDrawerOpen = !this.sidebarDrawerOpen;
  }
}

export const uiStore = new UIStore();
