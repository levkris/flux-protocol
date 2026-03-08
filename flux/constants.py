FLUX_VERSION = "1.0"

# API version
FLUX_API_VERSION = "2.0.0"

# Maximum size of the content field in bytes
MAX_CONTENT_BYTES = 65_536

# Maximum messages held in queue per address before new ones are rejected
MAX_PENDING_PER_ADDRESS = 500

# Clock skew tolerance in milliseconds (5 minutes)
MAX_MESSAGE_AGE_MS = 300_000

# WebSocket heartbeat interval in seconds
WS_PING_INTERVAL = 20

# Default server bind settings
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

# Address prefix used to identify FLUX addresses
ADDRESS_PREFIX = "fx1"

# Length of the address hash segment (hex chars after prefix)
ADDRESS_HASH_LEN = 40
