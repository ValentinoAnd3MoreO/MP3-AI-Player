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
_RECOMMENDATION_REQUEST = re.compile(
    r"\b(canciones?|temas?|hits?|recomiend\w*|suger\w*|"
    r"dame|pon(?:me|eme)?|busco|buscar|escuch\w*|reproduc\w*|"
    r"ten[eé]s|tienes|hay)\b",
    re.IGNORECASE,
)
_MUSIC_WANT_REQUEST = re.compile(
    r"\b(?:quiero|quisiera)\s+(?!(?:que|saber|preguntar|hablar|conocer|"
    r"entender|aprender|consultar)\b)\w+",
    re.IGNORECASE,
)
_PLAYBACK_REQUEST = re.compile(
    r"\b(reproductor|reprod\w*|"
    r"(?:quiero|quisiera|me gustaría)\s+(?:escuchar|oir|oír|poner)\b|"
    r"(?:pon(?:me|eme)?|poner(?:me)?)\s+(?:(?:esa|esta|la|el)\s+)?"
    r"(?:canción|tema|música)|"
    r"(?:play|toca)\s+(?:(?:esa|esta|la|el)\s+)?(?:canción|tema|song)|"
    r"me gustaría que me la reproduzcas)\b",
    re.IGNORECASE,
)
_MUSIC_PREFERENCE_REQUEST = re.compile(
    r"\b(me gusta(?:n)?(?: más)?|mi favorito es|soy fan de)\s+"
    r"(?P<artist>[^,.!?;:\n]{2,80})",
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
        "«poneme hits movidos de los 80». Si hay un MP3 local, toca el botón ▶ "
        "para mandarlo al ESP32. Usa /reset para borrar el contexto."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Escribe lo que quieres escuchar. También puedes usar /buscar seguido "
        "de una petición, por ejemplo /buscar rock de los 90. También puedes "
        "preguntarme sobre artistas, canciones, géneros e historia de la música. "
        "Las canciones con archivo local muestran un botón ▶ para reproducir "
        "en el ESP32. Usa /reset para iniciar una conversación nueva."
    )


async def reset_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    context.application.user_data[update.effective_user.id].pop(
        "music_history", None
    )
    await update.message.reply_text("Listo, empecemos una conversación nueva.")


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
        _remember_exchange(context, update, request_text, "No se encontraron canciones.")
        return

    message = "\n".join(_format_song(song, index) for index, song in enumerate(songs, 1))
    user_data = context.application.user_data[update.effective_user.id]
    user_data["last_recommendations"] = [str(song["_id"]) for song in songs]
    markup = _play_markup(songs, context, update.effective_user.id)
    if markup is not None:
        message += "\n\nToca una canción para enviarla al ESP32."
    elif not any(
        context.application.bot_data["music"].has_audio(song) for song in songs
    ):
        message += (
            "\n\nTodavía no hay MP3 locales para estas canciones. Copia tus archivos "
            "a la carpeta music y ejecuta `python -m scripts.index_music`."
        )
    elif not context.application.bot_data["device_bridge"].settings.device_enabled:
        message += (
            "\n\nHay MP3 locales, pero la reproducción ESP32 todavía no está "
            "habilitada en la configuración."
        )
    if web_failed:
        message += "\n\n<i>La búsqueda web no estuvo disponible; recomendé usando "
        message += "solo tu biblioteca local.</i>"
    await update.message.reply_text(
        message, parse_mode=ParseMode.HTML, reply_markup=markup
    )
    plain_list = "\n".join(
        f"{index}. {song.get('title', 'Sin título')} — "
        f"{song.get('artist', 'Artista desconocido')}"
        for index, song in enumerate(songs, 1)
    )
    _remember_exchange(context, update, request_text, plain_list)


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


def _play_markup(
    songs: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> InlineKeyboardMarkup | None:
    bridge = context.application.bot_data["device_bridge"]
    if not bridge.settings.device_enabled:
        return None

    user_data = context.application.user_data[user_id]
    play_tokens = user_data.setdefault("play_tokens", {})
    play_tokens.clear()
    rows = []
    for song in songs:
        if not bridge.has_audio(song):
            continue
        token = uuid.uuid4().hex[:20]
        play_tokens[token] = str(song["_id"])
        label = f"▶ {song.get('title') or 'Reproducir'}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"play:{token}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


async def play_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.partition(":")[2]
    song_id = context.application.user_data[query.from_user.id].get(
        "play_tokens", {}
    ).get(token)
    if not song_id:
        await query.message.reply_text(
            "Ese botón venció. Pídeme otra recomendación para generar uno nuevo."
        )
        return

    database: SongDatabase = context.application.bot_data["database"]
    bridge = context.application.bot_data["device_bridge"]
    song = database.find_song_by_id(song_id)
    if not song:
        await query.message.reply_text("No encuentro esa canción en MongoDB.")
        return
    try:
        await bridge.enqueue(song)
    except (RuntimeError, FileNotFoundError) as exc:
        await query.message.reply_text(str(exc))
        return
    await query.message.reply_text(
        f"Envié <b>{html.escape(str(song.get('title') or 'la canción'))}</b> "
        "a la cola del ESP32.",
        parse_mode=ParseMode.HTML,
    )


async def playback_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    database: SongDatabase = context.application.bot_data["database"]
    music = context.application.bot_data["music"]
    ai: AIService = context.application.bot_data["ai"]
    bridge = context.application.bot_data["device_bridge"]
    if not bridge.settings.device_enabled:
        await update.message.reply_text(
            "La conexión con el ESP32 todavía no está habilitada. Configura "
            "DEVICE_ENABLED, DEVICE_API_TOKEN y DEVICE_PUBLIC_URL en `.env`, "
            "y reinicia el bot."
        )
        return
    available = [
        song
        for song in database.find_candidates(
            {"path": {"$exists": True, "$ne": ""}}, limit=500
        )
        if music.has_audio(song)
    ]
    if not available:
        await update.message.reply_text(
            "Todavía no hay archivos MP3 indexados en la carpeta music. Copia allí "
            "tus canciones y ejecuta `python -m scripts.index_music`; luego puedo "
            "enviarlas al ESP32."
        )
        return

    history = context.application.user_data[update.effective_user.id].get(
        "music_history", []
    )
    try:
        song_id = await asyncio.to_thread(
            ai.choose_song_for_playback, message, history, available
        )
    except AIQuotaExceededError:
        await update.message.reply_text(
            "La cuenta de OpenAI se quedó sin créditos o alcanzó su cuota. "
            "Revisa la facturación de la API e inténtalo otra vez."
        )
        return
    except AIRateLimitError:
        await update.message.reply_text(
            "OpenAI está limitando temporalmente las solicitudes. Inténtalo en un momento."
        )
        return
    except Exception:
        logger.exception("No se pudo identificar la canción a reproducir")
        await update.message.reply_text(
            "No pude identificar qué canción quieres reproducir. Dime el título "
            "o el artista e inténtalo de nuevo."
        )
        return

    song = next((item for item in available if str(item["_id"]) == song_id), None)
    if not song:
        await update.message.reply_text(
            "No pude identificar con seguridad la canción. Dime el título y el artista."
        )
        return
    try:
        await bridge.enqueue(song)
    except (RuntimeError, FileNotFoundError) as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text(
        f"¡Dale! Envié <b>{html.escape(str(song.get('title') or 'la canción'))}</b> "
        "a la cola del ESP32.",
        parse_mode=ParseMode.HTML,
    )


async def chat_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    ai: AIService = context.application.bot_data["ai"]
    web: WebSearch = context.application.bot_data["web"]
    history = context.application.user_data[update.effective_user.id].get(
        "music_history", []
    )
    await update.message.chat.send_action("typing")

    def answer() -> str:
        web_context = ""
        if _needs_chat_web_lookup(message):
            try:
                web_context = web.search(message)
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.warning("Búsqueda web no disponible durante el chat: %s", exc)
        return ai.chat_music(message, history, web_context)

    try:
        response = await asyncio.to_thread(answer)
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
    _remember_exchange(context, update, message, response)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message.text
    if _PLAYBACK_REQUEST.search(message):
        await playback_request(update, context, message)
    elif _MUSIC_PREFERENCE_REQUEST.search(message):
        artist = _MUSIC_PREFERENCE_REQUEST.search(message).group("artist").strip()
        await recommend_text(
            f"Recomiéndame canciones de {artist}", update, context
        )
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
    from app.device_bridge import DeviceBridge
    from app.music_library import MusicLibrary

    music = MusicLibrary(settings)
    bridge = DeviceBridge(settings, database, music)
    ai = AIService(settings)
    web = WebSearch(settings)
    recommender = Recommender(database, ai, web)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(bridge.start)
        .post_shutdown(bridge.stop)
        .build()
    )
    application.bot_data["recommender"] = recommender
    application.bot_data["ai"] = ai
    application.bot_data["web"] = web
    application.bot_data["database"] = database
    application.bot_data["music"] = music
    application.bot_data["device_bridge"] = bridge
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_chat))
    application.add_handler(CommandHandler("buscar", search_command))
    application.add_handler(CallbackQueryHandler(play_callback, pattern=r"^play:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application
