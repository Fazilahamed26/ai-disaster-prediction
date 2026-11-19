import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from pymongo import DESCENDING, MongoClient


DATABASE_FILES = (
    "database.db",
    "disaster.db",
    "disaster_predictor.db",
    "users.db",
    "data/predictions.db",
)


def migrate_users_with_merge(rows, users_collection):
    existing_users = list(
        users_collection.find({}, {"_id": 1, "email": 1})
    )
    existing_ids = {user["_id"] for user in existing_users}
    existing_emails = {
        user["email"]: user["_id"]
        for user in existing_users
        if isinstance(user.get("email"), str)
    }
    numeric_ids = [
        user_id
        for user_id in existing_ids
        if isinstance(user_id, (int, float)) and not isinstance(user_id, bool)
    ]
    numeric_ids.extend(
        row["id"]
        for row in rows
        if isinstance(row.get("id"), (int, float))
        and not isinstance(row.get("id"), bool)
    )
    next_id = max(numeric_ids, default=0) + 1

    user_id_map = {}
    inserted = 0
    matched = 0
    for row in rows:
        document = dict(row)
        source_id = document.get("id")
        if source_id is None:
            raise ValueError("users.db:users contains a row without an id")

        email = document.get("email")
        if isinstance(email, str) and email in existing_emails:
            user_id_map[source_id] = existing_emails[email]
            matched += 1
            continue

        target_id = source_id
        if target_id in existing_ids:
            while next_id in existing_ids:
                next_id += 1
            target_id = next_id
            next_id += 1

        document["_id"] = target_id
        users_collection.insert_one(document)
        existing_ids.add(target_id)
        if isinstance(email, str):
            existing_emails[email] = target_id
        user_id_map[source_id] = target_id
        inserted += 1

    return user_id_map, inserted, matched


def sync_app_counters(database):
    counters_collection = database["counters"]
    for collection_name in ("users", "reports", "alerts"):
        latest = database[collection_name].find_one(
            {"_id": {"$type": "number"}}, sort=[("_id", DESCENDING)]
        )
        counters_collection.update_one(
            {"_id": collection_name},
            {"$max": {"sequence": latest["_id"] if latest else 0}},
            upsert=True,
        )


def migrate_database(sqlite_path, mongo_client):
    database_name = Path(sqlite_path).stem
    sqlite_connection = sqlite3.connect(sqlite_path)
    sqlite_connection.row_factory = sqlite3.Row
    try:
        table_names = [
            row[0]
            for row in sqlite_connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        migrated_counts = {}
        user_id_map = {}

        if database_name == "users" and "users" in table_names:
            user_rows = [
                dict(row)
                for row in sqlite_connection.execute('SELECT * FROM "users"')
            ]
            (
                user_id_map,
                inserted,
                matched,
            ) = migrate_users_with_merge(
                user_rows, mongo_client[database_name]["users"]
            )
            migrated_counts["users"] = (
                f"{inserted} inserted, {matched} matched by email"
            )

        for table_name in table_names:
            if database_name == "users" and table_name == "users":
                continue
            collection = mongo_client[database_name][table_name]
            rows = sqlite_connection.execute(
                'SELECT * FROM "' + table_name.replace('"', '""') + '"'
            )
            count = 0
            for row in rows:
                document = dict(row)
                if document.get("id") is None:
                    raise ValueError(
                        f"{sqlite_path}:{table_name} contains a row without an id"
                    )
                document["_id"] = document["id"]
                if (
                    database_name == "users"
                    and table_name == "reports"
                    and document.get("user_id") in user_id_map
                ):
                    document["user_id"] = user_id_map[document["user_id"]]
                collection.replace_one(
                    {"_id": document["_id"]}, document, upsert=True
                )
                count += 1
            if table_name != "users":
                migrated_counts[table_name] = count

        if database_name == "users":
            sync_app_counters(mongo_client[database_name])
        return database_name, migrated_counts
    finally:
        sqlite_connection.close()


def main():
    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI")
    if not mongo_uri:
        raise RuntimeError("Set MONGODB_URI in your local .env before migrating")

    project_root = Path(__file__).resolve().parent
    missing_files = [
        str(project_root / relative_path)
        for relative_path in DATABASE_FILES
        if not (project_root / relative_path).is_file()
    ]
    if missing_files:
        raise FileNotFoundError(
            "Required SQLite database file(s) not found: " + ", ".join(missing_files)
        )

    with MongoClient(mongo_uri, serverSelectionTimeoutMS=5000) as mongo_client:
        mongo_client.admin.command("ping")
        for relative_path in DATABASE_FILES:
            database_name, counts = migrate_database(
                project_root / relative_path, mongo_client
            )
            table_summary = ", ".join(
                f"{table}: {count}" for table, count in counts.items()
            )
            print(f"{relative_path} -> {database_name}: {table_summary}")


if __name__ == "__main__":
    main()
