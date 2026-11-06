# MP3 AI Player: Telegram, MongoDB e IA configurable

Prototipo ejecutable de un bot de Telegram que conversa sobre música, recomienda temas desde MongoDB y ofrece botones para reproducir los MP3 que estén mapeados a la microSD de un DFPlayer Mini conectado a un ESP32.

Todo este prototipo vive en esta carpeta; no modifica el código de las otras carpetas del repositorio.

## Qué hace

- Recibe texto y el comando `/buscar` en Telegram.
- Añade botones explícitos de reproducción a canciones recomendadas que tengan una ruta `sd_path` válida en MongoDB.
- Envía al ESP32 únicamente la ruta obtenida de MongoDB y validada; la IA no genera comandos ni rutas para el hardware.
- Responde como asistente conversacional sobre artistas, canciones, géneros e historia musical, con hasta seis intercambios recientes de contexto por usuario. Usa `/reset` para iniciar de nuevo. Los saludos no activan Tavily; la búsqueda web en el chat queda para preguntas que piden explícitamente información actual o noticias.
- Pide a un modelo configurable que convierta la consulta a filtros sencillos: artista, género, década, mood y palabras clave.
- Construye una consulta MongoDB segura a partir de esos campos (no ejecuta consultas MongoDB generadas por el modelo).
- Busca canciones candidatas en la colección `songs`.
- Opcionalmente consulta Internet mediante Tavily y entrega esos resultados como contexto adicional a la IA.
- Solicita a la IA que ordene las canciones candidatas y valida que cada ID elegido exista en MongoDB.
- Si la búsqueda web no está disponible, registra el problema y continúa usando la biblioteca local. Si falla el proveedor de IA, el bot informa que no pudo completar la búsqueda.
- Si OpenAI indica que se agotaron los créditos/cuota, el bot muestra un aviso específico y un enlace a facturación; los límites temporales de solicitudes se notifican por separado.

Las canciones no se transmiten desde Telegram ni desde OpenAI: el ESP32 ordena al DFPlayer Mini que reproduzca el archivo que ya existe en la microSD.

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

Los archivos deben tener etiquetas ID3 con al menos título y artista. Conecta la tarjeta al PC y ejecuta el indexador apuntando a su letra (ajusta `E:\`):

```powershell
python -m scripts.index_sd_card --sd-root E:\
```

El indexador asocia cada MP3 a la canción existente por sus etiquetas ID3 y agrega `sd_path`, `dfplayer_folder` y `dfplayer_track`. Si no encuentra una canción equivalente, la crea en MongoDB. Omite rutas/nombres incompatibles o archivos sin etiquetas legibles. No copia ni renombra archivos.

### Configurar el ESP32 y DFPlayer Mini

1. Instala MicroPython en el ESP32 y copia `esp32_firmware/main.py`, `esp32_firmware/dfplayer.py`, `esp32_firmware/protocol.py` y una copia de `esp32_firmware/config.py.example` renombrada a `config.py`.
2. Completa el Wi-Fi y genera un token aleatorio de al menos 24 caracteres en `config.py`.
3. Cableado de ejemplo para un ESP32 DevKit: GPIO17 (TX) al RX del DFPlayer (se recomienda una resistencia de 1 kΩ en serie); GPIO16 (RX) al TX del DFPlayer; GND común y alimentación estable de 5 V al módulo. Conecta el parlante a SPK1/SPK2, no entre una salida del módulo y GND. Confirma los pines/voltaje de tu placa antes de alimentar.
4. Copia los archivos MP3 y conserva exactamente la estructura numérica de carpetas/nombres. Expulsa la tarjeta con seguridad, insértala en el DFPlayer y reinicia el ESP32.
5. En `.env`, establece la IP que el ESP32 muestra en su consola serie, el mismo token y habilita el reproductor:

```env
PLAYER_ENABLED=true
ESP32_BASE_URL=http://192.168.1.50:8080
ESP32_API_TOKEN=el_mismo_token_de_config_py
```

El PC del bot y el ESP32 deben poder comunicarse en la misma red local. El endpoint solo acepta órdenes con el token Bearer configurado. No expongas el puerto del ESP32 a Internet.

Luego reinicia el bot, indexa la tarjeta y pide una canción. El bot ofrece un botón **Reproducir** para los resultados que tienen `sd_path`; la reproducción ocurre solo cuando lo pulsas. Las canciones de catálogo de prueba no tienen MP3 ni ruta SD hasta que indexas audio real.

Al pulsar reproducir, la consola serie del ESP32 muestra la respuesta UART del DFPlayer o indica que no hubo respuesta. La aceptación HTTP del ESP32 solo confirma que recibió la solicitud de red; no garantiza que el módulo haya empezado a reproducir.

## Ejecutar

Con MongoDB encendido, desde esta carpeta ejecuta:

```powershell
python run_bot.py
```

En Telegram prueba:

- `dame canciones movidas`
- `buenos hits de los 80`
- `canciones de Queen`
- `me gustaría que me reproduzcas esa canción` (resuelve referencias recientes y ofrece el botón si el tema está indexado)
- `/buscar rock argentino de los 90`

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
│   ├── recommender.py
│   └── sd_path.py
├── esp32_firmware/
│   ├── config.py.example
│   ├── dfplayer.py
│   ├── main.py
│   └── protocol.py
├── scripts/
│   ├── index_sd_card.py
│   ├── seed_80s.py
│   └── seed_alternative_rock.py
└── tests/
    └── test_recommender.py
```

## Límites conocidos y siguientes pasos

- La calidad depende de los metadatos disponibles en la biblioteca local; la IA no analiza el audio ni calcula BPM.
- La consulta web es optativa y requiere una cuenta/clave de Tavily. El modelo no obtiene acceso general a Internet por sí mismo.
- El historial conversacional se mantiene en memoria del proceso y se pierde al reiniciar el bot; no se guarda en MongoDB.
- Este prototipo todavía no registra historial de reproducción ni incluye `/reportes` o comandos específicos como `/top80`; esos extras del proyecto se pueden integrar después sobre la misma base.
- Para un despliegue real, añade controles de acceso al bot, gestión segura de claves, límites de uso y pruebas de integración con una instancia de MongoDB.
