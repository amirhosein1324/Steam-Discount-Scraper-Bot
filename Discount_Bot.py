import time
import sqlite3
import threading
import asyncio
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


from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes


DATABASE_NAME = 'steam_sales.db'
URL = 'https://store.steampowered.com/search/?supportedlang=english&specials=1&ndl=1'
SCROLL_PAUSE_TIME = 2.0
SURVEILLANCE_INTERVAL = 1800  


user_offsets = {}

def setup_database():
    """Initializes the SQLite database with WAL mode for concurrency."""
    conn = sqlite3.connect(DATABASE_NAME)
    
    conn.execute('PRAGMA journal_mode=WAL;')
    
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY,
            game_name TEXT NOT NULL,
            steam_link TEXT,
            scrape_date TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print(f"Database '{DATABASE_NAME}' set up successfully (WAL Mode Enabled).")

def clear_and_save_data(data):
    """Clears old data and saves new scrape results."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    try:
        cursor.execute('DELETE FROM sales')
        
        insert_sql = '''
            INSERT INTO sales (game_name, steam_link, scrape_date)
            VALUES (?, ?, ?)
        '''
        
        records_to_insert = [
            (item['name'], item['steam_link'], item['scrape_date'])
            for item in data
        ]
        
        cursor.executemany(insert_sql, records_to_insert)
        conn.commit()
        print(f"[DB] Successfully saved {len(records_to_insert)} games to database.")
    except Exception as e:
        print(f"[DB Error] Failed to save data: {e}")
    finally:
        conn.close()

def get_games_from_db_sync(limit, offset):
    """Synchronous function to fetch games (to be run in executor)."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT game_name, steam_link FROM sales ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset))
    results = cursor.fetchall()
    conn.close()
    return results

def get_total_count_sync():
    """Synchronous function to get total count."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sales")
    count = cursor.fetchone()[0]
    conn.close()
    return count

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
        return driver
    except Exception as e:
        print(f"[Scraper Error] Driver init failed: {e}")
        return None

def run_scraper_logic():
    """The core logic to scrape Steam."""
    driver = initialize_selenium()
    if not driver:
        return []

    print("[Scraper] Starting scan...")
    try:
        driver.get(URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "search_resultsRows")))
        
        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(SCROLL_PAUSE_TIME)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
        
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        sales_items = soup.select('#search_resultsRows a.search_result_row')
        
        print(f"[Scraper] Found {len(sales_items)} items.")
        
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
            except:
                continue
                
        return scraped_data

    except Exception as e:
        print(f"[Scraper Error] {e}")
        return []
    finally:
        if driver:
            driver.quit()

def surveillance_loop():
    """Runs the scraper in a background thread continuously."""
    print("--- Surveillance System Started ---")
    while True:
        data = run_scraper_logic()
        if data:
            clear_and_save_data(data)
            print(f"[Surveillance] Sleeping for {SURVEILLANCE_INTERVAL} seconds...")
        else:
            print("[Surveillance] Scrape failed. Retrying in 60 seconds...")
            time.sleep(60)
            continue
            
        time.sleep(SURVEILLANCE_INTERVAL)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message with the custom keyboard including /more."""
    keyboard = [['Discounted Steam Games'], ['/more']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Welcome to the Steam Sales Surveillance Bot! 🤖\n"
        "Press 'Discounted Steam Games' to start fresh.\n"
        "Press '/more' to load the next 50 games.",
        reply_markup=reply_markup
    )
    user_offsets[update.effective_chat.id] = 0

async def send_games_batch(update, chat_id, start_fresh=False):
    """Helper function to fetch and send a batch of games asynchronously."""
    
    if chat_id not in user_offsets or start_fresh:
        user_offsets[chat_id] = 0
        
    current_offset = user_offsets[chat_id]
    
    loop = asyncio.get_running_loop()
    
    if start_fresh:
        total_count = await loop.run_in_executor(None, get_total_count_sync)
        if total_count == 0:
            await update.message.reply_text("The database is currently empty. Please wait for the scraper to finish its first run.")
            return
        await update.message.reply_text(f"🔍 Found {total_count} games in database.\nHere are the first 50 results:")
    else:
        await update.message.reply_text(f"🔄 Loading next 50 games (Offset: {current_offset})...")
        
    games = await loop.run_in_executor(None, partial(get_games_from_db_sync, limit=50, offset=current_offset))
    
    if not games:
        if current_offset == 0:
             await update.message.reply_text("🚫 Database is empty or the scraper is running its first cycle.")
        else:
             await update.message.reply_text(f"🚫 No more games found after item {current_offset}.")
        user_offsets[chat_id] = 0
        return

    response_chunk = ""
    for name, link in games:
        entry = f"🎮 {name}\n🔗 {link}\n\n"
        if len(response_chunk) + len(entry) > 4000:
            await update.message.reply_text(response_chunk)
            response_chunk = ""
        response_chunk += entry
        
    if response_chunk:
        await update.message.reply_text(response_chunk)
        
    user_offsets[chat_id] += 50
    await update.message.reply_text(f"✅ Showing items {current_offset + 1} - {current_offset + len(games)}. Click /more for the next batch.")

async def more_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /more command triggered by the button or direct user input."""
    chat_id = update.effective_chat.id
    await send_games_batch(update, chat_id, start_fresh=False)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the general message (specifically the main button)."""
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == 'Discounted Steam Games':
        await send_games_batch(update, chat_id, start_fresh=True)
        


if __name__ == '__main__':
    setup_database()

    scraper_thread = threading.Thread(target=surveillance_loop, daemon=True)
    scraper_thread.start()

    BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
    
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("ERROR: You must edit the file and insert your Telegram Bot Token!")
    else:
        print("--- Bot Starting ---")
        application = Application.builder().token(BOT_TOKEN).build()
        

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("more", more_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        application.run_polling()