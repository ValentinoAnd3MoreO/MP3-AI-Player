import network
import urequests
import ujson
import time

ssid = 'Vllamada'
password = 'Video_2025'

station = network.WLAN(network.STA_IF)
station.active(True)
station.connect(ssid, password)

# Asegurarse que se conectó a internet
while not station.isconnected():
    print(".")
    time.sleep(0.2)

print(f'Connectado a wifi {ssid}')
print(station.ifconfig())

bot_token = '8867119429:AAF15RiE_5qZJo9mCsW8cGV4Jh1cREKku2Q'
chat_id = '6208949582'  # Chat id del chat a enviar el mensaje
message = 'Mensaje enviado desde NodeMCU!'

url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
payload = {
    'chat_id': chat_id,
    'text': message
}

headers = {'Content-Type': 'application/json'}
response = urequests.post(url, headers=headers, data=ujson.dumps(payload))

print('Response status:', response.status_code)
print('Response text:', response.text)
response.close()