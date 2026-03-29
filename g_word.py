import asyncio
import feedparser
from bs4 import BeautifulSoup
import discord
import json
import os

STATE_FILE = "wod_state.json"

current_word = None
current_meaning = None
current_dyk = None
active_game = False
user_attempts = {}
MAX_ATTEMPTS = 2


# ---------- STATE ----------

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


# ---------- FETCH + PARSE ----------

def get_wod():
    try:
        feed = feedparser.parse("https://www.merriam-webster.com/wotd/feed/rss2")

        if not feed.entries:
            return None, None, None

        entry = feed.entries[0]
        word = entry.title.strip().lower()

        soup = BeautifulSoup(entry.description, "html.parser")
        text = soup.get_text("\n")

        lines = [l.strip() for l in text.split("\n") if l.strip()]

        meaning = ""
        did_you_know = ""
        # 🔍 Extract Meaning (robust)
        for line in lines:
            lower = line.lower()
            # ❌ skip junk lines
            if "word of the day" in lower:
                continue
            if "merriam-webster" in lower:
               continue
            if "did you know" in lower:
               continue
            # ✅ definition-like lines
            if (
                "means" in lower or
                "is used to" in lower or
                "refers to" in lower or
                "describes" in lower or
                "is" in lower
            ):
                if len(line.split()) > 6:
                    meaning = line.strip(":- ")
                   break
        # 🔁 fallback (safe)
        if not meaning:
            for line in lines:
                lower = line.lower()
                if "word of the day" in lower:
                    continue
                if "merriam-webster" in lower:
                   continue
                if "did you know" in lower:
                    continue
                if len(line.split()) > 6:
                    meaning = line.strip(":- ")
                    break

        # 🔍 Extract Did You Know
        for i, line in enumerate(lines):
            if "did you know" in line.lower():
                did_you_know = " ".join(lines[i+1:i+3])[:300]
                break

        return word, meaning, did_you_know

    except Exception as e:
        print("RSS error:", e)
        return None, None, None


# ---------- MAIN LOOP ----------

async def word_loop(bot, channel_id):
    global current_word, current_meaning, current_dyk, active_game, user_attempts

    last_announced = load_state()

    while True:
        try:
            word, meaning, did_you_know = get_wod()

            if not word:
                await asyncio.sleep(120)
                continue

            # first load
            if current_word is None:
                current_word = word
                current_meaning = meaning
                current_dyk = did_you_know
                active_game = True

            # new word detected
            elif word != current_word and word != last_announced:
                current_word = word
                current_meaning = meaning
                current_dyk = did_you_know
                active_game = True
                user_attempts.clear()

                save_state(word)
                last_announced = word

                channel = bot.get_channel(channel_id)
                if channel:
                    embed = discord.Embed(
                        title=f"📌 {word.upper()}",
                        color=discord.Color.blue()
                    )

                    embed.add_field(
                        name="📖 Meaning",
                        value=meaning or "Not available",
                        inline=False
                    )

                    if did_you_know:
                        embed.add_field(
                            name="🧠 Did You Know?",
                            value=did_you_know,
                            inline=False
                        )

                    embed.set_footer(text="Word of the Day")

                    await channel.send(embed=embed)

        except Exception as e:
            print("Loop error:", e)

        await asyncio.sleep(180)