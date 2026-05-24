"""Standalone cleanup for a failed or interrupted Week 7 provision.py run."""

from __future__ import annotations

import sys

from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from config import REGION
from provision import cleanup_resources, load_state, make_clients


def main() -> int:
    """Remove assignment resources discovered from saved state or AWS tags."""
    clients = make_clients(REGION)
    state = load_state()
    try:
        print(f"Cleaning Week 7 resources in region: {REGION}")
        cleanup_resources(clients, state, remove_state=True)
        print("Cleanup complete.")
        return 0
    except (ClientError, BotoCoreError, WaiterError, RuntimeError) as error:
        print(f"Cleanup failed: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
