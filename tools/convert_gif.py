"""One-time script: convert GIF document to animation, save file_id."""
import asyncio
import os
import tempfile
from aiogram import Bot
from aiogram.types import FSInputFile
from auditor.config import settings


async def main():
    bot = Bot(token=settings.BOT_TOKEN)
    doc_id = "BQACAgIAAxkBAAIBZ2oRhpMTzIK72CwZLjzhpMghE3t_AALrmwAC8xaQSO_8_DvMGcLiOwQ"

    print("Downloading GIF document...")
    file = await bot.get_file(doc_id)
    dest = os.path.join(tempfile.gettempdir(), "bot_welcome.gif")
    await bot.download_file(file.file_path, dest)
    print(f"Downloaded to: {dest} ({os.path.getsize(dest)} bytes)")

    # Send to admin to get animation file_id
    print("Sending as animation to admin...")
    sent = await bot.send_animation(
        chat_id=settings.ADMIN_USER_ID,
        animation=FSInputFile(dest),
    )
    anim_id = sent.animation.file_id
    print(f"\nAnimation file_id: {anim_id}")

    # Save to file
    config_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(config_dir, "..", "auditor", "data", "gif_animation_id.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(anim_id)
    print(f"Saved to: {path}")
    print("\nDone! Use this file_id in handlers.py")
    await bot.session.close()


asyncio.run(main())
