"""Felhasználó létrehozása / jelszó módosítása.

Használat (Railway konzolon vagy lokálisan):

    python -m scripts.create_user raktar "Kovács Béla" titkos-jelszo

Ha a felhasználó már létezik, a jelszavát írja felül.
"""

import sys

from sqlalchemy import select

from src.auth import hash_password
from src.db.models import AppUser
from src.db.session import SessionLocal


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    username, display_name, password = sys.argv[1], sys.argv[2], sys.argv[3]

    with SessionLocal() as db:
        user = db.scalar(select(AppUser).where(AppUser.username == username))
        if user is None:
            user = AppUser(
                username=username,
                display_name=display_name,
                password_hash=hash_password(password),
            )
            db.add(user)
            action = "létrehozva"
        else:
            user.display_name = display_name
            user.password_hash = hash_password(password)
            user.active = True
            action = "frissítve"
        db.commit()

    print(f"Felhasználó {action}: {username} ({display_name})")


if __name__ == "__main__":
    main()
