# file — Cloud storage metadata document for managing file references

> This module defines the `FileObj` document model that stores metadata about files persisted in external cloud storage (S3, GCS, or local). Rather than storing actual file bytes in MongoDB, it maintains a lightweight reference with ownership, location, and access information. It's a critical bridge between the application's domain logic and cloud storage infrastructure.

**Categories:** data model, cloud storage, file management, MongoDB / Beanie  
**Concepts:** FileObj, Document, Indexed, Beanie ODM, Pydantic Field, MongoDB collection, cloud storage metadata, pre-signed URL, S3, GCS  
**Words:** 1297 | **Version:** 1

---

## Purpose

The `file` module solves a fundamental architectural problem: applications need to store files, but MongoDB is not an efficient or cost-effective choice for binary data. This module decouples file metadata (ownership, naming, access control) from file storage itself.

Instead of embedding or storing file bytes in the database, `FileObj` acts as a **pointer and metadata record**. When a user uploads or references a file, the application:
1. Stores the actual bytes in S3, GCS, or local disk
2. Creates a `FileObj` document that remembers *where* the file is and *who owns it*
3. Uses the `FileObj` to generate pre-signed URLs or validate access

This pattern is essential in modern cloud-native architectures because it:
- **Separates concerns**: Database handles structured data, object storage handles binary data
- **Enables scalability**: Files can be served directly from CDN-backed object stores
- **Controls costs**: MongoDB storage is expensive; S3/GCS is cheaper for unstructured data
- **Supports multi-tenancy**: The `owner` field enables workspace-scoped file access

## Key Classes and Methods

### `FileObj(Document)`

A Beanie ODM document representing file metadata stored in MongoDB's `files` collection.

**Fields:**

- **`owner: Indexed(str)`** — The user or workspace that owns this file. Indexed for fast lookup by owner. This is critical for multi-tenant access control—queries like "fetch all files owned by workspace X" depend on this index.

- **`file_name: str`** — The original filename as uploaded or referenced by the user (e.g., `"resume.pdf"`). Used for display and content-disposition headers in download responses.

- **`bucket: str`** — The storage bucket identifier. For S3, this might be `"my-app-prod-files"`; for GCS, `"project-files-bucket"`. Tells the application which cloud storage account to use.

- **`provider: str`** — One of `"gcs"`, `"s3"`, or `"local"`. A constrained enum validated by Pydantic's `pattern` validator. Determines which SDK the application uses to retrieve or generate signed URLs.

- **`path_in_bucket: str`** — The object key or path inside the bucket where the file actually lives (e.g., `"workspaces/123/documents/abc-def.pdf"`). This is the locator used in SDK calls like `s3_client.get_object(Bucket=bucket, Key=path_in_bucket)`.

- **`mime_type: str`** — The MIME type of the file (e.g., `"application/pdf"`, `"image/jpeg"`). Defaults to empty string. Used in HTTP Content-Type headers when serving downloads.

- **`size: int`** — File size in bytes. Defaults to 0. Used for quota enforcement, progress indicators, and validation that uploaded content matches expected size.

- **`public: bool`** — Whether the file is publicly accessible without authentication. Defaults to `False`. Used to determine whether to generate public URLs or require signed/temporary access tokens.

**Class-level Configuration:**

```python
class Settings:
    name = "files"
```

Maps the `FileObj` model to the `files` MongoDB collection. Without this, Beanie would use a auto-derived or default collection name.

**No explicit methods** — `FileObj` is a pure data model. It inherits from Beanie's `Document` base class, which provides:
- `save()` and `create()` for persistence
- `find()` and `find_one()` for queries
- `delete()` for removal
- Automatic `_id` and `created_at`/`updated_at` timestamps

## How It Works

### Typical File Upload Flow

1. **User uploads a file** via API (e.g., multipart form data)
2. **Application validates** the file (size, type, quota)
3. **Application uploads bytes to cloud storage** (S3/GCS) and gets back a cloud-side path or key
4. **Application creates a `FileObj` document**:
   ```python
   file_obj = FileObj(
       owner="workspace_123",
       file_name="report.xlsx",
       bucket="prod-files",
       provider="s3",
       path_in_bucket="workspaces/123/uploads/report-uuid.xlsx",
       mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
       size=2048576,
       public=False
   )
   await file_obj.create()
   ```
5. **Application returns the `FileObj.id`** (MongoDB ObjectId) to the client

### File Download/Access Flow

1. **Client requests file** by `FileObj.id`
2. **Application retrieves the `FileObj`** record
3. **Application validates ownership**: Check if `request.user.workspace == file_obj.owner`
4. **Application generates a pre-signed URL** using the `provider`, `bucket`, and `path_in_bucket` fields
5. **Application returns the URL** (or redirects to it)
6. **Client/browser downloads directly from cloud storage**, bypassing the application

### Query Patterns

Because `owner` is indexed:
```python
# Fast: indexed lookup
user_files = await FileObj.find(FileObj.owner == "user_123").to_list()

# Slower but possible: filter by provider
local_files = await FileObj.find(FileObj.provider == "local").to_list()

# Combined: workspace files that are public
public_workspace_files = await FileObj.find(
    FileObj.owner == "workspace_456",
    FileObj.public == True
).to_list()
```

## Authorization and Security

**Access Control is NOT enforced in this module**—it's a responsibility of the **caller**. The `FileObj` itself has no methods to validate access; it's just a data container.

**The `owner` field is the key**: Wherever files are retrieved or downloaded, the calling code must verify:
```python
file_obj = await FileObj.get(file_id)
if file_obj.owner != current_user.workspace_id:
    raise PermissionError("Cannot access this file")
```

**The `public` flag is informational**: It signals intent but does not enforce access. The API layer is responsible for checking this flag and deciding whether to grant unauthenticated access.

**Pre-signed URLs are time-limited**: When the application generates a pre-signed URL (via AWS SDK or GCS client), the cloud provider itself expires it after a period (typically 1 hour). This ensures files cannot be downloaded indefinitely with a leaked link.

## Dependencies and Integration

**Direct Dependencies:**
- **Beanie** (`from beanie import Document, Indexed`) — ODM (Object-Document Mapper) for MongoDB. Provides the base `Document` class and the `Indexed` type annotation for indexing.
- **Pydantic** (`from pydantic import Field`) — Data validation and serialization. The `Field` with `pattern` validator enforces that `provider` is one of the three allowed strings.

**Indirect Dependencies:**
- **MongoDB** — The persistence layer. `FileObj` records are stored and queried here.
- **AWS S3 SDK** or **Google Cloud Storage SDK** — Used by higher-level code to upload/download bytes and generate pre-signed URLs. This module does not depend on those SDKs directly; it just records the metadata needed to use them.

**Imported By:**
- **`__init__.py`** (in the parent `ee/cloud/models/` package) — Exports `FileObj` so other modules can import it as `from pocketPaw.ee.cloud.models import FileObj`.

**Used By (expected):**
- **File upload/download API routes** — Handle HTTP requests, validate access, call cloud SDKs, and create/retrieve `FileObj` documents
- **Workspace/organization services** — May query files by owner for listing or cleanup
- **Sharing/permission services** — May modify `public` flag or create access tokens for specific files
- **Quota/billing services** — Aggregate `size` field across workspace files to enforce limits

## Design Decisions

### 1. **Metadata-Only Model**
The module stores *only* metadata, not bytes. This is intentional. Storing binary data in MongoDB would:
- Inflate database size and backup costs
- Cause slower queries (binary fields slow down indexing)
- Complicate replication and sharding

By keeping only pointers, `FileObj` documents are lightweight and queryable.

### 2. **Multi-Provider Support**
The `provider` field (gcs | s3 | local) allows the application to support multiple storage backends. This enables:
- **Gradual migration** from local to S3, or S3 to GCS, without re-uploading
- **Hybrid deployments** where different workspaces use different storage
- **Testing** with local storage in dev, S3 in prod

### 3. **Pre-signed URL Pattern**
The design assumes the application will generate pre-signed (temporary, signed) URLs rather than proxying downloads through the application. This is efficient because:
- Cloud storage CDNs are faster and cheaper than application servers
- Reduces load on application servers
- Leverages cloud provider's security (signatures are cryptographically valid for only the specified object, method, and time)

### 4. **Indexed Owner Field**
The `owner` field is indexed because:
- Workspaces frequently list "my files" — a query on `owner`
- Access control checks happen on almost every request — index ensures sub-millisecond validation
- It's the only field with this pattern in the current model

### 5. **Beanie ODM Choice**
Using Beanie (an async-first MongoDB ODM) implies the application is:
- Built on async/await (likely FastAPI or similar)
- Comfortable with Python OOP abstractions over raw pymongo
- Willing to trade some flexibility for type safety and validation

### 6. **Minimal Defaults**
Fields like `mime_type` and `size` default to empty/zero. This allows creation of `FileObj` records even if those details are not immediately available, supporting two-phase uploads (create metadata stub, populate details later). It also prevents validation errors if callers are uncertain about a field's value.

---

## Related

- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
