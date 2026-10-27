import os
from dotenv import load_dotenv
import telebot

# Esto busca el archivo .env y carga las variables automáticamente
load_dotenv() 

# Ahora puedes acceder a tu token de forma segura
BOT_TOKEN = os.getenv("BOT_TOKEN")

print(f"Token cargado: {BOT_TOKEN}")

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start', 'hello'])
def send_welcome(message):
    bot.reply_to(message, "Howdy, how are you doing?")

@bot.message_handler(func=lambda msg: True)
def echo_all(message):
    bot.reply_to(message, message.text)

bot.infinity_polling()