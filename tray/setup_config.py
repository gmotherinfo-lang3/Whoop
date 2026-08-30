"""Kept here for the tray's imports; the implementation lives with the
bridge, because the CLI needs it too and whoop_bridge must not depend on tray."""

from whoop_bridge.setup_config import *  # noqa: F401,F403
from whoop_bridge.setup_config import (  # noqa: F401
    claim_pairing, existing_server, needs_setup, normalise_server,
    set_config_value, write_pairing)
