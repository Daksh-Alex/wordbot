import asyncio
import aiohttp
import re
import discord
from discord import app_commands
import mysql.connector
import time
from collections import defaultdict
import os
from dotenv import load_dotenv

import g_word
from g_word import word_of_day_loop

# ================= ENV =================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DISCORD_CHANNEL_ID = YOUR_CHANNEL_ID  # replace

# ================= DB =================
db = mysql.connector.connect(
    host="localhost",
    user=DB_USER,
    password=DB_PASSWORD,
    database="wordbot"
)
cursor = db.cursor()

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

task_queue = asyncio.Queue()
user_last_used = defaultdict(float)

COOLDOWN = 5
WORKERS = 5


# ================= AI =================
async def grade_sentence(word, sentence):
    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Strict evaluator"},
            {"role": "user", "content": f"""
Word: {word}
Sentence: {sentence}

Return:
Score: X/10
Reason: short
"""}
        ],
        "temperature": 0.3
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as res:
            data = await res.json()
            return data["choices"][0]["message"]["content"].strip()


# ================= HELPERS =================
def contains_word(sentence, word):
    return re.search(rf"\b{re.escape(word)}\b", sentence, re.IGNORECASE)


def is_spamming(user_id):
    now = time.time()
    if now - user_last_used[user_id] < COOLDOWN:
        return True
    user_last_used[user_id] = now
    return False


def update_score(user_id, username, points):
    cursor.execute("SELECT score FROM leaderboard WHERE user_id=%s", (user_id,))
    result = cursor.fetchone()

    if result:
        cursor.execute(
            "UPDATE leaderboard SET score = score + %s WHERE user_id=%s",
            (points, user_id)
        )
    else:
        cursor.execute(
            "INSERT INTO leaderboard (user_id, username, score) VALUES (%s, %s, %s)",
            (user_id, username, points)
        )

    db.commit()


# ================= PROCESS =================
async def process_message(message):
    try:
        if not g_word.active_game or not g_word.current_word:
            return

        if message.channel.id != DISCORD_CHANNEL_ID:
            return

        if not contains_word(message.content, g_word.current_word):
            return

        user_id = message.author.id

        if is_spamming(user_id):
            await message.reply("⏳ Cooldown active.")
            return

        count = g_word.user_attempts.get(user_id, 0)

        if count >= g_word.MAX_ATTEMPTS:
            await message.reply("⚠️ Max attempts reached.")
            return

        result = await grade_sentence(
            g_word.current_word,
            message.content[:200]
        )

        match = re.search(r"(\d+)/10", result)
        original_score = int(match.group(1)) if match else 0

        score = original_score

        if count > 0:
            score = max(score - 2, 1)

        result = re.sub(r"\d+/10", f"{score}/10", result)

        update_score(user_id, message.author.name, score)

        g_word.user_attempts[user_id] = count + 1

        color = (
            discord.Color.green() if score >= 7 else
            discord.Color.orange() if score >= 4 else
            discord.Color.red()
        )

        embed = discord.Embed(
            title="📊 Sentence Evaluation",
            description=result,
            color=color
        )

        embed.add_field(name="Word", value=g_word.current_word.upper(), inline=True)
        embed.add_field(name="Points Earned", value=f"+{score}", inline=True)

        await message.reply(
            content=message.author.mention,
            embed=embed
        )

    except Exception as e:
        print("❌ Error:", e)


# ================= WORKERS =================
async def worker():
    while True:
        msg = await task_queue.get()
        await process_message(msg)
        task_queue.task_done()


# ================= EVENTS =================
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    await tree.sync()

    asyncio.create_task(word_of_day_loop(client, DISCORD_CHANNEL_ID))

    for _ in range(WORKERS):
        asyncio.create_task(worker())


@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id != DISCORD_CHANNEL_ID:
        return

    await task_queue.put(message)


# ================= COMMANDS =================
@tree.command(name="wod", description="📌 Show current word")
async def wod(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"📌 {g_word.current_word.upper()}\n📖 {g_word.current_meaning}"
    )


@tree.command(name="fetch", description="⚡ Fetch new word")
async def fetch(interaction: discord.Interaction):

    await interaction.response.defer()

    old_word = g_word.current_word

    word = old_word
    meaning = g_word.current_meaning

    for _ in range(3):
        new_word, new_meaning = await g_word.get_merriam_webster_word()
        if new_word != old_word:
            word = new_word
            meaning = new_meaning
            break
        await asyncio.sleep(2)

    if word == old_word:
        await interaction.followup.send(
            f"⚠️ No new word yet.\nCurrent word is still **{old_word.upper()}**"
        )
        return

    g_word.current_word = word
    g_word.current_meaning = meaning
    g_word.active_game = True
    g_word.user_attempts.clear()

    await interaction.followup.send(
        f"🔥 **New Word:** {word.upper()}\n📖 {meaning}"
    )


@tree.command(name="leaderboard", description="🏆 Top players")
async def leaderboard(interaction: discord.Interaction):

    cursor.execute(
        "SELECT username, score FROM leaderboard ORDER BY score DESC LIMIT 10"
    )
    top = cursor.fetchall()

    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())

    for i, (username, score) in enumerate(top, start=1):
        embed.add_field(name=f"{i}. {username}", value=f"{score} pts", inline=False)

    await interaction.response.send_message(embed=embed)


client.run(DISCORD_TOKEN)