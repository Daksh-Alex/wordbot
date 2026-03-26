import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz

current_word = None
current_meaning = None
active_game = False
user_attempts = {}
MAX_ATTEMPTS = 2


async def get_merriam_webster_word():
    url = "https://www.merriam-webster.com/word-of-the-day"
    headers = {'User-Agent': 'Mozilla/5.0'}

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

    except:
        return "serendipity", "finding something good without looking for it"


async def word_of_day_loop(bot, channel_id):
    global current_word, current_meaning, active_game, user_attempts

    ist = pytz.timezone("Asia/Kolkata")

    while not bot.is_closed():
        now = datetime.now(ist)

        next_run = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)

        sleep_time = (next_run - now).total_seconds()
        print(f"Sleeping {sleep_time/3600:.2f} hours until 10 AM IST")

        await asyncio.sleep(sleep_time)

        current_word, current_meaning = await get_merriam_webster_word()
        active_game = True
        user_attempts.clear()

        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"🔥 **Word of the Day:** {current_word.upper()}\n📖 {current_meaning}"
            )
