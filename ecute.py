import discord
from discord import app_commands
import asyncio
import re
import os
from dotenv import load_dotenv
import mysql.connector
import aiohttp

import g_word

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ================= DB =================
db = mysql.connector.connect(
    host="localhost",
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database="wordbot"
)
cursor = db.cursor()

# ================= QUEUE =================
queue = asyncio.Queue(maxsize=100)

# ================= DUPLICATE =================
def is_duplicate(sentence):
    cursor.execute("SELECT 1 FROM submissions WHERE sentence=%s", (sentence,))
    return cursor.fetchone() is not None


def save_submission(user_id, sentence):
    try:
        cursor.execute(
            "INSERT INTO submissions (user_id, sentence) VALUES (%s,%s)",
            (user_id, sentence)
        )
        db.commit()
        return True
    except Exception as e:
        if "Duplicate entry" in str(e):
            return False
        print("DB error:", e)
        return False


# ================= AI GRADING =================
async def grade_sentence(sentence, word):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama3-8b-8192",
                    "messages": [
                        {
                            "role": "system",
                            "content": """
                            Grade the sentence based on:
                            1. Correct usage of the word
                            2. Grammar
                            3. Meaningfulness

                            Be lenient.

                            Give higher scores when word is used correctly even if sentence is simple.

                            Output STRICTLY:
                            Result: X/10
                            Reason: short
                            ="""
                        },
                        {
                            "role": "user",
                            "content": f"Word: {word}\nSentence: {sentence}"
                        }
                    ]
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as res:
                data = await res.json()
                return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI error:", e)
        return "Result: 5/10 Reason: fallback"


# ================= WORKER =================
async def worker():
    while True:
        msg = await queue.get()
        try:
            await process(msg)
        except Exception as e:
            print("Worker error:", e)
        finally:
            queue.task_done()


# ================= PROCESS =================
async def process(message):
    if message.author.bot:
        return

    if not g_word.active_game:
        return

    word = g_word.current_word
    content = message.content.lower()

    if not word or word not in content:
        return

    clean = re.sub(r"\s+", " ", content.strip())

    if is_duplicate(clean):
        await message.reply("❌ This sentence already used")
        return

    result = await grade_sentence(clean, word)

    saved = save_submission(message.author.id, clean)

    if not saved:
        await message.reply("❌ Duplicate sentence")
        return

    embed = discord.Embed(
        title="📊 Evaluation",
        description=result,
        color=discord.Color.green()
    )

    await message.reply(embed=embed)

def get_leaderboard():
    cursor.execute("""
        SELECT user_id, score
        FROM leaderboard
        ORDER BY score DESC
        LIMIT 10
    """)
    return cursor.fetchall()
# ================= EVENTS =================
@client.event
async def on_message(message):
    if queue.full():
        return
    await queue.put(message)


@client.event
async def on_ready():
    print("Logged in as", client.user)

    await tree.sync()

    if not hasattr(client, "started"):
        client.started = True
        asyncio.create_task(g_word.word_loop(client, CHANNEL_ID))

    for _ in range(3):
        asyncio.create_task(worker())


# ================= COMMANDS =================

@tree.command(name="leaderboard", description="Top players")
async def leaderboard(interaction: discord.Interaction):

    rows = get_leaderboard()

    if not rows:
        await interaction.response.send_message("No data yet")
        return

    desc = ""

    for i, (uid, score) in enumerate(rows, start=1):
        desc += f"{i}. <@{uid}> — {score}\n"

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description=desc,
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="wod", description="Show current word")
async def wod(interaction: discord.Interaction):
    word = g_word.current_word
    meaning = g_word.current_meaning

    if not word:
        await interaction.response.send_message("⚠️ No word loaded yet")
        return

    embed = discord.Embed(
        title="📌 Current Word",
        description=f"**{word.upper()}**\n\n{meaning}",
        color=discord.Color.blue()
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="fetch", description="Fetch new word manually")
async def fetch(interaction: discord.Interaction):

    await interaction.response.defer()

    old_word = g_word.current_word

    for _ in range(3):
        new_word, new_meaning = g_word.get_wod()

        if new_word and new_word != old_word:
            g_word.current_word = new_word
            g_word.current_meaning = new_meaning
            g_word.active_game = True
            g_word.user_attempts.clear()

            g_word.save_state(new_word)

            cursor.execute("DELETE FROM submissions")
            db.commit()

            embed = discord.Embed(
                title="🔥 New Word",
                description=f"**{new_word.upper()}**\n\n{new_meaning}",
                color=discord.Color.green()
            )

            await interaction.followup.send(embed=embed)
            return

        await asyncio.sleep(2)

    await interaction.followup.send(
        f"⚠️ No new word yet.\nCurrent: **{old_word.upper()}**"
    )


client.run(TOKEN)