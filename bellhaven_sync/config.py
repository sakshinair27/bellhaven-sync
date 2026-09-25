"""Runtime configuration. Everything can be overridden with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

BASE_URL = os.environ.get("BH_BASE_URL", "https://analyst-assessment-production.up.railway.app")
API_BASE = BASE_URL + "/api/v1"
API_TOKEN = os.environ.get("BH_API_TOKEN", "")
DB_PATH = Path(os.environ.get("BH_DB_PATH", ROOT / "data" / "state.db"))

# The operator we are reconciling. Matched by exact account name in the CRM.
TARGET_PARENT_NAME = os.environ.get("BH_TARGET_PARENT", "Bellhaven Senior Living (Parent Account)")
OPERATOR_KEYWORD = "bellhaven"

# Safety valve: abort a run if the website suddenly lists far fewer locations than last
# time (a broken scrape would otherwise flag every CRM account as "no longer on website").
MIN_SCRAPE_RATIO = float(os.environ.get("BH_MIN_SCRAPE_RATIO", "0.8"))

# Website care offerings -> CRM care_type picklist.
CARE_TYPE_MAP = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "independent living": "Independent Living",
}
