import asyncio
import html
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import requests
from telegram.error import TelegramError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
_LOOP_MODE_LABELS = {
    "off": "apagado",
    "all": "cola",
    "one": "canción",
}
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
from app.playback_queue import PlaybackQueue
from app.playback_state import PlaybackStateStore
from app.recommender import Recommender, requested_song_count
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
    r"(?:play|toca)\s+(?:esa\s+)?(?:canción|tema|song))\b"
    r"|\b(?:la|lo|esa|ese|esta|este)\s+quiero\s+(?:escuchar\w*|o[ií]r)\b"
    r"|\b(?:quiero|quisiera|me\s+gustar[ií]a)\s+"
    r"(?:escuchar|o[ií]r)\s+(?:la|lo|esa|ese|esta|este)\b"
    r"|\b(?:ten[eé]s|tienes)\b.{0,60}\bpara\s+que\s+(?:yo\s+)?"
    r"(?:la|lo)\s+(?:escuche|oiga)\b",
    re.IGNORECASE,
)
_AUTOPLAY_REQUEST = re.compile(
    r"\b(?:quiero|quisiera|me\s+gustar[ií]a)\s+"
    r"(?:que\s+me\s+)?(?:escuchar|o[ií]r)\b"
    r"|\bpon(?:me|eme)\s+(?!otra\b)",
    re.IGNORECASE,
)
_STOP_PLAYBACK_REQUEST = re.compile(
    r"\bno\s+(?:quiero|quisiera|deseo)\s+"
    r"(?:seguir|continuar)\s+(?:escuch\w*|oyend\w*|reproduc\w*)\b"
    r"|\bno\s+(?:quiero|quisiera|deseo)\s+"
    r"(?:escuch\w*|oyend\w*|reproduc\w*)\s+"
    r"(?:m[aá]s\s+)?(?:m[uú]sica|canciones?|nada)\b"
    r"|\bno\s+par[aá]\b"
    r"|\b(?:par[aá]|det[eé]n|deten\w*|fren\w*)\s+"
    r"(?:de\s+)?(?:la\s+)?(?:m[uú]sica|reproducci[oó]n|canci[oó]n)\b",
    re.IGNORECASE,
)
_QUEUE_CLEAR_REQUEST = re.compile(
    r"\b(?:borr\w*|elimin\w*|limpi\w*|vac[ií]\w*)\b"
    r".{0,100}\b(?:toda?\s+)?(?:la\s+)?cola\b",
    re.IGNORECASE,
)
_NEGATED_QUEUE_CHANGE_REQUEST = re.compile(
    r"\b(?:no|nunca)\s+(?:me\s+)?"
    r"(?:borr\w*|elimin\w*|limpi\w*|vac[ií]\w*|quit\w*)\b"
    r".{0,120}\bcola\b",
    re.IGNORECASE,
)
_ALBUM_PLAY_REQUEST = re.compile(
    r"\b(?:pon\w*|reproduc\w*|escuch\w*|"
    r"quiero\s+(?:poner|escuchar)|play)\b.{0,100}\b(?:[áa]lbum|disco)\b"
    r"|\b(?:[áa]lbum|disco)\b.{0,100}\b(?:pon\w*|"
    r"reproduc\w*|escuch\w*)\b",
    re.IGNORECASE,
)
_PERSONA_QUEUE_REQUEST = re.compile(
    r"(?:\b(?:a\s+la\s+cola|la\s+cola|encol\w*)\b.{0,120}\bpersona\b"
    r"|\bpersona\b.{0,120}\b(?:a\s+la\s+cola|la\s+cola|encol\w*)\b)",
    re.IGNORECASE,
)
_QUEUE_KEEP_REQUEST = re.compile(
    r"(?:\b(?:borr\w*|elimin\w*|quit\w*|sac\w*|limpi\w*)\b"
    r".{0,220}\b(?:menos|excepto|salvo)\b"
    r"|\b(?:dej\w*|mant\w*|conserv\w*)\b.{0,100}\bsolo\b)"
    r"\s*(?P<keep>[^.!?]*)",
    re.IGNORECASE,
)
_QUEUE_JUMP_REQUEST = re.compile(
    r"\b(?:saltar(?:me|te)?|salt[aá](?:me|te)?)\s+"
    r"(?:directamente\s+)?(?:a|hasta)\s+"
    r"(?:(?:la|el)\s+)?(?:(?:canci[oó]n|tema)\s+)?(?P<title>.+)$",
    re.IGNORECASE,
)
_CORRECTION_REQUEST = re.compile(
    r"\b(?:quise\s+decir|quer[ií]a\s+decir|me\s+refer[ií]a\s+a)\b",
    re.IGNORECASE,
)
_ALTERNATIVE_REQUEST = re.compile(
    r"\bno[, ]+(?:esa|ese|esta|este)\s+no\b"
    r"|\bpon(?:me|eme)\s+otra\b"
    r"|\bdame\s+otra\b"
    r"|\b(?:otra\s+(?:distinta|diferente)|otro\s+(?:distinto|diferente))\b",
    re.IGNORECASE,
)
_BARE_ALTERNATIVE_REQUEST = re.compile(
    r"^\s*(?:(?:no[, ]*)?(?:esa|ese|esta|este)\s+no[, ]*)?"
    r"(?:(?:pon(?:me|eme)|dame)\s+)?"
    r"(?:otra|otro)(?:\s+(?:canci[oó]n|tema))?[.!? ]*$",
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


def _device_failure_message(error: Exception, action: str) -> str:
    action_labels = {
        "pause": "pausar la canción",
        "resume": "reanudar la canción",
        "stop": "detener la música",
        "play": "reproducir la canción",
    }
    action = action_labels.get(action, action)
    if isinstance(error, PlayerNotConfiguredError):
        return "El reproductor ESP32 no está habilitado o le falta configuración."
    if isinstance(error, requests.exceptions.HTTPError):
        status = error.response.status_code if error.response is not None else None
        if status == 401:
            return (
                "El ESP32 rechazó la autenticación. Comprueba que API_TOKEN en "
                "su config.py coincida con ESP32_API_TOKEN del bot."
            )
        if status == 400:
            return (
                f"El ESP32 rechazó la orden para {action} (HTTP 400). Revisa "
                "el firmware y, si era una canción, que su ruta de microSD sea válida."
            )
        if status == 502:
            return (
                f"El ESP32 recibió la orden para {action}, pero el DFPlayer no "
                "confirmó que se haya realizado. Revisa la conexión UART, la "
                "alimentación del módulo y sus respuestas en Thonny."
            )
        return f"El ESP32 respondió con HTTP {status} al intentar {action}."
    if isinstance(error, requests.exceptions.RequestException):
        return (
            f"No pude conectar con el ESP32 para {action}. Comprueba que esté "
            "encendido, en la misma red Wi-Fi y que ESP32_BASE_URL sea correcto."
        )
    if isinstance(error, ValueError):
        return (
            f"No pude {action}: la ruta o los datos de la canción no son válidos. "
            "Revisa el indexado de la microSD."
        )
    return (
        f"El ESP32 no aceptó la orden para {action}. Comprueba el firmware, "
        "la conexión con el DFPlayer y las rutas de la microSD."
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
        "También puedes pedirme un álbum completo y controlar la cola con "
        "/cola, /skip y /loop. Puedes enviarme mensajes de voz. Usa /help "
        "para ver los comandos; /autoplay on|off activa o desactiva el inicio "
        "automático de canciones."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎵 MP3 AI Player — Ayuda\n\n"
        "¿QUÉ PUEDES PEDIR?\n"
        "• Canciones o estilos: «poneme rock de los 90».\n"
        "• Una canción: «quiero escuchar Purple Rain».\n"
        "• Un álbum: «reproduce el álbum Purple Rain».\n"
        "• Información musical: «¿quién es Prince?».\n"
        "También puedes enviar un mensaje de voz. Si hay una canción disponible "
        "en tu microSD, te ofreceré reproducirla.\n\n"
        "REPRODUCCIÓN\n"
        "• /pausar — pausa la canción.\n"
        "• /reanudar — continúa la canción.\n"
        "• /detener o /parar — detiene la música y vacía la cola.\n"
        "• /volumen 26 — ajusta el volumen (0 a 30).\n"
        "• /autoplay on|off — reproduce directamente los pedidos explícitos "
        "o pide confirmación. Está apagado por defecto.\n\n"
        "COLA Y ÁLBUMES\n"
        "• /cola — muestra la canción actual y las siguientes.\n"
        "• /skip — salta a la próxima; /skip Título salta a esa canción futura.\n"
        "• /loop — alterna entre no repetir, repetir la cola y repetir la canción.\n"
        "También puedes decir «borra toda la cola» para quitar las canciones "
        "futuras, o «deja solo las de Queen». La canción actual termina sin "
        "interrupción. Para detenerla también, di «pará la música».\n\n"
        "OTROS COMANDOS\n"
        "• /buscar rock de los 90 — busca música en tu biblioteca.\n"
        "• /reportes diario o /reportes semanal — resumen de reproducción.\n"
        "• /reset — borra el contexto reciente de la conversación.\n"
        "• /help — muestra esta ayuda."
    )


async def pause_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _send_player_control(update, context, "pause")


async def resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _send_player_control(update, context, "resume")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await stop_playback_request(update, context)


async def stop_playback_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    bot_data = context.application.bot_data
    settings: Settings = bot_data["player_settings"]
    if not settings.player_enabled:
        await update.effective_message.reply_text(
            "El reproductor ESP32 no está habilitado; no pude detener la música."
        )
        return

    lock = bot_data.setdefault("queue_advance_lock", asyncio.Lock())
    async with lock:
        device: DeviceClient = bot_data["device"]
        try:
            await asyncio.to_thread(device.stop)
        except (
            PlayerNotConfiguredError,
            requests.exceptions.RequestException,
            RuntimeError,
            ValueError,
        ) as exc:
            logger.exception("No se pudo confirmar la detención en el reproductor")
            await update.effective_message.reply_text(
                _device_failure_message(exc, "stop")
                + " La cola no cambió."
            )
            return

        _cancel_queue_timer(context)
        bot_data.pop("queue_timer_remaining", None)
        bot_data["queue_paused"] = False
        queue: PlaybackQueue = bot_data["playback_queue"]
        queue.clear()
        bot_data.pop("queue_song_started_at", None)
        bot_data.pop("queue_user_id", None)
        bot_data.pop("queue_chat_id", None)

    await _persist_queue_state(
        context, update.effective_user.id, update.effective_chat.id
    )
    history: PlaybackHistory = bot_data["playback_history"]
    history_warning = ""
    try:
        await asyncio.to_thread(history.stop)
    except PyMongoError:
        logger.exception("No se pudo actualizar el historial al detener la música")
        history_warning = " No pude actualizar el historial de reproducción."
    await update.effective_message.reply_text(
        "Detuve la reproducción y limpié la cola." + history_warning
    )


async def clear_upcoming_queue_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    bot_data = context.application.bot_data
    queue: PlaybackQueue = bot_data["playback_queue"]
    lock = bot_data.setdefault("queue_advance_lock", asyncio.Lock())
    async with lock:
        if queue.current is None:
            queue.clear()
            removed = 0
            has_current = False
        else:
            removed, _ = queue.retain_upcoming(lambda _song: False)
            has_current = True
    await _persist_queue_state(
        context, update.effective_user.id, update.effective_chat.id
    )
    if not has_current:
        await update.effective_message.reply_text("La cola ya estaba vacía.")
    elif removed:
        await update.effective_message.reply_text(
            f"Quité {removed} canciones futuras de la cola. La canción actual "
            "seguirá hasta terminar; si quieres detenerla también, dime "
            "«pará la música»."
        )
    else:
        await update.effective_message.reply_text(
            "No había canciones futuras en la cola; la canción actual sigue "
            "reproduciéndose."
        )


async def _persist_queue_state(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> bool:
    bot_data = context.application.bot_data
    store = bot_data.get("playback_state")
    if not isinstance(store, PlaybackStateStore):
        return True
    queue: PlaybackQueue = bot_data["playback_queue"]
    if not queue.current:
        user_id = None
        chat_id = None
    else:
        user_id = bot_data.get("queue_user_id", user_id)
        chat_id = bot_data.get("queue_chat_id", chat_id)
        bot_data["queue_user_id"] = user_id
        bot_data["queue_chat_id"] = chat_id
    try:
        await asyncio.to_thread(
            store.save_queue,
            queue.songs,
            queue.repeat_mode,
            bot_data.get("queue_song_started_at"),
            bool(bot_data.get("queue_paused")),
            user_id,
            chat_id,
        )
    except PyMongoError:
        logger.exception("No se pudo guardar el estado de reproducción en MongoDB")
        bot_data["queue_persistence_error"] = True
        destination = chat_id or bot_data.get("queue_chat_id")
        if destination is not None:
            try:
                await context.bot.send_message(
                    destination,
                    "La reproducción sigue, pero no pude guardar la cola en MongoDB. "
                    "Podrías perderla si se reinicia el bot.",
                )
            except TelegramError:
                logger.exception(
                    "No se pudo avisar que falló el guardado de la cola"
                )
        return False
    bot_data["queue_persistence_error"] = False
    return True


async def volume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not context.args or not context.args[0].isdecimal():
        await update.effective_message.reply_text(
            "Indica un volumen entero del 0 al 30. Ejemplo: /volumen 26"
        )
        return
    volume = int(context.args[0])
    if not 0 <= volume <= 30:
        await update.effective_message.reply_text(
            "El volumen debe estar entre 0 y 30."
        )
        return
    settings: Settings = context.application.bot_data["player_settings"]
    if not settings.player_enabled:
        await update.effective_message.reply_text(
            "El reproductor ESP32 no está habilitado."
        )
        return
    device: DeviceClient = context.application.bot_data["device"]
    try:
        await asyncio.to_thread(device.set_volume, volume)
    except PlayerNotConfiguredError:
        await update.effective_message.reply_text(
            "Falta configurar la conexión con el ESP32."
        )
        return
    except requests.exceptions.RequestException:
        logger.exception("No se pudo cambiar el volumen del DFPlayer")
        await update.effective_message.reply_text(
            "No pude contactar el ESP32. Revisa que esté encendido en la misma red."
        )
        return
    except Exception:
        logger.exception("El ESP32 rechazó el cambio de volumen")
        await update.effective_message.reply_text(
            "El reproductor rechazó el volumen. Comprueba el firmware del ESP32."
        )
        return
    await update.effective_message.reply_text(
        f"Volumen del DFPlayer ajustado a {volume}/30."
    )


async def autoplay_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_data = context.application.user_data[update.effective_user.id]
    _initialize_autoplay_preference(
        user_data, context.application, update.effective_user.id
    )
    if not context.args or context.args[0].casefold() == "estado":
        enabled = bool(user_data.get("autoplay_enabled", False))
    else:
        mode = context.args[0].casefold()
        if mode not in {"on", "off", "sí", "si", "no"}:
            await update.effective_message.reply_text(
                "Uso: /autoplay on o /autoplay off"
            )
            return
        enabled = mode in {"on", "sí", "si"}
        user_data["autoplay_enabled"] = enabled
        autoplay_users = context.application.bot_data.get("autoplay_users")
        if isinstance(autoplay_users, dict):
            autoplay_users[update.effective_user.id] = enabled
        store = context.application.bot_data.get("playback_state")
        if isinstance(store, PlaybackStateStore):
            try:
                await asyncio.to_thread(
                    store.save_autoplay, update.effective_user.id, enabled
                )
            except PyMongoError:
                logger.exception(
                    "No se pudo guardar la preferencia de autoplay en MongoDB"
                )
                await update.effective_message.reply_text(
                    "No pude guardar esta preferencia en MongoDB; se aplicó solo "
                    "hasta que se reinicie el bot."
                )
                return
    status = "activada" if enabled else "desactivada"
    await update.effective_message.reply_text(
        f"Reproducción automática {status}. "
        "Cuando está activa, los pedidos explícitos de escuchar una canción "
        "reproducen directamente la mejor coincidencia local. Usa "
        "/autoplay off para volver a pedir confirmación."
    )


async def reset_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_data = context.application.user_data[update.effective_user.id]
    for key in (
        "music_history",
        "suggested_play_songs",
        "suggested_play_albums",
        "pending_play",
        "pending_album",
        "pending_album_names",
    ):
        user_data.pop(key, None)
    await update.message.reply_text("Listo, empecemos una conversación nueva.")


def _initialize_autoplay_preference(
    user_data: dict, application: Application, user_id: int
) -> None:
    if "autoplay_enabled" in user_data:
        return
    autoplay_users = application.bot_data.get("autoplay_users", {})
    if not isinstance(autoplay_users, dict):
        autoplay_users = {}
    user_data["autoplay_enabled"] = bool(autoplay_users.get(user_id, False))


async def recommend_text(
    request_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    recommender: Recommender = context.application.bot_data["recommender"]
    user_data = context.application.user_data[update.effective_user.id]
    _initialize_autoplay_preference(
        user_data, context.application, update.effective_user.id
    )
    history = list(user_data.get("music_history", []))
    previous_request = user_data.get("last_recommendation_request")
    alternative_phrase = bool(_ALTERNATIVE_REQUEST.search(request_text))
    asking_for_alternative = alternative_phrase and bool(previous_request)
    if (
        not previous_request
        and _BARE_ALTERNATIVE_REQUEST.fullmatch(request_text)
    ):
        await update.message.reply_text(
            "¿Otra canción de qué artista o estilo? Decime qué querés escuchar "
            "y te recomiendo una."
        )
        return
    search_request = previous_request if asking_for_alternative else request_text
    excluded_ids = list(user_data.get("excluded_recommendation_ids", []))
    if asking_for_alternative:
        excluded_ids.extend(user_data.get("last_recommendation_ids", []))
        excluded_ids = list(dict.fromkeys(excluded_ids))
    else:
        excluded_ids = []
    await update.message.chat.send_action("typing")
    try:
        songs, web_failed = await asyncio.to_thread(
            recommender.recommend,
            search_request,
            history,
            excluded_ids=excluded_ids,
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
        if asking_for_alternative:
            await update.message.reply_text(
                "No encontré otra canción distinta de las que ya te sugerí "
                "que coincida con ese pedido en tu biblioteca local."
            )
            _remember_exchange(
                context, update, request_text, "No quedaron otras coincidencias."
            )
            return
        await chat_message(
            update, context, request_text, library_search_miss=True
        )
        return

    user_wants_playback = bool(
        (
            _PLAYBACK_REQUEST.search(request_text)
            or _AUTOPLAY_REQUEST.search(request_text)
        )
        and not asking_for_alternative
    )
    autoplay_enabled = bool(user_data.get("autoplay_enabled", False))
    player_enabled = context.application.bot_data["player_settings"].player_enabled
    autoplay_song = (
        next((song for song in songs if parse_sd_path(song.get("sd_path"))), None)
        if user_wants_playback and autoplay_enabled and player_enabled
        else None
    )
    message = "\n".join(
        _format_song(song, index) for index, song in enumerate(songs, 1)
    )
    keyboard = (
        None
        if autoplay_song
        else _play_keyboard(songs, context, update.effective_user.id)
    )
    if autoplay_song:
        user_data.setdefault("pending_play", {}).clear()
    has_sd_paths = any(parse_sd_path(song.get("sd_path")) for song in songs)
    if autoplay_song:
        message += (
            "\n\nReproducción automática activada: iniciaré la mejor coincidencia "
            "disponible en tu microSD."
        )
    elif keyboard:
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
    if autoplay_song:
        await _play_song_immediately(
            autoplay_song,
            context,
            update.effective_user.id,
            update.effective_chat.id,
            update.effective_message,
        )
    plain_list = "\n".join(
        f"{index}. {song.get('title', 'Sin título')} — "
        f"{song.get('artist', 'Artista desconocido')}"
        for index, song in enumerate(songs, 1)
    )
    if not asking_for_alternative:
        user_data["last_recommendation_request"] = request_text
        excluded_ids = []
    user_data["excluded_recommendation_ids"] = list(
        dict.fromkeys(excluded_ids + [str(song["_id"]) for song in songs])
    )[-50:]
    user_data["last_recommendation_ids"] = [
        str(song["_id"]) for song in songs
    ]
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


def _play_context_keyboard(
    song: dict | None,
    album_songs: list[dict],
    album: str | None,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> InlineKeyboardMarkup | None:
    if not context.application.bot_data["player_settings"].player_enabled:
        return None
    user_data = context.application.user_data[user_id]
    rows = []
    if song and parse_sd_path(song.get("sd_path")):
        suggested_songs = user_data.setdefault("suggested_play_songs", set())
        song_id = str(song["_id"])
        if song_id not in suggested_songs:
            suggested_songs.add(song_id)
            token = uuid.uuid4().hex[:16]
            user_data.setdefault("pending_play", {})[token] = song_id
            label = f"▶ Reproducir {song.get('title', 'canción')}"
            rows.append(
                [InlineKeyboardButton(label[:60], callback_data=f"play:{token}")]
            )
    if album and album_songs:
        normalized_album = album.casefold().strip()
        suggested_albums = user_data.setdefault("suggested_play_albums", set())
        if normalized_album not in suggested_albums:
            suggested_albums.add(normalized_album)
            token = uuid.uuid4().hex[:16]
            user_data.setdefault("pending_album", {})[token] = [
                str(item["_id"]) for item in album_songs
            ]
            user_data.setdefault("pending_album_names", {})[token] = album
            label = f"▶ Encolar {album} ({len(album_songs)})"
            rows.append(
                [
                    InlineKeyboardButton(
                        label[:60], callback_data=f"playalbum:{token}"
                    )
                ]
            )
    return InlineKeyboardMarkup(rows) if rows else None


def _player_control_keyboard(
    queue: PlaybackQueue, paused: bool = False
) -> InlineKeyboardMarkup:
    action = "resume" if paused else "pause"
    label = "▶ Reanudar" if paused else "⏸ Pausar"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"control:{action}"),
                InlineKeyboardButton("⏭ Siguiente", callback_data="queue:skip"),
            ],
            [
                InlineKeyboardButton("📃 Ver cola", callback_data="queue:show"),
                InlineKeyboardButton(
                    f"🔁 Loop: {_LOOP_MODE_LABELS[queue.repeat_mode]}",
                    callback_data="queue:loop",
                ),
            ],
        ]
    )


def _cancel_queue_timer(
    context: ContextTypes.DEFAULT_TYPE, preserve_remaining: bool = False
) -> None:
    bot_data = context.application.bot_data
    task = bot_data.pop("queue_timer_task", None)
    if task and not task.done() and task is not asyncio.current_task():
        if preserve_remaining:
            deadline = bot_data.get("queue_timer_deadline")
            bot_data["queue_timer_remaining"] = (
                max(0, deadline - asyncio.get_running_loop().time())
                if deadline is not None
                else None
            )
        task.cancel()
    bot_data.pop("queue_timer_deadline", None)


def _schedule_queue_timer(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    delay: float | None = None,
) -> None:
    _cancel_queue_timer(context)
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    song = queue.current
    if delay is None:
        duration = song.get("duration_sec") if song else None
        if not isinstance(duration, (int, float)) or duration <= 0:
            return
        delay = float(duration) + 2
    if delay <= 0:
        return

    bot_data = context.application.bot_data
    loop = asyncio.get_running_loop()
    bot_data["queue_timer_remaining"] = delay
    bot_data["queue_paused"] = False
    bot_data["queue_timer_deadline"] = loop.time() + delay

    async def advance_when_finished() -> None:
        try:
            await asyncio.sleep(delay)
            await _advance_playlist(
                context,
                chat_id,
                user_id,
                automatic=True,
                expected_song=song,
            )
        except asyncio.CancelledError:
            return

    bot_data["queue_timer_task"] = context.application.create_task(
        advance_when_finished()
    )


async def _start_queued_song(
    song: dict,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    status_note: str = "",
    device_already_started: bool = False,
) -> None:
    device: DeviceClient = context.application.bot_data["device"]
    if not device_already_started:
        await asyncio.to_thread(device.play, song["sd_path"])

    history = context.application.bot_data["playback_history"]
    try:
        await asyncio.to_thread(history.start, song, user_id)
    except PyMongoError:
        logger.exception("No se pudo guardar el inicio de reproducción en MongoDB")
        history_warning = (
            "\nNo pude guardar esta reproducción en el historial de MongoDB."
        )
    else:
        history_warning = ""

    user_data = context.application.user_data.get(user_id)
    if user_data is not None:
        user_data["last_played_song_id"] = str(song["_id"])
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    bot_data = context.application.bot_data
    bot_data["queue_song_started_at"] = datetime.now(timezone.utc)
    bot_data["queue_user_id"] = user_id
    bot_data["queue_chat_id"] = chat_id
    context.application.bot_data.pop("queue_timer_remaining", None)
    await _persist_queue_state(context, user_id, chat_id)
    _schedule_queue_timer(context, chat_id, user_id)
    try:
        await context.bot.send_message(
            chat_id,
            f"▶ Reproduciendo: {html.escape(str(song.get('title') or 'Sin título'))} "
            f"— {html.escape(str(song.get('artist') or 'Artista desconocido'))}"
            f"{history_warning}{status_note}",
            parse_mode=ParseMode.HTML,
            reply_markup=_player_control_keyboard(queue),
        )
    except TelegramError:
        logger.exception(
            "La canción empezó, pero no se pudo enviar la confirmación a Telegram"
        )


async def _play_song_immediately(
    song: dict,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    message: Message,
) -> None:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    _cancel_queue_timer(context)
    queue.clear()
    queue.enqueue([song])
    context.application.bot_data["queue_paused"] = False
    context.application.bot_data["queue_song_started_at"] = None
    context.application.bot_data["queue_user_id"] = user_id
    context.application.bot_data["queue_chat_id"] = chat_id
    await _persist_queue_state(context, user_id, chat_id)
    try:
        await _start_queued_song(song, context, user_id, chat_id)
    except (
        PlayerNotConfiguredError,
        requests.exceptions.RequestException,
        RuntimeError,
        ValueError,
    ) as exc:
        logger.exception("No se pudo confirmar la orden de reproducción")
        queue.clear()
        await _persist_queue_state(context, user_id, chat_id)
        await message.reply_text(_device_failure_message(exc, "play"))
    except Exception:
        queue.clear()
        await _persist_queue_state(context, user_id, chat_id)
        logger.exception("El ESP32 rechazó la orden de reproducción")
        await message.reply_text(
            "El reproductor rechazó la orden. Comprueba la ruta SD y el DFPlayer."
        )


async def _enqueue_album_songs(
    songs: list[dict],
    album: str,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    message: Message,
) -> None:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    was_idle = queue.current is None
    queue.enqueue(songs)
    if was_idle:
        context.application.bot_data["queue_user_id"] = user_id
        context.application.bot_data["queue_chat_id"] = chat_id
    if not was_idle:
        await _persist_queue_state(context, user_id, chat_id)
        await message.reply_text(
            f"Añadí {len(songs)} canciones de «{html.escape(album)}» "
            "a la cola. La reproducción actual sigue en curso.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        await _persist_queue_state(context, user_id, chat_id)
        await _start_queued_song(
            queue.current,
            context,
            user_id,
            chat_id,
            status_note=(
                f"\nAñadí {len(songs)} canciones de "
                f"«{html.escape(album)}» a la cola."
            ),
        )
    except (
        PlayerNotConfiguredError,
        requests.exceptions.RequestException,
        RuntimeError,
        ValueError,
    ) as exc:
        logger.exception("No se pudo confirmar el inicio del álbum")
        queue.clear()
        await _persist_queue_state(context, user_id, chat_id)
        await message.reply_text(
            _device_failure_message(exc, "reproducir el álbum")
        )
    except Exception:
        logger.exception("El ESP32 rechazó el inicio del álbum")
        queue.clear()
        await _persist_queue_state(context, user_id, chat_id)
        await message.reply_text(
            "El reproductor rechazó el álbum. Comprueba las rutas de la microSD."
        )


async def _advance_playlist(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    automatic: bool = False,
    expected_song: dict | None = None,
) -> bool:
    bot_data = context.application.bot_data
    lock = bot_data.setdefault("queue_advance_lock", asyncio.Lock())
    if lock.locked():
        if not automatic:
            await context.bot.send_message(
                chat_id, "Ya estoy intentando avanzar la cola; espera un momento."
            )
        return False
    async with lock:
        return await _advance_playlist_once(
            context,
            chat_id,
            user_id,
            automatic=automatic,
            expected_song=expected_song,
        )


async def _advance_playlist_once(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    automatic: bool = False,
    expected_song: dict | None = None,
) -> bool:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    if expected_song is not None and queue.current is not expected_song:
        return False
    song = queue.peek_next(manual=not automatic)
    if song is None:
        _cancel_queue_timer(context)
        queue.next(manual=not automatic)
        context.application.bot_data.pop("queue_timer_remaining", None)
        context.application.bot_data.pop("queue_song_started_at", None)
        await _persist_queue_state(context, user_id, chat_id)
        await context.bot.send_message(
            chat_id,
            "La cola terminó." if automatic else "No hay más canciones en la cola.",
        )
        return True
    device: DeviceClient = context.application.bot_data["device"]
    try:
        await asyncio.to_thread(device.play, song["sd_path"])
    except PlayerNotConfiguredError:
        logger.exception("No está configurado el ESP32 para avanzar la cola")
        await context.bot.send_message(
            chat_id, "No avancé la cola: falta configurar el ESP32."
        )
        return False
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logger.exception("El ESP32 rechazó la orden para avanzar la cola")
        if status == 401:
            message = (
                "El ESP32 rechazó la autenticación. Comprueba que API_TOKEN en "
                "su config.py coincida con ESP32_API_TOKEN del bot."
            )
        elif status == 502:
            message = (
                "El ESP32 recibió la orden, pero el DFPlayer no confirmó que "
                "haya empezado la siguiente canción. Revisa la conexión UART "
                "y la respuesta del módulo."
            )
        elif status is not None:
            message = (
                f"El ESP32 respondió con HTTP {status} y no aceptó la siguiente "
                "canción. Revisa el firmware y la ruta de la microSD."
            )
        else:
            message = "El ESP32 rechazó la siguiente canción."
        await context.bot.send_message(chat_id, message)
        return False
    except requests.exceptions.RequestException:
        logger.exception("No se pudo conectar con el ESP32 para avanzar la cola")
        await context.bot.send_message(
            chat_id,
            "No pude conectar con el ESP32 para reproducir la siguiente canción. "
            "La canción actual y la cola no cambiaron; comprueba que la placa esté "
            "encendida y conectada al Wi-Fi.",
        )
        return False
    except (RuntimeError, ValueError) as exc:
        logger.exception("El ESP32 no confirmó la siguiente canción")
        await context.bot.send_message(
            chat_id,
            _device_failure_message(exc, "reproducir la siguiente canción")
            + " La canción actual y la cola no cambiaron.",
        )
        return False
    queue.next(manual=not automatic)
    context.application.bot_data.pop("queue_timer_remaining", None)
    context.application.bot_data["queue_paused"] = False
    try:
        await _start_queued_song(
            song,
            context,
            user_id,
            chat_id,
            device_already_started=True,
        )
    except Exception:
        logger.exception("La siguiente canción empezó, pero falló la actualización del bot")
        await context.bot.send_message(
            chat_id,
            "El ESP32 aceptó la siguiente canción, pero no pude actualizar el "
            "mensaje o el historial del bot.",
        )
    return True


async def skip_queue_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer("Saltando a la siguiente…")
    await _advance_playlist(
        context, query.message.chat_id, update.effective_user.id
    )


def _format_queue(queue: PlaybackQueue) -> str:
    if queue.current is None:
        return "La cola está vacía."
    lines = [
        f"▶ Ahora: {html.escape(str(queue.current.get('title') or 'Sin título'))} "
        f"— {html.escape(str(queue.current.get('artist') or 'Artista desconocido'))}"
    ]
    for index, song in enumerate(queue.upcoming[:20], 1):
        lines.append(
            f"{index}. {html.escape(str(song.get('title') or 'Sin título'))} "
            f"— {html.escape(str(song.get('artist') or 'Artista desconocido'))}"
        )
    if len(queue.upcoming) > 20:
        lines.append(f"… y {len(queue.upcoming) - 20} más.")
    lines.append(f"Loop: {_LOOP_MODE_LABELS[queue.repeat_mode]}.")
    return "\n".join(lines)


async def show_queue_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    await query.answer()
    await query.message.reply_text(_format_queue(queue), parse_mode=ParseMode.HTML)


async def toggle_loop_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    mode = queue.cycle_repeat_mode()
    await _persist_queue_state(
        context, update.effective_user.id, update.effective_chat.id
    )
    await query.answer(f"Loop {_LOOP_MODE_LABELS[mode]}")
    await query.edit_message_reply_markup(
        reply_markup=_player_control_keyboard(
            queue,
            paused=bool(context.application.bot_data.get("queue_paused")),
        )
    )


async def album_play_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    user_id = update.effective_user.id
    user_data = context.application.user_data[user_id]
    ai: AIService = context.application.bot_data["ai"]
    database: SongDatabase = context.application.bot_data["database"]
    try:
        filters = await asyncio.to_thread(
            ai.extract_filters,
            message,
            list(user_data.get("music_history", [])),
        )
        album = filters.get("album")
        artist = filters.get("artist")
        start_track = filters.get("start_track")
        if not album:
            await update.effective_message.reply_text(
                "No pude identificar el nombre exacto del álbum. Probá: "
                "«poné el álbum *nombre del álbum* de *artista*»."
            )
            return
        songs = await asyncio.to_thread(
            database.find_playable_songs_by_album, album, artist, start_track
        )
        persona_fallback = False
        if not songs and re.search(r"\bpersona\b", message, re.IGNORECASE):
            songs = await asyncio.to_thread(
                database.find_playable_songs_by_folder, 2, start_track
            )
            persona_fallback = bool(songs)
    except PyMongoError:
        logger.exception("No se pudo buscar el álbum en MongoDB")
        await update.effective_message.reply_text(
            "No pude consultar la biblioteca para buscar ese álbum."
        )
        return
    except ValueError:
        await update.effective_message.reply_text(
            f"No encontré una pista única llamada «"
            f"{html.escape(str(start_track))}» al comienzo de títulos del álbum "
            f"«{html.escape(str(album))}». No inicié la reproducción.",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception:
        logger.exception("No se pudo interpretar el pedido de álbum")
        await update.effective_message.reply_text(
            "No pude identificar el álbum ahora. Comprueba la conexión con la IA "
            "e inténtalo otra vez."
        )
        return

    if not songs:
        await update.effective_message.reply_text(
            f"No encontré canciones reproducibles del álbum "
            f"«{html.escape(str(album))}» en la microSD.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not context.application.bot_data["player_settings"].player_enabled:
        await update.effective_message.reply_text(
            "Encontré el álbum, pero el reproductor ESP32 no está habilitado."
        )
        return

    if persona_fallback:
        keyboard = _play_context_keyboard(
            None,
            songs,
            "la carpeta Persona",
            context,
            user_id,
        )
        await update.effective_message.reply_text(
            f"No encontré el álbum «{html.escape(str(album))}» con metadatos "
            f"exactos, pero sí hay {len(songs)} canciones locales en la carpeta "
            "Persona de la microSD. No puedo confirmar que todas sean de esa "
            "entrega específica. Si quieres, puedes encolarlas:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    await _enqueue_album_songs(
        songs,
        str(album),
        context,
        user_id,
        update.effective_chat.id,
        update.effective_message,
    )


async def persona_queue_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    database: SongDatabase = context.application.bot_data["database"]
    try:
        songs = await asyncio.to_thread(database.find_playable_songs_by_folder, 2)
    except PyMongoError:
        logger.exception("No se pudo consultar la biblioteca local de Persona")
        await update.effective_message.reply_text(
            "No pude consultar la biblioteca local de Persona en MongoDB."
        )
        return
    if not songs:
        await update.effective_message.reply_text(
            "No encontré canciones de Persona con rutas reproducibles en la "
            "carpeta 02 de la microSD. Comprueba que la tarjeta esté indexada "
            "de nuevo en MongoDB."
        )
        return
    count = requested_song_count(message)
    selected_songs = songs[:count] if count else songs
    if not context.application.bot_data["player_settings"].player_enabled:
        await update.effective_message.reply_text(
            f"Encontré {len(selected_songs)} canciones locales de Persona, "
            "pero el reproductor ESP32 no está habilitado."
        )
        return
    await _enqueue_album_songs(
        selected_songs,
        "la carpeta Persona",
        context,
        update.effective_user.id,
        update.effective_chat.id,
        update.effective_message,
    )


def _normalize_queue_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[^\W_]+", without_accents, re.UNICODE))


def _queue_keep_label(request_text: str) -> str | None:
    match = _QUEUE_KEEP_REQUEST.search(request_text)
    if not match or not re.search(r"\bcola\b", request_text, re.IGNORECASE):
        return None
    label = match.group("keep").strip(" \t,;:¿¡\"'«»")
    label = re.sub(
        r"\s+(?:de|en)\s+la\s+cola(?:\s+de\s+reproducci[oó]n)?$",
        "",
        label,
        flags=re.IGNORECASE,
    )
    label = re.sub(
        r"^(?:(?:todas?\s+)?(?:el|la|los|las)\s+)?"
        r"(?:(?:canciones?|temas?|pistas?|[áa]lbum(?:es)?|discos?|"
        r"artistas?|g[eé]neros?)\s+)?(?:de\s+)?",
        "",
        label,
        flags=re.IGNORECASE,
    ).strip(" \t,;:¿¡\"'«»")
    return label or None


def _song_matches_queue_label(song: dict, label: str) -> bool:
    normalized_label = _normalize_queue_label(label)
    if not normalized_label:
        return False
    path = parse_sd_path(song.get("sd_path"))
    if normalized_label == "persona" and (
        song.get("dfplayer_folder") == 2 or (path is not None and path[0] == 2)
    ):
        return True

    fields = ("title", "artist", "album", "genre", "mood", "tags")
    for field in fields:
        value = song.get(field)
        values = value if isinstance(value, (list, tuple, set)) else [value]
        for item in values:
            if not isinstance(item, str):
                continue
            normalized_value = _normalize_queue_label(item)
            if (
                normalized_value == normalized_label
                or f" {normalized_label} " in f" {normalized_value} "
            ):
                return True
    return False


async def keep_queue_matching_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, label: str
) -> None:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    lock = context.application.bot_data.setdefault("queue_advance_lock", asyncio.Lock())
    async with lock:
        current = queue.current
        if current is None:
            await update.effective_message.reply_text("La cola está vacía.")
            return
        removed, retained = queue.retain_upcoming(
            lambda song: _song_matches_queue_label(song, label)
        )
    await _persist_queue_state(
        context, update.effective_user.id, update.effective_chat.id
    )
    if removed == 0:
        await update.effective_message.reply_text(
            f"La cola ya no tiene canciones futuras fuera de «{label}». "
            "La canción actual se conserva."
        )
        return
    current_note = (
        f"La canción actual no coincide con «{label}» y podrá terminar; "
        if not _song_matches_queue_label(current, label)
        else ""
    )
    await update.effective_message.reply_text(
        f"Quité {removed} canciones de la cola y conservé {retained} "
        f"canciones futuras que coinciden con «{label}». {current_note}"
        "La reproducción actual no se interrumpió."
    )


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

    await _play_song_immediately(
        song,
        context,
        update.effective_user.id,
        query.message.chat_id,
        query.message,
    )


async def play_album_suggestion_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    token = query.data.partition(":")[2]
    song_ids = context.user_data.get("pending_album", {}).pop(token, None)
    if not song_ids:
        await query.answer(
            "Esta sugerencia venció. Pide la recomendación otra vez.", show_alert=True
        )
        return
    await query.answer("Agregando el álbum a la cola…")
    settings: Settings = context.application.bot_data["player_settings"]
    if not settings.player_enabled:
        await query.message.reply_text("El reproductor ESP32 no está habilitado.")
        return
    database: SongDatabase = context.application.bot_data["database"]
    try:
        songs = await asyncio.to_thread(database.find_songs_by_ids, song_ids)
    except PyMongoError:
        logger.exception("No se pudo recuperar el álbum sugerido desde MongoDB")
        await query.message.reply_text(
            "No pude consultar la biblioteca para agregar ese álbum a la cola."
        )
        return
    if not songs:
        await query.message.reply_text(
            "Las canciones del álbum ya no tienen rutas válidas en la microSD."
        )
        return
    album = context.user_data.get("pending_album_names", {}).pop(token, "álbum")
    await _enqueue_album_songs(
        songs, album, context, update.effective_user.id,
        query.message.chat_id, query.message
    )


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
    bot_data = context.application.bot_data
    lock = bot_data.setdefault("queue_advance_lock", asyncio.Lock())
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    async with lock:
        try:
            await asyncio.to_thread(getattr(device, action))
        except (
            PlayerNotConfiguredError,
            requests.exceptions.RequestException,
            RuntimeError,
            ValueError,
        ) as exc:
            logger.exception("No se pudo confirmar el control %s en el ESP32", action)
            await update.effective_message.reply_text(
                _device_failure_message(exc, action)
            )
            return

        paused = action == "pause"
        bot_data["queue_paused"] = paused
        if paused:
            _cancel_queue_timer(context, preserve_remaining=True)
        else:
            remaining = bot_data.pop("queue_timer_remaining", None)
            _schedule_queue_timer(
                context,
                update.effective_chat.id,
                update.effective_user.id,
                delay=remaining,
            )
        await _persist_queue_state(
            context, update.effective_user.id, update.effective_chat.id
        )
    if update.callback_query:
        query = update.callback_query
        await query.edit_message_reply_markup(
            reply_markup=_player_control_keyboard(queue, paused=paused)
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


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_title = " ".join(context.args).strip()
    if target_title:
        await jump_to_queued_song(
            context,
            target_title,
            update.effective_message,
            update.effective_chat.id,
            update.effective_user.id,
        )
        return
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    if queue.current is None:
        await update.effective_message.reply_text("No hay una canción en reproducción.")
        return
    await _advance_playlist(
        context, update.effective_chat.id, update.effective_user.id
    )


async def jump_to_queued_song(
    context: ContextTypes.DEFAULT_TYPE,
    target_title: str,
    message: Message,
    chat_id: int,
    user_id: int,
) -> None:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    matches = queue.find_upcoming(target_title)
    if not matches:
        await message.reply_text(
            f"No encontré «{html.escape(target_title)}» entre las canciones "
            "siguientes de la cola. Usa /cola para ver los títulos."
        )
        return
    if len(matches) > 1:
        choices = "\n".join(
            f"• {html.escape(str(song.get('title') or 'Sin título'))} — "
            f"{html.escape(str(song.get('artist') or 'Artista desconocido'))}"
            for song in matches[:5]
        )
        await message.reply_text(
            "Hay más de una canción futura que coincide. Indica el título "
            "completo o usa /cola para identificarlas:\n" + choices,
            parse_mode=ParseMode.HTML,
        )
        return

    selected_song = matches[0]
    lock = context.application.bot_data.setdefault("queue_advance_lock", asyncio.Lock())
    if lock.locked():
        await message.reply_text(
            "Ya estoy cambiando la canción; espera un momento y vuelve a intentarlo."
        )
        return
    async with lock:
        if not any(song is selected_song for song in queue.upcoming):
            await message.reply_text(
                "La cola cambió mientras procesaba el pedido. Usa /cola e inténtalo otra vez."
            )
            return
        device: DeviceClient = context.application.bot_data["device"]
        try:
            await asyncio.to_thread(device.play, selected_song["sd_path"])
        except (
            PlayerNotConfiguredError,
            requests.exceptions.RequestException,
            RuntimeError,
        ) as exc:
            logger.exception("No se pudo confirmar el salto a otra canción")
            await message.reply_text(
                _device_failure_message(exc, "saltar a la canción elegida")
                + " La cola no cambió."
            )
            return

        _cancel_queue_timer(context)
        queue.jump_to(selected_song)
        context.application.bot_data["queue_paused"] = False
        try:
            await _start_queued_song(
                selected_song,
                context,
                user_id,
                chat_id,
                status_note="Quité de la cola las canciones anteriores a esta.",
                device_already_started=True,
            )
        except Exception:
            logger.exception(
                "El ESP32 aceptó el salto, pero falló la actualización del bot"
            )
            await message.reply_text(
                "El ESP32 aceptó la canción, pero no pude actualizar el estado de la cola."
            )


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    await update.effective_message.reply_text(
        _format_queue(queue), parse_mode=ParseMode.HTML
    )


async def loop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue: PlaybackQueue = context.application.bot_data["playback_queue"]
    mode = queue.cycle_repeat_mode()
    await _persist_queue_state(
        context, update.effective_user.id, update.effective_chat.id
    )
    await update.effective_message.reply_text(
        f"Modo loop: {_LOOP_MODE_LABELS[mode]}."
    )


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
    if not context.args:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📅 Diario", callback_data="report:daily"
                    ),
                    InlineKeyboardButton(
                        "🗓 Semanal", callback_data="report:weekly"
                    ),
                ]
            ]
        )
        await update.effective_message.reply_text(
            "¿Qué informe quieres consultar?",
            reply_markup=keyboard,
        )
        return

    period_text = context.args[0].casefold()
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


async def report_period_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    period = query.data.partition(":")[2]
    await query.answer()
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
    library_search_miss: bool = False,
) -> None:
    ai: AIService = context.application.bot_data["ai"]
    web: WebSearch = context.application.bot_data["web"]
    history = list(
        context.application.user_data[update.effective_user.id].get(
            "music_history", []
        )
    )
    await update.message.chat.send_action("typing")

    def answer() -> tuple[str, dict[str, str] | None]:
        web_context = ""
        if _needs_chat_web_lookup(message) or library_search_miss:
            try:
                web_context = web.search(message)
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.warning("Búsqueda web no disponible durante el chat: %s", exc)
        return ai.chat_music(
            message,
            history,
            web_context,
            library_search_miss=library_search_miss,
        )

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

    if library_search_miss and (
        _PLAYBACK_REQUEST.search(message) or _AUTOPLAY_REQUEST.search(message)
    ):
        if context.application.bot_data["player_settings"].player_enabled:
            response += (
                "\n\nEl reproductor puede reproducir canciones que estén "
                "indexadas en la microSD, pero no encontré este tema en tu "
                "biblioteca local; por eso todavía no puedo ponerlo."
            )
        else:
            response += (
                "\n\nNo encontré este tema en la biblioteca local y el "
                "reproductor todavía no está habilitado."
            )
    elif library_search_miss:
        response += (
            "\n\nNo encontré coincidencias en tu biblioteca local; esta respuesta "
            "no confirma que esas canciones estén en la microSD."
        )
    await update.message.reply_text(response)
    if context.application.bot_data["player_settings"].player_enabled:
        database: SongDatabase = context.application.bot_data["database"]
        recent_context_messages = [
            item["content"]
            for item in history
            if item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ][-10:]
        recent_context_messages.append(message)
        try:
            song = None
            if song_reference:
                recent_context_text = " ".join(recent_context_messages)
                title = song_reference.get("title")
                artist = song_reference.get("artist")
                if title and artist:
                    title_mentioned = re.search(
                        rf"(?<!\w){re.escape(title)}(?!\w)",
                        recent_context_text,
                        re.IGNORECASE,
                    )
                    artist_mentioned = re.search(
                        rf"(?<!\w){re.escape(artist)}(?!\w)",
                        recent_context_text,
                        re.IGNORECASE,
                    )
                    title_is_distinctive = (
                        len(re.findall(r"[^\W_]+", title, re.UNICODE)) >= 2
                    )
                    if title_mentioned and (
                        artist_mentioned or title_is_distinctive
                    ):
                        song = await asyncio.to_thread(
                            database.find_playable_song_by_title_artist,
                            title,
                            artist,
                        )
            if not song:
                song = await asyncio.to_thread(
                    database.find_playable_song_mentioned_in, [message]
                )
            album_songs = []
            album = None
            album_artist = None
            if song_reference:
                album = song_reference.get("album")
                album_artist = song_reference.get("album_artist")
                album_mentioned = bool(
                    album
                    and re.search(
                        rf"(?<!\w){re.escape(album)}(?!\w)",
                        message,
                        re.IGNORECASE,
                    )
                )
                if album_mentioned:
                    album_songs = await asyncio.to_thread(
                        database.find_playable_songs_by_album,
                        album,
                        album_artist,
                    )
        except PyMongoError:
            logger.exception("No se pudo comprobar la sugerencia en la biblioteca")
            await update.message.reply_text(
                "No pude consultar ahora si esa sugerencia está disponible para reproducir."
            )
        else:
            if song or album_songs:
                keyboard = _play_context_keyboard(
                    song,
                    album_songs,
                    album,
                    context,
                    update.effective_user.id,
                )
                if keyboard:
                    suggestions = []
                    if song and parse_sd_path(song.get("sd_path")):
                        suggestions.append(
                            f"«{html.escape(str(song.get('title')))}» de "
                            f"{html.escape(str(song.get('artist')))}"
                        )
                    if album_songs:
                        suggestions.append(
                            f"el álbum «{html.escape(str(album))}» "
                            f"({len(album_songs)} canciones)"
                        )
                    await update.message.reply_text(
                        "Está disponible en tu microSD: "
                        + " y ".join(suggestions)
                        + ". Puedes reproducir la canción o encolar el álbum:",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
    _remember_exchange(context, update, message, response)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await process_text_message(update, context, update.message.text)


async def process_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> None:
    if _NEGATED_QUEUE_CHANGE_REQUEST.search(message):
        await update.effective_message.reply_text(
            "No toqué la cola. Si querías cambiarla, dime qué canciones "
            "quieres conservar o si deseas vaciar las futuras."
        )
        return
    if _STOP_PLAYBACK_REQUEST.search(message):
        await stop_playback_request(update, context)
        return
    if await _try_natural_language_report(update, context, message):
        return
    if _QUEUE_KEEP_REQUEST.search(message) and re.search(
        r"\bcola\b", message, re.IGNORECASE
    ):
        keep_label = _queue_keep_label(message)
        if keep_label:
            await keep_queue_matching_request(update, context, keep_label)
        else:
            await update.effective_message.reply_text(
                "¿Qué canciones quieres conservar? Indica un artista, álbum, "
                "género, título o etiqueta."
            )
    elif _QUEUE_CLEAR_REQUEST.search(message):
        await clear_upcoming_queue_request(update, context)
    elif (jump := _QUEUE_JUMP_REQUEST.search(message)):
        target_title = jump.group("title").strip(" \t.,!?¿¡\"'«»")
        target_title = re.sub(
            r"\s+(?:de|en)\s+la\s+cola(?:\s+de\s+reproducci[oó]n)?$",
            "",
            target_title,
            flags=re.IGNORECASE,
        ).strip()
        if re.fullmatch(
            r"(?:alguna|una|la)?\s*(?:canci[oó]n|tema)?\s*"
            r"(?:siguiente|pr[oó]xima)?",
            target_title,
            re.IGNORECASE,
        ):
            await update.effective_message.reply_text(
                "Dime el título al que quieres saltar, por ejemplo: "
                "«saltá a Change»."
            )
        else:
            await jump_to_queued_song(
                context,
                target_title,
                update.effective_message,
                update.effective_chat.id,
                update.effective_user.id,
            )
    elif _PERSONA_QUEUE_REQUEST.search(message):
        await persona_queue_request(update, context, message)
    elif _ALBUM_PLAY_REQUEST.search(message):
        await album_play_request(update, context, message)
    elif _REPEAT_PLAYBACK_REQUEST.search(message):
        await repeat_last_song(update, context)
    elif _CORRECTION_REQUEST.search(message):
        await recommend_text(message, update, context)
    elif _ALTERNATIVE_REQUEST.search(message):
        await recommend_text(message, update, context)
    elif _PLAYBACK_REQUEST.search(message):
        await recommend_text(message, update, context)
    elif _LIBRARY_AVAILABILITY_QUESTION.search(message):
        await chat_message(update, context, message)
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


async def _restore_application_state(application: Application) -> None:
    bot_data = application.bot_data
    store: PlaybackStateStore = bot_data["playback_state"]
    database: SongDatabase = bot_data["database"]
    try:
        state = await asyncio.to_thread(store.load)
    except PyMongoError as exc:
        logger.exception("No se pudo leer el estado guardado en MongoDB")
        raise RuntimeError(
            "No se pudo restaurar la cola ni las preferencias desde MongoDB."
        ) from exc

    if state:
        autoplay_users = state.get("autoplay_users", {})
        if isinstance(autoplay_users, dict):
            bot_data["autoplay_users"] = {
                int(user_id): bool(enabled)
                for user_id, enabled in autoplay_users.items()
                if str(user_id).isdecimal()
            }
        song_ids = state.get("song_ids", [])
        if not isinstance(song_ids, list):
            logger.error("La cola persistida en MongoDB tiene un formato inválido.")
            song_ids = []
        try:
            songs = await asyncio.to_thread(database.find_songs_by_ids, song_ids)
        except PyMongoError as exc:
            logger.exception("No se pudieron cargar las canciones de la cola guardada")
            raise RuntimeError(
                "No se pudo restaurar la cola desde MongoDB."
            ) from exc
        repeat_mode = state.get("repeat_mode", "off")
        if repeat_mode not in PlaybackQueue.REPEAT_MODES:
            logger.error("Modo de repetición persistido inválido: %r", repeat_mode)
            repeat_mode = "off"
        queue: PlaybackQueue = bot_data["playback_queue"]
        queue.restore(songs, repeat_mode)
        if len(songs) != len(song_ids):
            logger.warning(
                "Se omitieron %d canciones guardadas que ya no están disponibles "
                "en la microSD.",
                len(song_ids) - len(songs),
            )
        if queue.current:
            bot_data["queue_user_id"] = state.get("user_id")
            bot_data["queue_chat_id"] = state.get("chat_id")
            bot_data["queue_song_started_at"] = state.get("started_at")
            bot_data["queue_paused"] = bool(state.get("paused"))
        elif song_ids:
            await asyncio.to_thread(
                store.save_queue, [], repeat_mode, None, False, None, None
            )

    if not state or not bot_data["playback_queue"].current:
        _start_device_event_monitor(application)
        return

    queue: PlaybackQueue = bot_data["playback_queue"]
    user_id = bot_data.get("queue_user_id")
    chat_id = bot_data.get("queue_chat_id")
    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        logger.error("La cola persistida no tiene un usuario o chat válido.")
        _start_device_event_monitor(application)
        return
    context = SimpleNamespace(application=application, bot=application.bot)
    settings: Settings = bot_data["player_settings"]
    if not settings.player_enabled:
        logger.warning("La cola se restauró, pero el reproductor ESP32 está desactivado.")
        _start_device_event_monitor(application)
        return

    device: DeviceClient = bot_data["device"]
    try:
        status = await asyncio.to_thread(device.poll_events)
    except (PlayerNotConfiguredError, requests.exceptions.RequestException, ValueError, RuntimeError):
        logger.exception(
            "No se pudo consultar el ESP32 al restaurar la cola; "
            "se reintentará el avance por duración."
        )
        status = {}

    if status.get("track_finished_events", 0):
        next_song = queue.peek_next(manual=False)
        if next_song is None:
            queue.next(manual=False)
            bot_data["queue_song_started_at"] = None
            await _persist_queue_state(context, user_id, chat_id)
            await application.bot.send_message(
                chat_id, "La canción guardada terminó mientras el bot se reiniciaba."
            )
            _start_device_event_monitor(application)
            return
        try:
            await asyncio.to_thread(device.play, next_song["sd_path"])
        except (
            PlayerNotConfiguredError,
            requests.exceptions.RequestException,
            RuntimeError,
            ValueError,
        ):
            logger.exception("No se pudo continuar la cola guardada")
            await application.bot.send_message(
                chat_id,
                "Restauré la cola, pero el ESP32 no confirmó la siguiente canción. "
                "La cola guardada no cambió.",
            )
            _start_device_event_monitor(application)
            return
        queue.next(manual=False)
        bot_data["queue_paused"] = False
        try:
            await _start_queued_song(
                next_song,
                context,
                user_id,
                chat_id,
                device_already_started=True,
                status_note="Continué la cola guardada tras el reinicio del bot.",
            )
        except (
            PlayerNotConfiguredError,
            requests.exceptions.RequestException,
            RuntimeError,
            ValueError,
        ):
            logger.exception("No se pudo continuar la cola guardada")
            await application.bot.send_message(
                chat_id,
                "El ESP32 aceptó la siguiente canción, pero no pude actualizar "
                "el estado del bot. "
                "La cola quedó guardada.",
            )
        _start_device_event_monitor(application)
        return

    current = queue.current
    if status.get("current_path") == current.get("sd_path"):
        started_at = bot_data.get("queue_song_started_at")
        if not isinstance(started_at, datetime):
            started_at = datetime.now(timezone.utc)
            bot_data["queue_song_started_at"] = started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        duration = current.get("duration_sec")
        if isinstance(duration, (int, float)) and duration > 0:
            elapsed = max(
                0.0, (datetime.now(timezone.utc) - started_at).total_seconds()
            )
            remaining = max(1.0, float(duration) + 2 - elapsed)
            if status.get("paused"):
                bot_data["queue_paused"] = True
                bot_data["queue_timer_remaining"] = remaining
            else:
                _schedule_queue_timer(context, chat_id, user_id, delay=remaining)
        await _persist_queue_state(context, user_id, chat_id)
        await application.bot.send_message(
            chat_id, "Restauré la cola; la canción actual sigue en el ESP32."
        )
        _start_device_event_monitor(application)
        return

    try:
        await _start_queued_song(
            current,
            context,
            user_id,
            chat_id,
            status_note=(
                "Reanudé la cola guardada; la canción actual comienza desde "
                "el principio."
            ),
        )
    except (
        PlayerNotConfiguredError,
        requests.exceptions.RequestException,
        RuntimeError,
        ValueError,
    ):
        logger.exception("No se pudo reanudar la canción guardada")
        await application.bot.send_message(
            chat_id,
            "Restauré la cola, pero no pude iniciar la canción actual. "
            "La cola quedó guardada.",
        )
    _start_device_event_monitor(application)


def _start_device_event_monitor(application: Application) -> None:
    if "device_event_task" not in application.bot_data:
        application.bot_data["device_event_task"] = asyncio.create_task(
            _monitor_device_events(application)
        )


async def _monitor_device_events(application: Application) -> None:
    bot_data = application.bot_data
    last_error_log = 0.0
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(0.75)
        settings: Settings = bot_data["player_settings"]
        queue: PlaybackQueue = bot_data["playback_queue"]
        if not settings.player_enabled or queue.current is None:
            continue
        expected_song = queue.current
        user_id = bot_data.get("queue_user_id")
        chat_id = bot_data.get("queue_chat_id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            continue
        device: DeviceClient = bot_data["device"]
        try:
            status = await asyncio.to_thread(device.poll_events)
        except (
            PlayerNotConfiguredError,
            requests.exceptions.RequestException,
            ValueError,
            RuntimeError,
        ):
            if loop.time() - last_error_log >= 30:
                logger.exception("No se pudo consultar el estado de fin de pista del ESP32")
                last_error_log = loop.time()
            continue
        if status.get("track_finished_events", 0):
            context = SimpleNamespace(application=application, bot=application.bot)
            await _advance_playlist(
                context,
                chat_id,
                user_id,
                automatic=True,
                expected_song=expected_song,
            )


async def _stop_background_tasks(application: Application) -> None:
    task = application.bot_data.pop("device_event_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_application(settings: Settings) -> Application:
    database = SongDatabase(settings)
    ai = AIService(settings)
    web = WebSearch(settings)
    recommender = Recommender(database, ai, web)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_restore_application_state)
        .post_shutdown(_stop_background_tasks)
        .build()
    )
    application.bot_data["recommender"] = recommender
    application.bot_data["ai"] = ai
    application.bot_data["web"] = web
    application.bot_data["device"] = DeviceClient(settings)
    application.bot_data["player_settings"] = settings
    application.bot_data["database"] = database
    application.bot_data["playback_history"] = PlaybackHistory(
        database.client[settings.mongo_db]["playback_sessions"]
    )
    application.bot_data["playback_state"] = PlaybackStateStore(
        database.client[settings.mongo_db]["bot_state"]
    )
    application.bot_data["playback_queue"] = PlaybackQueue()
    application.bot_data["queue_paused"] = False
    application.bot_data["autoplay_users"] = {}
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_chat))
    application.add_handler(CommandHandler("buscar", search_command))
    application.add_handler(CommandHandler("pausar", pause_command))
    application.add_handler(CommandHandler("reanudar", resume_command))
    application.add_handler(CommandHandler(["detener", "parar"], stop_command))
    application.add_handler(CommandHandler("volumen", volume_command))
    application.add_handler(CommandHandler("autoplay", autoplay_command))
    application.add_handler(CommandHandler("reportes", reports_command))
    application.add_handler(CommandHandler(["skip", "siguiente"], skip_command))
    application.add_handler(CommandHandler("cola", queue_command))
    application.add_handler(CommandHandler("loop", loop_command))
    application.add_handler(
        CallbackQueryHandler(play_song_callback, pattern=r"^play:[0-9a-f]{16}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            play_album_suggestion_callback,
            pattern=r"^playalbum:[0-9a-f]{16}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            dismiss_play_suggestion, pattern=r"^dismiss:[0-9a-f]{16}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(player_control_callback, pattern=r"^control:(pause|resume)$")
    )
    application.add_handler(
        CallbackQueryHandler(skip_queue_callback, pattern=r"^queue:skip$")
    )
    application.add_handler(
        CallbackQueryHandler(show_queue_callback, pattern=r"^queue:show$")
    )
    application.add_handler(
        CallbackQueryHandler(toggle_loop_callback, pattern=r"^queue:loop$")
    )
    application.add_handler(
        CallbackQueryHandler(
            report_period_callback, pattern=r"^report:(daily|weekly)$"
        )
    )
    application.add_handler(MessageHandler(filters.VOICE, voice_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application
