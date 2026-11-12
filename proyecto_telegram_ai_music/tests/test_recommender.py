import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests
from pymongo.errors import PyMongoError

from app.ai_service import AIService, _is_quota_error
from app.bot import (
    _CHAT_WEB_LOOKUP_REQUEST,
    _LIBRARY_AVAILABILITY_QUESTION,
    _MUSIC_WANT_REQUEST,
    _PLAYBACK_REQUEST,
    _CORRECTION_REQUEST,
    _ALTERNATIVE_REQUEST,
    _REPEAT_PLAYBACK_REQUEST,
    _RECOMMENDATION_REQUEST,
    _REPORT_CONTEXT_REQUEST,
    _REPORT_REQUEST,
    _format_playback_report,
    _device_failure_message,
    _send_player_control,
    report_period_callback,
    send_playback_report,
    _try_natural_language_report,
    _needs_chat_web_lookup,
    reports_command,
    volume_command,
    autoplay_command,
    voice_message,
    chat_message,
    recommend_text,
    persona_queue_request,
    keep_queue_matching_request,
    help_command,
    process_text_message,
)
from app.config import Settings
from app.database import SongDatabase
from app.device_client import DeviceClient
from app.playback_history import (
    PlaybackHistory,
    build_playback_report,
    report_period_bounds,
)
from app.recommender import (
    Recommender,
    _best_matching_artist,
    build_mongo_query,
    requested_song_count,
)
from app.sd_path import parse_sd_path
from scripts.index_sd_card import index_sd_card
from scripts.organize_sd_library import _infer_deftones_title
from esp32_firmware.protocol import build_frame, extract_finished_events
from scripts.seed_alternative_rock import (
    ALTERNATIVE_ROCK,
    build_documents as build_alternative_rock_documents,
)
from scripts.seed_80s import build_documents


class AIQuotaDetectionTests(unittest.TestCase):
    def test_detects_openai_insufficient_quota_code(self):
        error = Exception("API request failed")
        error.code = "insufficient_quota"
        self.assertTrue(_is_quota_error(error))

    def test_detects_credit_exhaustion_message(self):
        self.assertTrue(_is_quota_error(Exception("You have no credits remaining")))

    def test_does_not_mistake_temporary_rate_limit_for_empty_credits(self):
        self.assertFalse(_is_quota_error(Exception("Too many requests")))

    def test_transcribes_voice_in_spanish_with_configured_model(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_transcription_model="whisper-test")
        create = Mock(return_value=SimpleNamespace(text="  hits de los 80  "))
        ai.client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=create)
            )
        )

        transcript = ai.transcribe_audio(b"ogg-data")

        self.assertEqual(transcript, "hits de los 80")
        create.assert_called_once_with(
            model="whisper-test",
            file=("voice.ogg", b"ogg-data", "audio/ogg"),
            language="es",
        )

    def test_rejects_empty_audio_and_empty_transcriptions(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_transcription_model="whisper-test")
        create = Mock(return_value=SimpleNamespace(text="  "))
        ai.client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=create)
            )
        )

        with self.assertRaises(ValueError):
            ai.transcribe_audio(b"")
        with self.assertRaises(ValueError):
            ai.transcribe_audio(b"ogg-data")


class MessageRoutingTests(unittest.TestCase):
    def test_title_request_is_routed_to_library_search(self):
        message = "quiero purple rain"
        self.assertTrue(_MUSIC_WANT_REQUEST.search(message))
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_filter_extraction_receives_recent_context_for_playback_references(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_top_results=5)
        ai._complete = Mock(
            return_value=(
                '{"artist":"Queen","title":"Bohemian Rhapsody","genre":null,'
                '"decade":null,"mood":null,"keywords":[],"limit":1}'
            )
        )
        history = [
            {"role": "user", "content": "Vamos con Bohemian Rhapsody"},
            {"role": "assistant", "content": "Una gran canción de Queen."},
        ]
        result = ai.extract_filters(
            "me gustaría que me reproduzcas esa canción", history
        )
        prompt = ai._complete.call_args.args[1]
        self.assertIn("Bohemian Rhapsody", prompt)
        self.assertEqual(result["title"], "Bohemian Rhapsody")

    def test_chat_extracts_only_an_explicit_song_reference(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_model="test")
        ai.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        return_value=SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    message=SimpleNamespace(
                                        content=(
                                            '{"reply":"Es un tema muy conocido.",'
                                            '"mentioned_song_title":"Purple Rain",'
                                            '"mentioned_song_artist":"Prince"}'
                                        )
                                    )
                                )
                            ]
                        )
                    )
                )
            )
        )

        reply, song = ai.chat_music("¿Qué tiene de especial Purple Rain?", [])
        self.assertEqual(reply, "Es un tema muy conocido.")
        self.assertEqual(song, {"title": "Purple Rain", "artist": "Prince"})

    def test_chat_extracts_an_explicit_album_reference(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_model="test")
        ai.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        return_value=SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    message=SimpleNamespace(
                                        content=(
                                            '{"reply":"Es un álbum clásico.",'
                                            '"mentioned_song_title":null,'
                                            '"mentioned_song_artist":null,'
                                            '"mentioned_album":"Purple Rain",'
                                            '"mentioned_album_artist":"Prince"}'
                                        )
                                    )
                                )
                            ]
                        )
                    )
                )
            )
        )

        reply, reference = ai.chat_music("Hablemos del álbum Purple Rain", [])

        self.assertEqual(reply, "Es un álbum clásico.")
        self.assertEqual(
            reference,
            {"album": "Purple Rain", "album_artist": "Prince"},
        )

    def test_chat_prompt_handles_a_local_library_miss_as_a_conversation(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_model="test")
        create = Mock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"reply":"Esos temas son de distintos álbumes.",'
                                '"mentioned_song_title":null,'
                                '"mentioned_song_artist":null}'
                            )
                        )
                    )
                ]
            )
        )
        ai.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        ai.chat_music(
            "El álbum que tiene Error, Genesis y Urantia",
            [],
            "Resultados de búsqueda",
            library_search_miss=True,
        )

        messages = create.call_args.kwargs["messages"]
        system_text = " ".join(
            item["content"] for item in messages if item["role"] == "system"
        )
        self.assertIn("No respondas únicamente que no hay canciones", system_text)
        self.assertIn("Resultados de búsqueda", messages[-1]["content"])
        system_text = " ".join(
            item["content"] for item in messages if item["role"] == "system"
        )
        self.assertIn("no digas que el sistema no puede reproducir música", system_text)

    def test_music_question_is_routed_to_conversation(self):
        message = "¿Qué hace especial a Prince?"
        self.assertIsNone(_MUSIC_WANT_REQUEST.search(message))
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_internet_access_question_is_routed_to_conversation(self):
        message = "tienes acceso a internet?"
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_song_availability_question_still_routes_to_library(self):
        message = "tienes canciones de Queen?"
        self.assertTrue(_RECOMMENDATION_REQUEST.search(message))

    def test_playback_request_is_not_routed_as_a_recommendation(self):
        message = "puedes ponerme esa canción en el reproductor?"
        self.assertTrue(_PLAYBACK_REQUEST.search(message))

    def test_natural_request_to_listen_to_referenced_song_is_playback(self):
        for message in (
            "No la tenés para que yo la escuche? La quiero escuchar.",
            "La quiero escuchar",
            "Quiero escuchar esa canción",
        ):
            with self.subTest(message=message):
                self.assertTrue(_PLAYBACK_REQUEST.search(message))

    def test_explicit_correction_is_detected_as_a_new_library_search(self):
        self.assertTrue(_CORRECTION_REQUEST.search("No Voy en Tren quise decir"))
        self.assertTrue(_CORRECTION_REQUEST.search("Quise decir No Voy en Tren"))
        self.assertFalse(_CORRECTION_REQUEST.search("Quiero escuchar No Voy en Tren"))

    def test_alternative_request_detects_rejected_recommendation(self):
        self.assertTrue(_ALTERNATIVE_REQUEST.search("No, esa no, poneme otra"))
        self.assertTrue(_ALTERNATIVE_REQUEST.search("dame otra canción"))
        self.assertFalse(
            _ALTERNATIVE_REQUEST.search(
                "Carolina Durante, otro tema que no sea Joderse la vida"
            )
        )
        self.assertFalse(_ALTERNATIVE_REQUEST.search("quiero volver a esa canción"))

    def test_library_availability_question_stays_in_conversation(self):
        message = "no la tienes para escucharla?"
        self.assertTrue(_LIBRARY_AVAILABILITY_QUESTION.search(message))

    def test_song_recommendation_is_not_mistaken_for_playback(self):
        message = "poneme hits movidos de los 80"
        self.assertIsNone(_PLAYBACK_REQUEST.search(message))
        self.assertTrue(_RECOMMENDATION_REQUEST.search(message))

    def test_greeting_does_not_trigger_web_search(self):
        self.assertFalse(_needs_chat_web_lookup("hola"))

    def test_current_music_question_can_trigger_web_search(self):
        self.assertTrue(_needs_chat_web_lookup("¿Cuándo es la próxima gira de Prince?"))

    def test_static_music_question_does_not_trigger_web_search(self):
        self.assertFalse(_needs_chat_web_lookup("¿Qué hace especial a Prince?"))

    def test_natural_language_playback_report_request_is_detected(self):
        self.assertTrue(_REPORT_REQUEST.search("¿Qué escuché esta semana?"))
        self.assertTrue(_REPORT_REQUEST.search("¿Cuánto tiempo escuché hoy?"))
        self.assertTrue(_REPORT_CONTEXT_REQUEST.search("mis canciones más escuchadas"))
        self.assertFalse(_REPORT_REQUEST.search("¿Qué canciones movidas recomiendas?"))

    def test_follow_up_playback_request_is_detected(self):
        self.assertTrue(_PLAYBACK_REQUEST.search("me gustaría que me la reproduzcas"))

    def test_repeat_request_is_not_sent_as_an_unqualified_search(self):
        self.assertTrue(_REPEAT_PLAYBACK_REQUEST.search("quiero escucharla otra vez"))


class BuildMongoQueryTests(unittest.TestCase):
    def test_builds_artist_and_decade_filters(self):
        query = build_mongo_query(
            {
                "artist": "Queen",
                "title": None,
                "decade": 1980,
                "genre": None,
                "mood": None,
                "keywords": [],
            }
        )
        self.assertEqual(
            query,
            {
                "$and": [
                    {"artist": {"$regex": "Queen", "$options": "i"}},
                    {
                        "$or": [
                            {"year": {"$gte": 1980, "$lt": 1990}},
                            {"decade": 1980},
                        ]
                    },
                ]
            },
        )

    def test_escapes_regex_metacharacters(self):
        query = build_mongo_query(
            {
                "artist": "A+B",
                "title": None,
                "genre": None,
                "mood": None,
                "decade": None,
                "keywords": [],
            }
        )
        self.assertEqual(query["artist"]["$regex"], r"A\+B")

    def test_empty_filters_return_all_songs(self):
        self.assertEqual(
            build_mongo_query(
                {
                    "artist": None,
                    "title": None,
                    "genre": None,
                    "mood": None,
                    "decade": None,
                    "keywords": [],
                }
            ),
            {},
        )


class VoiceMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribes_voice_and_routes_transcript_through_text_flow(self):
        message = Mock()
        message.voice.file_id = "telegram-file"
        message.voice.file_size = 100
        message.chat.send_action = AsyncMock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message)
        audio_file = Mock()
        audio_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"ogg-data")
        )
        context = Mock()
        context.bot.get_file = AsyncMock(return_value=audio_file)
        ai = Mock()
        ai.transcribe_audio.return_value = "buenos hits de los 80"
        context.application.bot_data = {"ai": ai}

        with patch("app.bot.process_text_message", new_callable=AsyncMock) as route:
            await voice_message(update, context)

        ai.transcribe_audio.assert_called_once_with(b"ogg-data", "voice.ogg")
        message.reply_text.assert_awaited_once_with(
            "Entendí: «buenos hits de los 80»"
        )
        route.assert_awaited_once_with(
            update, context, "buenos hits de los 80"
        )

    async def test_rejects_voice_larger_than_telegram_download_limit(self):
        message = Mock()
        message.voice.file_id = "telegram-file"
        message.voice.file_size = 21 * 1024 * 1024
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message)
        context = Mock()
        context.bot.get_file = AsyncMock()

        await voice_message(update, context)

        message.reply_text.assert_awaited_once()
        context.bot.get_file.assert_not_awaited()


class NoLocalMatchFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_groups_examples_and_commands_by_task(self):
        update = Mock()
        update.message.reply_text = AsyncMock()

        await help_command(update, None)

        help_text = update.message.reply_text.await_args.args[0]
        for section in (
            "¿QUÉ PUEDES PEDIR?",
            "REPRODUCCIÓN",
            "COLA Y ÁLBUMES",
            "OTROS COMANDOS",
            "/detener o /parar",
            "/autoplay on|off",
            "deja solo las de Queen",
            "/reportes diario o /reportes semanal",
        ):
            with self.subTest(section=section):
                self.assertIn(section, help_text)

    async def test_persona_queue_cleanup_request_routes_to_filter_handler(self):
        message = "borra todas las canciones de la cola menos las de Persona"
        with (
            patch("app.bot._try_natural_language_report", new_callable=AsyncMock, return_value=False),
            patch("app.bot.keep_queue_matching_request", new_callable=AsyncMock) as keep,
        ):
            await process_text_message(None, None, message)

        keep.assert_awaited_once_with(None, None, "Persona")

    async def test_queue_cleanup_routes_arbitrary_filter(self):
        message = "borra todas las canciones de la cola excepto las de Queen"
        with (
            patch("app.bot._try_natural_language_report", new_callable=AsyncMock, return_value=False),
            patch("app.bot.keep_queue_matching_request", new_callable=AsyncMock) as keep,
        ):
            await process_text_message(None, None, message)

        keep.assert_awaited_once_with(None, None, "Queen")

    async def test_queue_cleanup_without_filter_asks_what_to_keep(self):
        update = Mock()
        update.effective_message.reply_text = AsyncMock()
        with patch(
            "app.bot._try_natural_language_report",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await process_text_message(update, None, "limpia la cola y deja solo")

        self.assertIn(
            "¿Qué canciones quieres conservar?",
            update.effective_message.reply_text.await_args.args[0],
        )

    async def test_negative_listening_requests_route_to_stop_not_recommendation(self):
        for message in (
            "No quiero escuchar más música",
            "Borrá toda la cola de reproducción. No para escuchar más música",
        ):
            with self.subTest(message=message):
                with (
                    patch(
                        "app.bot._try_natural_language_report",
                        new_callable=AsyncMock,
                        return_value=False,
                    ),
                    patch(
                        "app.bot.stop_playback_request",
                        new_callable=AsyncMock,
                    ) as stop,
                    patch(
                        "app.bot.recommend_text",
                        new_callable=AsyncMock,
                    ) as recommend,
                ):
                    await process_text_message(None, None, message)

                stop.assert_awaited_once_with(None, None)
                recommend.assert_not_awaited()

    async def test_rejecting_one_song_does_not_stop_playback(self):
        update = Mock()
        context = Mock()
        with (
            patch(
                "app.bot._try_natural_language_report",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.bot.stop_playback_request",
                new_callable=AsyncMock,
            ) as stop,
            patch(
                "app.bot.recommend_text",
                new_callable=AsyncMock,
            ) as recommend,
        ):
            await process_text_message(update, context, "No quiero escuchar Queen")

        stop.assert_not_awaited()
        recommend.assert_awaited_once_with("No quiero escuchar Queen", update, context)

    async def test_clear_queue_request_routes_to_queue_clear_handler(self):
        with (
            patch(
                "app.bot._try_natural_language_report",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.bot.clear_upcoming_queue_request",
                new_callable=AsyncMock,
            ) as clear,
        ):
            await process_text_message(
                None, None, "Borrá toda la cola de reproducción"
            )

        clear.assert_awaited_once_with(None, None)

    async def test_queue_clear_negative_intent_does_not_clear_the_queue(self):
        update = Mock()
        update.effective_message.reply_text = AsyncMock()
        with patch(
            "app.bot.clear_upcoming_queue_request",
            new_callable=AsyncMock,
        ) as clear:
            await process_text_message(update, None, "No borres toda la cola")

        clear.assert_not_awaited()
        self.assertIn(
            "No toqué la cola",
            update.effective_message.reply_text.await_args.args[0],
        )

    async def test_transcribed_stop_request_routes_to_stop_handler(self):
        message = Mock()
        message.voice.file_id = "telegram-file"
        message.voice.file_size = 100
        message.chat.send_action = AsyncMock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message)
        audio_file = Mock()
        audio_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"ogg-data")
        )
        context = Mock()
        context.bot.get_file = AsyncMock(return_value=audio_file)
        context.application.bot_data = {"ai": Mock()}
        context.application.bot_data["ai"].transcribe_audio.return_value = (
            "No quiero escuchar más música"
        )

        with (
            patch(
                "app.bot._try_natural_language_report",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.bot.stop_playback_request",
                new_callable=AsyncMock,
            ) as stop,
        ):
            await voice_message(update, context)

        stop.assert_awaited_once_with(update, context)

    def test_device_failure_explains_unconfirmed_dfplayer_pause(self):
        response = Mock(status_code=502)
        error = requests.exceptions.HTTPError(response=response)

        message = _device_failure_message(error, "pause")

        self.assertIn("DFPlayer no confirmó", message)
        self.assertIn("UART", message)

    async def test_unconfirmed_pause_reports_failure_not_success(self):
        response = Mock(status_code=502)
        device = Mock()
        device.pause.side_effect = requests.exceptions.HTTPError(response=response)
        reply = AsyncMock()
        update = SimpleNamespace(
            callback_query=None,
            effective_message=SimpleNamespace(reply_text=reply),
            effective_chat=SimpleNamespace(id=100),
            effective_user=SimpleNamespace(id=42),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "player_settings": SimpleNamespace(player_enabled=True),
                    "device": device,
                    "playback_queue": Mock(),
                }
            )
        )

        await _send_player_control(update, context, "pause")

        self.assertIn(
            "DFPlayer no confirmó",
            reply.await_args.args[0],
        )
        self.assertNotIn("Pausa enviada", reply.await_args.args[0])


    async def test_queue_jump_request_routes_named_future_song(self):
        update = Mock()
        update.effective_chat.id = 100
        update.effective_user.id = 42
        update.effective_message = Mock()
        context = Mock()
        message = "saltame a la canción Change de la cola"
        with (
            patch("app.bot._try_natural_language_report", new_callable=AsyncMock, return_value=False),
            patch("app.bot.jump_to_queued_song", new_callable=AsyncMock) as jump,
        ):
            await process_text_message(update, context, message)

        jump.assert_awaited_once_with(
            context, "Change", update.effective_message, 100, 42
        )

    async def test_unspecified_queue_jump_asks_for_song_title(self):
        update = Mock()
        update.effective_message.reply_text = AsyncMock()
        context = Mock()
        with patch(
            "app.bot._try_natural_language_report",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await process_text_message(
                update, context, "quiero saltarme a alguna canción siguiente de la cola"
            )

        self.assertIn(
            "Dime el título",
            update.effective_message.reply_text.await_args.args[0],
        )

    async def test_explicit_persona_queue_request_uses_persona_queue_handler(self):
        message = "Quiero que pongas a la cola 25 canciones del videojuego Persona"
        with (
            patch("app.bot._try_natural_language_report", new_callable=AsyncMock, return_value=False),
            patch("app.bot.persona_queue_request", new_callable=AsyncMock) as queue_request,
        ):
            await process_text_message(None, None, message)

        queue_request.assert_awaited_once_with(None, None, message)

    async def test_persona_queue_request_enqueues_requested_number_of_local_tracks(self):
        update = Mock()
        update.effective_user.id = 42
        update.effective_chat.id = 100
        update.effective_message.reply_text = AsyncMock()
        songs = [
            {"_id": str(index), "title": f"Persona Track {index}", "sd_path": "/02/001.mp3"}
            for index in range(30)
        ]
        database = Mock()
        database.find_playable_songs_by_folder.return_value = songs
        context = Mock()
        context.application.bot_data = {
            "database": database,
            "player_settings": SimpleNamespace(player_enabled=True),
        }

        with patch("app.bot._enqueue_album_songs", new_callable=AsyncMock) as enqueue:
            await persona_queue_request(
                update, context, "poné a la cola 25 canciones del videojuego Persona"
            )

        queued_songs = enqueue.await_args.args[0]
        self.assertEqual(len(queued_songs), 25)
        self.assertEqual(queued_songs, songs[:25])
        self.assertEqual(enqueue.await_args.args[1], "la carpeta Persona")

    async def test_autoplay_command_is_off_by_default_and_user_configurable(self):
        update = Mock()
        update.effective_user.id = 42
        update.effective_message.reply_text = AsyncMock()
        user_data = {}
        context = Mock()
        context.application.user_data = {42: user_data}
        context.args = []

        await autoplay_command(update, context)
        self.assertIn("desactivada", update.effective_message.reply_text.await_args.args[0])

        context.args = ["on"]
        await autoplay_command(update, context)
        self.assertTrue(user_data["autoplay_enabled"])
        self.assertIn("activada", update.effective_message.reply_text.await_args.args[0])

        context.args = ["off"]
        await autoplay_command(update, context)
        self.assertFalse(user_data["autoplay_enabled"])

    async def test_autoplay_starts_best_local_song_without_confirmation_button(self):
        update = Mock()
        update.effective_user.id = 42
        update.effective_chat.id = 100
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        update.effective_message = update.message
        songs = [
            {"_id": "unplayable", "title": "First", "artist": "Artist"},
            {
                "_id": "playable",
                "title": "Purple Rain",
                "artist": "Prince",
                "sd_path": "/02/001.mp3",
            },
        ]
        recommender = Mock()
        recommender.recommend.return_value = (songs, False)
        user_data = {"autoplay_enabled": True, "pending_play": {"old": "old-song"}}
        context = Mock()
        context.application.user_data = {42: user_data}
        context.application.bot_data = {
            "recommender": recommender,
            "player_settings": SimpleNamespace(player_enabled=True),
        }

        with (
            patch("app.bot._play_keyboard") as keyboard,
            patch("app.bot._play_song_immediately", new_callable=AsyncMock) as play,
        ):
            await recommend_text("quiero escuchar Purple Rain", update, context)

        keyboard.assert_not_called()
        play.assert_awaited_once_with(songs[1], context, 42, 100, update.message)
        self.assertEqual(user_data["pending_play"], {})
        self.assertIsNone(update.message.reply_text.await_args.kwargs["reply_markup"])

    async def test_autoplay_does_not_start_an_alternative_request(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        recommender = Mock()
        song = {
            "_id": "song-2",
            "title": "Another Song",
            "artist": "Artist",
            "sd_path": "/02/002.mp3",
        }
        recommender.recommend.return_value = ([song], False)
        context = Mock()
        context.application.user_data = {
            42: {
                "autoplay_enabled": True,
                "last_recommendation_request": "poneme canciones",
                "last_recommendation_ids": ["song-1"],
            }
        }
        context.application.bot_data = {
            "recommender": recommender,
            "player_settings": SimpleNamespace(player_enabled=True),
        }

        with (
            patch("app.bot._play_keyboard"),
            patch("app.bot._play_song_immediately", new_callable=AsyncMock) as play,
        ):
            await recommend_text("poneme otra", update, context)

        play.assert_not_awaited()

    async def test_initial_request_for_another_track_is_not_a_follow_up(self):
        update = Mock()
        context = Mock()
        with patch("app.bot.recommend_text", new_callable=AsyncMock) as recommend:
            await process_text_message(
                update,
                context,
                "Carolina Durante, otro tema que no sea Joderse la vida",
            )
        recommend.assert_awaited_once_with(
            "Carolina Durante, otro tema que no sea Joderse la vida",
            update,
            context,
        )

    async def test_alternative_reuses_previous_request_and_excludes_previous_results(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        recommender = Mock()
        recommender.recommend.return_value = (
            [{"_id": "song-2", "title": "Another Song", "artist": "Artist"}],
            False,
        )
        user_data = {
            "music_history": [],
            "last_recommendation_request": "Carolina Durante, otro tema que no sea Joderse la vida",
            "excluded_recommendation_ids": ["song-1"],
            "last_recommendation_ids": ["song-1"],
        }
        context = Mock()
        context.application.user_data = {42: user_data}
        context.application.bot_data = {
            "recommender": recommender,
            "player_settings": SimpleNamespace(player_enabled=False),
        }

        with patch("app.bot._play_keyboard", return_value=None):
            await recommend_text("No, esa no, poneme otra", update, context)

        recommender.recommend.assert_called_once_with(
            "Carolina Durante, otro tema que no sea Joderse la vida",
            [],
            excluded_ids=["song-1"],
        )
        self.assertEqual(user_data["last_recommendation_ids"], ["song-2"])
        self.assertEqual(user_data["excluded_recommendation_ids"], ["song-1", "song-2"])

    async def test_alternative_without_prior_recommendation_asks_for_context(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        context = Mock()
        context.application.user_data = {42: {}}
        context.application.bot_data = {"recommender": Mock()}

        await recommend_text("poneme otra", update, context)

        update.message.reply_text.assert_awaited_once()
        context.application.bot_data["recommender"].recommend.assert_not_called()

    async def test_correction_phrase_routes_back_to_song_search(self):
        update = Mock()
        context = Mock()
        with patch("app.bot.recommend_text", new_callable=AsyncMock) as recommend:
            await process_text_message(
                update, context, "No Voy en Tren quise decir"
            )
        recommend.assert_awaited_once_with(
            "No Voy en Tren quise decir", update, context
        )

    async def test_no_local_songs_falls_back_to_chat(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        recommender = Mock()
        recommender.recommend.return_value = ([], False)
        context = Mock()
        context.application.user_data = {42: {"music_history": []}}
        context.application.bot_data = {"recommender": recommender}

        with patch("app.bot.chat_message", new_callable=AsyncMock) as chat:
            await recommend_text(
                "el álbum con Error, Genesis y Urantia", update, context
            )

        chat.assert_awaited_once_with(
            update,
            context,
            "el álbum con Error, Genesis y Urantia",
            library_search_miss=True,
        )

    async def test_library_miss_uses_web_context_and_discloses_local_limit(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        web = Mock()
        web.search.return_value = "Resultados sobre los álbumes."
        ai = Mock()
        ai.chat_music.return_value = ("Parece que nombras temas de dos álbumes.", None)
        context = Mock()
        context.application.user_data = {42: {"music_history": []}}
        context.application.bot_data = {
            "ai": ai,
            "web": web,
            "player_settings": SimpleNamespace(player_enabled=False),
        }

        await chat_message(
            update,
            context,
            "El álbum que tiene Error, Genesis y Urantia",
            library_search_miss=True,
        )

        web.search.assert_called_once_with("El álbum que tiene Error, Genesis y Urantia")
        ai.chat_music.assert_called_once_with(
            "El álbum que tiene Error, Genesis y Urantia",
            [],
            "Resultados sobre los álbumes.",
            library_search_miss=True,
        )
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Parece que nombras temas de dos álbumes.", reply)
        self.assertIn("No encontré coincidencias en tu biblioteca local", reply)

    async def test_missing_requested_song_explains_player_and_local_availability(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        ai = Mock()
        ai.chat_music.return_value = ("No encontré ese tema en tu biblioteca.", None)
        web = Mock()
        web.search.return_value = ""
        context = Mock()
        context.application.user_data = {42: {"music_history": []}}
        context.application.bot_data = {
            "ai": ai,
            "web": web,
            "player_settings": SimpleNamespace(player_enabled=True),
            "database": Mock(),
        }

        await chat_message(
            update,
            context,
            "La quiero escuchar",
            library_search_miss=True,
        )

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("El reproductor puede reproducir canciones", reply)
        self.assertIn("no encontré este tema en tu biblioteca local", reply)
        ai.chat_music.assert_called_once_with(
            "La quiero escuchar", [], "", library_search_miss=True
        )

    async def test_suggests_song_named_by_assistant_when_user_refers_to_it(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        ai = Mock()
        ai.chat_music.return_value = (
            "¿Te gusta más esta versión?",
            {"title": "Memories of You", "artist": "ATLUS Sound Team"},
        )
        database = Mock()
        database.find_playable_song_by_title_artist.return_value = {
            "_id": "memories-of-you",
            "title": "Memories of You",
            "artist": "ATLUS Sound Team",
            "sd_path": "/02/002.mp3",
        }
        context = Mock()
        context.application.user_data = {
            42: {
                "music_history": [
                    {"role": "assistant", "content": "Memories of You es un tema de Persona 3 Reload."},
                    {"role": "assistant", "content": "¿Prefieres la nueva versión?"},
                ]
            }
        }
        context.application.bot_data = {
            "ai": ai,
            "web": Mock(),
            "player_settings": SimpleNamespace(player_enabled=True),
            "database": database,
        }
        keyboard = object()

        with patch(
            "app.bot._play_context_keyboard", return_value=keyboard
        ) as suggestion:
            await chat_message(
                update,
                context,
                "Me gustaría escuchar esa canción, ¿no la tenés?",
            )

        database.find_playable_song_by_title_artist.assert_called_once_with(
            "Memories of You", "ATLUS Sound Team"
        )
        suggestion.assert_called_once()
        self.assertEqual(update.message.reply_text.await_count, 2)
        self.assertIn(
            "disponible en tu microSD",
            update.message.reply_text.await_args.args[0],
        )
        self.assertIs(update.message.reply_text.await_args.kwargs["reply_markup"], keyboard)

    async def test_does_not_suggest_song_from_unrelated_older_user_message(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        message = "¿Tienes canciones del videojuego Persona?"
        ai = Mock()
        ai.chat_music.return_value = (
            "¿Buscas la música de algún juego de Persona en particular?",
            None,
        )
        database = Mock()
        database.find_playable_song_mentioned_in.return_value = None
        context = Mock()
        context.application.user_data = {
            42: {
                "music_history": [
                    {"role": "user", "content": "Me gustó la canción Fine de Lemon Demon."},
                    {"role": "assistant", "content": "Es una canción de Lemon Demon."},
                ]
            }
        }
        context.application.bot_data = {
            "ai": ai,
            "web": Mock(),
            "player_settings": SimpleNamespace(player_enabled=True),
            "database": database,
        }

        with patch("app.bot._play_context_keyboard") as keyboard:
            await chat_message(update, context, message)

        database.find_playable_song_mentioned_in.assert_called_once_with([message])
        keyboard.assert_not_called()
        update.message.reply_text.assert_awaited_once()

    def test_chat_prompt_recognizes_persona_as_a_game_series_not_assumed_band(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_model="test")
        create = Mock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"reply":"¿De qué juego de Persona buscas música?",'
                                '"mentioned_song_title":null,'
                                '"mentioned_song_artist":null}'
                            )
                        )
                    )
                ]
            )
        )
        ai.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        ai.chat_music("¿Tienes canciones del videojuego Persona?", [])

        system_prompt = create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("no inventes una banda llamada Persona", system_prompt)
        self.assertIn("qué entrega o banda sonora", system_prompt)

    async def test_suggests_local_album_only_when_user_named_the_album(self):
        update = Mock()
        update.effective_user.id = 42
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock()
        album_songs = [
            {"_id": "purple-rain-1", "title": "Let's Go Crazy", "sd_path": "/01/001.mp3"}
        ]
        ai = Mock()
        ai.chat_music.return_value = (
            "Purple Rain marcó una etapa importante.",
            {"album": "Purple Rain", "album_artist": "Prince"},
        )
        database = Mock()
        database.find_playable_song_mentioned_in.return_value = None
        database.find_playable_songs_by_album.return_value = album_songs
        context = Mock()
        context.application.user_data = {
            42: {
                "music_history": [
                    {"role": "user", "content": "Qué te parece el álbum Purple Rain?"}
                ]
            }
        }
        context.application.bot_data = {
            "ai": ai,
            "web": Mock(),
            "player_settings": SimpleNamespace(player_enabled=True),
            "database": database,
        }

        with patch("app.bot._play_context_keyboard", return_value=object()) as keyboard:
            await chat_message(
                update, context, "Qué te parece el álbum Purple Rain?"
            )

        database.find_playable_songs_by_album.assert_called_once_with(
            "Purple Rain", "Prince"
        )
        keyboard.assert_called_once_with(
            None, album_songs, "Purple Rain", context, 42
        )
        self.assertIn("el álbum", update.message.reply_text.await_args.args[0])


class RecommenderTests(unittest.TestCase):
    def test_requested_song_count_reads_explicit_quantities_and_caps_them(self):
        self.assertEqual(
            requested_song_count("poné a la cola 25 canciones de Persona"), 25
        )
        self.assertEqual(requested_song_count("dame 3 temas de rock"), 3)
        self.assertIsNone(requested_song_count("poné canciones de Persona"))
        self.assertEqual(
            requested_song_count("quiero 250 pistas"), 100
        )

    def test_persona_request_searches_only_playable_songs_in_persona_folder(self):
        songs = [
            {
                "_id": "persona-1",
                "title": "School Days",
                "artist": "ATLUS GAME MUSIC",
                "sd_path": "/02/001.mp3",
                "dfplayer_folder": 2,
            },
            {
                "_id": "persona-2",
                "title": "Memories of You",
                "artist": "ATLUS Sound Team",
                "sd_path": "/02/002.mp3",
                "dfplayer_folder": 2,
            },
        ]
        database = Mock()
        database.find_playable_songs_by_folder.return_value = songs
        ai = Mock()
        ai.extract_filters.return_value = {"limit": 5}
        ai.rank_songs.return_value = ["persona-2", "persona-1"]
        web = Mock()
        web.search.return_value = ""

        results, _ = Recommender(database, ai, web).recommend(
            "poneme a la cola 25 canciones del videojuego Persona"
        )

        database.find_playable_songs_by_folder.assert_called_once_with(2)
        database.find_candidates.assert_not_called()
        self.assertEqual(ai.extract_filters.return_value["limit"], 25)
        self.assertEqual(
            [song["_id"] for song in results], ["persona-2", "persona-1"]
        )

    def test_excludes_previously_recommended_song_ids_before_ranking(self):
        songs = [
            {"_id": "song-1", "artist": "Artist", "title": "First"},
            {"_id": "song-2", "artist": "Artist", "title": "Second"},
        ]

        class FakeDatabase:
            def find_candidates(self, query, limit):
                return songs

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {"limit": 5}

            def rank_songs(self, request_text, candidates, web_context):
                self.candidate_ids = [str(song["_id"]) for song in candidates]
                return ["song-1", "song-2"]

        class FakeWeb:
            def search(self, query):
                return ""

        ai = FakeAI()
        results, _ = Recommender(FakeDatabase(), ai, FakeWeb()).recommend(
            "another song", excluded_ids=["song-1"]
        )

        self.assertEqual([song["_id"] for song in results], ["song-2"])
        self.assertEqual(ai.candidate_ids, ["song-2"])

    def test_correction_searches_corrected_title_before_model_filters(self):
        corrected_song = {
            "_id": "no-voy-en-tren",
            "title": "No Voy en Tren",
            "artist": "Charly García",
        }

        class FakeDatabase:
            def __init__(self):
                self.queries = []

            def find_candidates(self, query, limit):
                self.queries.append(query)
                if query.get("title", {}).get("$regex") == "No\\ Voy\\ en\\ Tren":
                    return [corrected_song]
                return []

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {
                    "artist": "Charly García",
                    "title": "No Voy a Enterarme",
                    "genre": None,
                    "decade": None,
                    "mood": None,
                    "keywords": [],
                    "limit": 5,
                }

            def rank_songs(self, request_text, candidates, web_context):
                return ["no-voy-en-tren"]

        class FakeWeb:
            def search(self, query):
                return ""

        database = FakeDatabase()
        songs, _ = Recommender(database, FakeAI(), FakeWeb()).recommend(
            "No Voy en Tren quise decir"
        )

        self.assertEqual(songs, [corrected_song])
        self.assertEqual(
            database.queries[0],
            {"title": {"$regex": "No\\ Voy\\ en\\ Tren", "$options": "i"}},
        )

    def test_corrects_close_voice_transcription_of_artist_name(self):
        songs = [
            {"_id": "ch1", "artist": "Charly García", "title": "Los Dinosaurios"},
            {"_id": "ch2", "artist": "Charly García", "title": "Cuchillo"},
            {"_id": "other", "artist": "Charlie Puth", "title": "Attention"},
        ]

        self.assertEqual(
            _best_matching_artist("Charlie García", songs), "Charly García"
        )

    def test_fuzzy_artist_matching_recommends_indexed_songs_after_typo(self):
        songs = [
            {"_id": "ch1", "artist": "Charly García", "title": "Los Dinosaurios"},
            {"_id": "ch2", "artist": "Charly García", "title": "Cuchillo"},
            {"_id": "other", "artist": "Charlie Puth", "title": "Attention"},
        ]

        class FakeDatabase:
            def find_candidates(self, query, limit):
                requested_artist = query.get("artist", {}).get("$regex")
                if requested_artist:
                    return [
                        song for song in songs
                        if requested_artist.casefold() in song["artist"].casefold()
                    ]
                if "title" in query:
                    return []
                return songs

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {
                    "artist": "Charlie García",
                    "title": None,
                    "genre": None,
                    "decade": None,
                    "mood": None,
                    "keywords": [],
                    "limit": 5,
                }

            def rank_songs(self, request_text, candidates, web_context):
                self.candidate_ids = [song["_id"] for song in candidates]
                return ["ch2", "ch1"]

        class FakeWeb:
            def search(self, query):
                return ""

        ai = FakeAI()
        recommendations, _ = Recommender(
            FakeDatabase(), ai, FakeWeb()
        ).recommend("Quiero música de Charlie García")

        self.assertEqual([song["_id"] for song in recommendations], ["ch2", "ch1"])
        self.assertEqual(ai.candidate_ids, ["ch1", "ch2"])

    def test_only_returns_songs_from_database_candidates(self):
        songs = [
            {"_id": "local-1", "title": "Local song 1"},
            {"_id": "local-2", "title": "Local song 2"},
        ]

        class FakeDatabase:
            def find_candidates(self, query, limit):
                return songs

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {"limit": 2}

            def rank_songs(self, request_text, candidates, web_context):
                return ["not-in-library", "local-2"]

        class FakeWeb:
            def search(self, query):
                return ""

        results, web_failed = Recommender(
            FakeDatabase(), FakeAI(), FakeWeb()
        ).recommend("algo")
        self.assertEqual([song["_id"] for song in results], ["local-2", "local-1"])
        self.assertFalse(web_failed)

    def test_deduplicates_same_track_by_title_and_artist(self):
        songs = [
            {"_id": "first", "title": "Purple Rain", "artist": "Prince"},
            {"_id": "duplicate", "title": "purple rain ", "artist": " prince "},
        ]

        class FakeDatabase:
            def find_candidates(self, query, limit):
                return songs

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {"limit": 5}

            def rank_songs(self, request_text, candidates, web_context):
                self.candidate_count = len(candidates)
                return ["first"]

        class FakeWeb:
            def search(self, query):
                return ""

        ai = FakeAI()
        results, _ = Recommender(FakeDatabase(), ai, FakeWeb()).recommend("Purple Rain")
        self.assertEqual(len(results), 1)
        self.assertEqual(ai.candidate_count, 1)

    def test_title_search_finds_song_after_strict_filters_miss(self):
        purple_rain = {
            "_id": "purple-rain",
            "title": "Purple Rain",
            "artist": "Prince",
        }

        class FakeDatabase:
            def find_candidates(self, query, limit):
                pattern = query.get("title", {}).get("$regex")
                if pattern and re.fullmatch(pattern, "Purple Rain", re.IGNORECASE):
                    return [purple_rain]
                return []

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                self.history = history
                return {
                    "artist": None,
                    "title": None,
                    "genre": "alternative rock",
                    "decade": 1990,
                    "mood": None,
                    "keywords": [],
                    "limit": 1,
                }

            def rank_songs(self, request_text, candidates, web_context):
                return ["purple-rain"]

        class FakeWeb:
            def search(self, query):
                return ""

        songs, _ = Recommender(FakeDatabase(), FakeAI(), FakeWeb()).recommend(
            "quiero Purple Rain"
        )
        self.assertEqual(songs, [purple_rain])


class SDPathTests(unittest.TestCase):
    def test_parses_valid_dfplayer_sd_path(self):
        self.assertEqual(parse_sd_path("/01/003.mp3"), (1, 3))

    def test_rejects_paths_outside_the_dfplayer_card_layout(self):
        for path in ("../01/003.mp3", "/100/003.mp3", "/01/256.mp3", "/MP3/song.mp3"):
            with self.subTest(path=path):
                self.assertIsNone(parse_sd_path(path))

    def test_chat_song_lookup_requires_exact_names_and_a_valid_sd_path(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "invalid-path",
                    "title": "Fine",
                    "artist": "Lemon Demon",
                    "sd_path": None,
                },
                {
                    "_id": "playable",
                    "title": "Fine",
                    "artist": "Lemon Demon",
                    "sd_path": "/01/002.mp3",
                },
            ]
        )

        result = database.find_playable_song_by_title_artist(
            "Fine", "Lemon Demon"
        )

        self.assertEqual(result["_id"], "playable")
        query = database.find_candidates.call_args.args[0]
        self.assertEqual(query["title"]["$regex"], r"^Fine$")
        self.assertEqual(query["artist"]["$regex"], r"^Lemon\ Demon$")

    def test_chat_song_lookup_matches_title_and_artist_across_user_messages(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "fine",
                    "title": "Fine",
                    "artist": "Lemon Demon",
                    "sd_path": "/01/002.mp3",
                }
            ]
        )

        result = database.find_playable_song_mentioned_in(
            ["Me gusta mucho Lemon Demon", "Sii, Fine"]
        )

        self.assertEqual(result["_id"], "fine")
        database.find_candidates.assert_called_once_with(
            {"sd_path": {"$exists": True, "$nin": [None, ""]}},
            limit=500,
        )

    def test_chat_song_lookup_suggests_distinctive_multiword_title_without_artist(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "school-days",
                    "title": "School Days",
                    "artist": "ATLUS GAME MUSIC",
                    "sd_path": "/01/003.mp3",
                }
            ]
        )

        result = database.find_playable_song_mentioned_in(
            [
                "Te gusta la música del videojuego persona?",
                "Tenés alguna de persona 1?",
                "Me gusta school days",
            ]
        )

        self.assertEqual(result["_id"], "school-days")

    def test_chat_song_lookup_does_not_match_common_single_word_in_sentence(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "normal",
                    "title": "Normal",
                    "artist": "Carolina Durante",
                    "sd_path": "/03/001.mp3",
                }
            ]
        )

        result = database.find_playable_song_mentioned_in(
            ["Estoy hablando de Persona 3 Reload, diferente a Persona 3 normal."]
        )

        self.assertIsNone(result)

    def test_chat_song_lookup_requires_artist_for_single_word_song_title(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "normal",
                    "title": "Normal",
                    "artist": "Carolina Durante",
                    "sd_path": "/03/001.mp3",
                }
            ]
        )

        result = database.find_playable_song_mentioned_in(
            ["Me gusta Normal de Carolina Durante"]
        )

        self.assertEqual(result["_id"], "normal")

    def test_chat_song_lookup_requires_artist_to_disambiguate_duplicate_titles(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "school-days-atlus",
                    "title": "School Days",
                    "artist": "ATLUS GAME MUSIC",
                    "sd_path": "/01/003.mp3",
                },
                {
                    "_id": "school-days-other",
                    "title": "School Days",
                    "artist": "Other Artist",
                    "sd_path": "/02/001.mp3",
                },
            ]
        )

        without_artist = database.find_playable_song_mentioned_in(
            ["Me gusta School Days"]
        )
        with_artist = database.find_playable_song_mentioned_in(
            ["Me gusta School Days de ATLUS GAME MUSIC"]
        )

        self.assertIsNone(without_artist)
        self.assertEqual(with_artist["_id"], "school-days-atlus")

    def test_device_client_sends_only_a_valid_sd_path_to_esp32(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        response = Mock()
        response.json.return_value = {"accepted": True}
        with patch("app.device_client.requests.post", return_value=response) as post:
            DeviceClient(settings).play("/01/003.mp3")
        post.assert_called_once_with(
            "http://192.168.1.50:8080/api/play",
            json={"sd_path": "/01/003.mp3"},
            headers={"Authorization": f"Bearer {'a' * 24}"},
            timeout=3,
        )

    def test_device_client_rejects_invalid_sd_path_without_network_request(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        with patch("app.device_client.requests.post") as post:
            with self.assertRaises(ValueError):
                DeviceClient(settings).play("../../secret.mp3")
        post.assert_not_called()

    def test_device_client_sends_pause_and_resume_commands(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        response = Mock()
        response.json.return_value = {"accepted": True}
        with patch("app.device_client.requests.post", return_value=response) as post:
            device = DeviceClient(settings)
            device.pause()
            device.resume()
            device.stop()

        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                "http://192.168.1.50:8080/api/pause",
                "http://192.168.1.50:8080/api/resume",
                "http://192.168.1.50:8080/api/stop",
            ],
        )
        self.assertEqual(
            [call.kwargs["json"] for call in post.call_args_list],
            [{}, {}, {}],
        )

    def test_device_client_polls_authenticated_dfplayer_events(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        response = Mock()
        response.json.return_value = {
            "accepted": True,
            "track_finished_events": 1,
        }

        with patch("app.device_client.requests.get", return_value=response) as get:
            status = DeviceClient(settings).poll_events()

        get.assert_called_once_with(
            "http://192.168.1.50:8080/api/events",
            headers={"Authorization": f"Bearer {'a' * 24}"},
            timeout=2,
        )
        self.assertEqual(status["track_finished_events"], 1)

    def test_device_client_sends_and_validates_live_volume(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        response = Mock()
        response.json.return_value = {"accepted": True, "volume": 28}
        with patch("app.device_client.requests.post", return_value=response) as post:
            DeviceClient(settings).set_volume(28)
        post.assert_called_once_with(
            "http://192.168.1.50:8080/api/volume",
            json={"volume": 28},
            headers={"Authorization": f"Bearer {'a' * 24}"},
            timeout=3,
        )

        with patch("app.device_client.requests.post") as post:
            with self.assertRaises(ValueError):
                DeviceClient(settings).set_volume(31)
        post.assert_not_called()

    def test_sd_indexer_adds_route_to_matching_mongodb_song(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "003.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = {"_id": "existing-song"}
            tags = {
                "title": ["Purple Rain"],
                "artist": ["Prince"],
                "album": ["Purple Rain"],
                "date": ["1984"],
                "genre": ["Rock"],
                "tracknumber": ["3/9"],
                "discnumber": ["1/1"],
            }
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(
                    tags=tags, info=SimpleNamespace(length=227.2)
                ),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        update = collection.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["sd_path"], "/01/003.mp3")
        self.assertEqual(update["dfplayer_folder"], 1)
        self.assertEqual(update["dfplayer_track"], 3)
        self.assertEqual(update["album_track_number"], 3)
        self.assertEqual(update["album_disc_number"], 1)
        self.assertEqual(update["year"], 1984)
        self.assertEqual(update["duration_sec"], 228)
        self.assertEqual(collection.update_one.call_args.args[0], {"_id": "existing-song"})

    def test_sd_indexer_adds_route_to_duplicate_matching_songs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = None
            collection.update_many.return_value.matched_count = 2
            tags = {"title": ["Purple Rain"], "artist": ["Prince"]}
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        query = collection.update_many.call_args.args[0]
        self.assertEqual(query["title"]["$regex"], r"^Purple\ Rain$")
        self.assertEqual(query["artist"]["$regex"], r"^Prince$")
        update = collection.update_many.call_args.args[1]["$set"]
        self.assertEqual(update["sd_path"], "/01/001.mp3")
        collection.update_one.assert_not_called()

    def test_sd_indexer_unsets_unreliable_1970_year(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = {"_id": "existing-song"}
            tags = {
                "title": ["Old Song"],
                "artist": ["Example Artist"],
                "date": ["1970"],
            }
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                index_sd_card(root, collection)

        update = collection.update_one.call_args_list[0].args[1]
        self.assertNotIn("year", update["$set"])
        self.assertEqual(
            update["$unset"],
            {"album_track_number": "", "album_disc_number": ""},
        )
        self.assertEqual(
            collection.update_one.call_args_list[1].args[1],
            {"$unset": {"year": "", "decade": ""}},
        )

    def test_sd_indexer_does_not_unset_musicbrainz_years(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = {"_id": "existing-song"}
            collection.update_many.return_value.matched_count = 1
            tags = {
                "title": ["Old Song"],
                "artist": ["Example Artist"],
                "date": ["1970"],
            }
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                index_sd_card(root, collection)

        year_unset_query = collection.update_one.call_args_list[-1].args[0]
        self.assertIn("_id", year_unset_query)
        self.assertIn("$or", year_unset_query)
        self.assertIn(
            {"year_source": {"$exists": False}},
            year_unset_query["$or"],
        )
        self.assertEqual(
            collection.update_one.call_args_list[-1].args[1],
            {"$unset": {"year": "", "decade": ""}},
        )
        duplicate_unset_query = collection.update_many.call_args_list[-1].args[0]
        self.assertIn("$or", duplicate_unset_query)
        self.assertEqual(
            collection.update_many.call_args_list[-1].args[1],
            {"$unset": {"year": "", "decade": ""}},
        )

    def test_sd_indexer_ignores_non_dfplayer_filenames(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid_mp3 = root / "01" / "song.mp3"
            invalid_mp3.parent.mkdir()
            invalid_mp3.write_bytes(b"test")
            collection = Mock()
            with patch("scripts.index_sd_card.MutagenFile") as read_metadata:
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (0, 0))
        read_metadata.assert_not_called()

    def test_sd_indexer_avoids_deterministic_id_collision(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.side_effect = [None, {"_id": "another-song"}]
            collection.update_many.return_value.matched_count = 0
            tags = {"title": ["Purple Rain"], "artist": ["Prince"]}
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        query, update = collection.update_one.call_args.args
        self.assertEqual(query, {"sd_path": "/01/001.mp3"})
        self.assertTrue(collection.update_one.call_args.kwargs["upsert"])
        self.assertEqual(update["$set"]["title"], "Purple Rain")


class VolumeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_volume_command_sends_requested_level_to_device(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply)
        )
        device = SimpleNamespace(set_volume=Mock())
        settings = SimpleNamespace(player_enabled=True)
        context = SimpleNamespace(
            args=["28"],
            application=SimpleNamespace(
                bot_data={"player_settings": settings, "device": device}
            ),
        )

        await volume_command(update, context)

        device.set_volume.assert_called_once_with(28)
        self.assertIn("28/30", reply.await_args.args[0])

    async def test_volume_command_rejects_out_of_range_value(self):
        reply = AsyncMock()
        device = SimpleNamespace(set_volume=Mock())
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply)
        )
        context = SimpleNamespace(
            args=["31"],
            application=SimpleNamespace(
                bot_data={
                    "player_settings": SimpleNamespace(player_enabled=True),
                    "device": device,
                }
            ),
        )

        await volume_command(update, context)

        self.assertIn("0 y 30", reply.await_args.args[0])
        device.set_volume.assert_not_called()


class LibraryOrganizationTests(unittest.TestCase):
    def test_infers_deftones_title_from_filename(self):
        self.assertEqual(
            _infer_deftones_title("Deftones - Be Quiet and Drive - My Music Harbourage.mp3"),
            "Be Quiet and Drive",
        )


class PlaybackHistoryTests(unittest.TestCase):
    def test_daily_and_weekly_bounds_use_argentina_local_calendar(self):
        now = datetime(2026, 11, 11, 2, tzinfo=timezone.utc)

        daily_start, daily_end = report_period_bounds("daily", now)
        weekly_start, weekly_end = report_period_bounds("weekly", now)

        self.assertEqual(daily_start, datetime(2026, 11, 10, 3, tzinfo=timezone.utc))
        self.assertEqual(daily_end, datetime(2026, 11, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(weekly_start, datetime(2026, 11, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(weekly_end, datetime(2026, 11, 16, 3, tzinfo=timezone.utc))

    def test_rejects_unknown_report_period(self):
        with self.assertRaises(ValueError):
            report_period_bounds("monthly")

    def test_report_sums_play_time_minus_pauses_and_ranks_songs(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        first_start = start + timedelta(hours=10)
        sessions = [
            {
                "song_id": "song-a",
                "sd_path": "/01/001.mp3",
                "title": "First Song",
                "artist": "Artist",
                "genre": ["Rock"],
                "duration_sec": 3600,
                "started_at": first_start,
                "ended_at": first_start + timedelta(minutes=60),
                "pause_intervals": [
                    {
                        "started_at": first_start + timedelta(minutes=20),
                        "ended_at": first_start + timedelta(minutes=30),
                    }
                ],
            },
            {
                "song_id": "song-a",
                "sd_path": "/01/001.mp3",
                "title": "First Song",
                "artist": "Artist",
                "genre": ["Rock"],
                "duration_sec": 3600,
                "started_at": first_start + timedelta(hours=2),
                "ended_at": first_start + timedelta(hours=2, minutes=30),
                "pause_intervals": [],
            },
            {
                "song_id": "song-b",
                "sd_path": "/02/001.mp3",
                "title": "Second Song",
                "artist": "Other Artist",
                "genre": ["Pop"],
                "duration_sec": 2400,
                "started_at": first_start + timedelta(hours=3),
                "ended_at": first_start + timedelta(hours=3, minutes=40),
                "pause_intervals": [],
            },
        ]

        report = build_playback_report(sessions, "daily", now)

        self.assertEqual(report["play_count"], 3)
        self.assertEqual(report["total_seconds"], 7200)
        self.assertEqual(report["top_songs"][0]["title"], "First Song")
        self.assertEqual(report["top_songs"][0]["plays"], 2)
        self.assertEqual(report["top_genres"], [("Rock", 4800.0), ("Pop", 2400.0)])

    def test_report_counts_only_the_overlap_with_period(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        report = build_playback_report(
            [
                {
                    "song_id": "crossing",
                    "sd_path": "/01/001.mp3",
                    "title": "Crossing Song",
                    "artist": "Artist",
                    "genre": ["Rock"],
                    "started_at": start - timedelta(minutes=10),
                    "ended_at": start + timedelta(minutes=20),
                    "pause_intervals": [],
                },
                {
                    "song_id": "old",
                    "sd_path": "/01/002.mp3",
                    "title": "Old Song",
                    "artist": "Artist",
                    "started_at": start - timedelta(hours=2),
                    "ended_at": start - timedelta(hours=1),
                    "pause_intervals": [],
                },
            ],
            "daily",
            now,
        )

        self.assertEqual(report["play_count"], 1)
        self.assertEqual(report["total_seconds"], 1200)

    def test_report_excludes_time_after_the_current_open_pause(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        session_start = start - timedelta(minutes=20)
        report = build_playback_report(
            [
                {
                    "song_id": "paused",
                    "sd_path": "/01/001.mp3",
                    "title": "Paused Song",
                    "artist": "Artist",
                    "started_at": session_start,
                    "pause_started_at": start - timedelta(minutes=10),
                    "pause_intervals": [],
                }
            ],
            "daily",
            now,
        )

        self.assertEqual(report["play_count"], 0)
        self.assertEqual(report["total_seconds"], 0)

    def test_report_caps_an_active_session_at_track_duration(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        session_start = start + timedelta(hours=1)
        report = build_playback_report(
            [
                {
                    "song_id": "song-a",
                    "sd_path": "/01/001.mp3",
                    "title": "First Song",
                    "artist": "Artist",
                    "started_at": session_start,
                    "duration_sec": 180,
                    "pause_intervals": [],
                }
            ],
            "daily",
            now,
        )

        self.assertEqual(report["total_seconds"], 180)

    def test_report_caps_each_song_and_genre_lists_to_requested_top_counts(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        sessions = [
            {
                "song_id": str(index),
                "sd_path": f"/01/{index + 1:03}.mp3",
                "title": f"Song {index}",
                "artist": "Artist",
                "genre": [f"Genre {index}"],
                "started_at": start + timedelta(hours=index),
                "ended_at": start + timedelta(hours=index, minutes=1),
                "pause_intervals": [],
            }
            for index in range(8)
        ]

        report = build_playback_report(sessions, "daily", now)

        self.assertEqual(len(report["top_songs"]), 5)
        self.assertEqual(len(report["top_genres"]), 3)

    def test_report_returns_zero_totals_when_no_session_matches(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        report = build_playback_report([], "weekly", now)

        self.assertEqual(report["play_count"], 0)
        self.assertEqual(report["total_seconds"], 0)
        self.assertEqual(report["top_songs"], [])
        self.assertEqual(report["top_genres"], [])

    def test_start_closes_previous_session_and_saves_song_metadata(self):
        collection = Mock()
        collection.find_one.return_value = {"_id": "previous"}
        history = PlaybackHistory(collection)

        history.start(
            {
                "_id": "song-1",
                "title": "New Song",
                "artist": "Artist",
                "album": "Album",
                "genre": ["Rock"],
                "sd_path": "/01/001.mp3",
                "duration_sec": 200,
            },
            user_id=42,
        )

        self.assertEqual(
            collection.update_one.call_args.args[0],
            {"_id": "previous", "ended_at": {"$exists": False}},
        )
        document = collection.insert_one.call_args.args[0]
        self.assertEqual(document["song_id"], "song-1")
        self.assertEqual(document["user_id"], 42)
        self.assertEqual(document["duration_sec"], 200)
        self.assertEqual(document["genre"], ["Rock"])
        self.assertIsNotNone(document["started_at"].tzinfo)

    def test_pause_and_resume_update_the_active_session(self):
        collection = Mock()
        collection.find_one.side_effect = [
            {"_id": "session-1"},
            {
                "_id": "session-1",
                "pause_started_at": datetime(2026, 11, 10, 12, tzinfo=timezone.utc),
            },
        ]
        collection.update_one.return_value.modified_count = 1
        history = PlaybackHistory(collection)

        self.assertTrue(history.pause())
        self.assertIn("pause_started_at", collection.update_one.call_args.args[1]["$set"])
        self.assertTrue(history.resume())
        self.assertIn("$push", collection.update_one.call_args.args[1])
        self.assertEqual(
            collection.update_one.call_args.args[1]["$unset"],
            {"pause_started_at": ""},
        )

    def test_stop_ends_active_history_session_and_closes_pause_interval(self):
        paused_at = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        collection = Mock()
        collection.find_one.return_value = {
            "_id": "session-1",
            "pause_started_at": paused_at,
        }
        collection.update_one.return_value.modified_count = 1
        history = PlaybackHistory(collection)

        self.assertTrue(history.stop())

        query, update = collection.update_one.call_args.args
        self.assertEqual(query["pause_started_at"], paused_at)
        self.assertEqual(update["$set"]["end_reason"], "stopped")
        self.assertIn("ended_at", update["$set"])
        self.assertEqual(
            update["$push"]["pause_intervals"]["started_at"], paused_at
        )
        self.assertIn("ended_at", update["$push"]["pause_intervals"])
        self.assertEqual(update["$unset"], {"pause_started_at": ""})

    def test_stop_returns_false_without_an_active_session(self):
        collection = Mock()
        collection.find_one.return_value = None
        history = PlaybackHistory(collection)

        self.assertFalse(history.stop())
        collection.update_one.assert_not_called()

    def test_pause_and_resume_return_false_without_matching_session(self):
        collection = Mock()
        collection.find_one.return_value = None
        history = PlaybackHistory(collection)

        self.assertFalse(history.pause())
        self.assertFalse(history.resume())
        collection.update_one.assert_not_called()

    def test_start_handles_string_genre_and_missing_duration(self):
        collection = Mock()
        collection.find_one.return_value = None
        history = PlaybackHistory(collection)

        history.start(
            {
                "_id": "song-1",
                "title": "New Song",
                "artist": "Artist",
                "genre": "Pop",
                "sd_path": "/01/001.mp3",
            },
            user_id=42,
        )

        document = collection.insert_one.call_args.args[0]
        self.assertEqual(document["genre"], ["Pop"])
        self.assertIsNone(document["duration_sec"])

    def test_report_queries_only_sessions_overlapping_period(self):
        collection = Mock()
        collection.find.return_value = []
        history = PlaybackHistory(collection)
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)

        history.report("daily", now)

        query = collection.find.call_args.args[0]
        self.assertEqual(
            query["started_at"]["$lt"],
            datetime(2026, 11, 11, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(
            query["$or"][0]["ended_at"]["$gt"],
            datetime(2026, 11, 10, 3, tzinfo=timezone.utc),
        )

    def test_report_period_classifier_validates_model_output(self):
        ai = AIService.__new__(AIService)
        ai._complete = Mock(return_value='{"period":"weekly"}')

        self.assertEqual(
            ai.classify_report_period("¿Qué escuché esta semana?"), "weekly"
        )

        ai._complete.return_value = '{"period":"all_time"}'
        self.assertIsNone(ai.classify_report_period("muéstrame mis estadísticas"))
        ai._complete.return_value = '{"period":true}'
        self.assertIsNone(ai.classify_report_period("mis canciones más escuchadas"))


class PlaybackReportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_language_report_uses_ai_period_and_sends_report(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
        ai = SimpleNamespace(classify_report_period=Mock(return_value="weekly"))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            handled = await _try_natural_language_report(
                update, context, "¿Qué escuché esta semana?"
            )

        self.assertTrue(handled)
        send.assert_awaited_once_with(update, context, "weekly")
        ai.classify_report_period.assert_called_once_with("¿Qué escuché esta semana?")

    async def test_non_report_text_skips_ai_classification(self):
        ai = SimpleNamespace(classify_report_period=Mock())
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))

        handled = await _try_natural_language_report(
            update, context, "Recomiéndame canciones movidas."
        )

        self.assertFalse(handled)
        ai.classify_report_period.assert_not_called()

    async def test_report_command_without_period_shows_daily_and_weekly_buttons(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock())
        )
        context = SimpleNamespace(args=[])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        send.assert_not_awaited()
        reply_args = update.effective_message.reply_text.await_args
        self.assertIn("¿Qué informe quieres consultar?", reply_args.args)
        buttons = reply_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual(
            [button.callback_data for button in buttons],
            ["report:daily", "report:weekly"],
        )

    async def test_report_period_button_opens_selected_report(self):
        query = SimpleNamespace(
            data="report:weekly",
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace()

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await report_period_callback(update, context)

        query.answer.assert_awaited_once()
        send.assert_awaited_once_with(update, context, "weekly")

    async def test_report_command_rejects_unknown_period(self):
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(args=["mensual"])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        reply.assert_awaited_once()
        self.assertIn("Uso:", reply.await_args.args[0])
        send.assert_not_awaited()

    async def test_report_command_accepts_weekly_alias(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock())
        )
        context = SimpleNamespace(args=["semana"])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        send.assert_awaited_once_with(update, context, "weekly")

    async def test_report_command_accepts_daily_alias(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock())
        )
        context = SimpleNamespace(args=["hoy"])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        send.assert_awaited_once_with(update, context, "daily")

    async def test_natural_language_request_without_report_intent_falls_through(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
        ai = SimpleNamespace(classify_report_period=Mock(return_value=None))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))

        handled = await _try_natural_language_report(
            update, context, "Muéstrame mis canciones más escuchadas."
        )

        self.assertFalse(handled)
        ai.classify_report_period.assert_called_once()

    async def test_natural_language_request_reports_ai_failure_with_command_fallback(self):
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        ai = SimpleNamespace(classify_report_period=Mock(side_effect=RuntimeError("AI down")))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))

        with self.assertLogs("app.bot", level="ERROR"):
            handled = await _try_natural_language_report(
                update, context, "¿Qué escuché esta semana?"
            )

        self.assertTrue(handled)
        self.assertIn("/reportes diario", reply.await_args.args[0])

    async def test_send_report_surfaces_database_failure(self):
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        history = SimpleNamespace(report=Mock(side_effect=PyMongoError("database down")))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"playback_history": history})
        )

        with self.assertLogs("app.bot", level="ERROR"):
            await send_playback_report(update, context, "daily")

        self.assertIn("MongoDB", reply.await_args.args[0])

    def test_report_format_escapes_song_metadata(self):
        start, end = report_period_bounds(
            "daily", datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        )
        text = _format_playback_report(
            {
                "period": "daily",
                "start": start,
                "end": end,
                "play_count": 1,
                "total_seconds": 65,
                "top_songs": [
                    {
                        "title": "<Song>",
                        "artist": "Artist & Co",
                        "plays": 1,
                        "seconds": 65,
                    }
                ],
                "top_genres": [("<Rock>", 65)],
            }
        )

        self.assertIn("&lt;Song&gt;", text)
        self.assertIn("Artist &amp; Co", text)
        self.assertIn("&lt;Rock&gt;", text)
        self.assertNotIn("<Song>", text)

    def test_empty_report_format_explains_that_no_plays_are_available(self):
        start, end = report_period_bounds(
            "weekly", datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        )
        text = _format_playback_report(
            {
                "period": "weekly",
                "start": start,
                "end": end,
                "play_count": 0,
                "total_seconds": 0,
                "top_songs": [],
                "top_genres": [],
            }
        )

        self.assertIn("Informe semanal", text)
        self.assertIn("Todavía no hay reproducciones", text)


class DFPlayerProtocolTests(unittest.TestCase):
    def test_extracts_finished_notifications_and_keeps_partial_frame(self):
        buffer = bytearray(
            b"noise"
            + build_frame(0x41)
            + build_frame(0x3D, 1)
            + build_frame(0x3D, 2)
            + build_frame(0x3D)[:5]
        )

        self.assertEqual(extract_finished_events(buffer), 2)
        self.assertEqual(buffer, build_frame(0x3D)[:5])

    def test_builds_play_folder_command_frame(self):
        frame = build_frame(0x0F, (1 << 8) | 3)
        self.assertEqual(len(frame), 10)
        self.assertEqual(frame[:7], bytes((0x7E, 0xFF, 0x06, 0x0F, 0, 1, 3)))
        self.assertEqual(frame[-1], 0xEF)
        checksum = (frame[7] << 8) | frame[8]
        self.assertEqual(checksum, (-(0xFF + 0x06 + 0x0F + 1 + 3)) & 0xFFFF)

    def test_requests_feedback_for_dfplayer_command(self):
        frame = build_frame(0x0F, (1 << 8) | 1, feedback=1)
        self.assertEqual(frame[4], 1)
        checksum = (frame[7] << 8) | frame[8]
        self.assertEqual(
            checksum,
            (-(0xFF + 0x06 + 0x0F + 1 + 1 + 1)) & 0xFFFF,
        )

    def test_pause_and_resume_commands_use_dfplayer_protocol_codes(self):
        self.assertEqual(build_frame(0x0E)[3], 0x0E)
        self.assertEqual(build_frame(0x0D)[3], 0x0D)
        self.assertEqual(build_frame(0x16)[3], 0x16)

    def test_dfplayer_bass_equalizer_command_uses_supported_mode(self):
        frame = build_frame(0x07, 5)
        self.assertEqual(frame[3], 0x07)
        self.assertEqual(frame[6], 5)


class SeedCatalogTests(unittest.TestCase):
    def test_seed_catalog_has_100_unique_songs_and_includes_prince(self):
        songs = build_documents()
        self.assertEqual(len(songs), 100)
        self.assertEqual(len({song["_id"] for song in songs}), 100)
        self.assertTrue(any(song["artist"] == "Prince" for song in songs))
        self.assertTrue(all(song["decade"] == 1980 for song in songs))

    def test_alternative_catalog_has_unique_tracks_and_keeps_purple_rain_accurate(self):
        songs = build_alternative_rock_documents()
        self.assertEqual(len(songs), len(ALTERNATIVE_ROCK) + 1)
        self.assertEqual(len({song["_id"] for song in songs}), len(songs))
        purple_rain = next(song for song in songs if song["title"] == "Purple Rain")
        self.assertEqual(purple_rain["artist"], "Prince")
        self.assertEqual(purple_rain["genre"], ["rock"])
        self.assertEqual(purple_rain["year"], 1984)
        self.assertTrue(
            all(
                song["genre"] == ["alternative rock"]
                for song in songs
                if song["title"] != "Purple Rain"
            )
        )


if __name__ == "__main__":
    unittest.main()
