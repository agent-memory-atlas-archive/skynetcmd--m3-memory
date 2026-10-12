#!/usr/bin/env python3
from m3_core.seams import roots as _seam_roots

# --config FILE (the file this job was registered with) is pinned before any
# import below binds a root at import time (chatlog_config, log dirs).
_seam_roots.pin_from_argv()

import logging
import os
import secrets
import string
import sys
from datetime import UTC, datetime

# Add the bin directory to path so we can import SDK modules if called from elsewhere
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from auth_utils import set_api_key
from m3_sdk import M3Context

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("SecretRotator")

# Target keys that should be rotated. (Do NOT rotate external keys like OpenAI/Anthropic/Gemini)
# We only rotate internal OS keys that we control, like PostgreSQL or internal service tokens.
ROTATION_TARGETS = ["AGENT_OS_INTERNAL_SERVICE_TOKEN"]


def generate_secure_token(length: int = 64) -> str:
    """Generates a cryptographically secure random string."""
    alphabet = string.ascii_letters + string.digits + string.punctuation.replace('"', "").replace("'", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def rotate_secrets(dry_run: bool = False):
    logger.info(f"Starting automated secret rotation {'(DRY RUN)' if dry_run else ''}...")
    # The vault and its backups go through the seam, so rotation reads and
    # writes the live store on every backend (set_api_key already does).
    from m3_core.paths import active_database, seam_backend, seam_dialect

    _p = seam_dialect().param()
    ctx = M3Context.for_db(None)

    for key_name in ROTATION_TARGETS:
        try:
            logger.info(f"Rotating secret for: {key_name}")

            # 1. Fetch current secret for backup
            old_secret = ctx.get_secret(key_name)
            if old_secret and not dry_run:
                # Store backup in a dedicated table (migrations 052 / pg_061 own it)
                with active_database(ctx.db_path), seam_backend().connection() as conn:
                    # Get the encrypted blob directly from the source table
                    row = conn.execute(
                        f"SELECT encrypted_value FROM synchronized_secrets WHERE service_name = {_p}", (key_name,)
                    ).fetchone()
                    if row:
                        conn.execute(
                            "INSERT INTO secret_backups (service_name, encrypted_value, rotated_at) "
                            f"VALUES ({_p}, {_p}, {_p})",
                            (key_name, row[0], datetime.now(UTC).isoformat()),
                        )
                        conn.commit()
                        logger.info(f"Backup created for {key_name}.")

            # 2. Generate new cryptographically secure key
            new_secret = generate_secure_token()

            if dry_run:
                logger.info(f"[DRY RUN] Would rotate {key_name} to new {len(new_secret)} char token.")
                continue

            # 3. Store securely in the vault (the active backend's)
            set_api_key(key_name, new_secret)

            # 4. Log the rotation event
            ctx.log_event(
                category="security",
                detail_a="Automated Secret Rotation",
                detail_b=f"Successfully rotated {key_name} and encrypted to vault.",
            )
            logger.info(f"Successfully rotated and vaulted {key_name}.")

        except Exception as e:
            logger.error(
                f"Failed to rotate {key_name}; observed: {type(e).__name__}: {e}. "
                "inspect: the vault (synchronized_secrets) and the master key; "
                "re-run with --dry-run to check"
            )
            if not dry_run:
                ctx.log_event(
                    category="security_error",
                    detail_a="Automated Secret Rotation Failed",
                    detail_b=f"Failed to rotate {key_name}: {e}",
                )

    logger.info(f"Secret rotation complete {'(DRY RUN)' if dry_run else ''}.")


if __name__ == "__main__":
    import argparse
    import os as _os

    from _task_runtime import add_log_file_arg, setup_task_runtime
    from m3_sdk import add_database_arg

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    add_log_file_arg(parser)
    add_database_arg(parser)
    args = parser.parse_args()
    setup_task_runtime(args.log_file, lock_name="secret_rotator")
    if args.database:
        _os.environ["M3_DATABASE"] = args.database
    rotate_secrets(dry_run=args.dry_run)
