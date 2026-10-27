# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)
#import webrepl
#webrepl.start()
######################################################################################################

import network
import urequests
import ujson
import time
import usocket as socket
import uselect as uselect
from machine import Pin
from HCSR04 import HCSR04
from utime import sleep
from umqtt.simple import MQTTClient

ssid = 'Vllamada'
password = 'Video_2025'

station = network.WLAN(network.STA_IF)
station.active(True)
station.connect(ssid, password)

led = Pin(2, Pin.OUT)

# Asegurarse que se conectó a internet
while not station.isconnected():
    print(".")
    time.sleep(0.2)


print(f'Connectado a wifi {ssid}')
print(station.ifconfig())


########################### MQTT #######################################

# Conexión a los tópicos
mqtt_server = "io.adafruit.com"
port = 1883
user = "ValenStraw"
password = "aio_XbaY75IQacIj8mTs9gbgEuCg3kpz"
client_id_gauge = "Identificador"
topico_lectura="ValenStraw/feeds/lectura"
topico_lectura2="ValenStraw/feeds/lectura2"
topico_lectura3="ValenStraw/feeds/lectura3"

try:
   conexionMQTT = MQTTClient(client_id_gauge, mqtt_server,user=user,password=password,port=int(port))
   conexionMQTT.connect() #Hacemos la conexión.
   print("Conectado con Broker MQTT")
except OSError as e:
   #Si falló la conexión, reiniciamos todo
   print("Fallo la conexion al Broker, reiniciando...")
   sleep(5)



def enviar_msg(topico, msg):
  try:
      conexionMQTT.publish(topico,str(msg))
      print(f'Mensaje enviado con exito: {msg}')
  except OSError as e:
      print("Error ",e)
      sleep(5)

# ----------------------------------------------------

d2 = 2
d4 = 4
medidor = HCSR04 (trigger_pin = d2 , echo_pin = d4)
boton1 = Pin (18,Pin.IN,Pin.PULL_UP)
tiempo_mensaje = 0.5
buzzer = Pin (14, Pin.OUT)
ledestado = Pin (33,Pin.OUT)
ledalarma = Pin (32,Pin.OUT)
estado = 0



def blink_led(times=4, delay=0.2):
  original_state = led.value()

  for _ in range(times):
    led.value(1)
    time.sleep(delay)
    led.value(0)
    time.sleep(delay)

  led.value(original_state)


def get_led_state():
  if led.value() == 1:
    return 'ON'
  return 'OFF'


def get_request_path(request):
  try:
    request_str = str(request)
    first_line = request_str.split('\n')[0]
    parts = first_line.split(' ')
    if len(parts) >= 2:
      return parts[1]
  except Exception:
    pass
  return ''


alert_message = ""
led_on_count = 0
auto_mode = False
start_time = time.time()


def get_uptime():
  elapsed = int(time.time() - start_time)
  hours = elapsed // 3600
  minutes = (elapsed % 3600) // 60
  seconds = elapsed % 60
  return f"{hours}h {minutes}m {seconds}s"


def web_page():
  global alert_message, auto_mode

  if led.value() == 1:
    gpio_state = "ON"
    status_class = "online"
  else:
    gpio_state = "OFF"
    status_class = "offline"

  auto_status = "Activado" if auto_mode else "Desactivado"
  uptime = get_uptime()

  alert_html = ""
  if alert_message:
    alert_html = '<div class="alert" id="alertBox"><span>' + alert_message + '</span><button type="button" onclick="this.parentElement.style.display=\'none\'">✕</button></div>'

  html = """<html>
  <head>
    <title>ESP Web Server</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="data:,">
    <style>
      :root {
        --bg1: #07111f;
        --bg2: #14283f;
        --panel: rgba(255,255,255,0.12);
        --border: rgba(255,255,255,0.22);
        --text: #f8fbff;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        font-family: "Segoe UI", Arial, sans-serif;
        background: linear-gradient(135deg, var(--bg1), var(--bg2));
        color: var(--text);
        overflow: hidden;
        position: relative;
      }
      .orb {
        position: absolute;
        border-radius: 50%;
        filter: blur(40px);
        opacity: 0.35;
        animation: float 8s ease-in-out infinite;
      }
      .orb.one { width: 180px; height: 180px; background: #3b82f6; top: 10%; left: 8%; }
      .orb.two { width: 220px; height: 220px; background: #8b5cf6; bottom: 8%; right: 10%; animation-delay: 2s; }
      .orb.three { width: 140px; height: 140px; background: #10b981; top: 45%; right: 20%; animation-delay: 4s; }
      .panel {
        position: relative;
        z-index: 2;
        width: min(92vw, 640px);
        padding: 1.6rem;
        border-radius: 24px;
        background: var(--panel);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border: 1px solid var(--border);
        box-shadow: 0 20px 60px rgba(0,0,0,0.35);
        text-align: center;
      }
      h1 { margin: 0 0 0.4rem; font-size: 1.8rem; }
      .subtitle { margin-bottom: 1rem; opacity: 0.85; }
      .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.6rem 1rem;
        border-radius: 999px;
        font-weight: 700;
        margin-bottom: 1.2rem;
        animation: pulse 2s infinite;
      }
      .panel {
        animation: fadeInUp 0.8s ease;
      }
      .alert {
        animation: fadeInUp 0.5s ease;
      }
      .status-pill.online { background: rgba(34,197,94,0.2); color: #bbf7d0; }
      .status-pill.offline { background: rgba(248,113,113,0.2); color: #fecaca; }
      .alert {
        margin: 0.8rem 0 0.2rem;
        padding: 0.75rem 1rem;
        border-radius: 12px;
        background: rgba(249, 115, 22, 0.18);
        color: #ffedd5;
        border: 1px solid rgba(249, 115, 22, 0.35);
        font-weight: 600;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.7rem;
      }
      .alert button {
        border: none;
        background: rgba(255,255,255,0.15);
        color: white;
        border-radius: 999px;
        padding: 0.3rem 0.6rem;
        cursor: pointer;
      }
      .info {
        margin: 1rem 0;
        padding: 0.8rem 1rem;
        border-radius: 12px;
        background: rgba(99, 102, 241, 0.12);
        border: 1px solid rgba(99, 102, 241, 0.3);
        font-size: 0.95rem;
        color: #c7d2fe;
      }
      .info-item { margin: 0.4rem 0; }
      .info-label { font-weight: 700; color: #a5b4fc; }
      .buttons, .actions { display: flex; justify-content: center; gap: 0.8rem; flex-wrap: wrap; margin-top: 0.8rem; }
      .btn {
        display: inline-block;
        text-decoration: none;
        color: white;
        font-weight: 700;
        padding: 0.95rem 1.1rem;
        border-radius: 14px;
        transition: transform 0.2s ease, box-shadow 0.2s ease, filter 0.2s ease;
        box-shadow: 0 8px 20px rgba(0,0,0,0.2);
        animation: fadeInUp 0.6s ease both;
      }
      .btn:hover { transform: translateY(-3px) scale(1.03); box-shadow: 0 14px 30px rgba(0,0,0,0.25); filter: brightness(1.08); }
      .btn-on { background: linear-gradient(135deg, #f59e0b, #f97316); }
      .btn-off { background: linear-gradient(135deg, #3b82f6, #2563eb); }
      .btn-green { background: linear-gradient(135deg, #10b981, #059669); }
      .btn-blue { background: linear-gradient(135deg, #8b5cf6, #6366f1); }
      @keyframes float {
        0%, 100% { transform: translateY(0) translateX(0); }
        50% { transform: translateY(-18px) translateX(10px); }
      }
      @keyframes pulse {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.03); }
      }
      @keyframes fadeInUp {
        from { opacity: 0; transform: translateY(10px); }
        to { opacity: 1; transform: translateY(0); }
      }
      @keyframes shimmer {
        0% { background-position: -200% 0; }
        100% { background-position: 200% 0; }
      }
    </style>
  </head>
  <body>
    <div class="orb one"></div>
    <div class="orb two"></div>
    <div class="orb three"></div>

    <div class="panel">
      <h1>ESP32 Web Control</h1>
      <div class="subtitle">Lenguajes Electronicos - Valentino Straw ESP32</div>
      <div class="status-pill """ + status_class + """">
        <span class="dot"></span>
        LED: <span>""" + gpio_state + """</span>
      </div>

      <div class="buttons">
        <a class="btn btn-on" href="/?led=on">ENCENDER</a>
        <a class="btn btn-off" href="/?led=off">APAGAR</a>
      </div>

      <div class="info">
        <div class="info-item"><span class="info-label">Activaciones:</span> """ + str(led_on_count) + """</div>
        <div class="info-item"><span class="info-label">Uptime:</span> """ + uptime + """</div>
        <div class="info-item"><span class="info-label">Modo Auto:</span> """ + auto_status + """</div>
      </div>

      """ + alert_html + """

      <div class="actions">
        <a class="btn btn-green" href="/?action=blink">PARPADEAR</a>
        <a class="btn btn-blue" href="/?action=status">VER ESTADO</a>
        <a class="btn btn-blue" href="/?action=auto">MODO AUTO</a>
        <a class="btn btn-off" href="/?action=reset">RESET</a>
      </div>
    </div>
    <script>
      setTimeout(function() {
        var alertBox = document.getElementById('alertBox');
        if (alertBox) {
          alertBox.style.display = 'none';
        }
      }, 2500);
    </script>
  </body>
  </html>"""
  return html

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('', 80))
s.listen(5)
s.setblocking(False)  # <-- clave para que no se cuelgue esperando una conexión

print("Levantando web server")
print(f"WiFi localIP: {station.ifconfig()[0]}")

bot_token = '8867119429:AAF15RiE_5qZJo9mCsW8cGV4Jh1cREKku2Q'
chat_id = '6208949582'  # Chat id del chat a enviar el mensaje

url = f'https://api.telegram.org/bot{bot_token}/sendMessage'

def payload_message(input, retries=2):
  message = input
  payload = {
      'chat_id': chat_id,
      'text': message
  }

  headers = {'Content-Type': 'application/json'}

  for intento in range(retries + 1):
    response = None
    try:
      response = urequests.post(url, headers=headers, data=ujson.dumps(payload))
      print('Response status:', response.status_code)
      print('Response text:', response.text)
      response.close()
      return payload
    except OSError as e:
      print(f'Error de red al mandar a Telegram (intento {intento+1}/{retries+1}): {e}')
      if response is not None:
        try:
          response.close()
        except Exception:
          pass
      sleep(1)

  print('No se pudo enviar el mensaje a Telegram, sigo sin cortar el programa.')
  return payload

sleep(2)  # <-- le da un respiro a la pila de red antes del primer HTTPS
payload = payload_message('Mensaje enviado desde NodeMCU! La pagina web está corriendo!')

def web_loop():
  global led_on_count, auto_mode, alert_message  # <-- faltaba esto

  try:
    conn, addr = s.accept()
  except OSError:
    # No hay ninguna conexión esperando, seguimos de largo
    return

  mensaje_telegram = None  # <-- inicializada afuera del try por si falla el recv

  try:
    print('Got a connection from %s' % str(addr))
    request = conn.recv(1024)
    request_str = str(request)
    print('Content = %s' % request_str)
    path = get_request_path(request)
    alert_message = ""

    if path == '/?led=on':
      if led.value() == 1:
        alert_message = 'El LED ya está prendido.'
        print('LED ya estaba encendido')
      else:
        print('LED ON')
        led.value(1)
        led_on_count += 1
        mensaje_telegram = 'LED ENCENDIDO desde NodeMCU! (Total: ' + str(led_on_count) + ')'
    elif path == '/?led=off':
      if led.value() == 0:
        alert_message = 'El LED ya está apagado.'
        print('LED ya estaba apagado')
      else:
        print('LED OFF')
        led.value(0)
        mensaje_telegram = 'LED APAGADO desde NodeMCU!'
    elif path == '/?action=status' or path == '/status':
      print('Estado solicitado')
      mensaje_telegram = 'Estado del LED: ' + get_led_state() + ' | Activaciones: ' + str(led_on_count)
    elif path == '/?action=blink':
      print('Parpadeo solicitado')
      blink_led()
      mensaje_telegram = 'Parpadeo solicitado desde NodeMCU!'
    elif path == '/?action=auto':
      auto_mode = not auto_mode
      mensaje_telegram = 'Modo Auto: ' + ('Activado' if auto_mode else 'Desactivado')
    elif path == '/?action=reset':
      led_on_count = 0
      alert_message = 'Contadores reseteados.'
      mensaje_telegram = 'Contadores reseteados!'

    response = web_page()
    conn.send('HTTP/1.1 200 OK\n')
    conn.send('Content-Type: text/html\n')
    conn.send('Connection: close\n\n')
    conn.sendall(response)
  except OSError as e:
    print(f'El cliente cortó la conexión durante el manejo del request: {e}')
  finally:
    conn.close()

  # El navegador ya tiene su respuesta. Recién ahora avisamos por Telegram,
  # así el click no se siente lento aunque Telegram tarde en contestar.
  if mensaje_telegram is not None:
    payload_message(mensaje_telegram)

# -----------

def AdafruitAlarm():
    global estado  # <-- faltaba esto

    if (boton1.value()==0):
        estado=estado+1
        if estado>=2:
            estado=0
        print(estado)
    sleep(tiempo_mensaje)
    if(estado==0):
        ledestado.off()
        ledalarma.off()
        enviar_msg(topico_lectura2,0)
        sleep(tiempo_mensaje)
        enviar_msg(topico_lectura3,0)
    if(estado==1):
        ledestado.on()
        enviar_msg(topico_lectura2,1)
        distancia = medidor.distance_cm()
        print (f"la distancia es de {distancia} cm ")
        enviar_msg(topico_lectura, distancia)
        sleep (1)
        if (distancia>30):
            ledalarma.off()
            buzzer.off()
            enviar_msg(topico_lectura3,0)
        if (distancia<=30):
            enviar_msg(topico_lectura3,1)
            ledalarma.on()
            buzzer.on()
            sleep(1)
            ledalarma.off()
            buzzer.off()
            sleep(1)

# ----------------

def __main__():
    web_loop()
    AdafruitAlarm()


while True:
    __main__()