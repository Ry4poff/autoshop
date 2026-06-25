import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mrshop.astck.com").strip()
BOT_NAME = os.getenv("BOT_NAME", "MRSHOP").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN manquant. Mets-le dans les variables Railway ou dans un fichier .env local.")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


def inline_shop_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Ouvrir la boutique",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )


def reply_shop_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🛒 Boutique MRSHOP",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Ouvre la boutique MRSHOP",
    )


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        f"👋 Bienvenue sur <b>{BOT_NAME}</b>\n\n"
        "Clique sur le bouton ci-dessous pour ouvrir la boutique directement dans Telegram.",
        reply_markup=inline_shop_button(),
    )

    await message.answer(
        "Tu peux aussi utiliser le bouton clavier juste en dessous 👇",
        reply_markup=reply_shop_keyboard(),
    )


@dp.message(Command("shop"))
async def shop(message: Message):
    await message.answer(
        "🛒 Ouvre la boutique MRSHOP ici :",
        reply_markup=inline_shop_button(),
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "<b>Commandes disponibles :</b>\n\n"
        "/start — lancer le bot\n"
        "/shop — ouvrir la boutique\n"
        "/help — afficher l’aide"
    )


@dp.message(F.web_app_data)
async def webapp_data(message: Message):
    """
    Ce handler sert uniquement si ton site utilise plus tard :
    window.Telegram.WebApp.sendData(...)
    """
    data = message.web_app_data.data

    await message.answer(
        "✅ Donnée reçue depuis la WebApp :\n"
        f"<code>{data}</code>"
    )


async def main():
    logging.basicConfig(level=logging.INFO)

    await bot.delete_webhook(drop_pending_updates=True)

    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="🛒 Boutique",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    )

    print(f"✅ Bot lancé : {BOT_NAME}")
    print(f"✅ WebApp URL : {WEBAPP_URL}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
