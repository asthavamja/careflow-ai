import os
import sys

import psycopg
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    required_settings = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    settings = {name: os.getenv(name) for name in required_settings}
    missing_settings = [name for name, value in settings.items() if not value]
    if missing_settings:
        print(
            "Missing database settings: " + ", ".join(missing_settings),
            file=sys.stderr,
        )
        return 1

    try:
        port = int(settings["DB_PORT"])
    except ValueError:
        print("DB_PORT must be an integer.", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(
            host=settings["DB_HOST"],
            port=port,
            dbname=settings["DB_NAME"],
            user=settings["DB_USER"],
            password=settings["DB_PASSWORD"],
            connect_timeout=5,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), current_user, "
                    "(SELECT count(*) FROM careflow.patients)"
                )
                result = cursor.fetchone()
    except psycopg.Error as error:
        print(f"Database connection failed: {error}", file=sys.stderr)
        return 1

    print(
        f"Connection successful; database: {result[0]}, user: {result[1]}, "
        f"patients in careflow.patients: {result[2]}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
