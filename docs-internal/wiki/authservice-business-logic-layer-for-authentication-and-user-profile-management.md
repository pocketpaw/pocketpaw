# AuthService: Business Logic Layer for Authentication and User Profile Management

> AuthService is a stateless FastAPI service that encapsulates authentication and user profile management business logic. It provides three main operations: retrieving user profiles, updating mutable profile fields, and managing active workspace selection.

**Categories:** Authentication & Authorization, User Management, Business Logic Layer  
**Concepts:** AuthService, User Profile, ProfileUpdateRequest, Active Workspace, User Model, Email Verification, Avatar, HTTPException, Stateless Service, FastAPI  
**Words:** 207 | **Version:** 2

---

## Overview

AuthService is a stateless service class that handles core authentication and user profile business logic for the cloud platform. It operates as an abstraction layer between API endpoints and data models.

## Core Methods

### get_profile

Retrieves the current user's complete profile information and returns it as a dictionary.

**Returns:**
- `id`: User identifier (string)
- `email`: User email address
- `name`: User's full name
- `image`: User's avatar URL
- `emailVerified`: Boolean indicating email verification status
- `activeWorkspace`: Currently active workspace identifier
- `workspaces`: Array of workspace objects containing workspace ID and user role

### update_profile

Updates mutable user profile fields and persists changes to the database.

**Mutable Fields:**
- `full_name`: User's display name
- `avatar`: User's profile image
- `status`: User status indicator

All fields are optional and only updated if provided (non-null values). Returns the updated profile using `get_profile()` after persistence.

### set_active_workspace

Sets the user's active workspace context.

**Validation:**
- Raises `HTTPException` with status code 400 if `workspace_id` is empty or missing
- Persists the change to the database

## Architecture

All methods are implemented as static methods, making the service stateless and enabling straightforward testing and composition. The service depends on the `User` model and `ProfileUpdateRequest` schema for type definitions.