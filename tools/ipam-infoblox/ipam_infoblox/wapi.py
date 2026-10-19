"""Read-only access to the Infoblox WAPI (NIOS REST API), standard library only.

Only GET requests exist here: there is no method that could create, change
or delete anything in the Grid. The import itself stays a person's job,
through Grid Manager's CSV import, where it is reviewed and logged.

Credentials go in a Basic auth header over HTTPS; the session cookie NIOS
returns is reused so the Grid sees one login, not one per page. Plain HTTP
is refused unless asked for, because it would send the password in clear.
"""

from __future__ import annotations

import base64
import http.cookiejar
import ipaddress
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .records import Network

DEFAULT_VERSION = "2.12"
PAGE_SIZE = 1000
NETWORK_FIELDS = "network,network_view,comment,extattrs"
# WAPI object -> the kind it is in the plan.
NETWORK_OBJECTS = (("networkcontainer", "container"), ("network", "network"),
                   ("ipv6networkcontainer", "container"), ("ipv6network", "network"))


class WapiError(RuntimeError):
    """A WAPI call that failed, with the reason in words."""


class Wapi:
    """GET-only WAPI client with paging."""

    def __init__(self, url: str, username: str, password: str, version: str = DEFAULT_VERSION,
                 ca_bundle: Optional[str] = None, insecure: bool = False,
                 allow_http: bool = False, timeout: float = 60):
        parts = urllib.parse.urlsplit(url if "://" in url else f"https://{url}")
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ValueError(f"--wapi: not a Grid Manager URL: {url}")
        if parts.scheme == "http" and not allow_http:
            raise ValueError("--wapi uses http://, which would send the password in clear; use "
                             "https:// (or --allow-http for a lab)")
        self.base = f"{parts.scheme}://{parts.netloc}/wapi/v{version.lstrip('v')}"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._timeout = timeout
        handlers: List[Any] = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if parts.scheme == "https":
            context = ssl.create_default_context(cafile=ca_bundle)
            if insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)
        self.calls = 0

    def get(self, obj: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base}/{obj}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, method="GET",
                                         headers={"Authorization": self._auth,
                                                  "Accept": "application/json"})
        self.calls += 1
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            raise WapiError(_http_reason(e, obj)) from None
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise WapiError(f"the Grid Manager's certificate isn't trusted ({reason.verify_message}); "
                                "pass --ca-bundle with your CA's certificate") from None
            raise WapiError(f"cannot reach {self.base}: {reason}") from None
        except ValueError:
            raise WapiError(f"{obj}: the Grid Manager's answer isn't JSON") from None

    def all(self, obj: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Every object a search returns, page by page."""
        first = dict(params or {}, _paging=1, _max_results=PAGE_SIZE, _return_as_object=1)
        page = self.get(obj, first)
        items = list(page.get("result", []))
        while page.get("next_page_id"):
            page = self.get(obj, {"_page_id": page["next_page_id"]})
            items.extend(page.get("result", []))
        return items


def _http_reason(error: urllib.error.HTTPError, obj: str) -> str:
    if error.code == 401:
        return "the Grid Manager refused the username or password (401)"
    try:
        body = json.loads(error.read().decode("utf-8"))
        text = body.get("text") or body.get("Error") or ""
    except (ValueError, AttributeError):
        text = ""
    if error.code == 403:
        return f"{obj}: this account may not read it (403){': ' + text if text else ''}"
    return f"{obj}: HTTP {error.code}{': ' + text if text else ''}"


@dataclass
class Existing:
    """What the Grid already holds in the network views that matter."""
    objects: Dict[str, Dict[Network, Dict[str, Any]]] = field(default_factory=dict)  # view -> cidr -> info
    views: List[str] = field(default_factory=list)                                 # every view in the Grid
    ea_defs: Dict[str, Dict[str, Any]] = field(default_factory=dict)               # name -> {type, values}

    def in_view(self, view: str) -> Dict[Network, Dict[str, Any]]:
        return self.objects.get(view, {})


def load_existing(wapi: Wapi, views: Iterable[str]) -> Existing:
    """Networks and containers in each view, every view's name, and the
    extensible attribute definitions."""
    existing = Existing()
    existing.views = sorted(v.get("name", "") for v in wapi.all("networkview", {"_return_fields": "name"}))
    for view in sorted(set(views)):
        found: Dict[Network, Dict[str, Any]] = {}
        if view in existing.views:
            for obj, kind in NETWORK_OBJECTS:
                for item in wapi.all(obj, {"network_view": view, "_return_fields": NETWORK_FIELDS}):
                    try:
                        cidr = ipaddress.ip_network(item.get("network", ""), strict=False)
                    except ValueError:
                        continue
                    extattrs = {k: (v.get("value") if isinstance(v, dict) else v)
                                for k, v in (item.get("extattrs") or {}).items()}
                    found[cidr] = {"kind": kind, "comment": item.get("comment") or "",
                                   "extattrs": extattrs}
        existing.objects[view] = found
    for definition in wapi.all("extensibleattributedef", {"_return_fields": "name,type,list_values"}):
        values = [v.get("value") for v in definition.get("list_values") or [] if isinstance(v, dict)]
        existing.ea_defs[definition.get("name", "")] = {"type": definition.get("type", ""),
                                                         "values": values}
    return existing
