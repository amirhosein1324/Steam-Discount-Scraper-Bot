import time
import sqlite3
import threading
import asyncio
import re
from datetime import datetime
from bs4 import BeautifulSoup
from functools import partial

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, JobQueue,
    ConversationHandler, MessageHandler, filters
)

DATABASE_NAME = 'steam_sales.db'
URL = 'https://store.steampowered.com/search/?supportedlang=english&specials=1&ndl=1'
SCROLL_PAUSE_TIME = 2.0
SURVEILLANCE_INTERVAL = 240
SCRAPE_TOLERANCE = 0.90

subscribed_users = set()
new_game_queues = {}
bot_application = None
bot_loop = None
JOB_QUEUE_ERROR_MSG = None

WAITING_FOR_GAME_NAME = 1

def setup_database():
    conn = sqlite3.connect(DATABASE_NAME)
    conn.execute('PRAGMA journal_mode=WAL;')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY,
            game_name TEXT NOT NULL,
            steam_link TEXT UNIQUE,
            scrape_date TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER PRIMARY KEY
        )
    '''
    )
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS game_subscriptions (
            chat_id INTEGER NOT NULL,
            game_name TEXT NOT NULL,
            PRIMARY KEY (chat_id, game_name)
        )
    '''
    )
    conn.commit()
    conn.close()
    print(f"Database '{DATABASE_NAME}' set up successfully (WAL Mode Enabled).")

def load_subscriptions():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM subscriptions")
    global subscribed_users
    subscribed_users = {row[0] for row in cursor.fetchall()}
    conn.close()
    print(f"[DB] Loaded {len(subscribed_users)} existing subscriptions.")

def get_current_sales_map():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT steam_link, game_name FROM sales")

    sales_map = {}
    for link, name in cursor.fetchall():
        sales_map[link] = {'name': name}
    conn.close()
    return sales_map

def add_game_subscription_sync(chat_id, game_name):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    normalized_name = game_name.lower().strip()
    try:
        cursor.execute("INSERT OR IGNORE INTO game_subscriptions (chat_id, game_name) VALUES (?, ?)",
                       (chat_id, normalized_name))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"[DB Error] Failed to add game subscription: {e}")
        return False
    finally:
        conn.close()

def remove_all_game_subscriptions_for_user_sync(chat_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM game_subscriptions WHERE chat_id = ?", (chat_id,))
        conn.commit()
    except Exception as e:
        print(f"[DB Error] Failed to remove all game subscriptions: {e}")
    finally:
        conn.close()

def get_all_game_subscriptions_sync():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, game_name FROM game_subscriptions")
    subs = cursor.fetchall()
    conn.close()
    return subs

def get_game_details_by_name_sync(normalized_game_name):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    search_term = f'%{normalized_game_name}%'
    cursor.execute("""
        SELECT game_name, steam_link
        FROM sales
        WHERE LOWER(game_name) LIKE ?
        LIMIT 1
    """, (search_term,))
    result = cursor.fetchone()
    conn.close()
    return result

def process_scraped_data(scraped_data):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    db_map = get_current_sales_map()
    scraped_links = set()

    new_arrivals = []
    current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for item in scraped_data:
        link = item['steam_link']
        scraped_links.add(link)

        if link not in db_map:
            insert_sql = '''
                INSERT INTO sales (game_name, steam_link, scrape_date)
                VALUES (?, ?, ?)
            '''
            cursor.execute(insert_sql, (item['name'], link, current_date))
            new_arrivals.append(item)

        else:
            update_sql = '''
                UPDATE sales
                SET scrape_date = ?
                WHERE steam_link = ?
            '''
            cursor.execute(update_sql, (current_date, link))

    links_to_delete = [
        db_link for db_link in db_map.keys()
        if db_link not in scraped_links
    ]

    if links_to_delete:
        delete_sql = "DELETE FROM sales WHERE steam_link = ?"
        cursor.executemany(delete_sql, [(link,) for link in links_to_delete])
        print(f"[DB] Deleted {len(links_to_delete)} expired deal(s).")

    conn.commit()
    conn.close()

    print(f"[DB] Processed {len(scraped_data)} games: {len(new_arrivals)} NEW.")

    return new_arrivals

def get_random_games_sync(limit=5):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT game_name, steam_link FROM sales ORDER BY RANDOM() LIMIT ?", (limit,))
    results = cursor.fetchall()
    conn.close()
    return results

def get_latest_games_sync(limit=10):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT game_name, steam_link FROM sales ORDER BY id DESC LIMIT ?", (limit,))
    results = cursor.fetchall()
    conn.close()
    return results

def initialize_selenium():
    chrome_options = Options()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
    chrome_options.add_argument("--remote-debugging-pipe")

    try:
        driver_path = ChromeDriverManager().install()
        driver_service = Service(driver_path)
        driver = webdriver.Chrome(service=driver_service, options=chrome_options)
        driver.maximize_window()
        return driver
    except Exception as e:
        print(f"[Scraper Error] Driver init failed: {e}")
        return None

def get_expected_count(driver):
    try:
        count_element = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located((By.CLASS_NAME, "search_results_count"))
        )
        text = count_element.text.strip()

        match = re.search(r'([\d,]+)', text)
        if match:
            number_str = match.group(1).replace(',', '')
            return int(number_str)
    except Exception as e:
        print(f"[Scraper Warning] Could not detect total result count: {e}")
    return 0

def run_scraper_logic():
    driver = initialize_selenium()
    if not driver:
        return []

    print("[Scraper] Starting scan...")
    try:
        driver.get(URL)
        WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.ID, "search_resultsRows")))

        time.sleep(2)

        expected_total = get_expected_count(driver)
        print(f"[Scraper] Steam reports {expected_total} total discounted games available.")

        last_height = driver.execute_script("return document.body.scrollHeight")
        retries = 0
        max_scroll_retries = 5

        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(SCROLL_PAUSE_TIME)

            new_height = driver.execute_script("return document.body.scrollHeight")

            if new_height == last_height:
                retries += 1
                current_count = len(driver.find_elements(By.CSS_SELECTOR, '#search_resultsRows a.search_result_row'))

                print(f"[Scraper] Page stuck (Retry {retries}/{max_scroll_retries}). Current count: {current_count}")

                if expected_total > 0 and current_count >= (expected_total * SCRAPE_TOLERANCE):
                    print("[Scraper] Achieved target item count despite scroll stop. Stopping scroll.")
                    break

                if retries >= max_scroll_retries:
                    print(f"[Scraper] Max scroll retries ({max_scroll_retries}) reached. Stopping scroll.")
                    break

                time.sleep(3)
                continue

            retries = 0
            last_height = new_height

        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        sales_items = soup.select('#search_resultsRows a.search_result_row')

        scraped_count = len(sales_items)
        print(f"[Scraper] Physically scraped {scraped_count} items.")

        if expected_total > 0 and scraped_count < (expected_total * SCRAPE_TOLERANCE):
            print(f"🚨 [SAFETY ABORT] Scrape incomplete! Expected ~{expected_total}, but found {scraped_count}.")
            print("   Database update cancelled to prevent false 'new game' alerts.")
            return []

        scraped_data = []
        current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for item in sales_items:
            try:
                name_element = item.select_one('.title')
                game_name = name_element.text.strip() if name_element else "Unknown Game"
                steam_link = item.get('href', 'N/A')

                scraped_data.append({
                    'name': game_name,
                    'steam_link': steam_link,
                    'scrape_date': current_date
                })
            except Exception as item_e:
                print(f"[Scraper Error] Failed to process item: {item_e}")
                continue

        return scraped_data

    except Exception as e:
        print(f"[Scraper Critical Error] General scraper failure: {e}")
        return []
    finally:
        if driver:
            driver.quit()

async def queue_and_send_summary(new_games):
    if not bot_application or not subscribed_users:
        return

    num_new_games = len(new_games)

    if num_new_games == 0:
        return

    print(f"[Alert] Found {num_new_games} new games. Distributing...")

    for chat_id in subscribed_users:
        if chat_id not in new_game_queues:
            new_game_queues[chat_id] = []
        new_game_queues[chat_id].extend(new_games)

        msg = (f"🚨 <b>Steam Specials Alert:</b> {num_new_games} new game(s) on sale detected!\n\n"
              f"Deals will now be sent to you sequentially every 10 seconds. Use /latest_deals for the newest additions or /start for random deals.")

        try:
            await bot_application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
        except Exception as e:
            print(f"Failed to send summary to {chat_id}: {e}")

async def alert_subscribed_games(new_arrivals):
    if not bot_application:
        return

    loop = asyncio.get_running_loop()
    all_subscriptions = await loop.run_in_executor(None, get_all_game_subscriptions_sync)

    subscription_map = {}
    for chat_id, game_name in all_subscriptions:
        normalized_name = game_name.lower().strip()
        if normalized_name not in subscription_map:
            subscription_map[normalized_name] = []
        subscription_map[normalized_name].append(chat_id)

    if not subscription_map:
        return

    for game in new_arrivals:
        game_name = game['name']
        normalized_game_name = game_name.lower().strip()

        if normalized_game_name in subscription_map:
            subscribed_chat_ids = subscription_map[normalized_name]

            msg = (f"⭐️ GAME ALERT: <b>{game_name}</b> is NOW ON SALE! ⭐️\n"
                   f"🔗 {game['steam_link']}\n\n"
                   f"Use /subscribe_game to track other games or /cancel to stop all alerts.")

            for chat_id in subscribed_chat_ids:
                try:
                    await bot_application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                except Exception as e:
                    print(f"Failed to send game alert to {chat_id} for {game_name}: {e}")


async def process_pending_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(new_game_queues.keys()):
        queue = new_game_queues.get(chat_id)

        if not queue:
            new_game_queues.pop(chat_id, None)
            continue

        if chat_id not in subscribed_users:
            new_game_queues.pop(chat_id, None)
            continue

        try:
            game = queue.pop(0)

            name = game['name']
            link = game['steam_link']

            msg = (f"🔥 NEW DEAL! ({len(queue)} pending) 🔥\n"
                   f"🎮 <b>{name}</b>\n"
                   f"🔗 {link}")

            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')

        except Exception as e:
            print(f"[Alert Processor] Failed to send game to {chat_id}: {e}")

def surveillance_loop():
    print("--- Surveillance System Started ---")
    while True:
        data = run_scraper_logic()


        if data:
            new_arrivals = process_scraped_data(data)


            if new_arrivals:
                if bot_application and bot_loop:
                    asyncio.run_coroutine_threadsafe(
                        queue_and_send_summary(new_arrivals),
                        bot_loop
                    )
                    asyncio.run_coroutine_threadsafe(
                        alert_subscribed_games(new_arrivals),
                        bot_loop
                    )

            print(f"[Surveillance] Sleeping for {SURVEILLANCE_INTERVAL} seconds...")
            time.sleep(SURVEILLANCE_INTERVAL)
        else:
            print("[Surveillance] Scrape failed or aborted. Retrying in 60 seconds...")
            time.sleep(60)
            continue

async def subscribe_game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Please enter the exact name of the game you want to track for a Steam Special discount (e.g., 'Cyberpunk 2077'). You can cancel this process with /cancel."
    )
    return WAITING_FOR_GAME_NAME

async def receive_game_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game_name_raw = update.message.text
    normalized_game_name = game_name_raw.lower().strip()

    await update.message.reply_text(f"Searching for current deals on '{game_name_raw}'...")

    loop = asyncio.get_running_loop()

    game_details = await loop.run_in_executor(
        None,
        partial(get_game_details_by_name_sync, normalized_game_name)
    )

    if game_details:
        name, link = game_details
        msg = (f"🎉 GREAT NEWS! <b>{name}</b> is ALREADY ON SALE!\n"
               f"🔗 {link}\n\n"
               f"Since it's currently on sale, you don't need a specific subscription, but you can use /subscribe_game to track other titles.")
        await update.message.reply_text(msg, parse_mode='HTML')
    else:
        success = await loop.run_in_executor(
            None,
            partial(add_game_subscription_sync, chat_id, game_name_raw)
        )

        if success:
            await update.message.reply_text(
                f"✅ Success! You are now tracking <b>{game_name_raw}</b>. I will notify you immediately if it goes on sale!",
                parse_mode='HTML'
            )
        else:
             await update.message.reply_text(
                f"⚠️ Duplicate. You are already tracking <b>{game_name_raw}</b></b>.",
                parse_mode='HTML'
            )

    return ConversationHandler.END

async def cancel_subscription_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Game subscription request cancelled.")
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        "🤖 <b>Steam Specials Deals Tracker Help</b> 🕵️\n\n"
        "This bot continuously scrapes the Steam 'Specials' page to notify you about new discounts. It tracks *which* games are on sale, but not their specific prices.\n\n"
        "<b>Available Commands:</b>\n"
        "• /start - Subscribe to general alerts and receive an initial set of random deals.\n"
        "• /latest_deals - See the 10 most recently scraped discounted games.\n"
        "• /subscribe_game - Start a conversation to track a specific game name. I'll alert you instantly when that exact game appears on the specials page.\n"
        "• /cancel - Unsubscribe from ALL alerts (general and specific game tracking).\n"
        "• /help - Show this help message."
    )
    await update.message.reply_text(help_message, parse_mode='HTML')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO subscriptions (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

    subscribed_users.add(chat_id)

    response_text = "👋 Welcome! You are now subscribed to Steam Special Deals Tracker.\n"

    if JOB_QUEUE_ERROR_MSG:
        response_text += (f"\n⚠️ <b>ALERT WARNING:</b> Due to a missing system dependency, "
                         f"I cannot schedule automatic alerts. Please check the Python console "
                         f"for details on installing the 'job-queue' dependency. You can still use "
                         f"<code>/latest_deals</code> manually. ⚠️\n\n")
    else:
        response_text += ("New deals will be sent to you automatically (one every 10 seconds).\n\n")

    response_text += ("🎲 Here are 5 random deals from the vault right now. Use /latest_deals for the newest additions, or /subscribe_game to track a specific title.")

    await update.message.reply_text(response_text, parse_mode='HTML')

    loop = asyncio.get_running_loop()
    random_games = await loop.run_in_executor(None, partial(get_random_games_sync, limit=5))

    if not random_games:
        await update.message.reply_text("The database is currently initializing. Please wait a moment.")
        return

    for name, link in random_games:

        msg = (f"🎮 <b>{name}</b>\n"
               f"🔗 {link}")
        await update.message.reply_text(msg, parse_mode='HTML')

async def latest_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching the 10 most recently scraped discounted games...")

    loop = asyncio.get_running_loop()
    latest_games = await loop.run_in_executor(None, partial(get_latest_games_sync, limit=10))

    if not latest_games:
        await update.message.reply_text("No sales data available yet. Please wait for the scraper to complete its first run.")
        return

    for name, link in latest_games:
        msg = (f"🎮 <b>{name}</b>\n"
               f"🔗 {link}")
        await update.message.reply_text(msg, parse_mode='HTML')

    await update.message.reply_text(f"This is a sample of the most recent deals. Use /start for random deals.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

    if chat_id in subscribed_users:
        subscribed_users.remove(chat_id)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(remove_all_game_subscriptions_for_user_sync, chat_id))

    new_game_queues.pop(chat_id, None)

    await update.message.reply_text("😢 You have been unsubscribed from ALL Steam Special Deals alerts. Use /start or /subscribe_game to resubscribe anytime.")

async def post_init(application: Application):
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    print("[Bot] Event loop captured for background alerts.")

    if application.job_queue:
        application.job_queue.run_repeating(
            process_pending_alerts_job,
            interval=10,
            first=5,
            name="pending_alerts"
        )
        print("[Bot] Pending alerts job scheduled to run every 10 seconds.")
    else:
        print("[Bot WARNING] Job queue is missing. Automatic alerts will not run.")


if __name__ == '__main__':
    setup_database()
    load_subscriptions()

    BOT_TOKEN = "PASTE YOUR TELEGRAM BOT TOKEN HERE"

    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("ERROR: You must edit the file and insert your Telegram Bot Token!")
    else:
        print("--- Bot Starting ---")

        try:
            job_queue_instance = JobQueue()
            bot_application = Application.builder().token(BOT_TOKEN).job_queue(job_queue_instance).post_init(post_init).build()
        except Exception as e:
            JOB_QUEUE_ERROR_MSG = f"To use JobQueue, PTB must be installed via 'pip install \"python-telegram-bot[job-queue]\"'."

            print(f"[ERROR] Critical failure creating JobQueue ({e}). Falling back to simple Application build.")
            print(f"      Action Required: Please run 'pip install \"python-telegram-bot[job-queue]\"' to enable automatic alerts.")

            bot_application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

        game_sub_conv_handler = ConversationHandler(
            entry_points=[CommandHandler('subscribe_game', subscribe_game_start)],

            states={
                WAITING_FOR_GAME_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_game_name)
                ]
            },

            fallbacks=[CommandHandler('cancel', cancel_subscription_conversation)],

            allow_reentry=True
        )

        bot_application.add_handler(CommandHandler("start", start))
        bot_application.add_handler(CommandHandler("cancel", cancel))
        bot_application.add_handler(CommandHandler("latest_deals", latest_deals))
        bot_application.add_handler(CommandHandler("help", help_command))
        bot_application.add_handler(game_sub_conv_handler)

        scraper_thread = threading.Thread(target=surveillance_loop, daemon=True)
        scraper_thread.start()

        bot_application.run_polling()