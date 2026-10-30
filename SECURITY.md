<!-- Security policy: how to report a vulnerability in PocketPaw privately, and what we support. GitHub links this file from the repo's Security tab. -->
# Security policy

## Supported versions

We fix security issues in the latest release on PyPI (`pip install -U pocketpaw`) and on the `dev` branch. Older releases don't get backported fixes, so please upgrade first and check whether the issue is still there.

| Version | Supported |
|---|---|
| Latest 0.4.x release | Yes |
| `dev` branch | Yes |
| Anything older | No |

## Reporting a vulnerability

Please don't open a public issue, discussion or pull request for a security problem.

Report it privately instead, in either of these ways:

1. **GitHub private reporting.** Go to the [Security tab](https://github.com/pocketpaw/pocketpaw/security/advisories/new) and click "Report a vulnerability".
2. **Email** team@qbtrix.com with "PocketPaw security" in the subject.

It helps if you include:

- the version or commit you tested
- steps to reproduce, or a proof of concept
- what an attacker could do with it
- any fix you have in mind

## What happens next

We're a small team, so these are targets, not guarantees:

- We reply to say we got your report within 3 working days.
- We confirm or rule out the issue within 10 working days and tell you what we found.
- Once a fix ships, we publish a GitHub security advisory. We credit you in it unless you'd rather stay anonymous.

Please give us a reasonable chance to ship a fix before you talk about the issue in public.

## Scope

In scope: the code in this repository, including the `ee/` directory, the desktop launcher and the web dashboard.

Out of scope: problems in third-party services PocketPaw connects to (LLM providers, Gmail, Slack and so on), and issues that need an attacker who already has full access to your machine.

For how PocketPaw's own safety layers work (Guardian AI, injection scanning, the audit log), see [docs/security](./docs/security/index.mdx).
