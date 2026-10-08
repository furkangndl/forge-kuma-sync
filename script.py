import os
import sys
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from uptime_kuma_api import UptimeKumaApi, MonitorType, Event

def _event_info(self, data):
    self._event_data[Event.INFO] = data


UptimeKumaApi._event_info = _event_info

FORGE_API = "https://forge.laravel.com/api"
REQUEST_DELAY = 1
MONITOR_INTERVAL = 60
MONITOR_RETRIES = 3
ACCEPTED_STATUSCODES = ["200-299"]
NO_RESPONSE_TAG = "no-response"
ALLOWLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowlist.txt")


def load_allowlist(path: str = ALLOWLIST_PATH) -> set:
    """Her satirda bir domain; bos satir ve # yorumlari atlanir."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.split("#", 1)[0].strip().lower() for line in f]
    except FileNotFoundError:
        return set()
    return {line for line in lines if line}


def is_allowed(url: str, allowed: set) -> bool:
    """Host, allowlist'teki domainin kendisi veya alt domaini ise True; allowlist bossa hepsi."""
    if not allowed:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


class Forge:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        retry = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self.s.mount("https://", HTTPAdapter(max_retries=retry))

    def get_paginated(self, url: str) -> list:
        """links.next'i takip ederek tum sayfalari toplar."""
        items = []
        while url:
            r = self.s.get(url, timeout=30)
            r.raise_for_status()
            body = r.json()
            items.extend(body.get("data", []))
            url = (body.get("links") or {}).get("next")
            time.sleep(REQUEST_DELAY)
        return items

    def organizations(self) -> list:
        return self.get_paginated(f"{FORGE_API}/orgs")

    def servers(self, org_slug: str) -> list:
        return self.get_paginated(f"{FORGE_API}/orgs/{org_slug}/servers")

    def sites(self, org_slug: str, server_id) -> list:
        return self.get_paginated(
            f"{FORGE_API}/orgs/{org_slug}/servers/{server_id}/sites"
        )


def collect_sites(forge: Forge, allowed: set) -> tuple[dict, bool]:
    """{url: name} ve listenin eksiksiz olup olmadigini dondurur."""
    found = {}
    complete = True

    try:
        orgs = forge.organizations()
    except Exception:
        return found, False

    for org in orgs:
        slug = org["attributes"]["slug"]
        try:
            servers = forge.servers(slug)
        except Exception:
            complete = False
            continue

        for server in servers:
            attrs = server["attributes"]

            if attrs.get("connection_status") != "successful":
                continue

            if attrs.get("type") not in ("app", "web"):
                continue

            try:
                sites = forge.sites(slug, attrs["id"])
            except Exception:
                complete = False
                continue

            for site in sites:
                sa = site["attributes"]

                if sa.get("status") != "installed":
                    continue

                if sa.get("name") == "default":
                    continue

                url = (sa.get("url") or "").strip().rstrip("/")
                if not url or not is_allowed(url, allowed):
                    continue

                found[url] = sa["name"]

    return found, complete


def notification_ids(api, name: str) -> list:
    """Kuma'da adi birebir eslesen tum bildirimlerin id'sini dondurur."""
    return [n["id"] for n in api.get_notifications() if n.get("name") == name]


def fetch_status(url: str) -> str:
    """Yonlendirmeler takip edilir; son cevabin status kodunu veya no-response dondurur."""
    try:
        return str(requests.get(url, timeout=30, allow_redirects=True).status_code)
    except requests.RequestException:
        return NO_RESPONSE_TAG


def is_status_tag(name) -> bool:
    return name == NO_RESPONSE_TAG or (name or "").isdigit()


def status_color(name: str) -> str:
    return {"2": "#059669", "3": "#2563EB", "4": "#D97706", "5": "#DC2626"}.get(name[:1], "#4B5563")


def apply_status_tag(api, tags: dict, monitor: dict, url: str):
    """Monitorun status tag'ini guncel koda ceker; eskileri kaldirir."""
    name = fetch_status(url)
    if name not in tags:
        tags[name] = api.add_tag(name=name, color=status_color(name))["id"]
    tag_id = tags[name]

    present = False
    for t in monitor.get("tags") or []:
        if t.get("tag_id") == tag_id:
            present = True
        elif is_status_tag(t.get("name")):
            api.delete_monitor_tag(tag_id=t["tag_id"], monitor_id=monitor["id"], value=t.get("value", ""))
    if not present:
        api.add_monitor_tag(tag_id=tag_id, monitor_id=monitor["id"])


def sync(api, sites: dict, notification_ids: list, delete_missing: bool):
    tags = {t["name"]: t["id"] for t in api.get_tags()}
    existing = {}
    for m in api.get_monitors():
        url = (m.get("url") or "").rstrip("/")
        if url:
            existing[url] = m

    for url, name in sorted(sites.items(), key=lambda x: x[1]):
        if url in existing:
            monitor = existing[url]
            if monitor.get("type") == MonitorType.HTTP:
                edits = {}
                if monitor.get("maxretries") != MONITOR_RETRIES:
                    edits["maxretries"] = MONITOR_RETRIES
                if monitor.get("accepted_statuscodes") != ACCEPTED_STATUSCODES:
                    edits["accepted_statuscodes"] = ACCEPTED_STATUSCODES
                if not notification_ids and any((monitor.get("notificationIDList") or {}).values()):
                    edits["notificationIDList"] = {}
                if edits:
                    api.edit_monitor(monitor["id"], **edits)
            apply_status_tag(api, tags, monitor, url)
            continue

        added = api.add_monitor(
            type=MonitorType.HTTP,
            name=name,
            url=url,
            interval=MONITOR_INTERVAL,
            maxretries=MONITOR_RETRIES,
            retryInterval=MONITOR_INTERVAL,
            accepted_statuscodes=ACCEPTED_STATUSCODES,
            notificationIDList=notification_ids,
        )
        apply_status_tag(api, tags, {"id": added["monitorID"]}, url)

    if not delete_missing:
        return

    for url, m in existing.items():
        if url in sites:
            continue
        if m.get("type") != MonitorType.HTTP:
            continue
        api.delete_monitor(m["id"])


def main():
    load_dotenv()

    required = ["FORGE_API_TOKEN", "KUMA_URL", "KUMA_USERNAME", "KUMA_PASSWORD"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(1)

    allowed = load_allowlist()

    forge = Forge(os.getenv("FORGE_API_TOKEN"))
    sites, complete = collect_sites(forge, allowed)

    api = UptimeKumaApi(os.getenv("KUMA_URL"))
    try:
        with api.wait_for_event(Event.INFO):
            pass
        api.login(os.getenv("KUMA_USERNAME"), os.getenv("KUMA_PASSWORD"))
        notif_name = (os.getenv("KUMA_NOTIFICATION_NAME") or "").strip()
        notif = notification_ids(api, notif_name) if notif_name else []
        sync(api, sites, notif, delete_missing=complete)
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
