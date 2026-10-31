# Knowledge Base Index

**38 articles** | **504 concepts** | **131 categories**

## Categories

### API Gateway Layer

- [auth/__init__ — Central re-export hub for authentication and user management](authinit-central-re-export-hub-for-authentication-and-user-management.md) — This module serves as the public API facade for the entire authentication domain

### API Router / Endpoint Layer

- [ee/cloud/kb/__init__ — Knowledge Base Domain Package Initialization and Endpoint Exposure](eecloudkbinit-knowledge-base-domain-package-initialization-and-endpoint-exposure.md) — This module serves as the entry point for the Knowledge Base (KB) domain within 
- [ee.cloud.workspace — Router re-export for FastAPI workspace endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md) — This module serves as the public entry point for the workspace domain's FastAPI 

### API Router Layer

- [deps — FastAPI dependency injection layer for cloud router authentication and authorization](deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth.md) — This module provides FastAPI dependency functions that extract and validate user

### API Router — Bootstrap & Mounting

- [ee.cloud.__init__ — Cloud domain orchestration and FastAPI application bootstrap](eecloudinit-cloud-domain-orchestration-and-fastapi-application-bootstrap.md) — This module is the entry point for PocketPaw's enterprise cloud layer. It bootst

### API contract layer

- [schemas — Pydantic request/response contracts for session lifecycle operations](schemas-pydantic-requestresponse-contracts-for-session-lifecycle-operations.md) — This module defines the HTTP API contracts (request bodies and response payloads
- [schemas — Pydantic request/response data models for workspace domain operations](schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations.md) — This module defines the contract between the workspace API layer and its consume

### API gateway / facade

- [chat/__init__.py — Entry point for chat domain with groups, messages, and WebSocket real-time capabilities](chatinitpy-entry-point-for-chat-domain-with-groups-messages-and-websocket-real-t.md) — This module serves as the public API gateway for the chat domain, re-exporting t

### API layer

- [core — Enterprise JWT authentication with cookie and bearer transport for FastAPI](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md) — This module implements a complete authentication system for PocketPaw using fast
- [schemas — Pydantic request/response models for agent lifecycle and discovery operations](schemas-pydantic-requestresponse-models-for-agent-lifecycle-and-discovery-operat.md) — This module defines four Pydantic BaseModel classes that serve as the contract l
- [schemas — Request/response data validation for the knowledge base REST API](schemas-requestresponse-data-validation-for-the-knowledge-base-rest-api.md) — This module defines Pydantic request/response schemas for the knowledge base dom

### API router / integration layer

- [ee.cloud.agents — Package initialization and router export for enterprise cloud agent functionality](eecloudagents-package-initialization-and-router-export-for-enterprise-cloud-agen.md) — This is a minimal package initialization module that serves as the public API en

### API router and HTTP layer

- [ee.cloud.sessions — Entry point and router export for session management APIs](eecloudsessions-entry-point-and-router-export-for-session-management-apis.md) — This module serves as the public API entry point for the sessions package, expor

### API router layer

- [pockets.__init__ — Entry point and public API aggregator for the pockets subsystem](pocketsinit-entry-point-and-public-api-aggregator-for-the-pockets-subsystem.md) — This module serves as the public interface for the enterprise cloud pockets subs
- [router — FastAPI authentication endpoints and user profile management](router-fastapi-authentication-endpoints-and-user-profile-management.md) — This module exposes HTTP endpoints for user authentication, registration, profil

### API schemas and data models

- [schemas — Pydantic models for authentication request/response validation](schemas-pydantic-models-for-authentication-requestresponse-validation.md) — This module defines three Pydantic BaseModel classes that standardize the shape 

### Access Control & Security

- [Workspace Domain Service - Business Logic for Enterprise Cloud](untitled.md) — A stateless service layer that encapsulates workspace business logic including C

### Adapter/Bridge Pattern

- [backend_adapter — Adapter that makes PocketPaw's agent backends usable as knowledge base CompilerBackends](backendadapter-adapter-that-makes-pocketpaws-agent-backends-usable-as-knowledge.md) — This module provides `PocketPawCompilerBackend`, an adapter class that implement

### Agent Infrastructure

- [backend_adapter — Adapter that makes PocketPaw's agent backends usable as knowledge base CompilerBackends](backendadapter-adapter-that-makes-pocketpaws-agent-backends-usable-as-knowledge.md) — This module provides `PocketPawCompilerBackend`, an adapter class that implement

### Agent Integration Layer

- [ripple_normalizer — Normalizes AI-generated pocket specifications into a consistent, persistence-ready format](ripplenormalizer-normalizes-ai-generated-pocket-specifications-into-a-consistent.md) — This module provides a single public function, `normalize_ripple_spec()`, that t

### Async/Concurrency Patterns

- [events — In-process async pub/sub event bus for decoupled cross-domain side effects](events-in-process-async-pubsub-event-bus-for-decoupled-cross-domain-side-effects.md) — This module provides a simple in-process publish/subscribe event bus that enable

### Authentication & Authorization

- [auth/__init__ — Central re-export hub for authentication and user management](authinit-central-re-export-hub-for-authentication-and-user-management.md) — This module serves as the public API facade for the entire authentication domain
- [AuthService: Business Logic Layer for Authentication and User Profile Management](authservice-business-logic-layer-for-authentication-and-user-profile-management.md) — AuthService is a stateless FastAPI service that encapsulates authentication and 
- [deps — FastAPI dependency injection layer for cloud router authentication and authorization](deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth.md) — This module provides FastAPI dependency functions that extract and validate user

### Backend Service Architecture

- [Workspace Domain Service - Business Logic for Enterprise Cloud](untitled.md) — A stateless service layer that encapsulates workspace business logic including C

### Business Logic Layer

- [AuthService: Business Logic Layer for Authentication and User Profile Management](authservice-business-logic-layer-for-authentication-and-user-profile-management.md) — AuthService is a stateless FastAPI service that encapsulates authentication and 

### CRUD

- [pocket — Data models for Pocket workspaces with widgets, teams, and collaborative agents](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md) — This module defines the core document models (Pocket, Widget, WidgetPosition) th

### CRUD operations

- [router — FastAPI authentication endpoints and user profile management](router-fastapi-authentication-endpoints-and-user-profile-management.md) — This module exposes HTTP endpoints for user authentication, registration, profil
- [schemas — Pydantic request/response contracts for session lifecycle operations](schemas-pydantic-requestresponse-contracts-for-session-lifecycle-operations.md) — This module defines the HTTP API contracts (request bodies and response payloads

### CRUD schema definition

- [schemas — Pydantic request/response models for agent lifecycle and discovery operations](schemas-pydantic-requestresponse-models-for-agent-lifecycle-and-discovery-operat.md) — This module defines four Pydantic BaseModel classes that serve as the contract l

### Chat & Messaging

- [message — Data model for group chat messages with mentions, reactions, and threading support](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md) — This module defines the Pydantic data models that represent chat messages in gro

### Cloud Domain — Orchestration

- [ee.cloud.__init__ — Cloud domain orchestration and FastAPI application bootstrap](eecloudinit-cloud-domain-orchestration-and-fastapi-application-bootstrap.md) — This module is the entry point for PocketPaw's enterprise cloud layer. It bootst

### Cloud Infrastructure

- [Cloud Document Models Re-export Hub for Beanie ODM](eecloudmodelsinit-central-re-export-hub-for-beanie-odm-document-definitions.md) — This module serves as a central re-export point for Beanie ODM document definiti

### Collaboration Features

- [comment — Threaded comments on pockets and widgets with workspace isolation](comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation.md) — This module defines the data models for a collaborative commenting system that e

### Core Domain Model

- [comment — Threaded comments on pockets and widgets with workspace isolation](comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation.md) — This module defines the data models for a collaborative commenting system that e

### Cross-Domain Communication

- [events — In-process async pub/sub event bus for decoupled cross-domain side effects](events-in-process-async-pubsub-event-bus-for-decoupled-cross-domain-side-effects.md) — This module provides a simple in-process publish/subscribe event bus that enable

### Data Model / Persistence

- [comment — Threaded comments on pockets and widgets with workspace isolation](comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation.md) — This module defines the data models for a collaborative commenting system that e
- [notification — In-app notification data model and persistence for user workspace events](notification-in-app-notification-data-model-and-persistence-for-user-workspace-e.md) — This module defines the data models for in-app notifications that inform users a

### Data Model Layer

- [message — Data model for group chat messages with mentions, reactions, and threading support](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md) — This module defines the Pydantic data models that represent chat messages in gro

### Data Transformation & Normalization

- [ripple_normalizer — Normalizes AI-generated pocket specifications into a consistent, persistence-ready format](ripplenormalizer-normalizes-ai-generated-pocket-specifications-into-a-consistent.md) — This module provides a single public function, `normalize_ripple_spec()`, that t

### Database Models

- [Cloud Document Models Re-export Hub for Beanie ODM](eecloudmodelsinit-central-re-export-hub-for-beanie-odm-document-definitions.md) — This module serves as a central re-export point for Beanie ODM document definiti

### Domain Model

- [message — Data model for group chat messages with mentions, reactions, and threading support](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md) — This module defines the Pydantic data models that represent chat messages in gro

### Enterprise Edition (EE) Architecture

- [Cloud Document Models Re-export Hub for Beanie ODM](eecloudmodelsinit-central-re-export-hub-for-beanie-odm-document-definitions.md) — This module serves as a central re-export point for Beanie ODM document definiti

### Enterprise Edition Cloud Infrastructure

- [ee/cloud/kb/__init__ — Knowledge Base Domain Package Initialization and Endpoint Exposure](eecloudkbinit-knowledge-base-domain-package-initialization-and-endpoint-exposure.md) — This module serves as the entry point for the Knowledge Base (KB) domain within 

### Enterprise Features

- [ee.cloud.workspace — Router re-export for FastAPI workspace endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md) — This module serves as the public entry point for the workspace domain's FastAPI 

### Enterprise SaaS

- [Workspace Domain Service - Business Logic for Enterprise Cloud](untitled.md) — A stateless service layer that encapsulates workspace business logic including C

### Enterprise cloud features

- [ee.cloud.sessions — Entry point and router export for session management APIs](eecloudsessions-entry-point-and-router-export-for-session-management-apis.md) — This module serves as the public API entry point for the sessions package, expor

### Error Handling & Global Middleware

- [ee.cloud.__init__ — Cloud domain orchestration and FastAPI application bootstrap](eecloudinit-cloud-domain-orchestration-and-fastapi-application-bootstrap.md) — This module is the entry point for PocketPaw's enterprise cloud layer. It bootst

### Event-Driven Architecture

- [ee.cloud.__init__ — Cloud domain orchestration and FastAPI application bootstrap](eecloudinit-cloud-domain-orchestration-and-fastapi-application-bootstrap.md) — This module is the entry point for PocketPaw's enterprise cloud layer. It bootst
- [events — In-process async pub/sub event bus for decoupled cross-domain side effects](events-in-process-async-pubsub-event-bus-for-decoupled-cross-domain-side-effects.md) — This module provides a simple in-process publish/subscribe event bus that enable
- [notification — In-app notification data model and persistence for user workspace events](notification-in-app-notification-data-model-and-persistence-for-user-workspace-e.md) — This module defines the data models for in-app notifications that inform users a

### Facade & Re-export Pattern

- [auth/__init__ — Central re-export hub for authentication and user management](authinit-central-re-export-hub-for-authentication-and-user-management.md) — This module serves as the public API facade for the entire authentication domain

### FastAPI HTTP endpoints

- [router — FastAPI authentication endpoints and user profile management](router-fastapi-authentication-endpoints-and-user-profile-management.md) — This module exposes HTTP endpoints for user authentication, registration, profil

### FastAPI Middleware & Dependency Injection

- [deps — FastAPI dependency injection layer for cloud router authentication and authorization](deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth.md) — This module provides FastAPI dependency functions that extract and validate user

### FastAPI application architecture

- [ee.cloud.agents — Package initialization and router export for enterprise cloud agent functionality](eecloudagents-package-initialization-and-router-export-for-enterprise-cloud-agen.md) — This is a minimal package initialization module that serves as the public API en

### FastAPI integration

- [license — Enterprise license validation and feature gating for cloud deployments](license-enterprise-license-validation-and-feature-gating-for-cloud-deployments.md) — This module provides cryptographic validation of signed license keys, caching of

### HTTP validation layer

- [schemas — Pydantic models for authentication request/response validation](schemas-pydantic-models-for-authentication-requestresponse-validation.md) — This module defines three Pydantic BaseModel classes that standardize the shape 

### Infrastructure Layer — Lifecycle Management

- [ee.cloud.__init__ — Cloud domain orchestration and FastAPI application bootstrap](eecloudinit-cloud-domain-orchestration-and-fastapi-application-bootstrap.md) — This module is the entry point for PocketPaw's enterprise cloud layer. It bootst

### Infrastructure/Foundation

- [events — In-process async pub/sub event bus for decoupled cross-domain side effects](events-in-process-async-pubsub-event-bus-for-decoupled-cross-domain-side-effects.md) — This module provides a simple in-process publish/subscribe event bus that enable

### Knowledge Base — Integration Layer

- [backend_adapter — Adapter that makes PocketPaw's agent backends usable as knowledge base CompilerBackends](backendadapter-adapter-that-makes-pocketpaws-agent-backends-usable-as-knowledge.md) — This module provides `PocketPawCompilerBackend`, an adapter class that implement

### Knowledge Management Domain

- [ee/cloud/kb/__init__ — Knowledge Base Domain Package Initialization and Endpoint Exposure](eecloudkbinit-knowledge-base-domain-package-initialization-and-endpoint-exposure.md) — This module serves as the entry point for the Knowledge Base (KB) domain within 

### LLM Backend Abstraction

- [backend_adapter — Adapter that makes PocketPaw's agent backends usable as knowledge base CompilerBackends](backendadapter-adapter-that-makes-pocketpaws-agent-backends-usable-as-knowledge.md) — This module provides `PocketPawCompilerBackend`, an adapter class that implement

### Module Architecture / Facade Pattern

- [ee.cloud.workspace — Router re-export for FastAPI workspace endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md) — This module serves as the public entry point for the workspace domain's FastAPI 

### MongoDB / Beanie

- [file — Cloud storage metadata document for managing file references](file-cloud-storage-metadata-document-for-managing-file-references.md) — This module defines the `FileObj` document model that stores metadata about file

### MongoDB document

- [agent — Agent configuration and metadata storage for workspace-scoped AI agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md) — This module defines the data models for storing agent configurations in the OCEA

### MongoDB persistence

- [base — Foundational document model with automatic timestamp management for MongoDB persistence](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md) — This module provides `TimestampedDocument`, a base class that extends Beanie's O
- [session — Cloud-tracked chat session document model for pocket-scoped conversations](session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation.md) — The session module defines the Session document model that represents individual
- [workspace — Data model for organization workspaces in multi-tenant enterprise deployments](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md) — This module defines the core data models that represent a workspace: the contain

### MongoDB/Beanie Persistence

- [message — Data model for group chat messages with mentions, reactions, and threading support](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md) — This module defines the Pydantic data models that represent chat messages in gro

### MongoDB/Beanie — database technology and ORM layer

- [group — Multi-user chat channels with AI agent participants](group-multi-user-chat-channels-with-ai-agent-participants.md) — This module defines the data models for chat groups/channels that support multip

### Multi-Tenant Access Control

- [deps — FastAPI dependency injection layer for cloud router authentication and authorization](deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth.md) — This module provides FastAPI dependency functions that extract and validate user

### Multi-tenant Architecture

- [comment — Threaded comments on pockets and widgets with workspace isolation](comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation.md) — This module defines the data models for a collaborative commenting system that e

### Notification / User Communication

- [notification — In-app notification data model and persistence for user workspace events](notification-in-app-notification-data-model-and-persistence-for-user-workspace-e.md) — This module defines the data models for in-app notifications that inform users a

### ODM integration

- [db — MongoDB connection and Beanie ODM lifecycle management for PocketPaw cloud infrastructure](db-mongodb-connection-and-beanie-odm-lifecycle-management-for-pocketpaw-cloud-in.md) — This module provides a centralized, application-level abstraction for managing M

### Package structure and organization

- [ee.cloud.sessions — Entry point and router export for session management APIs](eecloudsessions-entry-point-and-router-export-for-session-management-apis.md) — This module serves as the public API entry point for the sessions package, expor

### Pydantic DTOs

- [schemas — Pydantic request/response data models for workspace domain operations](schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations.md) — This module defines the contract between the workspace API layer and its consume

### Security Infrastructure

- [auth/__init__ — Central re-export hub for authentication and user management](authinit-central-re-export-hub-for-authentication-and-user-management.md) — This module serves as the public API facade for the entire authentication domain

### Session management domain

- [ee.cloud.sessions — Entry point and router export for session management APIs](eecloudsessions-entry-point-and-router-export-for-session-management-apis.md) — This module serves as the public API entry point for the sessions package, expor

### Specification Management

- [ripple_normalizer — Normalizes AI-generated pocket specifications into a consistent, persistence-ready format](ripplenormalizer-normalizes-ai-generated-pocket-specifications-into-a-consistent.md) — This module provides a single public function, `normalize_ripple_spec()`, that t

### User Management

- [AuthService: Business Logic Layer for Authentication and User Profile Management](authservice-business-logic-layer-for-authentication-and-user-profile-management.md) — AuthService is a stateless FastAPI service that encapsulates authentication and 

### Utility & Infrastructure

- [ripple_normalizer — Normalizes AI-generated pocket specifications into a consistent, persistence-ready format](ripplenormalizer-normalizes-ai-generated-pocket-specifications-into-a-consistent.md) — This module provides a single public function, `normalize_ripple_spec()`, that t

### Workspace / Multi-tenancy

- [notification — In-app notification data model and persistence for user workspace events](notification-in-app-notification-data-model-and-persistence-for-user-workspace-e.md) — This module defines the data models for in-app notifications that inform users a

### Workspace Domain

- [ee.cloud.workspace — Router re-export for FastAPI workspace endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md) — This module serves as the public entry point for the workspace domain's FastAPI 

### Workspace-Scoped Feature

- [ee/cloud/kb/__init__ — Knowledge Base Domain Package Initialization and Endpoint Exposure](eecloudkbinit-knowledge-base-domain-package-initialization-and-endpoint-exposure.md) — This module serves as the entry point for the Knowledge Base (KB) domain within 

### agent management

- [agent — Agent configuration and metadata storage for workspace-scoped AI agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md) — This module defines the data models for storing agent configurations in the OCEA

### agents domain

- [schemas — Pydantic request/response models for agent lifecycle and discovery operations](schemas-pydantic-requestresponse-models-for-agent-lifecycle-and-discovery-operat.md) — This module defines four Pydantic BaseModel classes that serve as the contract l

### application lifecycle

- [db — MongoDB connection and Beanie ODM lifecycle management for PocketPaw cloud infrastructure](db-mongodb-connection-and-beanie-odm-lifecycle-management-for-pocketpaw-cloud-in.md) — This module provides a centralized, application-level abstraction for managing M

### architectural pattern — facade

- [db — Backward compatibility facade for cloud database initialization](db-backward-compatibility-facade-for-cloud-database-initialization.md) — This module is a thin re-export layer that delegates all database functionality 

### architectural refactoring

- [service — Chat domain re-export facade for backward compatibility](service-chat-domain-re-export-facade-for-backward-compatibility.md) — This module serves as a thin re-export layer for the chat domain, consolidating 

### architecture — module organization and facade patterns

- [__init__ — Facade module exposing shared cross-cutting concerns for the PocketPaw cloud ecosystem](init-facade-module-exposing-shared-cross-cutting-concerns-for-the-pocketpaw-clou.md) — This module serves as the public interface for shared utilities, services, and i

### auth domain

- [schemas — Pydantic models for authentication request/response validation](schemas-pydantic-models-for-authentication-requestresponse-validation.md) — This module defines three Pydantic BaseModel classes that standardize the shape 

### authentication

- [core — Enterprise JWT authentication with cookie and bearer transport for FastAPI](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md) — This module implements a complete authentication system for PocketPaw using fast
- [router — FastAPI authentication endpoints and user profile management](router-fastapi-authentication-endpoints-and-user-profile-management.md) — This module exposes HTTP endpoints for user authentication, registration, profil

### authorization

- [core — Enterprise JWT authentication with cookie and bearer transport for FastAPI](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md) — This module implements a complete authentication system for PocketPaw using fast

### authorization & access control

- [license — Enterprise license validation and feature gating for cloud deployments](license-enterprise-license-validation-and-feature-gating-for-cloud-deployments.md) — This module provides cryptographic validation of signed license keys, caching of

### backward compatibility

- [service — Chat domain re-export facade for backward compatibility](service-chat-domain-re-export-facade-for-backward-compatibility.md) — This module serves as a thin re-export layer for the chat domain, consolidating 

### chat / messaging

- [session — Cloud-tracked chat session document model for pocket-scoped conversations](session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation.md) — The session module defines the Session document model that represents individual

### chat domain

- [chat/__init__.py — Entry point for chat domain with groups, messages, and WebSocket real-time capabilities](chatinitpy-entry-point-for-chat-domain-with-groups-messages-and-websocket-real-t.md) — This module serves as the public API gateway for the chat domain, re-exporting t
- [service — Chat domain re-export facade for backward compatibility](service-chat-domain-re-export-facade-for-backward-compatibility.md) — This module serves as a thin re-export layer for the chat domain, consolidating 

### chat/collaboration — domain area for group conversations

- [group — Multi-user chat channels with AI agent participants](group-multi-user-chat-channels-with-ai-agent-participants.md) — This module defines the data models for chat groups/channels that support multip

### cloud storage

- [file — Cloud storage metadata document for managing file references](file-cloud-storage-metadata-document-for-managing-file-references.md) — This module defines the `FileObj` document model that stores metadata about file

### collaborative features

- [pocket — Data models for Pocket workspaces with widgets, teams, and collaborative agents](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md) — This module defines the core document models (Pocket, Widget, WidgetPosition) th

### compatibility layer

- [db — Backward compatibility facade for cloud database initialization](db-backward-compatibility-facade-for-cloud-database-initialization.md) — This module is a thin re-export layer that delegates all database functionality 

### configuration storage

- [agent — Agent configuration and metadata storage for workspace-scoped AI agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md) — This module defines the data models for storing agent configurations in the OCEA

### cross-cutting concerns

- [base — Foundational document model with automatic timestamp management for MongoDB persistence](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md) — This module provides `TimestampedDocument`, a base class that extends Beanie's O

### cross-cutting concerns — auth, errors, events shared across all features

- [__init__ — Facade module exposing shared cross-cutting concerns for the PocketPaw cloud ecosystem](init-facade-module-exposing-shared-cross-cutting-concerns-for-the-pocketpaw-clou.md) — This module serves as the public interface for shared utilities, services, and i

### data model

- [file — Cloud storage metadata document for managing file references](file-cloud-storage-metadata-document-for-managing-file-references.md) — This module defines the `FileObj` document model that stores metadata about file
- [schemas — Pydantic request/response models for agent lifecycle and discovery operations](schemas-pydantic-requestresponse-models-for-agent-lifecycle-and-discovery-operat.md) — This module defines four Pydantic BaseModel classes that serve as the contract l
- [workspace — Data model for organization workspaces in multi-tenant enterprise deployments](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md) — This module defines the core data models that represent a workspace: the contain

### data model / ORM

- [session — Cloud-tracked chat session document model for pocket-scoped conversations](session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation.md) — The session module defines the Session document model that represents individual

### data model / schema

- [pocket — Data models for Pocket workspaces with widgets, teams, and collaborative agents](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md) — This module defines the core document models (Pocket, Widget, WidgetPosition) th

### data model layer

- [agent — Agent configuration and metadata storage for workspace-scoped AI agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md) — This module defines the data models for storing agent configurations in the OCEA
- [base — Foundational document model with automatic timestamp management for MongoDB persistence](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md) — This module provides `TimestampedDocument`, a base class that extends Beanie's O

### data model — core persistent entity

- [group — Multi-user chat channels with AI agent participants](group-multi-user-chat-channels-with-ai-agent-participants.md) — This module defines the data models for chat groups/channels that support multip

### data model: ODM document

- [invite — Workspace membership invitation document model](invite-workspace-membership-invitation-document-model.md) — The invite module defines the Invite document class that represents pending work

### data persistence

- [db — MongoDB connection and Beanie ODM lifecycle management for PocketPaw cloud infrastructure](db-mongodb-connection-and-beanie-odm-lifecycle-management-for-pocketpaw-cloud-in.md) — This module provides a centralized, application-level abstraction for managing M

### data validation

- [schemas — Pydantic request/response contracts for session lifecycle operations](schemas-pydantic-requestresponse-contracts-for-session-lifecycle-operations.md) — This module defines the HTTP API contracts (request bodies and response payloads
- [schemas — Pydantic request/response data models for workspace domain operations](schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations.md) — This module defines the contract between the workspace API layer and its consume
- [schemas — Request/response data validation for the knowledge base REST API](schemas-requestresponse-data-validation-for-the-knowledge-base-rest-api.md) — This module defines Pydantic request/response schemas for the knowledge base dom

### dependency injection — fastapi and inversion of control

- [__init__ — Facade module exposing shared cross-cutting concerns for the PocketPaw cloud ecosystem](init-facade-module-exposing-shared-cross-cutting-concerns-for-the-pocketpaw-clou.md) — This module serves as the public interface for shared utilities, services, and i

### document structure

- [pocket — Data models for Pocket workspaces with widgets, teams, and collaborative agents](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md) — This module defines the core document models (Pocket, Widget, WidgetPosition) th

### domain: workspace access control

- [invite — Workspace membership invitation document model](invite-workspace-membership-invitation-document-model.md) — The invite module defines the Invite document class that represents pending work

### enterprise cloud agents

- [ee.cloud.agents — Package initialization and router export for enterprise cloud agent functionality](eecloudagents-package-initialization-and-router-export-for-enterprise-cloud-agen.md) — This is a minimal package initialization module that serves as the public API en

### enterprise cloud platform

- [pockets.__init__ — Entry point and public API aggregator for the pockets subsystem](pocketsinit-entry-point-and-public-api-aggregator-for-the-pockets-subsystem.md) — This module serves as the public interface for the enterprise cloud pockets subs

### enterprise security

- [core — Enterprise JWT authentication with cookie and bearer transport for FastAPI](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md) — This module implements a complete authentication system for PocketPaw using fast

### file management

- [file — Cloud storage metadata document for managing file references](file-cloud-storage-metadata-document-for-managing-file-references.md) — This module defines the `FileObj` document model that stores metadata about file

### foundational infrastructure

- [base — Foundational document model with automatic timestamp management for MongoDB persistence](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md) — This module provides `TimestampedDocument`, a base class that extends Beanie's O

### infrastructure layer

- [db — MongoDB connection and Beanie ODM lifecycle management for PocketPaw cloud infrastructure](db-mongodb-connection-and-beanie-odm-lifecycle-management-for-pocketpaw-cloud-in.md) — This module provides a centralized, application-level abstraction for managing M

### infrastructure — cloud database

- [db — Backward compatibility facade for cloud database initialization](db-backward-compatibility-facade-for-cloud-database-initialization.md) — This module is a thin re-export layer that delegates all database functionality 

### knowledge base domain

- [schemas — Request/response data validation for the knowledge base REST API](schemas-requestresponse-data-validation-for-the-knowledge-base-rest-api.md) — This module defines Pydantic request/response schemas for the knowledge base dom

### licensing & commercialization

- [license — Enterprise license validation and feature gating for cloud deployments](license-enterprise-license-validation-and-feature-gating-for-cloud-deployments.md) — This module provides cryptographic validation of signed license keys, caching of

### module initialization

- [chat/__init__.py — Entry point for chat domain with groups, messages, and WebSocket real-time capabilities](chatinitpy-entry-point-for-chat-domain-with-groups-messages-and-websocket-real-t.md) — This module serves as the public API gateway for the chat domain, re-exporting t

### multi-tenancy

- [workspace — Data model for organization workspaces in multi-tenant enterprise deployments](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md) — This module defines the core data models that represent a workspace: the contain

### multi-tenancy — workspace scoping and data isolation

- [__init__ — Facade module exposing shared cross-cutting concerns for the PocketPaw cloud ecosystem](init-facade-module-exposing-shared-cross-cutting-concerns-for-the-pocketpaw-clou.md) — This module serves as the public interface for shared utilities, services, and i

### multi-tenant architecture

- [router — FastAPI authentication endpoints and user profile management](router-fastapi-authentication-endpoints-and-user-profile-management.md) — This module exposes HTTP endpoints for user authentication, registration, profil

### multi-user feature — supports multiple participants with different roles

- [group — Multi-user chat channels with AI agent participants](group-multi-user-chat-channels-with-ai-agent-participants.md) — This module defines the data models for chat groups/channels that support multip

### package initialization

- [ee.cloud.agents — Package initialization and router export for enterprise cloud agent functionality](eecloudagents-package-initialization-and-router-export-for-enterprise-cloud-agen.md) — This is a minimal package initialization module that serves as the public API en

### package initialization and namespacing

- [pockets.__init__ — Entry point and public API aggregator for the pockets subsystem](pocketsinit-entry-point-and-public-api-aggregator-for-the-pockets-subsystem.md) — This module serves as the public interface for the enterprise cloud pockets subs

### pattern: invitation lifecycle

- [invite — Workspace membership invitation document model](invite-workspace-membership-invitation-document-model.md) — The invite module defines the Invite document class that represents pending work

### real-time messaging infrastructure

- [chat/__init__.py — Entry point for chat domain with groups, messages, and WebSocket real-time capabilities](chatinitpy-entry-point-for-chat-domain-with-groups-messages-and-websocket-real-t.md) — This module serves as the public API gateway for the chat domain, re-exporting t

### request/response contracts

- [schemas — Request/response data validation for the knowledge base REST API](schemas-requestresponse-data-validation-for-the-knowledge-base-rest-api.md) — This module defines Pydantic request/response schemas for the knowledge base dom

### schema definition

- [agent — Agent configuration and metadata storage for workspace-scoped AI agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md) — This module defines the data models for storing agent configurations in the OCEA
- [schemas — Pydantic request/response data models for workspace domain operations](schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations.md) — This module defines the contract between the workspace API layer and its consume

### security & cryptography

- [license — Enterprise license validation and feature gating for cloud deployments](license-enterprise-license-validation-and-feature-gating-for-cloud-deployments.md) — This module provides cryptographic validation of signed license keys, caching of

### security: token-based invitations

- [invite — Workspace membership invitation document model](invite-workspace-membership-invitation-document-model.md) — The invite module defines the Invite document class that represents pending work

### service layer

- [core — Enterprise JWT authentication with cookie and bearer transport for FastAPI](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md) — This module implements a complete authentication system for PocketPaw using fast
- [service — Chat domain re-export facade for backward compatibility](service-chat-domain-re-export-facade-for-backward-compatibility.md) — This module serves as a thin re-export layer for the chat domain, consolidating 

### sessions domain

- [schemas — Pydantic request/response contracts for session lifecycle operations](schemas-pydantic-requestresponse-contracts-for-session-lifecycle-operations.md) — This module defines the HTTP API contracts (request bodies and response payloads

### system-wide contracts

- [schemas — Pydantic models for authentication request/response validation](schemas-pydantic-models-for-authentication-requestresponse-validation.md) — This module defines three Pydantic BaseModel classes that standardize the shape 

### temporal auditing

- [base — Foundational document model with automatic timestamp management for MongoDB persistence](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md) — This module provides `TimestampedDocument`, a base class that extends Beanie's O

### workspace and collaboration domain

- [pockets.__init__ — Entry point and public API aggregator for the pockets subsystem](pocketsinit-entry-point-and-public-api-aggregator-for-the-pockets-subsystem.md) — This module serves as the public interface for the enterprise cloud pockets subs

### workspace domain

- [schemas — Pydantic request/response data models for workspace domain operations](schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations.md) — This module defines the contract between the workspace API layer and its consume

### workspace management

- [pocket — Data models for Pocket workspaces with widgets, teams, and collaborative agents](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md) — This module defines the core document models (Pocket, Widget, WidgetPosition) th
- [session — Cloud-tracked chat session document model for pocket-scoped conversations](session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation.md) — The session module defines the Session document model that represents individual
- [workspace — Data model for organization workspaces in multi-tenant enterprise deployments](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md) — This module defines the core data models that represent a workspace: the contain

