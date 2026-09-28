import os
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from uptime_kuma_api import UptimeKumaApi, MonitorType

FORGE_API = "https://forge.laravel.com/api"
REQUEST_DELAY = 1
MONITOR_INTERVAL = 60
MONITOR_RETRIES = 3


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


def collect_sites(forge: Forge) -> tuple[dict, bool]:
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
                if not url:
                    continue

                found[url] = sa["name"]

    return found, complete


def reachable(url: str) -> bool:
    """Kuma ile ayni kural: yonlendirme sonrasi cevap 200-299 ise site eklenir."""
    session = requests.Session()
    session.max_redirects = 10
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
    except requests.RequestException:
        return False
    return 200 <= response.status_code < 300


def notification_ids(api, name: str) -> list:
    """Kuma'da adi birebir eslesen tum bildirimlerin id'sini dondurur."""
    return [n["id"] for n in api.get_notifications() if n.get("name") == name]


def sync(api, sites: dict, notification_ids: list, delete_missing: bool):
    existing = {}
    for m in api.get_monitors():
        url = (m.get("url") or "").rstrip("/")
        if url:
            existing[url] = m

    for url, name in sorted(sites.items(), key=lambda x: x[1]):
        if url in existing:
            monitor = existing[url]
            if monitor.get("type") == MonitorType.HTTP and monitor.get("maxretries") != MONITOR_RETRIES:
                api.edit_monitor(monitor["id"], maxretries=MONITOR_RETRIES)
            continue

        if not reachable(url):
            continue

        api.add_monitor(
            type=MonitorType.HTTP,
            name=name,
            url=url,
            interval=MONITOR_INTERVAL,
            maxretries=MONITOR_RETRIES,
            retryInterval=MONITOR_INTERVAL,
            notificationIDList=notification_ids,
        )

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

    required = ["FORGE_API_TOKEN", "KUMA_URL", "KUMA_USERNAME", "KUMA_PASSWORD", "KUMA_NOTIFICATION_NAME"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(1)

    forge = Forge(os.getenv("FORGE_API_TOKEN"))
    sites, complete = collect_sites(forge)

    api = UptimeKumaApi(os.getenv("KUMA_URL"))
    try:
        api.login(os.getenv("KUMA_USERNAME"), os.getenv("KUMA_PASSWORD"))
        notif = notification_ids(api, os.getenv("KUMA_NOTIFICATION_NAME"))
        sync(api, sites, notif, delete_missing=complete)
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
