# MP3 AI Player: Telegram, MongoDB e IA configurable

Prototipo ejecutable de un bot de Telegram que conversa sobre música, recomienda temas desde MongoDB y ofrece botones para reproducir los MP3 que estén mapeados a la microSD de un DFPlayer Mini conectado a un ESP32.

Todo este prototipo vive en esta carpeta; no modifica el código de las otras carpetas del repositorio.

## Qué hace

- Recibe texto y el comando `/buscar` en Telegram.
- Acepta mensajes de voz de hasta 20 MB, los transcribe en español con el modelo configurable `AI_TRANSCRIPTION_MODEL` y procesa la transcripción con el mismo flujo de texto. La descarga desde Telegram usa 30 segundos de espera de lectura y 10 de conexión, con hasta 3 intentos ante timeouts o fallos de red y pausas de 1 y 2 segundos entre intentos. Si no puede descargarla, avisa y no ejecuta el pedido; los reintentos no repiten la transcripción ni la reproducción.
- Añade botones explícitos de reproducción a canciones recomendadas que tengan una ruta `sd_path` válida en MongoDB. Tras conversar sobre una canción o mencionar claramente un álbum, comprueba la disponibilidad en MongoDB y ofrece reproducir la canción o encolar el álbum completo.
- `/autoplay on|off` activa o desactiva por usuario el inicio automático, apagado por defecto. Cuando está activo, un pedido explícito como «quiero escuchar Purple Rain» o «poneme hits movidos» inicia la mejor coincidencia local reproducible sin pedir confirmación; las alternativas («poneme otra») y la conversación normal no disparan autoplay. `/autoplay` muestra el estado. La preferencia se guarda en MongoDB y sobrevive a los reinicios.
- Para encolar una selección completa, pide «poneme 10 canciones de Charly García en la cola» o «quiero escuchar 10 canciones de Queen». Con autoplay activado agrega todas las coincidencias reproducibles seleccionadas, no solo la primera; con autoplay apagado muestra un único botón para confirmar toda la selección. Si ya suena una canción, la conserva y añade las nuevas al final; si la cola está vacía, inicia la primera y deja las demás pendientes. Si hay menos canciones reproducibles de las solicitadas, informa la cantidad disponible. Pedir solo recomendaciones («dame 10 canciones de Queen») no inicia la reproducción.
- Si pides escuchar un tema que no está indexado, explica que el reproductor funciona con canciones de la microSD y que falta ese tema concreto, en vez de afirmar que el bot no puede reproducir música.
- Después de hablar de una canción concreta, ofrece reproducirla cuando exista en la biblioteca con una ruta SD válida. La sugerencia por coincidencia se basa en el mensaje actual, no en canciones mencionadas en conversaciones anteriores; para referencias como «esa canción» el modelo debe resolver el tema explícitamente desde el contexto. Evita sugerencias por coincidencias accidentales con palabras comunes: los títulos de una sola palabra requieren mencionar también al artista.
- Si se habla de Persona como videojuego, no lo interpreta como una banda; si no queda claro qué entrega o banda sonora se busca, pide una aclaración.
- Incluye controles para pausar, reanudar y detener la reproducción desde Telegram (`/pausar`, `/reanudar`, `/detener` o `/parar`); los botones de reproducción permiten pausar o reanudar. El firmware consulta el estado de reproducción del DFPlayer y solo confirma la pausa/reanudación/detención si el módulo la verifica. Si no puede confirmarla, el bot indica el problema en vez de afirmar que funcionó. «No quiero escuchar más música» también detiene el audio y vacía la cola; borrar solo la cola deja terminar la canción actual.
- Permite cambiar en caliente el volumen del DFPlayer con `/volumen 0` a `/volumen 30`, sin reiniciar la placa.
- Incluye una cola de reproducción compartida por el bot, controles de siguiente canción y modos de loop (cola completa o canción actual). `/cola`, `/skip` y `/loop` también están disponibles como comandos. `/skip Título` o «saltá a [título]» salta a una canción futura, elimina de la cola las pistas que se omitieron y mantiene las posteriores. Puedes vaciar las canciones futuras sin interrumpir la actual diciendo «borra toda la cola». También puedes filtrar la cola por artista, álbum, género, título o etiqueta, por ejemplo «borra todas las canciones de la cola menos las de Queen» o «deja solo el álbum Purple Rain en la cola»; la canción actual termina sin interrumpirse. La cola, el modo loop y la preferencia de autoplay se guardan en MongoDB. Al reiniciar el bot, la pista actual continúa si el ESP32 sigue reproduciéndola; si la placa se reinició, el bot retoma esa pista desde el principio. Al iniciar un álbum, el mensaje de reproducción incluye la confirmación de la cola y un único panel de controles.
- Permite pedir un álbum completo por su nombre; agrega a la cola todas las canciones locales reproducibles y las ordena por número de pista ID3 cuando está disponible. Si indicas «empezando por [canción]», inicia en esa pista y continúa por el álbum, envolviendo al principio para conservar todas las canciones.
- Avanza automáticamente cuando recibe del ESP32 la notificación UART de fin de pista del DFPlayer. Si el ESP32 no puede consultar esa notificación, conserva un temporizador basado en la duración indexada como alternativa.
- Guarda en MongoDB las sesiones de reproducción iniciadas desde el bot, incluidos título, artista, género, fecha/hora y pausas, y ofrece informes diarios/semanales con `/reportes` o pedidos en lenguaje natural.
- Recuerda la última canción reproducida durante la sesión del bot; “otra vez” ofrece reproducirla de nuevo. Si el bot se reinició y perdió ese contexto, pide el título en vez de adivinar.
- Envía al ESP32 únicamente la ruta obtenida de MongoDB y validada; la IA no genera comandos ni rutas para el hardware.
- Responde como asistente conversacional sobre artistas, canciones, géneros e historia musical, con hasta seis intercambios recientes de contexto por usuario. Usa `/reset` para iniciar de nuevo. Si una búsqueda no encuentra temas locales, continúa la conversación con la IA y consulta Tavily si está habilitado; separa claramente lo que sabe de la música de lo disponible en tu microSD. Los saludos no activan Tavily; el resto del chat lo consulta cuando pide información actual o una búsqueda local no encuentra resultados.
- Pide a un modelo configurable que convierta la consulta a filtros sencillos: artista, género, década, mood y palabras clave.
- Si una búsqueda por artista no encuentra resultados, compara con prudencia el nombre reconocido con los artistas de la biblioteca para tolerar errores menores de transcripción; solo usa el artista si la coincidencia es clara.
- Cuando rechazas una recomendación y pides otra, conserva la búsqueda anterior y excluye las canciones ya sugeridas para no repetirlas.
- Si corriges un título con frases como «quise decir» o «me refería a», vuelve a buscar primero el título corregido en la biblioteca local.
- Construye una consulta MongoDB segura a partir de esos campos (no ejecuta consultas MongoDB generadas por el modelo).
- Busca canciones candidatas en la colección `songs`.
- Opcionalmente consulta Internet mediante Tavily y entrega esos resultados como contexto adicional a la IA.
- Solicita a la IA que ordene las canciones candidatas y valida que cada ID elegido exista en MongoDB.
- Si la búsqueda web no está disponible, registra el problema y continúa usando la biblioteca local. Si falla el proveedor de IA, el bot informa que no pudo completar la búsqueda.
- Si OpenAI indica que se agotaron los créditos/cuota, el bot muestra un aviso específico y un enlace a facturación; los límites temporales de solicitudes se notifican por separado.

Las canciones no se transmiten desde Telegram ni desde OpenAI: el ESP32 ordena al DFPlayer Mini que reproduzca el archivo que ya existe en la microSD.

## Estado de los requisitos del curso

| Requisito | Estado | Evidencia / pendiente |
|---|---|---|
| Bot de Telegram que entiende pedidos por texto y busca canciones locales | Cumple | Usa IA configurable y MongoDB; la búsqueda web es opcional. |
| Biblioteca con géneros, épocas, artistas y álbumes | Parcial | De 468 canciones indexadas, 425 tienen álbum y número de pista, 279 tienen género y 391 tienen año/década verificados con coincidencia exacta de MusicBrainz. Las restantes carecen de álbum, género fiable o coincidencia exacta. |
| Mensajes de voz desde Telegram | Cumple | Transcripción configurable y procesada por el mismo flujo de pedidos de texto. |
| Historial de reproducciones persistente | Cumple con alcance aclarado | Guarda cada orden aceptada con canción y fecha/hora en MongoDB; el informe calcula las reproducciones contando esas sesiones. No confirma que el usuario haya oído el audio. |
| Comando `/reportes` diario/semanal | Cumple | `/reportes` muestra botones para elegir período; también acepta `/reportes diario` y `/reportes semanal`. Incluye top 5, tiempo estimado y hasta 3 géneros. |
| Salida por parlante y auriculares | Parcial | El software reproduce hacia el hardware. El TDA7297 sirve para parlantes pasivos; los auriculares requieren un amplificador de auriculares dedicado. Falta validar físicamente ambos montajes. |
| Cola, siguiente, loop y reproducción de álbumes | Implementado | Controles en Telegram y comandos `/cola`, `/skip` y `/loop`; cola persistida en MongoDB y fin de pista notificado por el ESP32. |
| Modo de reproducción automática | Implementado | `/autoplay on|off`, por usuario y apagado por defecto; preferencia persistida y solo inicia automáticamente ante pedidos explícitos. |

## Requisitos

- Python 3.10 o superior.
- MongoDB en ejecución (por defecto `mongodb://localhost:27017`).
- Un bot creado con [@BotFather](https://t.me/BotFather).
- Una API compatible con OpenAI. Para búsqueda web opcional, una clave de Tavily.

## Preparación

Abre PowerShell en esta carpeta y ejecuta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Completa `.env` con el token de Telegram y los datos del proveedor de IA. No publiques ese archivo ni compartas las claves.

### Seleccionar proveedor y modelo

La integración usa una API compatible con OpenAI; `AI_MODEL`, `AI_BASE_URL` y `AI_API_KEY` definen el proveedor y el modelo:

| Proveedor | `AI_MODEL` | `AI_BASE_URL` | `AI_API_KEY` |
|---|---|---|---|
| OpenAI | Modelo habilitado en tu cuenta, por ejemplo `gpt-4o-mini` | Vacío | Tu clave |
| Ollama | Nombre del modelo instalado, por ejemplo `llama3.1` | `http://localhost:11434/v1` | `ollama` |
| LM Studio | Identificador del modelo cargado | `http://localhost:1234/v1` | Valor requerido por el servidor, habitualmente `lm-studio` |
| Otro compatible | Según el proveedor | URL base de su API compatible | Clave del proveedor |

El servicio elegido por `AI_BASE_URL` y `AI_MODEL` tiene que aceptar llamadas de chat completions. Azure OpenAI y algunos proveedores pueden requerir un adaptador específico de autenticación; no se asume compatibilidad automática.

La transcripción de voz usa `AI_TRANSCRIPTION_MODEL` mediante el endpoint de transcripciones compatible con OpenAI, compartiendo `AI_BASE_URL` y `AI_API_KEY`. El proveedor configurado debe admitir ese endpoint y el formato OGG de Telegram; OpenAI utiliza `whisper-1`. El audio y su transcripción se envían al proveedor configurado y no se guardan en MongoDB. Si el proveedor no admite audio, puedes seguir usando el bot por texto.

### Búsqueda en Internet (opcional)

La búsqueda web utiliza la API de Tavily. Para activarla, configura:

```env
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tu_clave_de_tavily
```

El texto de la consulta se envía al proveedor de búsqueda y el texto de la consulta junto con metadatos de las canciones candidatas se envía al proveedor de IA. La información web solo aporta contexto; nunca agrega canciones a la biblioteca ni reemplaza MongoDB.

## Preparar MongoDB y canciones

La aplicación usa la base `mp3_ai_player` y la colección `songs` por defecto. Puedes cambiar ambos nombres en `.env`. Cada documento debería tener metadatos compatibles con este ejemplo:

```json
{
  "_id": "song_001",
  "title": "Take On Me",
  "artist": "a-ha",
  "album": "Hunting High and Low",
  "genre": ["new wave", "synth pop"],
  "year": 1985,
  "decade": 1980,
  "mood": ["movido", "energético"],
  "tags": ["hit", "80s", "classic"],
  "bpm": 168,
  "duration_sec": 227,
  "sd_path": "/01/001.mp3",
  "dfplayer_folder": 1,
  "dfplayer_track": 1
}
```

El documento puede tener un `_id` generado por MongoDB si no necesitas asignarlo tú. `year` o `decade` sirven para filtrar por época. `genre`, `mood` y `tags` pueden ser una cadena o una lista de cadenas. `sd_path` es la ruta relativa al contenido de la microSD y no una ruta del disco del PC.

Carga los documentos en MongoDB con tu herramienta habitual, por ejemplo `mongoimport` desde un archivo JSON, o con una interfaz como MongoDB Compass. El esquema de datos real debe incluir metadatos suficientes para que las consultas por cantante, década y estilo encuentren coincidencias.

### Cargar catálogo de prueba de los 80

El proyecto incluye un catálogo inicial de 100 canciones conocidas de los 80. Contiene metadatos solamente (no archivos MP3) y se carga sin borrar ni sobrescribir otros documentos:

```powershell
python -m scripts.seed_80s
```

El script usa la base y colección configuradas en `.env`; por defecto `110Music.songs` si así lo configuraste. Es idempotente: puedes ejecutarlo nuevamente y no insertará copias de los documentos del catálogo.

### Cargar rock alternativo y Purple Rain

Para sumar 53 temas de rock alternativo de los 80 y 90, además de asegurar **Purple Rain** de Prince en la colección:

```powershell
python -m scripts.seed_alternative_rock
```

La carga es idempotente y no borra otras canciones. Purple Rain conserva sus metadatos de rock/pop-rock (no se etiqueta como rock alternativo). Los archivos de audio no se copian: el catálogo contiene metadatos.

### Preparar e indexar la microSD

El DFPlayer Mini no recibe rutas arbitrarias del PC. Para este proyecto, organiza la tarjeta en FAT32 usando carpetas numéricas `01` a `99` y MP3 numerados `001.mp3` a `255.mp3` dentro de cada carpeta:

```text
microSD/
├── 01/
│   ├── 001.mp3
│   ├── 002.mp3
│   └── ...
└── 02/
    └── 001.mp3
```

Usa la carpeta `02` para las canciones de la saga de videojuegos Persona; numéralas en orden (`001.mp3`, `002.mp3`, etc.). Por ejemplo, **School Days** está en `/02/001.mp3`. El indexador guarda la carpeta y el número que realmente encuentre en la tarjeta. Las solicitudes de música de Persona buscan primero en esa carpeta y solo ofrecen canciones locales indexadas allí. Si pides una entrega concreta cuyo álbum no está identificado en los metadatos, el bot ofrece encolar las canciones Persona disponibles y aclara que no puede verificar que todas sean de esa entrega.

Los archivos deben tener etiquetas ID3 con al menos título y artista. Conecta la tarjeta al PC y ejecuta el indexador apuntando a su letra (ajusta `E:\`):

```powershell
python -m scripts.index_sd_card --sd-root E:\
```

El indexador asocia cada MP3 a la canción existente por sus etiquetas ID3 y agrega `sd_path`, `dfplayer_folder`, `dfplayer_track` y los números de pista/disco del álbum si están etiquetados. Si no encuentra una canción equivalente, la crea en MongoDB. Omite rutas/nombres incompatibles o archivos sin etiquetas legibles. No copia ni renombra archivos. Un año ID3 no fiable (`1970`) no borra el año verificado de MusicBrainz. Vuelve a ejecutar el indexador después de actualizar el bot para guardar los números de pista/disco que ordenan correctamente la cola de álbumes.

#### Completar años y géneros con MusicBrainz

La biblioteca local puede tener fechas ID3 incorrectas (por ejemplo, `1970`) o no tener género. El script consulta MusicBrainz usando artista y álbum, valida coincidencias exactas y muestra una vista previa sin escribir cambios:

```powershell
python -m scripts.enrich_metadata
```

Solo después de revisar la salida, aplica las coincidencias con:

```powershell
python -m scripts.enrich_metadata --apply
```

El script reemplaza `1970` como año no fiable, calcula `decade` a partir de la primera publicación del álbum y agrega solo géneros de una lista conocida que tengan respaldo suficiente en las etiquetas comunitarias. Exige coincidencias claras de artista, título y tipo de álbum; no sobrescribe años válidos ni géneros existentes. MongoDB conserva `year_source`, `genre_source`, `metadata_confidence` y el identificador de MusicBrainz para rastrear el origen. Requiere Internet; consulta la API pública de MusicBrainz respetando su límite de solicitudes. No cambia etiquetas de audio ni escribe en la microSD.

#### Organizar una biblioteca completa

`scripts.organize_sd_library` crea una vista previa por defecto. Agrupa la música por artista y álbum en carpetas numéricas, mantiene Persona en `02` y conserva las pistas Persona ya presentes en la tarjeta si también están en la descarga. Los nueve temas Deftones sin etiquetas se identifican por nombre de archivo y se guardan sin álbum; no se modifican los MP3 originales de descargas. Los años ID3 `1970` no se consideran fechas de lanzamiento.

Para preparar una copia local lista para transferir, usa `--prepare-output`. Esto lee las canciones actuales de la tarjeta para conservar las pistas que no están en Descargas, pero escribe el resultado únicamente en la carpeta local indicada:

```powershell
python -m scripts.organize_sd_library --source-root "C:\Users\Admin\Downloads\Musica\Musica" --sd-root D:\ --prepare-output "C:\Users\Admin\Desktop\Musica organizada para microSD"
```

Abre la carpeta de salida y sigue `LEEME.txt`: quita primero las carpetas numéricas antiguas de la tarjeta y copia las carpetas numéricas preparadas directamente a la raíz de la microSD. No copies la carpeta contenedora como subcarpeta. `MP3-library.csv` es un índice opcional, no hace falta para reproducir. Una vez copiados los archivos, vuelve a indexar MongoDB para actualizar las rutas.

La opción `--apply` copia directamente y reemplaza las carpetas numéricas de la tarjeta, además de actualizar MongoDB; úsala solo con una tarjeta estable y tras revisar la vista previa. Nunca formatea la tarjeta ni altera la carpeta de descargas.

### Configurar el ESP32 y DFPlayer Mini

1. Instala MicroPython en el ESP32 y copia `esp32_firmware/main.py`, `esp32_firmware/dfplayer.py`, `esp32_firmware/protocol.py` y una copia de `esp32_firmware/config.py.example` renombrada a `config.py`.
2. Copia `config.py.example` como `config.py`, completa el Wi-Fi y genera un token aleatorio de al menos 24 caracteres. `PLAYER_EQ` acepta valores de 0 a 5: Normal 0, Pop 1, Rock 2, Jazz 3, Classic 4 y Bass 5.
3. Cableado de ejemplo para un ESP32 DevKit: GPIO17 (TX) al RX del DFPlayer (se recomienda una resistencia de 1 kΩ en serie); GPIO16 (RX) al TX del DFPlayer; GND común y alimentación estable de 5 V a VCC. El DFPlayer se enciende al recibir alimentación; el firmware lo inicializa al arrancar y no empieza a reproducir hasta recibir una orden. Deja IO_1, IO_2 y ADKEY desconectados; no los unas a GND para encender el módulo. Conecta el parlante a SPK1/SPK2, no entre una salida del módulo y GND. Confirma los pines/voltaje de tu placa antes de alimentar.
4. Copia los archivos MP3 y conserva exactamente la estructura numérica de carpetas/nombres. Expulsa la tarjeta con seguridad, insértala en el DFPlayer y reinicia el ESP32.
5. En `.env`, establece la IP que el ESP32 muestra en su consola serie, el mismo token y habilita el reproductor:

```env
PLAYER_ENABLED=true
ESP32_BASE_URL=http://192.168.1.50:8080
ESP32_API_TOKEN=el_mismo_token_de_config_py
```

El PC del bot y el ESP32 deben poder comunicarse en la misma red local. El endpoint solo acepta órdenes con el token Bearer configurado. No expongas el puerto del ESP32 a Internet.

Luego reinicia el bot, indexa la tarjeta y pide una canción. El bot ofrece un botón **Reproducir** para los resultados que tienen `sd_path`; la reproducción ocurre solo cuando lo pulsas. Las canciones de catálogo de prueba no tienen MP3 ni ruta SD hasta que indexas audio real.

Al actualizar el firmware del ESP32, copia de nuevo tanto `main.py` como `dfplayer.py` (además de los otros archivos si cambiaron). Al pulsar reproducir, la consola serie muestra la respuesta UART del DFPlayer. La aceptación HTTP solo confirma que recibió la solicitud de red; no garantiza que haya empezado a reproducir.

Si Thonny muestra `TypeError: 'bytearray' object doesn't support item deletion` en `extract_frames`, copia el `esp32_firmware/protocol.py` actualizado a la raíz del ESP32 como `protocol.py` y reinicia la placa. El parser usa asignación de segmentos en lugar de `del` o `clear()` para ser compatible con MicroPython; no hace falta reinstalar MicroPython ni cambiar el cableado.

### Volumen y calidad de audio

- `DAC_L` y `DAC_R` son salidas de nivel de línea, no amplifican auriculares. Para auriculares usa un amplificador de auriculares; para parlantes activos conecta la entrada AUX a `DAC_L`, `DAC_R` y GND.
- Para un parlante pasivo usa `SPK1` y `SPK2` (no conectes ninguno a GND). La potencia y los graves dependen del parlante, de la caja acústica y de una alimentación estable de 5 V con corriente suficiente.
- El firmware lee `PLAYER_EQ` de `config.py` y aplica por defecto **Bass** (`PLAYER_EQ = 5`; valores 0–5): Normal 0, Pop 1, Rock 2, Jazz 3, Classic 4. Para cambiar el sonido, edita este valor en el `config.py` que copiaste al ESP32 y reinicia el módulo; no basta con cambiar `dfplayer.py`. La consola serie informa el valor de ecualización leído al arrancar.
- El volumen inicial está en `PLAYER_VOLUME = 22` (rango 0–30) en el `config.py` del ESP32. Modificar el archivo de la PC no cambia el valor que ya está en la placa: copia el `config.py` actualizado al ESP32 y reinícialo. La consola serie informa el volumen efectivo aplicado al arrancar. Ajusta de a poco; al máximo puede distorsionar. Apaga el circuito antes de cambiar conexiones.
- Copia `main.py`, `dfplayer.py` y `protocol.py` actualizados al ESP32 y reinícialo para habilitar la verificación de pausa/reanudación/detención y la notificación de fin de pista. Después, puedes cambiar el volumen durante la reproducción desde Telegram: `/volumen 26` (rango 0–30); no hace falta reiniciar la placa para ajustes siguientes.
- Si conectas auriculares directamente a `DAC_L`/`DAC_R`, la señal de nivel de línea puede no tener potencia suficiente. La ecualización puede cambiar el balance tonal, pero no sustituye un amplificador de auriculares; compara con parlantes activos por AUX o un amplificador.

## Ejecutar

Con MongoDB encendido, desde esta carpeta ejecuta:

```powershell
python run_bot.py
```

En Telegram prueba:

- `dame canciones movidas`
- Envía un mensaje de voz diciendo, por ejemplo, «buenos hits de los 80».
- `buenos hits de los 80`
- `canciones de Queen`
- `¿Qué te parece Purple Rain?` (si está indexada con una ruta SD, ofrecerá reproducirla)
- `me gustaría que me reproduzcas esa canción` (resuelve referencias recientes y ofrece el botón si el tema está indexado)
- `/buscar rock argentino de los 90`
- `/pausar`, `/reanudar` y `/detener`
- `/reportes diario` y `/reportes semanal`, o pregunta “¿Qué escuché esta semana?” / “¿Cuánto escuché hoy?”

El bot también ofrece `/start` y `/help`. Si no hay resultados, comprueba los metadatos de la colección, el nombre de la base y el de la colección.

## Ejecutar pruebas

Desde esta carpeta:

```powershell
python -m unittest discover -s tests -v
```

## Estructura

```text
proyecto_telegram_ai_music/
├── .env.example
├── README.md
├── requirements.txt
├── run_bot.py
├── app/
│   ├── ai_service.py
│   ├── bot.py
│   ├── config.py
│   ├── database.py
│   ├── device_client.py
│   ├── playback_history.py
│   ├── playback_queue.py
│   ├── playback_state.py
│   ├── recommender.py
│   └── sd_path.py
├── esp32_firmware/
│   ├── config.py.example
│   ├── dfplayer.py
│   ├── main.py
│   └── protocol.py
├── scripts/
│   ├── enrich_metadata.py
│   ├── index_sd_card.py
│   ├── organize_sd_library.py
│   ├── seed_80s.py
│   └── seed_alternative_rock.py
└── tests/
    ├── test_playback_queue.py
    ├── test_playback_state.py
    └── test_recommender.py
```

## Límites conocidos y siguientes pasos

- La calidad depende de los metadatos disponibles en la biblioteca local; la IA no analiza el audio ni calcula BPM.
- La consulta web es optativa y requiere una cuenta/clave de Tavily. El modelo no obtiene acceso general a Internet por sí mismo.
- El historial conversacional se mantiene en memoria del proceso y se pierde al reiniciar el bot; no se guarda en MongoDB.
- El historial registra las órdenes de reproducción aceptadas por el ESP32, no confirma que el usuario haya oído el audio. El avance automático puede usar la notificación UART de fin de pista; el tiempo escuchado sigue siendo una estimación basada en pausas/reanudaciones y la duración del archivo MP3.
- La cola se guarda en `bot_state` dentro de la base MongoDB seleccionada. El ESP32 conserva el progreso al reiniciar solo mientras la placa siga encendida; si se reinicia, el bot retoma la pista actual desde el principio. La notificación de fin de pista depende de la respuesta UART del DFPlayer; si no está disponible, se usa el temporizador de respaldo con `duration_sec`.
- Los informes cuentan solo reproducciones registradas desde que está instalada esta función; no hay datos retroactivos.
- Los géneros de muchas canciones no están etiquetados. Los informes solo agrupan por género cuando existe ese dato; el indexador guarda la duración del MP3 para limitar el tiempo estimado de escucha.
- Para un despliegue real, añade controles de acceso al bot, gestión segura de claves, límites de uso y pruebas de integración con una instancia de MongoDB.
