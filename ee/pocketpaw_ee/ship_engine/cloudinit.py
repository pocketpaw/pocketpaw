# ee/pocketpaw_ee/ship_engine/cloudinit.py — the cloud-init user-data a fresh
# box boots with so it comes up as a Dokku-ready deploy target.
#
# ``render_user_data`` returns the cloud-init YAML (a "#cloud-config" document)
# that: installs Docker, installs a PINNED Dokku version, installs nixpacks (the
# default builder for /code-built apps), and authorizes our provisioning public
# key for the root and ``dokku`` users. Provisioning is poll-and-probe, not
# phone-home (v1): the provisioner watches the provider status, then SSHes in
# and runs ``dokku version`` to confirm readiness — so this template installs,
# it does not call back.
#
# SECURITY: the template carries only the PUBLIC key. Private-key material never
# appears in user-data (it is minted by the provisioner and stored Fernet-
# encrypted on the ShipBox doc). The Dokku + nixpacks versions are pinned so a
# box's control surface is reproducible and the SHIP-1 driver's transcript
# assumptions hold.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.

from __future__ import annotations

# Pinned so every box exposes the same Dokku control surface (the SHIP-1 driver
# parses versioned output). Bump deliberately, in lockstep with the driver's
# transcripts.
DOKKU_VERSION = "0.35.20"
NIXPACKS_VERSION = "1.29.1"


def render_user_data(*, ssh_public_key: str) -> str:
    """Render the cloud-init user-data for a Dokku-ready box.

    ``ssh_public_key`` is the provisioning key's PUBLIC half — authorized for
    root and the ``dokku`` user so the driver can reach the box. It must be a
    single-line OpenSSH public key; anything else is rejected (a malformed key
    would silently lock us out of the box).
    """
    key = ssh_public_key.strip()
    if not key or "\n" in key or not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise ValueError("ssh_public_key must be a single-line OpenSSH public key")

    # A #cloud-config document. runcmd order matters: Docker first (Dokku's
    # bootstrap needs it), then the pinned Dokku bootstrap, then nixpacks, then
    # authorize the key for the dokku user (created by the Dokku install).
    # The dokku-user authorize command is assembled here so no source line runs
    # past the line-length limit (it is a single shell command by nature).
    dokku_ssh_dir = "/home/dokku/.ssh"
    authorize_dokku = (
        f"mkdir -p {dokku_ssh_dir} && "
        f"echo '{key}' >> {dokku_ssh_dir}/authorized_keys && "
        f"chown -R dokku:dokku {dokku_ssh_dir} && "
        f"chmod 600 {dokku_ssh_dir}/authorized_keys"
    )
    dokku_bootstrap = f"https://dokku.com/install/v{DOKKU_VERSION}/bootstrap.sh"

    # A #cloud-config document. runcmd order matters: Docker first (Dokku's
    # bootstrap needs it), then the pinned Dokku bootstrap, then nixpacks, then
    # authorize the key for the dokku user (created by the Dokku install).
    return f"""#cloud-config
users:
  - name: root
    ssh_authorized_keys:
      - {key}
package_update: true
packages:
  - curl
  - ca-certificates
runcmd:
  - [ sh, -c, "curl -fsSL https://get.docker.com | sh" ]
  - [ sh, -c, "wget -qO /tmp/bootstrap.sh {dokku_bootstrap}" ]
  - [ sh, -c, "DOKKU_TAG=v{DOKKU_VERSION} bash /tmp/bootstrap.sh" ]
  - [ sh, -c, "curl -fsSL https://nixpacks.com/install.sh | VERSION={NIXPACKS_VERSION} bash" ]
  - [ sh, -c, "{authorize_dokku}" ]
  - [ sh, -c, "echo '{key}' | dokku ssh-keys:add admin" ]
"""
