"""Minimal Zotero Web API (v3) client for bibfix: find items by citation key,
export them as BibTeX, update fields, and create items.

zotero.org is the master copy of the bibliography, so this works from any
machine with network access, including CI, without the Zotero
app running.

Configuration (environment, e.g. via direnv):
  ZOTERO_API_KEY       key from zotero.org/settings/keys (needs write access for push/add)
  ZOTERO_LIBRARY_ID    numeric user ID (shown on the same page) or group ID
  ZOTERO_LIBRARY_TYPE  "user" (default) or "group"

Citation keys: Better BibTeX pins keys into the synced item, either as the
native `citationKey` field or as a `Citation Key: <key>` line in Extra.
Both are read; `add` uses the native field when available.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

API = "https://api.zotero.org"
BATCH = 50  # Zotero's limit for itemKey lists and multi-object writes
_EXTRA_KEY_RE = re.compile(r"^\s*(?:Citation Key|bibtex)\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_SKIP_TYPES = {"attachment", "note", "annotation"}


class ZoteroError(RuntimeError):
    pass


@dataclass
class ZoteroConfig:
    api_key: str = field(repr=False)
    library_id: str
    library_type: str = "user"

    def __post_init__(self) -> None:
        if self.library_type not in ("user", "group"):
            raise ZoteroError("Zotero library type must be 'user' or 'group'.")
        if not self.library_id.isascii() or not self.library_id.isdigit():
            raise ZoteroError("Zotero library ID must be numeric.")

    @classmethod
    def from_env(cls) -> "ZoteroConfig":
        key = os.environ.get("ZOTERO_API_KEY", "").strip()
        lib = os.environ.get("ZOTERO_LIBRARY_ID", "").strip()
        if not key or not lib:
            raise ZoteroError(
                "Zotero is not configured: set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID "
                "(both from https://www.zotero.org/settings/keys), e.g. in the project's .envrc."
            )
        kind = os.environ.get("ZOTERO_LIBRARY_TYPE", "user").strip() or "user"
        if kind not in ("user", "group"):
            raise ZoteroError(f"ZOTERO_LIBRARY_TYPE must be 'user' or 'group', not {kind!r}")
        return cls(key, lib, kind)

    @property
    def prefix(self) -> str:
        return f"/{self.library_type}s/{self.library_id}"


def citation_key(data: dict[str, Any]) -> str | None:
    """The pinned citation key of an item's `data`, if any."""
    if data.get("citationKey"):
        return str(data["citationKey"]).strip() or None
    m = _EXTRA_KEY_RE.search(data.get("extra") or "")
    return m.group(1) if m else None


class ZoteroClient:
    def __init__(self, config: ZoteroConfig, *, session: requests.Session | None = None,
                 cache_dir: Path | None = None):
        self.config = config
        self.http = session or requests.Session()
        self.http.headers.update({"Zotero-API-Key": config.api_key, "Zotero-API-Version": "3"})
        self.cache_dir = cache_dir or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bibfix"
        self._items: list[dict] | None = None
        self._type_fields: dict[str, set[str]] = {}

    # -- low level -------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> requests.Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ZoteroError("Zotero requests require a relative API path.")
        url = f"{API}{path}"
        for attempt in range(5):
            try:
                r = self.http.request(method, url, timeout=30, allow_redirects=False, **kw)
            except requests.RequestException as exc:
                raise ZoteroError(f"Zotero {method} request failed ({type(exc).__name__}); check the library before retrying a write.") from exc
            if r.status_code == 429 or r.status_code >= 500:
                if method == "POST" and r.status_code >= 500:
                    raise ZoteroError("Zotero item creation returned a server error; check the library before retrying to avoid duplicates.")
                if attempt == 4:
                    break
                try:
                    wait = float(r.headers.get("Retry-After") or r.headers.get("Backoff") or 2 ** attempt)
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(max(0, min(wait, 60)))
                continue
            if r.status_code == 403:
                raise ZoteroError("Zotero rejected the API key (403). Check the key's library access and write permission.")
            if r.status_code == 412:
                raise ZoteroError("Item changed in Zotero since it was read (412); re-run to pick up the new version.")
            if 300 <= r.status_code < 400 and r.status_code != 304:
                raise ZoteroError("Zotero returned an unexpected redirect; the API key was not forwarded.")
            if r.status_code >= 400:
                raise ZoteroError(f"Zotero API {method} request failed (HTTP {r.status_code}).")
            return r
        raise ZoteroError(f"Zotero API {method} kept failing (rate limited or server error).")

    # -- reading -----------------------------------------------------------
    def items(self, *, refresh: bool = False) -> list[dict]:
        """All top-level bibliographic items (no attachments/notes), cached on
        disk and revalidated with a single If-Modified-Since-Version request."""
        if self._items is not None and not refresh:
            return self._items
        cache = self.cache_dir / f"zotero-{self.config.library_type}-{self.config.library_id}.json"
        cached = None
        if cache.exists() and not refresh:
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if not (isinstance(cached, dict) and isinstance(cached.get("version"), int)
                        and isinstance(cached.get("items"), list)):
                    cached = None
            except (OSError, ValueError):
                cached = None

        headers = {"If-Modified-Since-Version": str(cached["version"])} if cached else {}
        params = {"format": "json", "limit": 100, "start": 0, "itemType": "-attachment"}
        r = self._request("GET", f"{self.config.prefix}/items/top", params=params, headers=headers)
        if r.status_code == 304 and cached:
            self._items = cached["items"]
            return self._items

        version = int(r.headers.get("Last-Modified-Version", 0))
        total = int(r.headers.get("Total-Results", 0))
        out = list(r.json())
        while len(out) < total:
            params["start"] = len(out)
            page = self._request("GET", f"{self.config.prefix}/items/top", params=params).json()
            if not page:
                break
            out.extend(page)
        out = [it for it in out if it.get("data", {}).get("itemType") not in _SKIP_TYPES]

        # Owner-only (NamedTemporaryFile is 0600) and replaced atomically; the cache is optional.
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".zotero-", delete=False) as handle:
                handle.write(json.dumps({"version": version, "items": out}))
            Path(handle.name).replace(cache)
        except OSError:
            pass
        self._items = out
        return out

    def index_by_citekey(self) -> tuple[dict[str, dict], list[dict]]:
        """(citekey -> item, items without a pinned key)."""
        index: dict[str, dict] = {}
        unkeyed: list[dict] = []
        for it in self.items():
            key = citation_key(it["data"])
            if key:
                if key in index:
                    raise ZoteroError(f"More than one Zotero item has citation key {key!r}; resolve the duplicate before syncing or pushing.")
                index[key] = it
            else:
                unkeyed.append(it)
        return index, unkeyed

    def bibtex(self, item_keys: Iterable[str]) -> dict[str, str]:
        """Zotero item key -> BibTeX text, using the server's BibTeX export."""
        item_keys = list(item_keys)
        out: dict[str, str] = {}
        for i in range(0, len(item_keys), BATCH):
            chunk = item_keys[i : i + BATCH]
            r = self._request(
                "GET", f"{self.config.prefix}/items",
                params={"itemKey": ",".join(chunk), "include": "bibtex", "format": "json", "limit": BATCH},
            )
            for obj in r.json():
                out[obj["key"]] = obj.get("bibtex", "").strip()
        return out

    def type_fields(self, item_type: str) -> set[str]:
        if item_type not in self._type_fields:
            r = self._request("GET", "/itemTypeFields", params={"itemType": item_type})
            self._type_fields[item_type] = {f["field"] for f in r.json()}
        return self._type_fields[item_type]

    def template(self, item_type: str) -> dict:
        return self._request("GET", "/items/new", params={"itemType": item_type}).json()

    # -- writing -----------------------------------------------------------
    def update(self, item: dict, new_data: dict) -> None:
        """Replace an item's data (PUT), guarded by its version so a concurrent
        edit in Zotero is never silently overwritten."""
        data = dict(new_data)
        data["key"] = item["key"]
        data["version"] = item["version"]
        self._request(
            "PUT", f"{self.config.prefix}/items/{item['key']}",
            data=json.dumps(data), headers={"Content-Type": "application/json"},
        )
        self._items = None

    def create(self, data_list: list[dict]) -> list[str]:
        """Create items; returns the new Zotero item keys in order."""
        created: list[str] = []
        for i in range(0, len(data_list), BATCH):
            chunk = data_list[i : i + BATCH]
            r = self._request(
                "POST", f"{self.config.prefix}/items",
                data=json.dumps(chunk), headers={"Content-Type": "application/json"},
            )
            body = r.json()
            if body.get("failed"):
                self._items = None
                raise ZoteroError("Zotero refused some items; check the library before retrying a partially completed batch.")
            success = body.get("success", {})
            created.extend(success[str(j)] for j in range(len(chunk)) if str(j) in success)
            self._items = None
            if len(success) != len(chunk):
                raise ZoteroError("Zotero did not confirm every created item; check the library before retrying.")
        return created

    def collection_key(self, name: str) -> str:
        start = 0
        matches = []
        while True:
            r = self._request("GET", f"{self.config.prefix}/collections", params={"limit": 100, "start": start})
            page = r.json()
            matches.extend(c["key"] for c in page if c["data"]["name"] == name)
            start += len(page)
            if not page or start >= int(r.headers.get("Total-Results", start + 1)):
                break
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ZoteroError(f"Multiple Zotero collections are named {name!r}; use a unique collection name.")
        raise ZoteroError(f"No Zotero collection named {name!r}.")
