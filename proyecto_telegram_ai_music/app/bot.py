import asyncio
import html
import logging
import re
import uuid
from datetime import timedelta

import requests
from telegram.error import TelegramError
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
from app.playback_history import PlaybackHistory, REPORT_TIMEZONE
from app.recommender import Recommender
from app.sd_path import parse_sd_path

logger = logging.getLogger(__name__)
_MAX_VOICE_FILE_SIZE = 20 * 1024 * 1024
_RECOMMENDATION_REQUEST = re.compile(
    r"\b(canciones?|temas?|hits?|recomiend\w*|suger\w*|"
    r"dame|pon(?:me|eme)?|busco|buscar|escuch\w*|reproduc\w*)\b",
    re.IGNORECASE,
)
_REPORT_REQUEST = re.compile(
    r"\b(?:report(?:e|es)|inform(?:e|es)|estad[ií]stic\w*|res[uú]men)\b"
    r"|\b(?:qu[eé]|cu[aá]nt[oa]s?|mostr[aá]me|mu[eé]strame|dime)\b"
    r".{0,80}\b(?:escuch\w*|reproduc\w*|sonad\w*)\b"
    r"|\btop\b.{0,50}\b(?:escuch\w*|reproduc\w*)\b",
    re.IGNORECASE,
)
_REPORT_CONTEXT_REQUEST = re.compile(
    r"\bmis?\b.{0,60}\b(?:semana|d[ií]a|escuch\w*|reproduc\w*|"
    r"historial|actividad musical)\b"
    r"|\bm[aá]s escuchad\w*\b",
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
        "También puedes enviarme un mensaje de voz con tu pedido. "
        "Usa /reset para borrar el contexto."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Escribe lo que quieres escuchar. También puedes usar /buscar seguido "
        "de una petición, por ejemplo /buscar rock de los 90. También puedes "
        "preguntarme sobre artistas, canciones, géneros e historia de la música. "
        "Las recomendaciones con una ruta SD válida tendrán un botón de reproducción. "
        "También puedes enviar un mensaje de voz de hasta 20 MB; lo transcribiré "
        "con el proveedor de IA configurado. "
        "Usa /pausar y /reanudar para controlar el audio, /reportes diario o "
        "/reportes semanal para ver tus estadísticas, y /reset para iniciar "
        "una conversación nueva. También puedes pedirme el informe con tus palabras."
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

    history = context.application.bot_data["playback_history"]
    try:
        await asyncio.to_thread(
            history.start, song, update.effective_user.id
        )
    except PyMongoError:
        logger.exception("No se pudo guardar el inicio de reproducción en MongoDB")
        history_warning = (
            "La orden de reproducción se envió, pero no pude guardar el historial "
            "en MongoDB."
        )
    else:
        history_warning = None

    await query.message.reply_text(
        f"Orden enviada al ESP32: {song.get('title', 'la canción')}.",
        reply_markup=_player_control_keyboard(),
    )
    context.user_data["last_played_song_id"] = str(song["_id"])
    if history_warning:
        await query.message.reply_text(history_warning)


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
    history = context.application.bot_data["playback_history"]
    try:
        changed = await asyncio.to_thread(getattr(history, action))
    except PyMongoError:
        logger.exception("No se pudo actualizar la sesión de reproducción")
        await update.effective_message.reply_text(
            "El control se envió al reproductor, pero no pude actualizar el "
            "historial en MongoDB."
        )
        return
    if not changed:
        logger.info("No había una sesión activa que actualizar para %s.", action)
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


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {seconds:02d} s"
    return f"{seconds} s"


def _format_playback_report(report: dict) -> str:
    period = report["period"]
    start_local = report["start"].astimezone(REPORT_TIMEZONE)
    if period == "daily":
        heading = f"Informe diario — {start_local:%d/%m/%Y}"
    else:
        end_local = report["end"].astimezone(REPORT_TIMEZONE)
        heading = (
            f"Informe semanal — {start_local:%d/%m} al "
            f"{(end_local - timedelta(days=1)):%d/%m/%Y}"
        )
    lines = [f"<b>{heading}</b>"]
    if report["play_count"] == 0:
        lines.append("Todavía no hay reproducciones registradas en este período.")
        return "\n".join(lines)

    lines.append(
        f"Tiempo escuchado: <b>{_format_duration(report['total_seconds'])}</b>"
    )
    lines.append(f"Reproducciones registradas: {report['play_count']}")
    if report["top_songs"]:
        lines.append("\n<b>Top 5 canciones:</b>")
        for index, song in enumerate(report["top_songs"], 1):
            title = html.escape(str(song["title"]))
            artist = html.escape(str(song["artist"]))
            lines.append(
                f"{index}. {title} — {artist} · {song['plays']} "
                f"reprod. · {_format_duration(song['seconds'])}"
            )
    if report["top_genres"]:
        lines.append("\n<b>Géneros más escuchados:</b>")
        for index, (genre, seconds) in enumerate(report["top_genres"], 1):
            lines.append(
                f"{index}. {html.escape(genre)} · {_format_duration(seconds)}"
            )
        if len(report["top_genres"]) < 3:
            lines.append(
                "Nota: varias canciones todavía no tienen género en sus etiquetas."
            )
    else:
        lines.append(
            "\nTodavía no hay géneros etiquetados en las canciones reproducidas."
        )
    return "\n".join(lines)


async def send_playback_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period: str
) -> None:
    history = context.application.bot_data["playback_history"]
    try:
        report = await asyncio.to_thread(history.report, period)
    except PyMongoError:
        logger.exception("No se pudo consultar el historial de reproducción")
        await update.effective_message.reply_text(
            "No pude consultar el historial de reproducciones en MongoDB."
        )
        return
    await update.effective_message.reply_text(
        _format_playback_report(report), parse_mode=ParseMode.HTML
    )


async def reports_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    period_text = context.args[0].casefold() if context.args else "diario"
    if period_text in {"diario", "diaria", "hoy", "daily"}:
        period = "daily"
    elif period_text in {"semanal", "semana", "weekly"}:
        period = "weekly"
    else:
        await update.effective_message.reply_text(
            "Uso: /reportes diario o /reportes semanal"
        )
        return
    await send_playback_report(update, context, period)


async def _try_natural_language_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> bool:
    if not (
        _REPORT_REQUEST.search(message) or _REPORT_CONTEXT_REQUEST.search(message)
    ):
        return False
    ai: AIService = context.application.bot_data["ai"]
    try:
        period = await asyncio.to_thread(ai.classify_report_period, message)
    except AIQuotaExceededError:
        await update.effective_message.reply_text(
            "No pude interpretar el pedido porque se agotó la cuota del proveedor "
            "de IA. También puedes usar /reportes diario o /reportes semanal."
        )
        return True
    except AIRateLimitError:
        await update.effective_message.reply_text(
            "La IA está limitando solicitudes temporalmente. Prueba /reportes "
            "diario o /reportes semanal."
        )
        return True
    except Exception:
        logger.exception("No se pudo interpretar el pedido de informe")
        await update.effective_message.reply_text(
            "No pude interpretar el pedido ahora. Prueba /reportes diario o "
            "/reportes semanal."
        )
        return True

    if period:
        await send_playback_report(update, context, period)
        return True
    return False


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
    await process_text_message(update, context, update.message.text)


async def process_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    if await _try_natural_language_report(update, context, message):
        return
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


async def voice_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    voice = message.voice if message else None
    if not message or not voice:
        return
    if voice.file_size and voice.file_size > _MAX_VOICE_FILE_SIZE:
        await message.reply_text(
            "Ese audio supera el límite de 20 MB que puedo procesar. "
            "Envía un mensaje de voz más corto."
        )
        return

    await message.chat.send_action("typing")
    try:
        audio_file = await context.bot.get_file(voice.file_id)
        audio = bytes(await audio_file.download_as_bytearray())
    except TelegramError:
        logger.exception("No se pudo descargar el mensaje de voz de Telegram")
        await message.reply_text(
            "No pude descargar el audio de Telegram. Inténtalo otra vez."
        )
        return

    ai: AIService = context.application.bot_data["ai"]
    try:
        transcript = await asyncio.to_thread(
            ai.transcribe_audio, audio, "voice.ogg"
        )
    except AIQuotaExceededError:
        await message.reply_text(
            "Se agotó la cuota del proveedor de IA y no pude transcribir el audio. "
            "Revisa la facturación de tu proveedor e inténtalo luego."
        )
        return
    except AIRateLimitError:
        await message.reply_text(
            "El proveedor de IA está limitando solicitudes temporalmente. "
            "Inténtalo de nuevo en un momento."
        )
        return
    except Exception:
        logger.exception("No se pudo transcribir el mensaje de voz")
        await message.reply_text(
            "No pude transcribir el audio. Comprueba que tu proveedor de IA "
            "admita transcripciones de voz y vuelve a intentarlo."
        )
        return

    await message.reply_text(f"Entendí: «{transcript}»")
    await process_text_message(update, context, transcript)


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
    application.bot_data["playback_history"] = PlaybackHistory(
        database.client[settings.mongo_db]["playback_sessions"]
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_chat))
    application.add_handler(CommandHandler("buscar", search_command))
    application.add_handler(CommandHandler("pausar", pause_command))
    application.add_handler(CommandHandler("reanudar", resume_command))
    application.add_handler(CommandHandler("reportes", reports_command))
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
    application.add_handler(MessageHandler(filters.VOICE, voice_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application
