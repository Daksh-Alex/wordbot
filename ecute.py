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
cursor = db.cursor()


def safe_execute(query, params=None, fetch=False):
    global db, cursor
    try:
        cursor.execute(query, params or ())

        if fetch:
            result = cursor.fetchall()
        else:
            # 🔥 fix unread result bug
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

    if result == "DUPLICATE":
        return False

    return True


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
                        {
                            "role": "system",
                            "content": """Grade the sentence based on usage, grammar, and meaning.
Output STRICTLY:
Result: X/10
Reason: short"""
                        },
                        {
                            "role": "user",
                            "content": f"Word: {word}\nSentence: {sentence}"
                        }
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

    # 🔒 DUPLICATE CHECK FIRST (no attempt loss)
    saved = save_submission(uid, clean)
    if not saved:
        await message.reply("❌ This sentence has already been used.")
        return

    # 🎯 CHECK FIRST ATTEMPT BEFORE INCREMENT
    current_attempts = g_word.user_attempts.get(uid, 0)

    if current_attempts >= 2:
        await message.reply("❌ You have used all 2 attempts.")
        return

    is_first_attempt = current_attempts == 0

    # ➕ INCREMENT ATTEMPT
    g_word.user_attempts[uid] = current_attempts + 1

    # 🤖 AI GRADING
    result = await grade_sentence(clean, word)

    # 🔢 EXTRACT SCORE
    match = re.search(r"Result:\s*(\d+)/10", result)
    score = int(match.group(1)) if match else 7

    # 🏆 UPDATE LEADERBOARD (ONLY FIRST ATTEMPT)
    if is_first_attempt:
        update_leaderboard(uid, score)

    # 🎨 EMBED COLOR BASED ON SCORE
    embed = discord.Embed(
        title="📊 Evaluation",
        description=result,
        color=get_color(score)
    )

    # 🧠 FOOTER (CLEAR UX)
    if is_first_attempt:
        embed.set_footer(text="Counted attempt (1/2)")
    else:
        embed.set_footer(text="Practice attempt (not counted)")

    await message.reply(embed=embed)


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

    asyncio.create_task(keep_db_alive())

    if not hasattr(client, "started"):
        client.started = True
        asyncio.create_task(g_word.word_loop(client, CHANNEL_ID))

    for _ in range(3):
        asyncio.create_task(worker())


# ================= COMMANDS =================

@tree.command(name="leaderboard")
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

    rank, score = get_user_rank(interaction.user.id)

    if rank:
        embed.set_footer(text=f"Your position: #{rank} (Score: {score})")
    else:
        embed.set_footer(text="You are not ranked yet.")

    await interaction.response.send_message(embed=embed)


@tree.command(name="wod")
async def wod(interaction: discord.Interaction):

    word = g_word.current_word
    meaning = g_word.current_meaning
    dyk = g_word.current_dyk

    if not word:
        await interaction.response.send_message("⚠️ No word loaded yet")
        return

    embed = discord.Embed(
        title=f"📌 {word.upper()}",
        color=discord.Color.blue()
    )

    embed.add_field(name="📖 Meaning", value=meaning, inline=False)

    if dyk:
        embed.add_field(name="🧠 Did You Know?", value=dyk, inline=False)

    await interaction.response.send_message(embed=embed)


@tree.command(name="fetch")
async def fetch(interaction: discord.Interaction):

    await interaction.response.defer()

    old_word = g_word.current_word

    for _ in range(3):
        new_word, new_meaning, did_you_know = g_word.get_wod()

        if new_word and new_word != old_word:
            g_word.current_word = new_word
            g_word.current_meaning = new_meaning
            g_word.current_dyk = did_you_know
            g_word.active_game = True
            g_word.user_attempts.clear()

            g_word.save_state(new_word)

            safe_execute("DELETE FROM submissions")

            embed = discord.Embed(
                title=f"📌 {new_word.upper()}",
                color=discord.Color.blue()
            )

            embed.add_field(
                name="📖 Meaning",
                value=new_meaning or "Not available",
                inline=False
            )

            if did_you_know:
                embed.add_field(
                    name="🧠 Did You Know?",
                    value=did_you_know,
                    inline=False
                )

            await interaction.followup.send(embed=embed)
            return

        await asyncio.sleep(2)

    await interaction.followup.send(
        f"⚠️ No new word yet.\nCurrent: **{old_word.upper()}**"
    )


client.run(TOKEN)