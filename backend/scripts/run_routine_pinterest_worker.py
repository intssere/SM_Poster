import asyncio

from app.db.session import SessionLocal
from app.services.routine_pinterest_worker import run_once


async def main():
    db = SessionLocal()
    try:
        result = await run_once(db)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
