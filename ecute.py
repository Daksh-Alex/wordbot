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
            result = cursor.fetchall()
        else:
            try:
                cursor.fetchall()
            except:
                pass
            result = None

        return result

    except mysql.connector.errors.IntegrityError:
        return "DUPLICATE"

    except Exception as e:
        print("DB error:", e)

        try:
            db.reconnect(attempts=3, delay=2)
            cursor = db.cursor()

            cursor.execute(query, params or ())
            if fetch:
                return cursor.fetchall()
        except Exception as e2:
            print("Reconnect failed:", e2)

        return None


# ================= WOD (SQL STORAGE) =================

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


async def keep_db_alive():
    global db, cursor
    while True:
        try:
            cursor.execute("SELECT 1")
        except:
            try:
                db.reconnect(attempts=3, delay=2)
                cursor = db.cursor()
                print("DB reconnected (keepalive)")
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


# ================= SCORE COLOR =================
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
                    "messages": [
                        {"role": "system", "content": "Grade sentence. Output:\nResult: X/10\nReason: short"},
                        {"role": "user", "content": f"Word: {word}\nSentence: {sentence}"}
                    ]
                },
                timeout=aiohttp.ClientTimeout(total=6)
            ) as res:

                data = await res.json()
                print("AI RAW:", data)

                if "choices" not in data:
                    return "Result: 7/10 Reason: AI issue"

                return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI EXCEPTION:", e)
        return "Result: 7/10 Reason: system error"


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

    uid = message.author.id
    clean = re.sub(r"\s+", " ", content.strip())

    if not save_submission(uid, clean):
        await message.reply("❌ This sentence has already been used.")
        return

    current_attempts = g_word.user_attempts.get(uid, 0)

    if current_attempts >= 2:
        await message.reply("❌ You have used all 2 attempts.")
        return

    is_first_attempt = current_attempts == 0
    g_word.user_attempts[uid] = current_attempts + 1

    result = await grade_sentence(clean, word)

    match = re.search(r"Result:\s*(\d+)/10", result)
    score = int(match.group(1)) if match else 7

    if is_first_attempt:
        update_leaderboard(uid, score)

    embed = discord.Embed(
        title="📊 Evaluation",
        description=result,
        color=get_color(score)
    )

    embed.set_footer(
        text="Counted attempt (1/2)" if is_first_attempt else "Practice attempt"
    )

    await message.reply(embed=embed)


# ================= EVENTS =================
@client.event
async def on_message(message):
    if not queue.full():
        await queue.put(message)


@client.event
async def on_ready():
    print("Logged in as", client.user)

    await tree.sync()
    asyncio.create_task(keep_db_alive())

    # 🔥 LOAD WOD FROM SQL
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

    new_word, new_meaning, did_you_know = g_word.get_wod()

    if not new_word:
        await interaction.followup.send("⚠️ Failed to fetch word")
        return

    g_word.current_word = new_word
    g_word.current_meaning = new_meaning
    g_word.current_dyk = did_you_know
    g_word.active_game = True
    g_word.user_attempts.clear()

    save_wod(new_word, new_meaning, did_you_know)
    clear_submissions()

    embed = discord.Embed(title=f"📌 {new_word.upper()}", color=discord.Color.blue())
    embed.add_field(name="📖 Meaning", value=new_meaning, inline=False)

    if did_you_know:
        embed.add_field(name="🧠 Did You Know?", value=did_you_know, inline=False)

    await interaction.followup.send(embed=embed)


client.run(TOKEN)