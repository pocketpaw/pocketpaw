# Workspace Domain Service - Business Logic for Enterprise Cloud

> A stateless service layer that encapsulates workspace business logic including CRUD operations, member management, and invite handling. Implements role-based access control, seat limits, and event-driven notifications for multi-tenant workspace management.

**Categories:** Enterprise SaaS, Backend Service Architecture, Access Control & Security  
**Concepts:** WorkspaceService, Workspace, User, WorkspaceMembership, Invite, Role-based access control, Seat limit, Soft delete, Token-based invitations, Event bus  
**Words:** 869 | **Version:** 22

---

## Overview

The Workspace Domain service is a stateless business logic layer for managing enterprise cloud workspaces. It handles workspace lifecycle management, member administration, and invitation workflows with built-in authorization checks and seat limiting.

## Workspace CRUD Operations

### Create Workspace
Creates a new workspace with a unique slug and automatically adds the creator as the owner. Validates that the slug is not already in use by checking for existing non-deleted workspaces.

- Slug must be unique across non-deleted workspaces
- Creator is added with `owner` role
- Creator's `active_workspace` is set to the new workspace
- Returns workspace response with member count (1 on creation)

### Get Workspace
Retrieves a workspace by ID, requiring the requesting user to be a member. Returns current member count.

- Requires workspace membership
- Excludes soft-deleted workspaces (`deleted_at` is not null)
- Returns serialized workspace with computed member count

### Update Workspace
Updates workspace metadata (name and settings). Requires admin or higher role.

- Can update name and settings fields
- Requires `admin` minimum role
- Settings are wrapped in `WorkspaceSettings` model

### Delete Workspace
Soft-deletes a workspace by setting `deleted_at` timestamp. Requires owner role.

- Only owners can delete
- Soft delete prevents accidental data loss
- Workspace remains in database but is excluded from queries

### List User Workspaces
Returns all non-deleted workspaces a user belongs to, with member counts.

- Filters by user's workspace memberships
- Excludes deleted workspaces
- Returns serialized list with member counts

## Member Management

### List Members
Lists all members of a workspace with their roles and join dates. Requires workspace membership.

- Returns email, name, avatar, role, and join date for each member
- Includes metadata for each user's workspace membership

### Update Member Role
Changes a member's role within a workspace. Requires admin or higher role.

- Cannot demote the workspace owner
- Owner check prevents removing the last owner
- Validates target user exists and is a member

### Remove Member
Removes a member from a workspace. Requires admin or higher role.

- Cannot remove the workspace owner
- Clears the member's `active_workspace` if it was the removed workspace
- Emits `member.removed` event with workspace_id, user_id, and remover info

## Invite Workflow

### Create Invite
Generates an invitation to a workspace with a secure token. Requires admin or higher role.

- Validates seat limit not exceeded before issuing invite
- Prevents duplicate pending invites for same email and group combination
- Different groups can each have their own pending invite for the same email
- Workspace-level invites (no group) limited to one pending at a time
- Uses 32-byte URL-safe random tokens
- Expired invites can be re-issued

### Validate Invite
Checks invite status by token without authentication. Returns invite details including accepted, revoked, and expired flags.

- No authorization required
- Returns complete invite state

### Accept Invite
Accepts an invitation and adds the user to the workspace. User must be authenticated.

- Validates invite exists and is not already accepted, revoked, or expired
- Checks workspace still exists and is not deleted
- Only checks seat limit for new members (skips check if already a member)
- Adds user with invite's specified role
- Sets `active_workspace` to invited workspace
- Emits `invite.accepted` event with workspace_id, user_id, invite_id, and group_id

### Revoke Invite
Revokes an outstanding invitation. Requires admin or higher role.

- Sets `revoked` flag on invite
- Validates invite exists and belongs to specified workspace

## Authorization Model

### Role Hierarchy
- **owner**: Full workspace control, can delete, cannot be demoted or removed
- **admin**: Can manage members and invites, cannot delete workspace
- **member**: Basic access (implied lower tier)

### Access Control
- Workspace operations require membership via `_get_membership()` check
- Administrative operations require role validation via `check_workspace_role()`
- Owner-specific operations prevent degradation of sole owner status

## Data Models

### Workspace
- `id`: ObjectId
- `name`: Workspace display name
- `slug`: Unique URL identifier
- `owner`: User ID of owner
- `plan`: Plan type
- `seats`: Maximum member count
- `createdAt`: Workspace creation timestamp
- `deleted_at`: Soft delete timestamp (null if active)
- `settings`: WorkspaceSettings object

### WorkspaceMembership
- `workspace`: Workspace ID reference
- `role`: Member role (owner, admin, member)
- `joined_at`: Membership creation timestamp

### Invite
- `workspace`: Target workspace ID
- `email`: Invitee email address
- `role`: Role to assign upon acceptance
- `invited_by`: User ID of inviter
- `token`: Secure URL-safe token
- `group`: Optional group ID for scoped invites
- `accepted`: Boolean flag
- `revoked`: Boolean flag
- `expired`: Boolean flag
- `expires_at`: Expiration timestamp

## Response Serialization

All responses convert internal models to frontend-compatible dictionaries:
- Object IDs are converted to strings
- Timestamps are serialized to ISO format
- Sensitive fields are excluded from responses

## Event Emission

The service emits events via `event_bus` for audit and downstream processing:
- `member.removed`: When a member is removed from a workspace
- `invite.accepted`: When an invitation is accepted

## Error Handling

### Error Types
- `ConflictError`: Slug taken, invite already pending, invite already accepted
- `NotFound`: Workspace, user, member, or invite not found
- `Forbidden`: Permission denied, invite revoked/expired, cannot demote owner
- `SeatLimitError`: Member count equals or exceeds workspace seat limit