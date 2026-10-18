"""Read-only access to Google Cloud REST APIs, shared by the GCP collectors.

Two independent guards keep collection read-only:

* Credentials are requested with the `cloud-platform.read-only` OAuth
  scope, so Google refuses any write at the token level (for service
  accounts and impersonation; user credentials from `gcloud auth
  application-default login` carry whatever scopes they were granted).
* `Api` can only send GET requests. There is no other method to call.

Talking REST through `google-auth` keeps the extra small (no per-service
client libraries), and lets tests swap in a fake transport.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

READ_ONLY_SCOPE = "https://www.googleapis.com/auth/cloud-platform.read-only"

CRM = "https://cloudresourcemanager.googleapis.com/v3"
COMPUTE = "https://compute.googleapis.com/compute/v1"
ORG_POLICY = "https://orgpolicy.googleapis.com/v2"
ASSURED_WORKLOADS = "https://assuredworkloads.googleapis.com/v1"
NCC = "https://networkconnectivity.googleapis.com/v1"

_RETRY_STATUS = {429, 500, 502, 503, 504}
_DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}

# A transport takes (url, params) and returns (HTTP status, decoded JSON body).
Transport = Callable[[str, Dict[str, Any]], Tuple[int, Any]]


class ApiError(Exception):
    """A Google API call that failed after retries."""

    def __init__(self, status: int, body: Any, url: str):
        error = body.get("error", {}) if isinstance(body, dict) else {}
        self.status = status
        self.code = str(error.get("status") or status)
        self.message = str(error.get("message") or "")
        self.url = url
        reasons = {e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)}
        reasons |= {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
        self.reasons = {r for r in reasons if r}
        super().__init__(f"{self.code} ({status})")

    @property
    def disabled(self) -> bool:
        """The API isn't enabled in the project the call was billed to."""
        return bool(self.reasons & _DISABLED_REASONS) or "has not been used in project" in self.message

    @property
    def denied(self) -> bool:
        return self.status == 403 and not self.disabled

    @property
    def missing(self) -> bool:
        return self.status in (400, 404)


class Api:
    """GET-only access to Google APIs, with retries and pagination."""

    def __init__(self, transport: Transport, retries: int = 4, backoff: float = 1.0):
        self._transport = transport
        self._retries = retries
        self._backoff = backoff
        self.calls = 0

    def get(self, url: str, **params: Any) -> Dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(self._retries + 1):
            self.calls += 1
            status, body = self._transport(url, params)
            if status < 400:
                return body if isinstance(body, dict) else {}
            if status not in _RETRY_STATUS or attempt == self._retries:
                raise ApiError(status, body, url)
            time.sleep(self._backoff * (2 ** attempt))
        raise AssertionError("unreachable")

    def pages(self, url: str, key: str, **params: Any) -> List[dict]:
        """Every item of a paginated list."""
        items: List[dict] = []
        token: Optional[str] = None
        while True:
            body = self.get(url, pageToken=token, **params)
            items.extend(body.get(key, []))
            token = body.get("nextPageToken")
            if not token:
                return items

    def aggregated(self, url: str, key: str, **params: Any) -> List[dict]:
        """Every item of a Compute aggregated list (`{"items": {"regions/x":
        {key: [...]}}}`), across all regions and zones."""
        items: List[dict] = []
        token: Optional[str] = None
        while True:
            body = self.get(url, pageToken=token, **params)
            for scoped in (body.get("items") or {}).values():
                items.extend(scoped.get(key, []))
            token = body.get("nextPageToken")
            if not token:
                return items


def require_google_auth() -> Any:
    try:
        import google.auth  # noqa: F401
        import google.auth.transport.requests  # noqa: F401
    except ImportError:
        raise RuntimeError("the GCP collectors need google-auth: pip install 'topomap[gcp]'") from None
    import google.auth
    return google.auth


def default_transport(impersonate: Optional[str] = None, quota_project: Optional[str] = None,
                      timeout: float = 60) -> Transport:
    """A transport using Application Default Credentials (gcloud, a service
    account key, or the metadata server), optionally impersonating a
    service account."""
    google_auth = require_google_auth()
    from google.auth.exceptions import DefaultCredentialsError
    from google.auth.transport.requests import AuthorizedSession
    try:
        credentials, _ = google_auth.default(scopes=[READ_ONLY_SCOPE])
    except DefaultCredentialsError:
        raise RuntimeError("no Google Cloud credentials found: run 'gcloud auth "
                           "application-default login' or set GOOGLE_APPLICATION_CREDENTIALS") from None
    if impersonate:
        from google.auth import impersonated_credentials
        credentials = impersonated_credentials.Credentials(
            source_credentials=credentials, target_principal=impersonate,
            target_scopes=[READ_ONLY_SCOPE])
    if quota_project and hasattr(credentials, "with_quota_project"):
        credentials = credentials.with_quota_project(quota_project)
    session = AuthorizedSession(credentials)

    def get(url: str, params: Dict[str, Any]) -> Tuple[int, Any]:
        response = session.get(url, params=params, timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body

    return get


def add_auth_arguments(parser) -> None:
    parser.add_argument("--impersonate-service-account", metavar="EMAIL",
                        help="act as this service account (needs roles/iam.serviceAccountTokenCreator "
                             "on it)")
    parser.add_argument("--quota-project", metavar="PROJECT",
                        help="project to bill API quota to (some APIs require one with user "
                             "credentials)")


def api_from_args(args: Any) -> Api:
    return Api(default_transport(getattr(args, "impersonate_service_account", None),
                                 getattr(args, "quota_project", None)))


# -- Resource Manager: walking the hierarchy -------------------------------------------

def find_organization(api: Api, org: Optional[str]) -> Dict[str, Any]:
    """The organization to collect: the one asked for, or the only one the
    caller can see."""
    if org:
        org_id = org.split("/")[-1]
        return api.get(f"{CRM}/organizations/{org_id}")
    found = api.pages(f"{CRM}/organizations:search", "organizations")
    if len(found) == 1:
        return found[0]
    if not found:
        raise RuntimeError("these credentials can't see any Google Cloud organization; pass "
                           "--organization ID (and check roles/resourcemanager.organizationViewer)")
    names = ", ".join(sorted(o.get("name", "?") for o in found))
    raise RuntimeError(f"these credentials can see several organizations ({names}); pick one "
                       "with --organization")


def children(api: Api, parent: str) -> Tuple[List[dict], List[dict]]:
    """(folders, projects) directly under `parent` ("organizations/N" or
    "folders/N"), skipping ones being deleted."""
    folders = api.pages(f"{CRM}/folders", "folders", parent=parent)
    projects = api.pages(f"{CRM}/projects", "projects", parent=parent)
    return ([f for f in folders if f.get("state", "ACTIVE") == "ACTIVE"],
            [p for p in projects if p.get("state", "ACTIVE") == "ACTIVE"])


def walk(api: Api, root: str, warn: Callable[[str], None]) -> Iterable[Tuple[str, str, List[dict], List[dict]]]:
    """Breadth-first through the folder tree below `root`, yielding
    (parent name, its path, folders, projects). A branch that can't be
    listed is a warning, not the end of the walk."""
    queue = [(root, "")]
    while queue:
        parent, path = queue.pop(0)
        try:
            folders, projects = children(api, parent)
        except ApiError as e:
            warn(f"cannot list {path or parent} ({e.code}); its contents are missing")
            continue
        yield parent, path, folders, projects
        for folder in folders:
            queue.append((folder["name"], f"{path}/{folder.get('displayName', folder['name'])}".lstrip("/")))


def short(url_or_name: Optional[str]) -> str:
    """The last path segment of a self link or resource name."""
    return (url_or_name or "").rstrip("/").split("/")[-1]


def link_project(url: Optional[str]) -> str:
    """The project in a Compute self link (".../projects/P/...")."""
    parts = (url or "").split("/")
    return parts[parts.index("projects") + 1] if "projects" in parts else ""


def link_region(url: Optional[str]) -> str:
    parts = (url or "").split("/")
    for key in ("regions", "zones"):
        if key in parts:
            value = parts[parts.index(key) + 1]
            return value if key == "regions" else value.rsplit("-", 1)[0]
    return ""
