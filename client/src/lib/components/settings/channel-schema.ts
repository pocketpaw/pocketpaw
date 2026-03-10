export type FieldType = "text" | "password" | "select" | "list" | "textarea";

export interface ChannelField {
  key: string;
  label: string;
  type: FieldType;
  required: boolean;
  placeholder?: string;
  helpText?: string;
  options?: { value: string; label: string }[];
  showWhen?: { field: string; value: string };
}

export interface ChannelMeta {
  label: string;
  description: string;
  icon: string;
  color: string;
  fields: ChannelField[];
}

export const CHANNEL_SCHEMA: Record<string, ChannelMeta> = {
  telegram: {
    label: "Telegram",
    description: "Bot messaging via Telegram Bot API",
    icon: "Send",
    color: "sky",
    fields: [
      {
        key: "bot_token",
        label: "Bot Token",
        type: "password",
        required: true,
        placeholder: "123456:ABC-DEF...",
        helpText: "Get from @BotFather on Telegram",
      },
      {
        key: "allowed_user_id",
        label: "Allowed User ID",
        type: "text",
        required: false,
        placeholder: "123456789",
        helpText: "Your Telegram user ID (leave empty to allow all)",
      },
    ],
  },
  discord: {
    label: "Discord",
    description: "Bot integration via Discord gateway",
    icon: "Hash",
    color: "violet",
    fields: [
      {
        key: "bot_token",
        label: "Bot Token",
        type: "password",
        required: true,
        placeholder: "MTIz...",
        helpText: "From Discord Developer Portal → Bot → Token",
      },
      {
        key: "allowed_guild_ids",
        label: "Allowed Guild IDs",
        type: "list",
        required: false,
        placeholder: "guild1, guild2",
        helpText: "Comma-separated server IDs (leave empty to allow all)",
      },
      {
        key: "allowed_user_ids",
        label: "Allowed User IDs",
        type: "list",
        required: false,
        placeholder: "user1, user2",
        helpText: "Comma-separated user IDs (leave empty to allow all)",
      },
    ],
  },
  slack: {
    label: "Slack",
    description: "Workspace bot via Slack Socket Mode",
    icon: "Hash",
    color: "emerald",
    fields: [
      {
        key: "bot_token",
        label: "Bot Token",
        type: "password",
        required: true,
        placeholder: "xoxb-...",
        helpText: "OAuth Bot Token from Slack App settings",
      },
      {
        key: "app_token",
        label: "App-Level Token",
        type: "password",
        required: true,
        placeholder: "xapp-...",
        helpText: "App-Level Token with connections:write scope",
      },
      {
        key: "allowed_channel_ids",
        label: "Allowed Channel IDs",
        type: "list",
        required: false,
        placeholder: "C01ABC, C02DEF",
        helpText: "Comma-separated channel IDs (leave empty to allow all)",
      },
    ],
  },
  whatsapp: {
    label: "WhatsApp",
    description: "Personal QR pairing or Business API",
    icon: "Phone",
    color: "green",
    fields: [
      {
        key: "mode",
        label: "Mode",
        type: "select",
        required: true,
        options: [
          { value: "personal", label: "Personal (QR Code)" },
          { value: "business", label: "Business API" },
        ],
      },
      {
        key: "access_token",
        label: "Access Token",
        type: "password",
        required: true,
        placeholder: "EAABs...",
        helpText: "WhatsApp Business API access token",
        showWhen: { field: "mode", value: "business" },
      },
      {
        key: "phone_number_id",
        label: "Phone Number ID",
        type: "text",
        required: true,
        placeholder: "1234567890",
        helpText: "WhatsApp Business phone number ID",
        showWhen: { field: "mode", value: "business" },
      },
      {
        key: "verify_token",
        label: "Verify Token",
        type: "text",
        required: false,
        placeholder: "my-verify-token",
        helpText: "Webhook verification token",
        showWhen: { field: "mode", value: "business" },
      },
      {
        key: "allowed_phone_numbers",
        label: "Allowed Phone Numbers",
        type: "list",
        required: false,
        placeholder: "+1234567890, +0987654321",
        helpText: "Comma-separated phone numbers (leave empty to allow all)",
      },
    ],
  },
  signal: {
    label: "Signal",
    description: "Encrypted messaging via signal-cli",
    icon: "Shield",
    color: "blue",
    fields: [
      {
        key: "phone_number",
        label: "Phone Number",
        type: "text",
        required: true,
        placeholder: "+1234567890",
        helpText: "Signal phone number registered with signal-cli",
      },
      {
        key: "api_url",
        label: "API URL",
        type: "text",
        required: false,
        placeholder: "http://localhost:8080",
        helpText: "signal-cli REST API URL",
      },
      {
        key: "allowed_phone_numbers",
        label: "Allowed Phone Numbers",
        type: "list",
        required: false,
        placeholder: "+1234567890, +0987654321",
        helpText: "Comma-separated phone numbers (leave empty to allow all)",
      },
    ],
  },
  matrix: {
    label: "Matrix",
    description: "Federated protocol via matrix-nio",
    icon: "AtSign",
    color: "purple",
    fields: [
      {
        key: "homeserver",
        label: "Homeserver URL",
        type: "text",
        required: true,
        placeholder: "https://matrix.org",
        helpText: "Matrix homeserver URL",
      },
      {
        key: "user_id",
        label: "User ID",
        type: "text",
        required: true,
        placeholder: "@bot:matrix.org",
        helpText: "Bot's Matrix user ID",
      },
      {
        key: "access_token",
        label: "Access Token",
        type: "password",
        required: false,
        placeholder: "syt_...",
        helpText: "Access token (use this OR password)",
      },
      {
        key: "password",
        label: "Password",
        type: "password",
        required: false,
        placeholder: "password",
        helpText: "Account password (use this OR access token)",
      },
      {
        key: "device_id",
        label: "Device ID",
        type: "text",
        required: false,
        placeholder: "ABCDEFGHIJ",
        helpText: "Device ID for E2EE sessions",
      },
      {
        key: "allowed_room_ids",
        label: "Allowed Room IDs",
        type: "list",
        required: false,
        placeholder: "!room1:matrix.org, !room2:matrix.org",
        helpText: "Comma-separated room IDs (leave empty to allow all)",
      },
    ],
  },
  teams: {
    label: "Microsoft Teams",
    description: "Enterprise chat via Azure Bot Service",
    icon: "Building2",
    color: "indigo",
    fields: [
      {
        key: "app_id",
        label: "App ID",
        type: "text",
        required: true,
        placeholder: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        helpText: "Azure Bot registration App ID",
      },
      {
        key: "app_password",
        label: "App Password",
        type: "password",
        required: true,
        placeholder: "App secret",
        helpText: "Azure Bot registration App Password",
      },
      {
        key: "allowed_tenant_ids",
        label: "Allowed Tenant IDs",
        type: "list",
        required: false,
        placeholder: "tenant1, tenant2",
        helpText: "Comma-separated Azure tenant IDs (leave empty to allow all)",
      },
      {
        key: "webhook_port",
        label: "Webhook Port",
        type: "text",
        required: false,
        placeholder: "3978",
        helpText: "Port for the Teams webhook listener",
      },
    ],
  },
  google_chat: {
    label: "Google Chat",
    description: "Workspace chat via Pub/Sub or webhook",
    icon: "MessageCircle",
    color: "red",
    fields: [
      {
        key: "mode",
        label: "Mode",
        type: "select",
        required: true,
        options: [
          { value: "webhook", label: "Webhook" },
          { value: "pubsub", label: "Pub/Sub" },
        ],
      },
      {
        key: "service_account_key",
        label: "Service Account Key",
        type: "textarea",
        required: true,
        placeholder: "Path or JSON key",
        helpText: "Path to service account JSON or the key contents",
      },
      {
        key: "project_id",
        label: "Project ID",
        type: "text",
        required: true,
        placeholder: "my-gcp-project",
        helpText: "Google Cloud project ID",
        showWhen: { field: "mode", value: "pubsub" },
      },
      {
        key: "subscription_id",
        label: "Subscription ID",
        type: "text",
        required: true,
        placeholder: "my-subscription",
        helpText: "Pub/Sub subscription ID",
        showWhen: { field: "mode", value: "pubsub" },
      },
      {
        key: "allowed_space_ids",
        label: "Allowed Space IDs",
        type: "list",
        required: false,
        placeholder: "space1, space2",
        helpText: "Comma-separated space IDs (leave empty to allow all)",
      },
    ],
  },
};

export const CHANNEL_ORDER = [
  "telegram",
  "discord",
  "slack",
  "whatsapp",
  "signal",
  "matrix",
  "teams",
  "google_chat",
];

/** Map of channel color names to Tailwind bg/text class pairs */
export const CHANNEL_COLORS: Record<string, { bg: string; text: string }> = {
  sky: { bg: "bg-sky-500/15", text: "text-sky-500" },
  violet: { bg: "bg-violet-500/15", text: "text-violet-500" },
  emerald: { bg: "bg-emerald-500/15", text: "text-emerald-500" },
  green: { bg: "bg-green-500/15", text: "text-green-500" },
  blue: { bg: "bg-blue-500/15", text: "text-blue-500" },
  purple: { bg: "bg-purple-500/15", text: "text-purple-500" },
  indigo: { bg: "bg-indigo-500/15", text: "text-indigo-500" },
  red: { bg: "bg-red-500/15", text: "text-red-500" },
};
