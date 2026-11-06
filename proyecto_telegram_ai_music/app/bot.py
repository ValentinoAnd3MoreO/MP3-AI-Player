import asyncio
import html
import logging
import re
import uuid

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from pymongo.errors import PyMongoError

from app.ai_service import (
    AIQuotaExceededError,
    AIRateLimitError,
    AIService,
    WebSearch,
)
from app.config import Settings
from app.database import SongDatabase
from app.device_client import DeviceClient, PlayerNotConfiguredError
from app.recommender import Recommender
from app.sd_path import parse_sd_path

logger = logging.getLogger(__name__)
_RECOMMENDATION_REQUEST = re.compile(
    r"\b(canciones?|temas?|hits?|recomiend\w*|suger\w*|"
    r"dame|pon(?:me|eme)?|busco|buscar|escuch\w*|reproduc\w*)\b",
    re.IGNORECASE,
)
_MUSIC_WANT_REQUEST = re.compile(
    r"\b(?:quiero|quisiera)\s+(?!(?:que|saber|preguntar|hablar|conocer|"
    r"entender|aprender|consultar)\b)\w+",
    re.IGNORECASE,
)
_PLAYBACK_REQUEST = re.compile(
    r"\b(reproductor|reprodu\w*|"
    r"(?:pon(?:me|eme)?|ponerme)\s+(?:esa\s+)?(?:canción|tema|música)|"
    r"(?:play|toca)\s+(?:esa\s+)?(?:canción|tema|song))\b",
    re.IGNORECASE,
)
_REPEAT_PLAYBACK_REQUEST = re.compile(
    r"\b(?:otra\s+vez|de\s+nuevo|nuevamente|"
    r"repetir(?:la|lo)?|la\s+misma\s+canci[oó]n)\b",
    re.IGNORECASE,
)
_LIBRARY_AVAILABILITY_QUESTION = re.compile(
    r"\b(?:la|lo|esa|ese|esta|este)\s+(?:ten[eé]s|tienes|tiene|hay|está|esta)\b"
    r".{0,80}\b(?:escuchar\w*|reproduc\w*|o[ií]r|poner\w*)\b",
    re.IGNORECASE,
)
_CHAT_WEB_LOOKUP_REQUEST = re.compile(
    r"\b(hoy|actual(?:mente)?|últim[oa]s?|recient(?:e|es)|"
    r"noticias?|noticia|gira|tour|conciertos?|fecha de|"
    r"está tocando|va a tocar|busca en internet|averigua|investiga)\b",
    re.IGNORECASE,
)


def _needs_chat_web_lookup(message: str) -> bool:
    return bool(_CHAT_WEB_LOOKUP_REQUEST.search(message))


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
        "¡Hola! Puedo conversar sobre música y también buscar canciones en tu "
        "biblioteca. Por ejemplo: «¿Qué hace especial a Prince?» o "
        "«poneme hits movidos de los 80». Si la canción está mapeada en la "
        "microSD y el ESP32 está conectado, también verás un botón para reproducirla. "
        "Usa /reset para borrar el contexto."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Escribe lo que quieres escuchar. También puedes usar /buscar seguido "
        "de una petición, por ejemplo /buscar rock de los 90. También puedes "
        "preguntarme sobre artistas, canciones, géneros e historia de la música. "
        "Las recomendaciones con una ruta SD válida tendrán un botón de reproducción. "
        "Usa /pausar y /reanudar para controlar el audio, y /reset para iniciar "
        "una conversación nueva."
    )


async def pause_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _send_player_control(update, context, "pause")


async def resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _send_player_control(update, context, "resume")


async def reset_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_data = context.application.user_data[update.effective_user.id]
    user_data.pop("music_history", None)
    user_data.pop("suggested_play_songs", None)
    await update.message.reply_text("Listo, empecemos una conversación nueva.")


async def recommend_text(
    request_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    recommender: Recommender = context.application.bot_data["recommender"]
    history = context.application.user_data[update.effective_user.id].get(
        "music_history", []
    )
    await update.message.chat.send_action("typing")
    try:
        songs, web_failed = await asyncio.to_thread(
            recommender.recommend, request_text, history
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
        _remember_exchange(context, update, request_text, "No se encontraron canciones.")
        return

    message = "\n".join(
        _format_song(song, index) for index, song in enumerate(songs, 1)
    )
    keyboard = _play_keyboard(songs, context, update.effective_user.id)
    has_sd_paths = any(parse_sd_path(song.get("sd_path")) for song in songs)
    if keyboard:
        message += "\n\nToca una canción para reproducirla en el ESP32:"
    elif not has_sd_paths:
        message += "\n\nEstas canciones aún no tienen una ruta válida en la microSD."
    else:
        message += "\n\nEl reproductor todavía no está habilitado."
    if web_failed:
        message += "\n\n<i>La búsqueda web no estuvo disponible; recomendé usando "
        message += "solo tu biblioteca local.</i>"
    await update.message.reply_text(
        message, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    plain_list = "\n".join(
        f"{index}. {song.get('title', 'Sin título')} — "
        f"{song.get('artist', 'Artista desconocido')}"
        for index, song in enumerate(songs, 1)
    )
    _remember_exchange(context, update, request_text, plain_list)


def _play_keyboard(
    songs: list[dict], context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> InlineKeyboardMarkup | None:
    rows = []
    user_data = context.application.user_data[user_id]
    pending = user_data.setdefault("pending_play", {})
    pending.clear()
    if not context.application.bot_data["player_settings"].player_enabled:
        return None
    for song in songs:
        if not parse_sd_path(song.get("sd_path")):
            continue
        token = uuid.uuid4().hex[:16]
        pending[token] = str(song["_id"])
        label = f"▶ {song.get('title', 'Reproducir')} — {song.get('artist', '')}"
        rows.append(
            [InlineKeyboardButton(label[:60], callback_data=f"play:{token}")]
        )
    return InlineKeyboardMarkup(rows) if rows else None


def _play_suggestion_keyboard(
    song: dict, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> InlineKeyboardMarkup | None:
    if not context.application.bot_data["player_settings"].player_enabled:
        return None
    if not parse_sd_path(song.get("sd_path")):
        return None

    token = uuid.uuid4().hex[:16]
    user_data = context.application.user_data[user_id]
    suggested = user_data.setdefault("suggested_play_songs", set())
    song_id = str(song["_id"])
    if song_id in suggested:
        return None
    suggested.add(song_id)
    pending = user_data.setdefault("pending_play", {})
    pending[token] = str(song["_id"])
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Sí, reproducir", callback_data=f"play:{token}"),
            InlineKeyboardButton("No, gracias", callback_data=f"dismiss:{token}"),
        ]]
    )


def _player_control_keyboard(action: str = "pause") -> InlineKeyboardMarkup:
    if action == "pause":
        button = InlineKeyboardButton("⏸ Pausar", callback_data="control:pause")
    else:
        button = InlineKeyboardButton("▶ Reanudar", callback_data="control:resume")
    return InlineKeyboardMarkup([[button]])


async def play_song_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    token = query.data.partition(":")[2]
    pending = context.user_data.get("pending_play", {})
    song_id = pending.pop(token, None)
    if not song_id:
        await query.answer(
            "Este botón venció. Pide la recomendación otra vez.", show_alert=True
        )
        return

    await query.answer("Enviando la orden al reproductor…")
    settings: Settings = context.application.bot_data["player_settings"]
    if not settings.player_enabled:
        await query.message.reply_text(
            "El reproductor ESP32 no está habilitado. Configura PLAYER_ENABLED."
        )
        return

    database: SongDatabase = context.application.bot_data["database"]
    song = await asyncio.to_thread(database.find_song_by_id, song_id)
    if not song or not parse_sd_path(song.get("sd_path")):
        await query.message.reply_text(
            "La canción ya no tiene una ruta SD válida en la biblioteca."
        )
        return

    device: DeviceClient = context.application.bot_data["device"]
    try:
        await asyncio.to_thread(device.play, song["sd_path"])
    except PlayerNotConfiguredError:
        await query.message.reply_text("Falta configurar la conexión con el ESP32.")
        return
    except requests.exceptions.RequestException:
        logger.exception("No se pudo enviar la orden al ESP32")
        await query.message.reply_text(
            "No pude contactar el ESP32. Revisa que esté encendido en la misma red."
        )
        return
    except Exception:
        logger.exception("El ESP32 rechazó la orden de reproducción")
        await query.message.reply_text(
            "El reproductor rechazó la orden. Comprueba la ruta SD y el DFPlayer."
        )
        return

    await query.message.reply_text(
        f"Orden enviada al ESP32: {song.get('title', 'la canción')}.",
        reply_markup=_player_control_keyboard(),
    )
    context.user_data["last_played_song_id"] = str(song["_id"])


async def repeat_last_song(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    song_id = context.user_data.get("last_played_song_id")
    if not song_id:
        await update.message.reply_text(
            "No tengo registrada la última canción reproducida (por ejemplo, "
            "si el bot se reinició). Dime el título o artista y la busco."
        )
        return

    database: SongDatabase = context.application.bot_data["database"]
    try:
        song = await asyncio.to_thread(database.find_song_by_id, song_id)
    except PyMongoError:
        logger.exception("No se pudo recuperar la última canción reproducida")
        await update.message.reply_text(
            "No pude consultar la biblioteca para recuperar la última canción."
        )
        return
    if not song or not parse_sd_path(song.get("sd_path")):
        await update.message.reply_text(
            "La última canción ya no tiene una ruta válida en la microSD. "
            "Dime el título y la vuelvo a buscar."
        )
        return

    keyboard = _play_keyboard([song], context, update.effective_user.id)
    await update.message.reply_text(
        f"{_format_song(song, 1)}\n\n¿Quieres escucharla otra vez?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def dismiss_play_suggestion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    token = query.data.partition(":")[2]
    context.user_data.get("pending_play", {}).pop(token, None)
    await query.answer("Entendido.")
    await query.edit_message_reply_markup(reply_markup=None)


async def _send_player_control(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> None:
    if update.callback_query:
        await update.callback_query.answer("Enviando la orden…")

    settings: Settings = context.application.bot_data["player_settings"]
    if not settings.player_enabled:
        await update.effective_message.reply_text(
            "El reproductor ESP32 no está habilitado."
        )
        return

    device: DeviceClient = context.application.bot_data["device"]
    try:
        await asyncio.to_thread(getattr(device, action))
    except PlayerNotConfiguredError:
        await update.effective_message.reply_text(
            "Falta configurar la conexión con el ESP32."
        )
        return
    except requests.exceptions.RequestException:
        logger.exception("No se pudo enviar el control %s al ESP32", action)
        await update.effective_message.reply_text(
            "No pude contactar el ESP32. Revisa que esté encendido en la misma red."
        )
        return
    except Exception:
        logger.exception("El ESP32 rechazó el control %s", action)
        await update.effective_message.reply_text(
            "El reproductor rechazó la orden. Comprueba el ESP32 y el DFPlayer."
        )
        return

    if update.callback_query:
        query = update.callback_query
        await query.edit_message_reply_markup(
            reply_markup=_player_control_keyboard(
                "resume" if action == "pause" else "pause"
            )
        )
    else:
        label = (
            "Pausa enviada al ESP32."
            if action == "pause"
            else "Reanudación enviada al ESP32."
        )
        await update.effective_message.reply_text(label)


async def player_control_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    action = update.callback_query.data.partition(":")[2]
    await _send_player_control(update, context, action)


def _remember_exchange(
    context: ContextTypes.DEFAULT_TYPE, update: Update, user_text: str, answer: str
) -> None:
    user_id = update.effective_user.id
    history = context.application.user_data[user_id].setdefault("music_history", [])
    history.extend(
        [
            {"role": "user", "content": user_text[:2000]},
            {"role": "assistant", "content": answer[:2000]},
        ]
    )
    del history[:-12]


async def chat_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    ai: AIService = context.application.bot_data["ai"]
    web: WebSearch = context.application.bot_data["web"]
    history = context.application.user_data[update.effective_user.id].get(
        "music_history", []
    )
    await update.message.chat.send_action("typing")

    def answer() -> tuple[str, dict[str, str] | None]:
        web_context = ""
        if _needs_chat_web_lookup(message):
            try:
                web_context = web.search(message)
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.warning("Búsqueda web no disponible durante el chat: %s", exc)
        return ai.chat_music(message, history, web_context)

    try:
        response, song_reference = await asyncio.to_thread(answer)
    except AIQuotaExceededError:
        await update.message.reply_text(
            "La cuenta de OpenAI se quedó sin créditos o alcanzó su cuota. "
            "Revisa https://platform.openai.com/settings/organization/billing/ "
            "y vuelve a intentarlo."
        )
        return
    except AIRateLimitError:
        await update.message.reply_text(
            "OpenAI está recibiendo demasiadas solicitudes por ahora. "
            "Espera un poco e inténtalo de nuevo."
        )
        return
    except Exception:
        logger.exception("Falló la conversación musical")
        await update.message.reply_text(
            "No pude responder ahora. Comprueba la conexión y la configuración "
            "del proveedor de IA, e inténtalo otra vez."
        )
        return

    await update.message.reply_text(response)
    if context.application.bot_data["player_settings"].player_enabled:
        database: SongDatabase = context.application.bot_data["database"]
        user_messages = [
            item["content"]
            for item in history
            if item.get("role") == "user" and isinstance(item.get("content"), str)
        ][-5:]
        user_messages.append(message)
        try:
            song = None
            if song_reference:
                recent_user_text = " ".join(user_messages).casefold()
                if (
                    song_reference["title"].casefold() in recent_user_text
                    and song_reference["artist"].casefold() in recent_user_text
                ):
                    song = await asyncio.to_thread(
                        database.find_playable_song_by_title_artist,
                        song_reference["title"],
                        song_reference["artist"],
                    )
            if not song:
                song = await asyncio.to_thread(
                    database.find_playable_song_mentioned_in, user_messages
                )
        except PyMongoError:
            logger.exception("No se pudo comprobar la canción en la biblioteca")
            await update.message.reply_text(
                "No pude consultar ahora si esa canción está disponible para reproducir."
            )
        else:
            if song:
                keyboard = _play_suggestion_keyboard(
                    song, context, update.effective_user.id
                )
                if keyboard:
                    await update.message.reply_text(
                        f"Veo que «{song.get('title')}» de "
                        f"{song.get('artist')} está lista en tu microSD. "
                        "¿Quieres que la reproduzca?",
                        reply_markup=keyboard,
                    )
    _remember_exchange(context, update, message, response)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message.text
    if _REPEAT_PLAYBACK_REQUEST.search(message):
        await repeat_last_song(update, context)
    elif _LIBRARY_AVAILABILITY_QUESTION.search(message):
        await chat_message(update, context, message)
    elif _PLAYBACK_REQUEST.search(message):
        await recommend_text(message, update, context)
    elif (
        _RECOMMENDATION_REQUEST.search(message)
        or _MUSIC_WANT_REQUEST.search(message)
    ):
        await recommend_text(message, update, context)
    else:
        await chat_message(update, context, message)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Uso: /buscar canciones movidas de los 80")
        return
    await recommend_text(query, update, context)


def create_application(settings: Settings) -> Application:
    database = SongDatabase(settings)
    ai = AIService(settings)
    web = WebSearch(settings)
    recommender = Recommender(database, ai, web)
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["recommender"] = recommender
    application.bot_data["ai"] = ai
    application.bot_data["web"] = web
    application.bot_data["device"] = DeviceClient(settings)
    application.bot_data["player_settings"] = settings
    application.bot_data["database"] = database
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_chat))
    application.add_handler(CommandHandler("buscar", search_command))
    application.add_handler(CommandHandler("pausar", pause_command))
    application.add_handler(CommandHandler("reanudar", resume_command))
    application.add_handler(
        CallbackQueryHandler(play_song_callback, pattern=r"^play:[0-9a-f]{16}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            dismiss_play_suggestion, pattern=r"^dismiss:[0-9a-f]{16}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(player_control_callback, pattern=r"^control:(pause|resume)$")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application
