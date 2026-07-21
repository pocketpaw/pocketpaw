import type { createAuth } from './index';

// Session/User types inferred from the actual Better Auth configuration, so
// they track your auth options (extra fields, plugins) automatically.
type AuthInstance = ReturnType<typeof createAuth>;

export type Session = AuthInstance['$Infer']['Session']['session'];
export type User = AuthInstance['$Infer']['Session']['user'];
