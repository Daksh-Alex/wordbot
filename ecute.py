import discord
from discord import app_commands
import asyncio
import re
import os
from dotenv import load_dotenv
import mysql.connector
import aiohttp

import g_word

processing_users = set()

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ================= DB =================

def get_db():
    return mysql.connector.connect(
        host="localhost",
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database="wordbot",
        autocommit=True
    )

db = get_db()
cursor = db.cursor(buffered=True)


def safe_execute(query, params=None, fetch=False):
    global db, cursor
    try:
        cursor.execute(query, params or ())

        if fetch:
            return cursor.fetchall()

        return None

    except mysql.connector.errors.IntegrityError:
        return "DUPLICATE"

    except Exception as e:
        print("DB error:", e)

        try:
            db.reconnect(attempts=3, delay=2)
            cursor = db.cursor(buffered=True)

            cursor.execute(query, params or ())
            if fetch:
                return cursor.fetchall()

        except Exception as e2:
            print("Reconnect failed:", e2)

        return None


# ================= WOD =================

def save_wod(word, meaning, dyk):
    safe_execute("""
        INSERT INTO wod (id, word, meaning, dyk)
        VALUES (1, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            word = %s,
            meaning = %s,
            dyk = %s
    """, (word, meaning, dyk, word, meaning, dyk))


def load_wod():
    row = safe_execute(
        "SELECT word, meaning, dyk FROM wod WHERE id=1",
        fetch=True
    )
    return row[0] if row else None


def clear_submissions():
    safe_execute("DELETE FROM submissions")


def update_leaderboard(user_id, score):
    safe_execute("""
        INSERT INTO leaderboard (user_id, score)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE score = score + %s
    """, (user_id, score, score))


# ================= KEEP ALIVE =================

async def keep_db_alive():
    global db, cursor
    while True:
        try:
            cursor.execute("SELECT 1")
        except:
            try:
                db.reconnect(attempts=3, delay=2)
                cursor = db.cursor(buffered=True)
                print("DB reconnected")
            except Exception as e:
                print("Keepalive failed:", e)

        await asyncio.sleep(300)


# ================= QUEUE =================

queue = asyncio.Queue(maxsize=100)


# ================= DUPLICATE =================

def save_submission(user_id, sentence):
    result = safe_execute(
        "INSERT INTO submissions (user_id, sentence) VALUES (%s,%s)",
        (user_id, sentence)
    )
    return result != "DUPLICATE"


# ================= COLOR =================

def get_color(score):
    if score <= 5:
        return discord.Color.red()
    elif score < 10:
        return discord.Color.green()
    else:
        return discord.Color.gold()


# ================= AI =================

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
                    "model": "llama-3.1-8b-instant",
                    "temperature": 0.15,
                    "messages": [
                        {
                            "role": "system",
                            "content": """
You are a generous English evaluator.

Default to HIGH scores unless clearly wrong.

10/10 → correct + natural or creative
9/10 → correct but simple
8/10 → minor issues
≤7 → noticeable issues

Creative sentences ALWAYS get 10.

STRICT FORMAT:
Result: X/10
Reason: short
"""
                        },
                        {
                            "role": "user",
                            "content": f"""
Word: {word}

Be generous.

Sentence:
{sentence}
"""
                        }
                    ]
                },
                timeout=aiohttp.ClientTimeout(total=6)
            ) as res:

                data = await res.json()

                if "choices" not in data:
                    return "Result: 10/10\nReason: default reward"

                return data["choices"][0]["message"]["content"]

    except Exception:
        return "Result: 10/10\nReason: fallback reward"


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
    uid = message.author.id

    if uid in processing_users:
        return

    processing_users.add(uid)

    try:
        if message.author.bot:
            return

        if not g_word.active_game:
            return

        word = g_word.current_word
        content = message.content.lower()

        if not word or word not in content:
            return

        # 🔥 normalize
        clean = re.sub(r"\s+", " ", content.strip().lower())
        clean = clean.rstrip(".!?")

        if not save_submission(uid, clean):
            await message.reply("❌ Already used sentence.")
            return

        attempts = g_word.user_attempts.get(uid, 0)

        if attempts >= 2:
            await message.reply("❌ Only 2 attempts allowed.")
            return

        is_first = attempts == 0
        g_word.user_attempts[uid] = attempts + 1

        result = await grade_sentence(clean, word)

        match = re.search(r"Result:\s*(\d+)/10", result)
        score = int(match.group(1)) if match else 10

        if is_first:
            update_leaderboard(uid, score)

        creative = score == 10 and len(clean.split()) > 8

        title = "📊 Evaluation 🔥" if creative else "📊 Evaluation"

        embed = discord.Embed(
            title=title,
            description=result,
            color=get_color(score)
        )

        embed.set_footer(
            text="🏆 Counted attempt" if is_first else "🧪 Practice"
        )

        await message.reply(embed=embed)

    finally:
        processing_users.discard(uid)


# ================= EVENTS =================

@client.event
async def on_message(message):
    if queue.full():
        return

    if message.author.id in processing_users:
        return

    await queue.put(message)


@client.event
async def on_ready():
    print("Logged in as", client.user)

    await tree.sync()
    asyncio.create_task(keep_db_alive())

    state = load_wod()
    if state:
        g_word.current_word, g_word.current_meaning, g_word.current_dyk = state
        g_word.active_game = True

    if not hasattr(client, "started"):
        client.started = True
        asyncio.create_task(
            g_word.word_loop(
                client,
                CHANNEL_ID,
                save_wod,
                clear_submissions
            )
        )

    for _ in range(3):
        asyncio.create_task(worker())


# ================= COMMANDS =================

@tree.command(name="wod")
async def wod(interaction: discord.Interaction):
    word = g_word.current_word
    meaning = g_word.current_meaning
    dyk = g_word.current_dyk

    if not word:
        await interaction.response.send_message("⚠️ No word loaded yet")
        return

    embed = discord.Embed(title=f"📌 {word.upper()}", color=discord.Color.blue())
    embed.add_field(name="📖 Meaning", value=meaning, inline=False)

    if dyk:
        embed.add_field(name="🧠 Did You Know?", value=dyk, inline=False)

    await interaction.response.send_message(embed=embed)


@tree.command(name="fetch")
async def fetch(interaction: discord.Interaction):
    await interaction.response.defer()

    new_word, new_meaning = g_word.get_wod()

    if not new_word:
        await interaction.followup.send("⚠️ Failed to fetch word")
        return

    g_word.current_word = new_word
    g_word.current_meaning = new_meaning
    g_word.active_game = True
    g_word.user_attempts.clear()

    save_wod(new_word, new_meaning)
    clear_submissions()

    embed = discord.Embed(title=f"📌 {new_word.upper()}", color=discord.Color.blue())
    embed.add_field(name="📖 Meaning", value=new_meaning, inline=False)

    await interaction.followup.send(embed=embed)


# ================= LEADERBOARD =================

def get_leaderboard():
    return safe_execute("""
        SELECT user_id, score
        FROM leaderboard
        ORDER BY score DESC
        LIMIT 10
    """, fetch=True)


def get_user_rank(user_id):
    row = safe_execute(
        "SELECT score FROM leaderboard WHERE user_id=%s",
        (user_id,),
        fetch=True
    )

    if not row:
        return None, 0

    score = row[0][0]

    rank = safe_execute("""
        SELECT COUNT(*) + 1 FROM leaderboard
        WHERE score > %s
    """, (score,), fetch=True)[0][0]

    return rank, score


@tree.command(name="leaderboard")
async def leaderboard(interaction: discord.Interaction):

    rows = get_leaderboard()

    if not rows:
        await interaction.response.send_message("No leaderboard data yet.")
        return

    desc = ""
    for i, (uid, score) in enumerate(rows, start=1):
        desc += f"{i}. <@{uid}> — {score}\n"

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description=desc,
        color=discord.Color.gold()
    )

    rank, score = get_user_rank(interaction.user.id)

    if rank:
        embed.set_footer(text=f"Your rank: #{rank} • Score: {score}")
    else:
        embed.set_footer(text="You are not ranked yet.")

    await interaction.response.send_message(embed=embed)

client.run(TOKEN)