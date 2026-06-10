# _dev_license_key.py — DEV-ONLY Ed25519 license signing key.
# Created: 2026-06-10 (sov/w0a-license).
#
# WHY THIS EXISTS: so a fresh checkout / CI run can MINT a signed license
# (``pocketpaw_ee.cloud.mint``) and have it VERIFY (``...cloud.license``)
# with zero setup. The matching PUBLIC key is baked into
# ``license._DEV_PUBLIC_KEY_HEX``.
#
# !!! NOT A SECRET. NOT FOR PRODUCTION. !!!
# This private seed is committed in the open. Anyone can read it and mint a
# license that the DEV public key accepts. That is fine for local dev / tests
# and ONLY for that. In production an operator MUST:
#   1. Generate their own keypair (``mint generate-keypair``).
#   2. Bake/export their PUBLIC key via ``POCKETPAW_LICENSE_PUBLIC_KEY``.
#   3. Keep the PRIVATE key out of the repo (a file / secret manager / env),
#      and pass it to ``mint`` via ``--private-key-file`` or
#      ``POCKETPAW_LICENSE_PRIVATE_KEY``.
# Once the operator public key is set it overrides this DEV key, so a license
# minted with this dev seed will NOT verify in production.

from __future__ import annotations

# Raw 32-byte Ed25519 private seed, hex-encoded. Pairs with
# ``license._DEV_PUBLIC_KEY_HEX``.
DEV_PRIVATE_KEY_HEX = "3a8241e44ce06ac23faba38e6193a55c5a6a6c72678e48e717b5815905d60ed7"
