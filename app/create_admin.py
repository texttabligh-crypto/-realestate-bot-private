import os
import getpass
import bcrypt
import psycopg

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")


def main():
    print("\n=== Create Admin User ===\n")

    username = input("Username: ").strip()
    display_name = input("Display name: ").strip()

    password = getpass.getpass("Password: ")
    password_confirm = getpass.getpass("Confirm password: ")

    if not username:
        print("Username cannot be empty.")
        return

    if password != password_confirm:
        print("Passwords do not match.")
        return

    if len(password) < 6:
        print("Password must be at least 6 characters.")
        return

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    conn = psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users
                    (username, password_hash, display_name, role, is_active)
                VALUES
                    (%s, %s, %s, 'ADMIN', TRUE)
                ON CONFLICT (username)
                DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    display_name = EXCLUDED.display_name,
                    role = 'ADMIN',
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                (username, password_hash, display_name),
            )

        conn.commit()
        print("\nAdmin user created successfully.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
