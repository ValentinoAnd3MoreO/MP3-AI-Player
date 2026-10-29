# MP3 AI Player: Telegram, MongoDB e IA configurable

Prototipo ejecutable de un bot de Telegram que interpreta pedidos musicales, busca candidatos en MongoDB local y deja que una IA los ordene. La recomendación final siempre se arma con canciones reales encontradas en la colección local.

Todo este prototipo vive en esta carpeta; no modifica el código de las otras carpetas del repositorio.

## Qué hace

- Recibe texto y el comando `/buscar` en Telegram.
- Pide a un modelo configurable que convierta la consulta a filtros sencillos: artista, género, década, mood y palabras clave.
- Construye una consulta MongoDB segura a partir de esos campos (no ejecuta consultas MongoDB generadas por el modelo).
- Busca canciones candidatas en la colección `songs`.
- Opcionalmente consulta Internet mediante Tavily y entrega esos resultados como contexto adicional a la IA.
- Solicita a la IA que ordene las canciones candidatas y valida que cada ID elegido exista en MongoDB.
- Si la búsqueda web no está disponible, registra el problema y continúa usando la biblioteca local. Si falla el proveedor de IA, el bot informa que no pudo completar la búsqueda.
- Si OpenAI indica que se agotaron los créditos/cuota, el bot muestra un aviso específico y un enlace a facturación; los límites temporales de solicitudes se notifican por separado.

Este prototipo recomienda; todavía no reproduce ni envía archivos de audio.

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
  "path": "C:\\Musica\\TakeOnMe.mp3"
}
```

El documento puede tener un `_id` generado por MongoDB si no necesitas asignarlo tú. `year` o `decade` sirven para filtrar por época. `genre`, `mood` y `tags` pueden ser una cadena o una lista de cadenas. `path` queda disponible para integrar más adelante el reproductor, pero no se expone al modelo.

Carga los documentos en MongoDB con tu herramienta habitual, por ejemplo `mongoimport` desde un archivo JSON, o con una interfaz como MongoDB Compass. El esquema de datos real debe incluir metadatos suficientes para que las consultas por cantante, década y estilo encuentren coincidencias.

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
│   └── recommender.py
└── tests/
    └── test_recommender.py
```

## Límites conocidos y siguientes pasos

- La calidad depende de los metadatos disponibles en la biblioteca local; la IA no analiza el audio ni calcula BPM.
- La consulta web es optativa y requiere una cuenta/clave de Tavily. El modelo no obtiene acceso general a Internet por sí mismo.
- Este prototipo no contiene `/reportes`, historial de reproducción, botones de audio ni comandos específicos como `/top80`; esos extras del proyecto se pueden integrar después sobre la misma base.
- Para un despliegue real, añade controles de acceso al bot, gestión segura de claves, límites de uso y pruebas de integración con una instancia de MongoDB.
