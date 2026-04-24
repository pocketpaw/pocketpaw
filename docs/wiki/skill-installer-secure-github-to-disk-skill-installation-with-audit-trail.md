---
{
  "title": "Skill Installer: Secure GitHub-to-Disk Skill Installation with Audit Trail",
  "summary": "This module handles the full lifecycle of installing skills from GitHub repositories: validating the source string, cloning to a temp directory, stripping symlinks, copying SKILL.md directories to the install location, and emitting audit events for every installation attempt. Symlink stripping is the key defensive measure against path traversal attacks via malicious repositories.",
  "concepts": [
    "skill installer",
    "GitHub cloning",
    "symlink stripping",
    "path traversal prevention",
    "SkillInstallError",
    "SKILL.md convention",
    "audit trail",
    "tempfile isolation",
    "AgentSkills",
    "supply chain security",
    "shutil.copytree"
  ],
  "categories": [
    "skills system",
    "security",
    "package management"
  ],
  "source_docs": [
    "1e166e209d3a2f99"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why a Dedicated Installer?

Installing code from an external source (GitHub) into a directory that an agent reads and executes is inherently risky. The installer exists to put security controls around this operation: validate inputs, isolate the clone, strip attack vectors, and log everything.

## Input Validation

`install_skill_from_source(source)` begins with strict validation of the `source` string. Only specific GitHub URL formats are accepted — bare `owner/repo` shorthand and full `https://github.com/owner/repo` URLs. The validation uses regex to prevent path traversal in the source string itself (e.g., `../../evil/path` as a "GitHub source").

## Temp Directory Isolation

The clone is performed into a `tempfile.TemporaryDirectory()` context, ensuring that even if the clone or copy fails partway through, no partial files remain in the install directory. The temp directory is cleaned up automatically on exit from the context manager.

## Symlink Stripping: The Key Defense

`_ignore_symlinks(src, names)` returns the set of names that are symlinks so `shutil.copytree()` skips them. This is the primary defense against a category of supply chain attack:

A malicious skill repository could contain a symlink like `SKILL.md -> /etc/passwd`. When `shutil.copytree()` follows that symlink, it reads the target file and copies it into the install directory — potentially overwriting a legitimate skill with system file contents, or disclosing the target file to whoever reads the installed "skill".

By refusing to copy any symlink, the installer ensures that the installed skill directory contains only regular files.

## SKILL.md Directory Discovery

After cloning, the installer walks the temp directory looking for directories that contain a `SKILL.md` file. This follows the AgentSkills convention: a skill is a directory, not a single file. Finding multiple `SKILL.md` files in one repo means installing multiple skills from that repo in one operation.

## Audit Integration

Every installation attempt — successful or failed — is logged via `get_audit_logger()`. The audit event captures the source URL, the number of skills installed, and the installation status. This creates an evidence trail: if a malicious skill is later discovered, operators can trace when it was installed and from what source.

## `SkillInstallError`

`SkillInstallError` carries both a human-readable `message` and a `status_code` (HTTP status integer). The status code allows the API layer to return appropriate HTTP responses (e.g., `400` for invalid source, `500` for clone failure) without the API layer needing to inspect error messages.

## Constants

`INSTALL_DIR = Path.home() / ".agents" / "skills"` is the canonical install location, shared with the AgentSkills ecosystem. Defining it as a module-level constant ensures the installer and loader agree on where skills live.

## Known Gaps

- **No signature verification**: There is no GPG or other cryptographic verification of the cloned content. A compromised GitHub repository (account takeover) would install its malicious content without detection.
- **Branch pinning**: The clone uses the repository's default branch unless the source string specifies otherwise. Skills can change silently between installations.
- **No rollback**: If installation of multiple skills from one repo partially succeeds (some installed, then an error), there is no rollback to remove the partially installed skills.