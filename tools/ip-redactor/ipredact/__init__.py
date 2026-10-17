"""Structure-preserving IPv4 / IPv6 redaction."""

from .core import DEFAULT_EXCLUDES, Redactor, fmt_v4, fmt_v6, mask_len

__all__ = ["DEFAULT_EXCLUDES", "Redactor", "fmt_v4", "fmt_v6", "mask_len"]
__version__ = "0.3.0"
