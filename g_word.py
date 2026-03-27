import asyncio
import aiohttp
from bs4 import BeautifulSoup
import time

current_word = None
current_meaning = None
active_game = False
user_attempts = {}
MAX_ATTEMPTS = 2


async def get_merriam_webster_word():
    url = f"https://www.merriam-webster.com/word-of-the-day?nocache={time.time()}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as res:
                html = await res.text()

                soup = BeautifulSoup(html, 'html.parser')

                word_tag = soup.select_one("h2.word-header-txt")
                def_tag = soup.select_one(".wod-definition-container p")

                if not word_tag or not def_tag:
                    return "serendipity", "finding something good without looking for it"

                return word_tag.text.strip().lower(), def_tag.text.strip()

    except Exception as e:
        print("❌ Scraping error:", e)
        return "serendipity", "finding something good without looking for it"


async def word_of_day_loop(bot, channel_id):
    global current_word, current_meaning, active_game, user_attempts

    while not bot.is_closed():
        try:
            print("🔍 Checking for new word...")

            word, meaning = await get_merriam_webster_word()

            if current_word is None:
                current_word = word
                current_meaning = meaning
                active_game = True
                user_attempts.clear()
                print(f"🟢 Initial word set: {word}")

            elif word != current_word:
                print(f"🔥 New word detected: {word}")

                current_word = word
                current_meaning = meaning
                active_game = True
                user_attempts.clear()

                channel = bot.get_channel(channel_id)

                if channel:
                    await channel.send(
                        f"🔥 **NEW WORD OF THE DAY!**\n\n"
                        f"📌 **{word.upper()}**\n"
                        f"📖 {meaning}"
                    )

                    print("📢 New word announced")

            else:
                print("⏳ No new word yet")

        except Exception as e:
            print("❌ Loop error:", e)

        await asyncio.sleep(600)  # check every 10 min