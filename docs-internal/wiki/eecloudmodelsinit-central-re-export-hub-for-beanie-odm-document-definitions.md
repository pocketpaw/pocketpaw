# Cloud Document Models Re-export Hub for Beanie ODM

> This module serves as a central re-export point for Beanie ODM document definitions used in the EE Cloud application. It consolidates imports from 11 specialized model modules and defines a core list of documents used throughout the system.

**Categories:** Database Models, Cloud Infrastructure, Enterprise Edition (EE) Architecture  
**Concepts:** Beanie ODM, Document Models, User, Agent, Workspace, Message, Comment, Notification, Session, Group  
**Words:** 240 | **Version:** 2

---

## Overview

The `ee.cloud.models.__init__` module functions as a centralized hub for re-exporting all Beanie ODM document model definitions across the EE Cloud infrastructure. This pattern enables cleaner imports and maintains a single source of truth for document model availability.

## Model Categories and Imports

### User and Authentication Models
- **User**: Core user entity with associated `OAuthAccount` and `WorkspaceMembership` classes
- Imported from `ee.cloud.models.user`

### Agent Models
- **Agent**: Agent entity with configuration
- **AgentConfig**: Configuration settings for agents
- Imported from `ee.cloud.models.agent`

### Workspace Models
- **Workspace**: Workspace entity with associated settings
- **WorkspaceSettings**: Configuration for workspace-level preferences
- **WorkspaceMembership**: User membership within workspaces
- Imported from `ee.cloud.models.workspace`

### Collaboration and Communication Models
- **Message**: Message entity with `Mention`, `Attachment`, and `Reaction` sub-classes
- **Comment**: Comment entity with `CommentAuthor` and `CommentTarget`
- Imported from `ee.cloud.models.message` and `ee.cloud.models.comment`

### Organization Models
- **Group**: Group entity with associated `GroupAgent`
- Imported from `ee.cloud.models.group`

### Utility and Infrastructure Models
- **Session**: User session tracking
- **Notification**: Notification entity with `NotificationSource`
- **Invite**: Invitation entity
- **FileObj**: File object storage
- **Pocket**: Data container with `Widget` and `WidgetPosition` classes
- Imported from respective model modules

## Core Documents List

The module defines `ALL_DOCUMENTS` containing the primary document classes used throughout the system:

```
User, Agent, Pocket, Session, Comment, Notification, FileObj, Workspace, Invite, Group, Message
```

This list serves as the canonical reference for which documents are actively managed by the Beanie ODM layer.