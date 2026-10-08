import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SEARCHES = {
    "fountains": "cat water fountain",
    "feeders": "automatic cat feeder",
}
ENDPOINT = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def collect(session, folder, name, query):
    destination = folder / name
    destination.mkdir()
    info = {
        "query": query,
        "status": "running",
        "pages": 0,
        "records": 0,
        "unique_item_ids": 0,
    }
    seen_ids = set()
    params = {
        "q": query,
        "category_ids": "177784",
        "limit": 200,
        "offset": 0,
    }

    try:
        while True:
            response = session.get(
                ENDPOINT, params=params, timeout=30
            )
            response.raise_for_status()
            result = response.json()
            page = info["pages"] + 1

            save_json(
                destination / f"page_{page:03d}.json",
                {"collected_at": now(), "response": result},
            )

            items = result.get("itemSummaries", [])
            seen_ids.update(item["itemId"] for item in items)
            info.update({
                "pages": page,
                "records": info["records"] + len(items),
                "unique_item_ids": len(seen_ids),
                "last_reported_total": result.get("total"),
            })
            print(
                f"{name}: page {page}, "
                f"records {info['records']}",
                flush=True,
            )

            if not result.get("next"):
                info["status"] = "completed"
                break

            if page >= 50:
                info["status"] = "partial_page_limit"
                break

            params["offset"] += 200
            time.sleep(0.5)

    except (requests.RequestException, ValueError, KeyError) as error:
        info["status"] = "failed"
        info["error"] = str(error)
        print(f"{name}: {error}", flush=True)

    return info


def main():
    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Missing eBay credentials")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    folder = Path("data") / f"snapshot_{stamp}"
    folder.mkdir(parents=True)

    manifest = {
        "started_at": now(),
        "marketplace": "EBAY_US",
        "category_id": "177784",
        "searches": {},
        "status": "running",
    }
    save_json(folder / "manifest.json", manifest)

    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))

    try:
        auth = session.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=30,
        )
        auth.raise_for_status()
        session.headers.update({
            "Authorization": f"Bearer {auth.json()['access_token']}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        })

        for name, query in SEARCHES.items():
            manifest["searches"][name] = collect(
                session, folder, name, query
            )
            save_json(folder / "manifest.json", manifest)

        manifest["status"] = (
            "completed"
            if all(
                entry["status"] == "completed"
                for entry in manifest["searches"].values()
            )
            else "incomplete"
        )

    except Exception as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        print(f"Collection failed: {error}", flush=True)

    finally:
        manifest["finished_at"] = now()
        save_json(folder / "manifest.json", manifest)
        session.close()

    print(f"Snapshot: {folder}", flush=True)
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
