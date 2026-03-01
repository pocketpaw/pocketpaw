/**
 * PocketPaw - Routines Feature Module
 * Updated: 2026-02-16
 *
 * Navigation model:
 *   routineView = 'list'   → shows all routines
 *   routineView = 'detail' → shows one routine (read or edit mode)
 *
 * routineEditMode = false → read-only detail
 * routineEditMode = true  → editable form
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Routines = {
  name: "Routines",

  getState() {
    const localTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    return {
      showRoutines: false,
      routineView: "list", // 'list' | 'detail'
      routineDetailTab: "info", // 'info' | 'history'
      routineEditMode: false,
      routines: [],
      routineSelected: null, // the full routine object being viewed
      routineHistory: [],
      routineLoading: false,
      routineHistoryLoading: false,
      routineSaving: false,
      tzSearch: "",
      tzDropdownOpen: false,
      routineForm: {
        title: "",
        recipient: "",
        recipient_name: "",
        template: "",
        frequency: "daily",
        weekday: "1",
        hour: "9",
        minute: "0",
        timezone: localTz,
        channel: "whatsapp",
        enabled: true,
        variables: [],
      },
      formErrors: {},
      commonTimezones: [
        "UTC",
        "Asia/Kolkata",
        "Asia/Colombo",
        "Asia/Dhaka",
        "Asia/Karachi",
        "Asia/Dubai",
        "Asia/Singapore",
        "Asia/Tokyo",
        "Asia/Shanghai",
        "Asia/Seoul",
        "Europe/London",
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Moscow",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Sao_Paulo",
        "Australia/Sydney",
        "Pacific/Auckland",
      ],
    };
  },

  getMethods() {
    return {
      // ── Open modal ────────────────────────────────────────────
      openRoutines() {
        this.showRoutines = true;
        this.routineView = "list";
        this.routineSelected = null;
        this.routineEditMode = false;
        Object.keys(this.formErrors).forEach((k) => delete this.formErrors[k]);
        this.loadRoutines();
        this._refreshRoutineIcons();
      },
      _refreshRoutineIcons() {
        this.$nextTick(() => {
          setTimeout(() => {
            if (window.lucide) window.lucide.createIcons();
          }, 30);
        });
      },

      // ── List view ─────────────────────────────────────────────
      async loadRoutines() {
        this.routineLoading = true;
        try {
          const resp = await fetch("/api/routines");
          const data = await resp.json();
          this.routines = data.routines || [];
        } catch (e) {
          this.showToast("Failed to load routines", "error");
        } finally {
          this.routineLoading = false;
          this._refreshRoutineIcons();
        }
      },

      // ── Open detail (click row) ───────────────────────────────
      openRoutineDetail(routine, editMode = false) {
        this.routineSelected = { ...routine };
        this.routineEditMode = editMode;
        this.routineDetailTab = "info";
        this.routineView = "detail";
        if (editMode) this._populateForm(routine);
        this._refreshRoutineIcons();
      },

      // ── Back to list ──────────────────────────────────────────
      routineGoBack() {
        this.routineView = "list";
        this.routineSelected = null;
        this.routineEditMode = false;
        this.tzDropdownOpen = false;
        this.tzSearch = "";
        Object.keys(this.formErrors).forEach((k) => delete this.formErrors[k]);
        this._refreshRoutineIcons();
      },

      // ── Toggle edit mode (from detail view) ───────────────────
      toggleRoutineEdit() {
        this.routineEditMode = !this.routineEditMode;
        if (this.routineEditMode) {
          this._populateForm(this.routineSelected);
        }
        Object.keys(this.formErrors).forEach((k) => delete this.formErrors[k]);
        this._refreshRoutineIcons();
      },

      _populateForm(routine) {
        const cf = this._parseCron(routine.schedule);
        this.routineForm = {
          title: routine.title || "",
          recipient: routine.recipient || "",
          recipient_name: routine.recipient_name || "",
          template: routine.template || "",
          frequency: cf.frequency || "daily",
          weekday: cf.weekday || "1",
          hour: cf.hour || "9",
          minute: cf.minute || "0",
          timezone: routine.timezone || "UTC",
          channel: routine.channel || "whatsapp",
          enabled: routine.enabled !== false,
          variables: this._dictToVars(routine.variables),
        };
        this.tzSearch = "";
        this.tzDropdownOpen = false;
        this.$nextTick(() => this.syncRoutineVariables());
      },

      // ── New routine (from list) ───────────────────────────────
      openNewRoutine() {
        const localTz =
          Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
        this.routineSelected = null;
        this.routineEditMode = true;
        this.routineDetailTab = "info";
        this.routineView = "detail";
        this.routineForm = {
          title: "",
          recipient: "",
          recipient_name: "",
          template: "",
          frequency: "daily",
          weekday: "1",
          hour: "9",
          minute: "0",
          timezone: localTz,
          channel: "whatsapp",
          enabled: true,
          variables: [],
        };
        this.tzSearch = "";
        this.tzDropdownOpen = false;
        Object.keys(this.formErrors).forEach((k) => delete this.formErrors[k]);
        this._refreshRoutineIcons();
      },

      //validate fields before saving
      validateAndSaveRoutine() {
        // 1. Clear by deleting each key — do NOT replace the object reference
        Object.keys(this.formErrors).forEach((k) => delete this.formErrors[k]);

        // 2. Run validation
        const errors = this.validateScheduledMessageFields(this.routineForm);

        // 3. Copy errors in — again, mutate don't replace
        Object.assign(this.formErrors, errors);

        if (Object.keys(this.formErrors).length > 0) {
          this.showToast(
            `Please fix ${Object.keys(this.formErrors).length} error(s)`,
            "error"
          );

          this.$nextTick(() => {
            const firstErrorField = Object.keys(this.formErrors)[0];
            const errorElement = this.$el.querySelector(
              `[data-field="${firstErrorField}"]`
            );
            if (errorElement) {
              errorElement.scrollIntoView({
                behavior: "smooth",
                block: "center",
              });
            }
          });

          this._refreshRoutineIcons();
          return;
        }

        this.saveRoutine();
      },

      // ── Save (create or update) ───────────────────────────────
      async saveRoutine() {
        console.log("FORM TITLE:", this.routineForm.title);
        console.log("FULL FORM:", JSON.parse(JSON.stringify(this.routineForm)));

        const f = this.routineForm;

        if (!f.recipient.trim()) {
          this.showToast("Please enter a recipient", "error");
          return;
        }
        if (!f.template.trim()) {
          this.showToast("Please enter a message", "error");
          return;
        }

        const payload = {
          title: f.title.trim(),
          recipient: f.recipient.trim(),
          recipient_name: f.recipient_name.trim(),
          template: f.template.trim(),
          schedule: this._buildCron(f),
          timezone: f.timezone || "UTC",
          channel: f.channel,
          enabled: f.enabled,
          variables: this._varsToDict(f.variables),
        };
        this.routineSaving = true;
        try {
          const isEdit = !!this.routineSelected;
          const url = isEdit
            ? `/api/routines/${this.routineSelected.id}`
            : "/api/routines";
          const method = isEdit ? "PATCH" : "POST";
          const resp = await fetch(url, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const data = await resp.json();
          if (data.error) throw new Error(data.error);

          const routine = data.routine;
          if (isEdit) {
            const idx = this.routines.findIndex((r) => r.id === routine.id);
            if (idx !== -1) this.routines[idx] = routine;
          } else {
            this.routines.push(routine);
          }
          this.routineSelected = routine;
          this.routineEditMode = false;
          this.showToast(
            isEdit ? "Routine updated" : "Routine created",
            "success"
          );
          this._refreshRoutineIcons();
        } catch (e) {
          this.showToast(e.message || "Failed to save", "error");
        } finally {
          this.routineSaving = false;
        }
      },

      // ── Toggle enabled ────────────────────────────────────────
      async toggleRoutine(id, event) {
        if (event) event.stopPropagation();
        try {
          const resp = await fetch(`/api/routines/${id}/toggle`, {
            method: "POST",
          });
          const data = await resp.json();
          if (data.error) throw new Error(data.error);
          const idx = this.routines.findIndex((r) => r.id === id);
          if (idx !== -1) this.routines[idx] = data.routine;
          if (this.routineSelected?.id === id)
            this.routineSelected = data.routine;
          this._refreshRoutineIcons();
        } catch (e) {
          this.showToast(e.message || "Failed", "error");
        }
      },

      // ── Delete ────────────────────────────────────────────────
      async deleteRoutine(id, event) {
        if (event) event.stopPropagation();
        if (!confirm("Delete this routine?")) return;
        try {
          const resp = await fetch(`/api/routines/${id}`, { method: "DELETE" });
          const data = await resp.json();
          if (data.error) throw new Error(data.error);
          this.routines = this.routines.filter((r) => r.id !== id);
          this.showToast("Routine deleted", "success");
          if (this.routineSelected?.id === id) this.routineGoBack();
        } catch (e) {
          this.showToast(e.message || "Failed", "error");
        }
      },

      //Validate scheduled message entry
      validateScheduledMessageFields(entry) {
        const errors = {};

        // Title
        if (!entry.title || entry.title.trim() === "") {
          errors.title = "Routine title is required";
        } else if (entry.title.trim().length < 3) {
          errors.title = "Routine name must be at least 3 characters";
        }

        // Recipient
        if (!entry.recipient || entry.recipient.trim() === "") {
          errors.recipient = "Recipient is required";
        } else {
          const channel = entry.channel;
          const recipient = entry.recipient.trim();

          switch (channel) {
            case "whatsapp":
            case "signal": {
              const phoneRegex = /^\+?[1-9]\d{1,14}$/;
              if (!phoneRegex.test(recipient.replace(/\s/g, ""))) {
                errors.recipient = `${
                  channel === "whatsapp" ? "WhatsApp" : "Signal"
                } recipient must be a valid phone number (e.g., +1234567890)`;
              }
              break;
            }
            case "telegram":
              if (!(/^\d+$/.test(recipient) || recipient.startsWith("@"))) {
                errors.recipient =
                  "Telegram recipient must be numeric user ID or @username";
              }
              break;
            case "discord":
              if (!/^\d+$/.test(recipient)) {
                errors.recipient = "Discord recipient must be a numeric ID";
              }
              break;
            case "slack":
              if (!/^[CUD][A-Z0-9]+$/.test(recipient)) {
                errors.recipient =
                  "Slack recipient must be a valid ID (e.g. C123ABC, U456DEF)";
              }
              break;
          }
        }

        // Recipient name
        if (!entry.recipient_name || entry.recipient_name.trim() === "") {
          errors.recipient_name = "Recipient name is required";
        } else if (entry.recipient_name.trim().length < 2) {
          errors.recipient_name = "Name must be at least 2 characters";
        }

        // Template
        if (!entry.template || entry.template.trim() === "") {
          errors.template = "Message template is required";
        } else if (entry.template.trim().length < 5) {
          errors.template = "Message must be at least 5 characters";
        }

        // Channel
        if (!entry.channel) {
          errors.channel = "Please select a channel";
        }

        // Template variables
        if (entry.template && entry.template.trim().length >= 5) {
          const placeholders = [...entry.template.matchAll(/\{(\w+)\}/g)].map(
            (m) => m[1]
          );
          const customVars = placeholders.filter((p) => p !== "name");

          if (customVars.length > 0) {
            if (!entry.variables || !Array.isArray(entry.variables)) {
              errors.variables = `Template uses variables: ${customVars.join(
                ", "
              )}. Please add them below.`;
            } else {
              const varMap = {};
              entry.variables.forEach((v) => {
                if (v.key) varMap[v.key] = v.value;
              });
              const missingVars = customVars.filter(
                (varName) => !varMap[varName] || varMap[varName].trim() === ""
              );
              if (missingVars.length > 0) {
                errors.variables = `Please provide values for: ${missingVars.join(
                  ", "
                )}`;
              }
            }
          }
        }

        return errors;
      },

      // ── History per routine ───────────────────────────────────
      async loadRoutineHistory() {
        if (!this.routineSelected) return;
        this.routineHistoryLoading = true;
        try {
          const resp = await fetch(
            `/api/routines/history?entry_id=${this.routineSelected.id}`
          );
          const data = await resp.json();
          this.routineHistory = data.history || [];
        } catch (e) {
          this.routineHistory = [];
        } finally {
          this.routineHistoryLoading = false;
        }
      },

      // ── Schedule helpers ──────────────────────────────────────
      _buildCron(f) {
        const h = parseInt(f.hour, 10),
          m = parseInt(f.minute, 10);
        switch (f.frequency) {
          case "weekdays":
            return `${m} ${h} * * 1-5`;
          case "weekends":
            return `${m} ${h} * * 0,6`;
          case "weekly":
            return `${m} ${h} * * ${f.weekday}`;
          default:
            return `${m} ${h} * * *`;
        }
      },

      cronToLabel(cron) {
        if (!cron) return "";
        const p = cron.split(" ");
        if (p.length < 5) return cron;
        const [min, hour, , , dow] = p;
        const t = this._fmtTime(parseInt(hour), parseInt(min));
        if (dow === "*") return `Daily at ${t}`;
        if (dow === "1-5") return `Weekdays at ${t}`;
        if (dow === "0,6") return `Weekends at ${t}`;
        const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
        const d = parseInt(dow, 10);
        return !isNaN(d) && days[d] ? `Every ${days[d]} at ${t}` : cron;
      },

      _fmtTime(h, m) {
        return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${
          h >= 12 ? "PM" : "AM"
        }`;
      },

      _parseCron(cron) {
        const p = (cron || "").split(" ");
        if (p.length < 5) return {};
        const [min, hour, , , dow] = p;
        let frequency = "daily";
        if (dow === "1-5") frequency = "weekdays";
        else if (dow === "0,6") frequency = "weekends";
        else if (dow !== "*") frequency = "weekly";
        return {
          frequency,
          weekday: frequency === "weekly" ? dow : "1",
          hour: String(parseInt(hour)),
          minute: String(parseInt(min)),
        };
      },

      routineHours() {
        return Array.from({ length: 24 }, (_, i) => ({
          value: String(i),
          label: `${i % 12 || 12} ${i >= 12 ? "PM" : "AM"}`,
        }));
      },

      routineMinutes() {
        return Array.from({ length: 12 }, (_, i) => ({
          value: String(i * 5),
          label: String(i * 5).padStart(2, "0"),
        }));
      },

      // ── Timezone helpers ──────────────────────────────────────
      filteredTimezones() {
        const q = this.tzSearch.toLowerCase();
        return this.commonTimezones.filter((tz) =>
          tz.toLowerCase().includes(q)
        );
      },

      selectTimezone(tz) {
        this.routineForm.timezone = tz;
        this.tzDropdownOpen = false;
        this.tzSearch = "";
      },

      // ── Variables helpers ─────────────────────────────────────
      addVariable() {
        this.routineForm.variables.push({ key: "", value: "" });
      },
      removeVariable(i) {
        this.routineForm.variables.splice(i, 1);
      },
      _varsToDict(vars) {
        const d = {};
        for (const v of vars) if (v.key.trim()) d[v.key.trim()] = v.value;
        return d;
      },
      _dictToVars(dict) {
        return Object.entries(dict || {}).map(([key, value]) => ({
          key,
          value,
        }));
      },

      detectedPlaceholders() {
        const m = (this.routineForm.template || "").match(/\{(\w+)\}/g) || [];
        return [...new Set(m.map((x) => x.slice(1, -1)))].filter(
          (p) => p !== "name"
        );
      },

      // ── Display helpers ───────────────────────────────────────
      channelIcon(ch) {
        return (
          {
            whatsapp: "message-circle",
            telegram: "send",
            discord: "hash",
            slack: "slack",
            signal: "shield",
          }[ch] || "message-square"
        );
      },

      channelLabel(ch) {
        return (
          {
            whatsapp: "WhatsApp",
            telegram: "Telegram",
            discord: "Discord",
            slack: "Slack",
            signal: "Signal",
          }[ch] || ch
        );
      },

      historyStatusColor(s) {
        return (
          {
            sent: "text-white/50",
            delivered: "text-[var(--success-color)]",
            failed: "text-[var(--danger-color)]",
          }[s] || "text-white/30"
        );
      },

      historyStatusIcon(s) {
        return (
          { sent: "check", delivered: "check-check", failed: "x-circle" }[s] ||
          "clock"
        );
      },

      // ── Template variable sync ────────────────────────────────
      // Called on every template keystroke — keeps variables[] in sync with {placeholders}
      syncRoutineVariables() {
        const matches =
          (this.routineForm.template || "").match(/\{(\w+)\}/g) || [];
        const keys = [...new Set(matches.map((m) => m.slice(1, -1)))].filter(
          (k) => k !== "name"
        );
        // Add new keys not yet in the list
        for (const key of keys) {
          if (!this.routineForm.variables.find((v) => v.key === key)) {
            this.routineForm.variables.push({ key, value: "" });
          }
        }
        // Remove keys no longer in template
        this.routineForm.variables = this.routineForm.variables.filter((v) =>
          keys.includes(v.key)
        );
      },

      // Live preview substituting all known values
      previewRoutineMessage() {
        let msg = this.routineForm.template || "";
        msg = msg.replace(
          /\{name\}/g,
          this.routineForm.recipient_name || "{name}"
        );
        for (const v of this.routineForm.variables) {
          if (v.key && v.value) {
            msg = msg.replace(new RegExp(`\\{${v.key}\\}`, "g"), v.value);
          }
        }
        return msg;
      },
    };
  },
};

window.PocketPaw.Loader.register("Routines", window.PocketPaw.Routines);
