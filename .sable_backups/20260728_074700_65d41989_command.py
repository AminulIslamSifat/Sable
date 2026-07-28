from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import main_keyboard, user_data_path, WEB_APP_URL
from bot.services.database import db
import json
import os



admin_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🛠 Admin Panel", url=f"{WEB_APP_URL}/panel/"), InlineKeyboardButton("Show User", callback_data="admin:show_user")],
    [InlineKeyboardButton("Toggle Routine", callback_data="admin:routine_toggle"), InlineKeyboardButton("Circulate Routine", callback_data="admin:circulate_routine")],
    [InlineKeyboardButton("Publish Notice", callback_data="admin:notice"), InlineKeyboardButton("Share File", callback_data="admin:share_file")],
    [InlineKeyboardButton("Circulate Schedule", callback_data="admin:circulate_schedule")],
    [InlineKeyboardButton("Cancel", callback_data="admin:cancel")]
])

help_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("Readme", url="https://github.com/AminulIslamSifat/phantom_bot/blob/main/README.md")],
    [InlineKeyboardButton("User Guide", url="https://github.com/AminulIslamSifat/phantom_bot/blob/main/user_guide.md")],
    [InlineKeyboardButton("Developer Guide", url="https://github.com/AminulIslamSifat/phantom_bot/blob/main/developer_helper.md")]
])



async def start(update:Update, context: ContextTypes) -> None:
    user_id = update.effective_user.id
    active_user = []
    if os.path.exists(user_data_path):
        with open(user_data_path, "r") as file:
            user_data = json.load(file)
    
    for user, data in user_data.items():
        if data["user_id"] != None:
            active_user.append(data["user_id"])
    
    if user_id not in active_user:
        await update.message.reply_text(f"Hello {update.effective_user.last_name}, Please register by /register to enjoy the full survices.", reply_markup=main_keyboard)
    else:
        await update.message.reply_text(f"Welcome Back {update.effective_user.last_name}", reply_markup=main_keyboard)


async def help(update:Update, context:ContextTypes) -> None:
    await update.message.reply_text("The Guide on how to use this is given below: ", reply_markup=help_keyboard)

async def admin(update:Update, context:ContextTypes) -> None:
    user_id = update.effective_user.id
    import asyncio
    is_admin = await asyncio.to_thread(db["admin"].find_one, {"user_id": {"$in": [user_id, str(user_id)]}})
    if not is_admin:
        await update.message.reply_text("Sorry, You are not an admin")
        return
    await update.message.reply_text("Admin Panel: ", reply_markup=admin_keyboard)
