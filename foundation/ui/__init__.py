from pathlib import Path

from fastapi.templating import Jinja2Templates

from foundation.auth import SEED_USERS
from foundation.policy import can
from foundation.ui.audit_format import describe, raw_json

UI_DIR = Path(__file__).parent
STATIC_DIR = UI_DIR / "static"


def make_templates(*extra_dirs: str | Path) -> Jinja2Templates:
    """Templates for an app: its own directories first, then foundation/ui (base + partials)."""
    templates = Jinja2Templates(directory=[*extra_dirs, UI_DIR])
    templates.env.globals.update(
        can=can, switcher_users=SEED_USERS, audit_lines=describe, audit_json=raw_json
    )
    return templates


templates = make_templates()
