# MP3 AI Player: Telegram, MongoDB e IA configurable

Prototipo ejecutable de un bot de Telegram que conversa sobre música y también busca canciones en MongoDB local. En las recomendaciones, la IA ordena candidatos reales de la biblioteca; no inventa canciones disponibles.

Todo este prototipo vive en esta carpeta; no modifica el código de las otras carpetas del repositorio.

## Qué hace

- Recibe texto y el comando `/buscar` en Telegram.
- Reconoce pedidos de reproducción y aclara que todavía falta conectar el reproductor, en vez de responder con una recomendación distinta.
- Responde como asistente conversacional sobre artistas, canciones, géneros e historia musical, con hasta seis intercambios recientes de contexto por usuario. Usa `/reset` para iniciar de nuevo. Los saludos no activan Tavily; la búsqueda web en el chat queda para preguntas que piden explícitamente información actual o noticias.
- Pide a un modelo configurable que convierta la consulta a filtros sencillos: artista, género, década, mood y palabras clave.
- Construye una consulta MongoDB segura a partir de esos campos (no ejecuta consultas MongoDB generadas por el modelo).
- Busca canciones candidatas en la colección `songs`.
- Opcionalmente consulta Internet mediante Tavily y entrega esos resultados como contexto adicional a la IA.
- Solicita a la IA que ordene las canciones candidatas y valida que cada ID elegido exista en MongoDB.
- Si la búsqueda web no está disponible, registra el problema y continúa usando la biblioteca local. Si falla el proveedor de IA, el bot informa que no pudo completar la búsqueda.
- Si OpenAI indica que se agotaron los créditos/cuota, el bot muestra un aviso específico y un enlace a facturación; los límites temporales de solicitudes se notifican por separado.

La reproducción al ESP32 está disponible cuando configures tus MP3, el puente de red y el firmware VS1053 descritos abajo.

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
  "path": "80s/TakeOnMe.mp3"
}
```

El documento puede tener un `_id` generado por MongoDB si no necesitas asignarlo tú. `year` o `decade` sirven para filtrar por época. `genre`, `mood` y `tags` pueden ser una cadena o una lista de cadenas. `path` es relativo a la carpeta `MUSIC_DIR` y no se expone al modelo.

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

## Reproducir en ESP32 con VS1053

La IA no reproduce el audio por sí sola. La computadora ejecuta el bot, busca el MP3 en `music`, y lo transmite por la red local al ESP32. El VS1053 decodifica el MP3 y entrega audio al amplificador/auriculares conectados a su salida. El ESP32 y la computadora deben estar en la misma red Wi-Fi.

### Añadir e indexar tus MP3

1. Copia archivos `.mp3` a `music` (también se admiten subcarpetas).
2. Es recomendable que los MP3 tengan etiquetas ID3 de título y artista.
3. Desde esta carpeta ejecuta:

   ```powershell
   python -m scripts.index_music
   ```

El indexador guarda o actualiza los metadatos y la ruta relativa en MongoDB. Los archivos permanecen en la computadora; no se copian a MongoDB ni se suben a OpenAI. Sin archivos locales no se muestran botones de reproducción.

### Configurar el puente del servidor

En `.env`, determina la IP local de la computadora con `ipconfig`. Configura:

```env
MUSIC_DIR=music
DEVICE_ENABLED=true
DEVICE_API_TOKEN=un_token_aleatorio_largo_compartido_con_el_esp32
DEVICE_HOST=0.0.0.0
DEVICE_PORT=8765
DEVICE_PUBLIC_URL=http://192.168.1.100:8765
```

Reemplaza `192.168.1.100` por la IPv4 de la PC en tu red. Genera un token aleatorio (por ejemplo, con `python -c "import secrets; print(secrets.token_urlsafe(32))"`), ponlo en `.env` y en `esp32_firmware/config.py`. No compartas ni subas ese token. Si Windows Firewall pregunta, permite el puerto TCP 8765 únicamente en la red privada/local. Reinicia `run_bot.py` después de cambiar `.env`.

### Preparar ESP32 y VS1053

El firmware de ejemplo está en `esp32_firmware`. Con un ESP32 DevKit típico, conecta el VS1053 por SPI:

| VS1053 | ESP32 DevKit predeterminado |
|---|---:|
| SCK | GPIO 18 |
| SI / MOSI | GPIO 23 |
| SO / MISO | GPIO 19 |
| XCS | GPIO 5 |
| XDCS | GPIO 16 |
| DREQ | GPIO 4 |
| XRST / RESET | GPIO 17 |
| GND | GND |
| VCC | Según el módulo VS1053; verifica su documentación antes de alimentar |

Conecta la salida de audio del módulo al amplificador o salida adecuada para tus auriculares/parlante. No conectes un parlante pasivo directamente al ESP32. Los números GPIO son configurables y pueden variar según la placa; confirma el pinout del hardware real.

Instala MicroPython en el ESP32 y copia `main.py`, `vs1053.py` y `config.py.example` al dispositivo; crea allí `config.py` con esos valores. Completa Wi-Fi, IP de la PC, puerto y el mismo token secreto. La placa debe alcanzar `DEVICE_PUBLIC_URL` por la red local.

Al pedir recomendaciones, el bot adjuntará botones `▶` a las canciones que tengan un MP3 local indexado. Al tocar un botón, el puente encola la selección y el ESP32 consulta la orden y transmite el archivo. También puedes pedir que reproduzca la canción mencionada en la conversación, siempre que esté entre los MP3 locales indexados.

## Ejecutar

Con MongoDB encendido, desde esta carpeta ejecuta:

```powershell
python run_bot.py
```

En Telegram prueba:

- `dame canciones movidas`
- `buenos hits de los 80`
- `canciones de Queen`
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
│   ├── recommender.py
│   ├── device_bridge.py
│   └── music_library.py
├── scripts/
│   ├── index_music.py
│   ├── seed_80s.py
│   └── seed_alternative_rock.py
├── esp32_firmware/
│   ├── config.py.example
│   ├── main.py
│   └── vs1053.py
├── music/
│   └── (tus archivos MP3; no se suben a Git)
└── tests/
    ├── test_device_bridge.py
    └── test_recommender.py
```

## Límites conocidos y siguientes pasos

- La calidad depende de los metadatos disponibles en la biblioteca local; la IA no analiza el audio ni calcula BPM.
- La consulta web es optativa y requiere una cuenta/clave de Tavily. El modelo no obtiene acceso general a Internet por sí mismo.
- El historial conversacional se mantiene en memoria del proceso y se pierde al reiniciar el bot; no se guarda en MongoDB.
- La reproducción requiere MP3 locales con metadatos, ESP32 configurado y un VS1053 conectado; el firmware de ejemplo necesita probarse en el hardware concreto antes de usarlo.
- El puente es HTTP para la red local. No lo expongas directamente a Internet; el token solo autentica dentro de la red confiable.
- Este prototipo todavía no registra historial de reproducciones ni reportes; esos extras se pueden integrar después sobre la misma base.
- Para un despliegue real, añade controles de acceso al bot, gestión segura de claves, límites de uso y pruebas de integración con una instancia de MongoDB.
