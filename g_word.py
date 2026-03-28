import asyncio
import feedparser
from bs4 import BeautifulSoup
import discord
import json
import os

STATE_FILE = "wod_state.json"

current_word = None
current_meaning = None
active_game = False
user_attempts = {}
MAX_ATTEMPTS = 2


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f).get("word")
    except:
        return None


def save_state(word):
    with open(STATE_FILE, "w") as f:
        json.dump({"word": word}, f)


def get_wod():
    try:
        feed = feedparser.parse("https://www.merriam-webster.com/wotd/feed/rss2")

        if not feed.entries:
            return None, None

        entry = feed.entries[0]
        word = entry.title.strip().lower()
        meaning = BeautifulSoup(entry.description, "html.parser").text.strip()

        return word, meaning

    except Exception as e:
        print("RSS error:", e)
        return None, None


async def word_loop(bot, channel_id):
    global current_word, current_meaning, active_game, user_attempts

    last_announced = load_state()

    while True:
        try:
            word, meaning = get_wod()

            if not word:
                await asyncio.sleep(120)
                continue

            if current_word is None:
                current_word = word
                current_meaning = meaning
                active_game = True

            elif word != current_word and word != last_announced:
                current_word = word
                current_meaning = meaning
                active_game = True
                user_attempts.clear()

                save_state(word)
                last_announced = word

                channel = bot.get_channel(channel_id)
                if channel:
                    embed = discord.Embed(
                        title="🔥 New Word of the Day",
                        description=f"**{word.upper()}**\n\n{meaning}",
                        color=discord.Color.blue()
                    )
                    await channel.send(embed=embed)

        except Exception as e:
            print("Loop error:", e)

        await asyncio.sleep(180)