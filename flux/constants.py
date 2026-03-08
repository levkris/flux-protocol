import re
from pathlib import Path

FLUX_VERSION = "1.0"
FLUX_API_VERSION = "2.1.0"

MAX_CONTENT_BYTES = 65_536
MAX_PENDING_PER_ADDRESS = 500
MAX_MESSAGE_AGE_MS = 300_000  # ms, 5 minutes
WS_PING_INTERVAL = 20 # seconds

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

ADDRESS_PREFIX = "fx1"
ADDRESS_HASH_LEN = 40

DEFAULT_INBOX = "inbox"

TAG_IMPORTANT = "important"
TAG_FAVORITED = "favorited"
RESERVED_TAGS = {TAG_IMPORTANT, TAG_FAVORITED}

MESH_HEADER = "X-Flux-Mesh-Token"
MESH_CONFIG_PATH = Path("mesh.config.json")

FEDERATION_CACHE_TTL = 300 # seconds
FEDERATED_RE = re.compile(r"^([a-zA-Z0-9._-]+)@([a-zA-Z0-9._:-]+)$")

TRUST_THRESHOLD = 3 # tamper reports before a server is quarantined

# Spam
MAX_MSGS_PER_MINUTE = 20
MIN_CONTENT_LEN = 1
URL_COUNT_THRESHOLD = 5 # URLs per 200 chars
REPETITION_RATIO = 0.85 # single char dominance → spam
SPAM_SCORE_THRESHOLD = 4

SPAM_KEYWORDS: list[tuple[str, int]] = [
    ("winner", 2), ("congratulations", 1), ("claim your prize", 3),
    ("click here", 2), ("free money", 3), ("make money fast", 3),
    ("limited time offer", 2), ("act now", 2), ("risk free", 2),
    ("100% free", 2), ("guaranteed", 1), ("no cost", 1),
    ("earn extra cash", 3), ("work from home", 2),
    ("verify your account", 3), ("confirm your details", 3),
    ("update your information", 2), ("suspended", 2),
    ("unusual activity", 2), ("click the link below", 2),
    ("buy now", 2), ("cheap meds", 3), ("discount pills", 3),
    ("enlarge", 2), ("lose weight", 2),
]

SPAM_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SPAM_SUBJECT_CAPS_RE = re.compile(r"[A-Z]{5,}")
SPAM_SUBJECT_PUNCT_RE = re.compile(r"[!?]{3,}")