import logging

from app.bot import create_application
from app.config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    settings.validate()
    application = create_application(settings)
    try:
        application.run_polling()
    finally:
        database = application.bot_data.get("database")
        if database:
            database.close()


if __name__ == "__main__":
    main()
