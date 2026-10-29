import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.ai_service import (
    AIQuotaExceededError,
    AIRateLimitError,
    AIService,
    WebSearch,
)
from app.config import Settings
from app.database import SongDatabase
from app.recommender import Recommender

logger = logging.getLogger(__name__)


def _format_song(song: dict, number: int) -> str:
    title = html.escape(str(song.get("title") or "Sin título"))
    artist = html.escape(str(song.get("artist") or "Artista desconocido"))
    album = song.get("album")
    year = song.get("year")
    details = []
    if album:
        details.append(str(album))
    if year:
        details.append(str(year))
    suffix = f" ({html.escape(' · '.join(details))})" if details else ""
    return f"{number}. <b>{title}</b> — {artist}{suffix}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "¡Hola! Pídeme música en lenguaje natural, por ejemplo: "
        "\"hits movidos de los 80\" o \"canciones de Queen\"."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Escribe lo que quieres escuchar. También puedes usar /buscar seguido "
        "de una petición, por ejemplo /buscar rock de los 90."
    )


async def recommend_text(
    request_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    recommender: Recommender = context.application.bot_data["recommender"]
    await update.message.chat.send_action("typing")
    try:
        songs, web_failed = await asyncio.to_thread(
            recommender.recommend, request_text
        )
    except AIQuotaExceededError:
        logger.warning("La cuenta del proveedor de IA no tiene créditos disponibles.")
        await update.message.reply_text(
            "La cuenta de OpenAI se quedó sin créditos o alcanzó su cuota. "
            "Revisa la facturación y el uso de la API en "
            "https://platform.openai.com/settings/organization/billing/ "
            "y vuelve a intentarlo."
        )
        return
    except AIRateLimitError:
        logger.warning("El proveedor de IA está limitando temporalmente las solicitudes.")
        await update.message.reply_text(
            "OpenAI está recibiendo demasiadas solicitudes por ahora. "
            "Espera un poco e inténtalo de nuevo."
        )
        return
    except Exception:
        logger.exception("Falló la recomendación")
        await update.message.reply_text(
            "No pude completar la búsqueda. Comprueba la conexión con MongoDB "
            "y la configuración del proveedor de IA."
        )
        return

    if not songs:
        await update.message.reply_text(
            "No encontré canciones que coincidan en tu biblioteca local."
        )
        return

    message = "\n".join(_format_song(song, index) for index, song in enumerate(songs, 1))
    if web_failed:
        message += "\n\n<i>La búsqueda web no estuvo disponible; recomendé usando "
        message += "solo tu biblioteca local.</i>"
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await recommend_text(update.message.text, update, context)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Uso: /buscar canciones movidas de los 80")
        return
    await recommend_text(query, update, context)


def create_application(settings: Settings) -> Application:
    database = SongDatabase(settings)
    recommender = Recommender(database, AIService(settings), WebSearch(settings))
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["recommender"] = recommender
    application.bot_data["database"] = database
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("buscar", search_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application
