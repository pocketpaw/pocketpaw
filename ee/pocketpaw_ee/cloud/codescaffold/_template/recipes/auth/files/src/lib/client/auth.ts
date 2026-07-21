import { createAuthClient } from 'better-auth/svelte';

/**
 * Browser auth client. Talks to the Better Auth endpoints mounted by the server
 * hook at `/api/auth/*` (same origin), so no base URL is required.
 */
export const authClient = createAuthClient();
