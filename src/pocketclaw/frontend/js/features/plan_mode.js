// Plan Mode — approval flow UI for tool execution.
// Created: 2026-02-07

const PlanMode = {
  activePlan: null,

  init() {
    // Listen for plan events on the WebSocket
    document.addEventListener('ws-message', (e) => {
      const data = e.detail;
      if (data.type === 'plan_proposed') {
        this.showPlan(data.plan);
      } else if (data.type === 'plan_approved') {
        this.hidePlan();
      } else if (data.type === 'plan_rejected') {
        this.hidePlan();
      }
    });
  },

  showPlan(plan) {
    this.activePlan = plan;
    const modal = document.getElementById('plan-approval-modal');
    if (!modal) return;

    // Build steps HTML
    const stepsHtml = (plan.steps || []).map((step, i) => {
      const preview = this.escapeHtml(step.preview || `${step.tool_name}(...)`);
      return `<div class="plan-step">
        <span class="plan-step-num">${i + 1}.</span>
        <span class="plan-step-tool">${this.escapeHtml(step.tool_name)}</span>
        <pre class="plan-step-preview">${preview}</pre>
      </div>`;
    }).join('');

    const content = modal.querySelector('.plan-steps');
    if (content) content.innerHTML = stepsHtml;

    modal.style.display = 'flex';
  },

  hidePlan() {
    this.activePlan = null;
    const modal = document.getElementById('plan-approval-modal');
    if (modal) modal.style.display = 'none';
  },

  approve() {
    if (!this.activePlan) return;
    const ws = window._pocketpawWs;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        action: 'approve_plan',
        session_key: this.activePlan.session_key,
      }));
    }
    this.hidePlan();
  },

  reject() {
    if (!this.activePlan) return;
    const ws = window._pocketpawWs;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        action: 'reject_plan',
        session_key: this.activePlan.session_key,
      }));
    }
    this.hidePlan();
  },

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  },
};

// Auto-init
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => PlanMode.init());
} else {
  PlanMode.init();
}
