import csv
import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


ARCHIVE_DIR = Path("snapshots")
OUTPUT_DIR = Path("tables")

HISTORY_FIELDS = [
    "snapshot_id", "snapshot_date", "collected_at",
    "search_group",
    "item_id", "legacy_item_id", "title",
    "price", "currency",
    "seller_username", "seller_feedback_percentage",
    "seller_feedback_score",
    "condition", "condition_id", "item_country",
    "buying_options", "item_creation_date", "item_url",
]

SEARCH_FIELDS = [
    "snapshot_id", "snapshot_date", "search_name", "item_id",
]

LOG_FIELDS = [
    "snapshot_id", "snapshot_date", "search_name", "query",
    "snapshot_status", "search_status",
    "pages_read", "raw_records", "unique_item_ids",
    "duplicate_records", "last_reported_total",
]


def read_json(archive, name):
    return json.loads(archive.read(name).decode("utf-8"))


def make_history_row(snapshot_id, snapshot_date, collected_at, item):
    price = item.get("price") or {}
    seller = item.get("seller") or {}
    location = item.get("itemLocation") or {}

    return {
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot_date,
        "collected_at": collected_at,
        "item_id": item["itemId"],
        "legacy_item_id": item.get("legacyItemId"),
        "title": item.get("title"),
        "price": price.get("value"),
        "currency": price.get("currency"),
        "seller_username": seller.get("username"),
        "seller_feedback_percentage": seller.get("feedbackPercentage"),
        "seller_feedback_score": seller.get("feedbackScore"),
        "condition": item.get("condition"),
        "condition_id": item.get("conditionId"),
        "item_country": location.get("country"),
        "buying_options": "|".join(item.get("buyingOptions") or []),
        "item_creation_date": item.get("itemCreationDate"),
        "item_url": item.get("itemWebUrl"),
    }


def write_csv(filename, fields, rows):
    path = OUTPUT_DIR / filename
    temporary = path.with_suffix(".csv.tmp")

    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    temporary.replace(path)
    print(f"{path}: {len(rows)} rows", flush=True)


def main():
    archives = sorted(ARCHIVE_DIR.glob("snapshot_*.zip"))
    if not archives:
        raise RuntimeError("No snapshot archives found in snapshots/")

    history = {}
    search_results = {}
    collection_log = []
    seen_snapshots = set()

    for archive_path in archives:
        with ZipFile(archive_path) as archive:
            names = archive.namelist()
            manifests = [
                name for name in names
                if PurePosixPath(name).name == "manifest.json"
            ]
            if len(manifests) != 1:
                raise ValueError(f"Expected one manifest: {archive_path}")

            manifest_name = manifests[0]
            manifest = read_json(archive, manifest_name)

            # Час початку визначає збір незалежно від назви ZIP.
            snapshot_id = manifest["started_at"]
            snapshot_date = snapshot_id[:10]

            if snapshot_id in seen_snapshots:
                raise ValueError(f"Duplicate snapshot: {archive_path}")
            seen_snapshots.add(snapshot_id)

            # Підтримуємо JSON як у корені ZIP, так і в підпапці.
            prefix = manifest_name.removesuffix("manifest.json")

            for search_name, info in sorted(manifest["searches"].items()):
                page_prefix = f"{prefix}{search_name}/"
                pages = sorted(
                    name for name in names
                    if name.startswith(page_prefix)
                    and PurePosixPath(name).name.startswith("page_")
                    and name.endswith(".json")
                )

                raw_records = 0
                item_ids = set()
                last_total = None

                for page_name in pages:
                    page = read_json(archive, page_name)
                    response = page["response"]
                    items = response.get("itemSummaries", [])
                    collected_at = page["collected_at"]
                    last_total = response.get("total")
                    raw_records += len(items)

                    for item in items:
                        item_id = item["itemId"]
                        item_ids.add(item_id)

                        history_key = (snapshot_id, item_id)
                        row = make_history_row(
                            snapshot_id, snapshot_date, collected_at, item
                        )

                        # Для повторів залишаємо останнє спостереження.
                        previous = history.get(history_key)
                        if (
                            previous is None
                            or collected_at > previous["collected_at"]
                        ):
                            history[history_key] = row

                        search_key = (snapshot_id, search_name, item_id)
                        search_results[search_key] = {
                            "snapshot_id": snapshot_id,
                            "snapshot_date": snapshot_date,
                            "search_name": search_name,
                            "item_id": item_id,
                        }

                # Перевіряємо, що архів відповідає журналу збору.
                for field, actual in [
                    ("pages", len(pages)),
                    ("records", raw_records),
                    ("unique_item_ids", len(item_ids)),
                ]:
                    if field in info and info[field] != actual:
                        raise ValueError(
                            f"{archive_path.name}, {search_name}: "
                            f"{field} mismatch"
                        )

                collection_log.append({
                    "snapshot_id": snapshot_id,
                    "snapshot_date": snapshot_date,
                    "search_name": search_name,
                    "query": info.get("query"),
                    "snapshot_status": manifest.get("status"),
                    "search_status": info.get("status"),
                    "pages_read": len(pages),
                    "raw_records": raw_records,
                    "unique_item_ids": len(item_ids),
                    "duplicate_records": raw_records - len(item_ids),
                    "last_reported_total": last_total,
                })

        print(f"Read: {archive_path.name}", flush=True)

    # Збираємо групи пошуку для кожного оголошення в кожному зборі.
    groups = {}

    for snapshot_id, search_name, item_id in search_results:
        key = (snapshot_id, item_id)
        groups.setdefault(key, set()).add(search_name)

    for key, row in history.items():
        names = groups[key]
        row["search_group"] = (
            "both" if len(names) > 1 else next(iter(names))
        )

    # Записуємо CSV лише після успішного читання всіх архівів.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    write_csv(
        "ebay_history.csv",
        HISTORY_FIELDS,
        [history[key] for key in sorted(history)],
    )
    write_csv(
        "ebay_search_results.csv",
        SEARCH_FIELDS,
        [search_results[key] for key in sorted(search_results)],
    )
    write_csv(
        "ebay_collection_log.csv",
        LOG_FIELDS,
        collection_log,
    )


if __name__ == "__main__":
    main()
