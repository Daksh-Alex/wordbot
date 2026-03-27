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

# ================= LOAD ENV =================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DISCORD_CHANNEL_ID = 1486439713949614120

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

# ================= SYSTEM =================
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
            {"role": "system", "content": "Lenient evaluator"},
            {"role": "user", "content": f"""
Word: {word}
Sentence: {sentence}

Return:

Evaluation criteria:
1. Relevance: The sentence should use the given word in a generally correct way.
2. Grammar: Minor grammar mistakes are acceptable if meaning is clear.
3. Clarity: The sentence should be understandable.
4. Context: The word should fit reasonably well, even if slightly forced.
5. Effort: Reward genuine attempts positively.

Scoring rules:
- Score must be an integer from 0 to 10.
- Be slightly lenient in grading.
- Give higher scores (8–10) for most correct attempts.
- Give 10/10 for any clear, correct, and natural sentence, even if simple.
- Do NOT penalize small grammar or punctuation mistakes heavily.

Score guide:
- 0–2: Completely incorrect or meaningless.
- 3–5: Weak attempt but shows some relevance.
- 6–7: Understandable with noticeable issues.
- 8–9: Good sentence with minor or no issues.
- 10: Clear, correct, and natural usage (even if simple).

Score: X/10
Reason: <very short and crisp explanation>

Rules for output:
- Do NOT write anything except the two lines.
- Keep the reason under 10 words.
- No extra punctuation, emojis, or explanations.
- Keep tone neutral and concise.
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
    if not g_word.active_game or not g_word.current_word:
        return

    if not contains_word(message.content, g_word.current_word):
        return

    user_id = message.author.id

    if is_spamming(user_id):
        await message.reply("⏳ Cooldown active.")
        return

    count = g_word.user_attempts.get(user_id, 0)

    if count >= g_word.MAX_ATTEMPTS:
        await message.reply("⚠️ Max attempts reached")
        return

    result = await grade_sentence(g_word.current_word, message.content[:200])

    match = re.search(r"(\d+)/10", result)
    score = int(match.group(1)) if match else 0

    if count > 0:
        score = max(score - 2, 1)

    update_score(user_id, message.author.name, score)

    g_word.user_attempts[user_id] = count + 1

    embed = discord.Embed(
    title="📊 Sentence Evaluation",
    description=result,
    color=discord.Color.blue()
    )
    embed.add_field(name="Points Earned", value=f"+{score}", inline=False)
    embed.set_footer(text=f"User: {message.author.name}")
    await message.reply(
        content=message.author.mention,
        embed=embed
    )


# ================= WORKERS =================
async def worker():
    while True:
        msg = await task_queue.get()
        try:
            await process_message(msg)
        except Exception as e:
            print("Worker error:", e)
        task_queue.task_done()


# ================= COMMANDS =================

@tree.command(name="leaderboard", description="Top 10 players")
async def leaderboard(interaction: discord.Interaction):

    cursor.execute(
        "SELECT username, score FROM leaderboard ORDER BY score DESC LIMIT 10"
    )
    top = cursor.fetchall()

    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())

    for i, (username, score) in enumerate(top, start=1):
        embed.add_field(name=f"{i}. {username}", value=f"{score} pts", inline=False)

    cursor.execute(
        "SELECT score FROM leaderboard WHERE user_id=%s",
        (interaction.user.id,)
    )
    user_score = cursor.fetchone()

    if user_score and interaction.user.name not in [x[0] for x in top]:
        embed.add_field(
            name="🔍 Your Score",
            value=f"{interaction.user.name}: {user_score[0]} pts",
            inline=False
        )

    embed.set_footer(text=f"Requested by {interaction.user.name}")

    await interaction.response.send_message(embed=embed)


@tree.command(name="fetchword", description="Resend current word")
async def fetchword(interaction: discord.Interaction):

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Admin only command.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"📌 {g_word.current_word.upper()}\n📖 {g_word.current_meaning}"
    )


@tree.command(name="fetchnewword", description="Force fetch new word")
async def fetchnewword(interaction: discord.Interaction):

    await interaction.response.defer()

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admin only command.")
        return

    word, meaning = await g_word.get_merriam_webster_word()

    g_word.current_word = word
    g_word.current_meaning = meaning
    g_word.active_game = True
    g_word.user_attempts.clear()

    await interaction.followup.send(
        f"⚡ **New Word:** {word.upper()}\n📖 {meaning}"
    )


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

    await task_queue.put(message)


client.run(DISCORD_TOKEN)
