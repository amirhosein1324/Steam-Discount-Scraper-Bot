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
from telegram.ext import Application, CommandHandler, ContextTypes

DATABASE_NAME = 'steam_sales.db'
URL = 'https://store.steampowered.com/search/?supportedlang=english&specials=1&ndl=1'
SCROLL_PAUSE_TIME = 2.0
SURVEILLANCE_INTERVAL = 1800
SCRAPE_TOLERANCE = 0.90 

subscribed_users = set()
bot_application = None
bot_loop = None

def price_cleanup(price_str):
    
    
    
    
    if not isinstance(price_str, str):
        return price_str
    
    cleaned_str = price_str.replace('%', '').strip()
    
    match = re.search(r'([\d,\.]+[€$£]|[\d,\.]+\s*TL|[\d,\.]+)[\s]*$', cleaned_str)
    
    if match:
        return match.group(1).strip()
    
    return re.sub(r'^\s*-\s*', '', cleaned_str).strip()


def setup_database():
    
    conn = sqlite3.connect(DATABASE_NAME)
    conn.execute('PRAGMA journal_mode=WAL;')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY,
            game_name TEXT NOT NULL,
            steam_link TEXT UNIQUE,
            original_price TEXT,
            discount_price TEXT,
            scrape_date TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER PRIMARY KEY
        )
    ''')
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

def get_existing_links():
    
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT steam_link FROM sales")
    links = {row[0] for row in cursor.fetchall()}
    conn.close()
    return links

def save_new_data(data):
    
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    existing_links = get_existing_links()
    
    previous_count = len(existing_links)
    new_arrivals = []
    
    try:
        cursor.execute('DELETE FROM sales')
        
        insert_sql = '''
            INSERT INTO sales (game_name, steam_link, original_price, discount_price, scrape_date)
            VALUES (?, ?, ?, ?, ?)
        '''
        
        records_to_insert = []
        for item in data:
            records_to_insert.append((
                item['name'], 
                item['steam_link'], 
                item['original_price'],
                item['discount_price'],
                item['scrape_date']
            ))
            
            if item['steam_link'] not in existing_links:
                new_arrivals.append(item)
        
        cursor.executemany(insert_sql, records_to_insert)
        conn.commit()
        
        
        current_scrape_count = len(records_to_insert)
        if current_scrape_count > 0 and previous_count < (current_scrape_count * SCRAPE_TOLERANCE):
             print(f"[DB] Saved {current_scrape_count} games. Previous DB size ({previous_count}) was too small to generate meaningful alerts. Suppressing 'NEW' alerts.")
             new_arrivals = [] 
        else:
             print(f"[DB] Saved {current_scrape_count} games. Found {len(new_arrivals)} NEW discounts.")

        
    except Exception as e:
        print(f"[DB Error] Failed to save data: {e}")
        new_arrivals = [] 
    finally:
        conn.close()
        
    return new_arrivals, previous_count

def get_random_games_sync(limit=5):
    
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT game_name, steam_link, original_price, discount_price FROM sales ORDER BY RANDOM() LIMIT ?", (limit,))
    results = cursor.fetchall()
    conn.close()
    return results

def get_latest_games_sync(limit=10):
    
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT game_name, steam_link, original_price, discount_price FROM sales ORDER BY id DESC LIMIT ?", (limit,))
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
        return driver
    except Exception as e:
        print(f"[Scraper Error] Driver init failed: {e}")
        return None

def get_expected_count(driver):
    
    try:
        
        count_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "search_results_count"))
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
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "search_resultsRows")))
        
        
        expected_total = get_expected_count(driver)
        print(f"[Scraper] Steam reports {expected_total} total discounted games available.")

        last_height = driver.execute_script("return document.body.scrollHeight")
        retries = 0
        
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(SCROLL_PAUSE_TIME)
            new_height = driver.execute_script("return document.body.scrollHeight")
            
            if new_height == last_height:
                if retries < 3:
                    retries += 1
                    print(f"[Scraper] Page stuck (Retry {retries}/3)... waiting longer...")
                    time.sleep(3) 
                    continue
                else:
                    print("[Scraper] Reached bottom of page.")
                    break
            
            retries = 0
            last_height = new_height
        
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        sales_items = soup.select('#search_resultsRows a.search_result_row')
        
        scraped_count = len(sales_items)
        print(f"[Scraper] Physically scraped {scraped_count} items.")

        
        if expected_total > 0:
            
            if scraped_count < (expected_total * SCRAPE_TOLERANCE):
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
                
                original_price = "N/A"
                discount_price = "N/A"
                
                
                
                price_element = item.select_one('.search_price')
                
                if price_element:
                    
                    
                    original_element = price_element.select_one('strike')
                    
                    if original_element:
                        
                        original_price = price_cleanup(original_element.text)
                        
                        
                        
                        discount_price_parts = []
                        for content in price_element.contents:
                            if isinstance(content, str) and content.strip():
                                discount_price_parts.append(content.strip())
                            
                        
                        
                        
                        
                        if discount_price_parts:
                            discount_price = price_cleanup(discount_price_parts[-1])
                        else:
                            
                            combined_text_container = item.select_one('.search_price_discount_combined')
                            if combined_text_container:
                                full_text = combined_text_container.get_text()
                                
                                
                                
                                
                                
                                
                                
                                
                                
                                
                                discount_text_raw = full_text.replace(original_element.text, '').split()
                                
                                
                                discount_price = price_cleanup(discount_text_raw[-1]) if discount_text_raw else 'N/A'
                            else:
                                all_texts = [t.strip() for t in price_element.get_text().split()]
                                discount_price = price_cleanup(all_texts[-1]) if all_texts else 'N/A'
                            
                        
                    else:
                        
                        full_price_text = price_element.get_text().strip()
                        discount_price = price_cleanup(full_price_text)
                        original_price = discount_price 
                        
                        
                        if "free" in full_price_text.lower():
                            original_price = "N/A"
                            discount_price = "Free"


                scraped_data.append({
                    'name': game_name,
                    'steam_link': steam_link,
                    'original_price': original_price,
                    'discount_price': discount_price,
                    'scrape_date': current_date
                })
            except Exception as item_e:
                
                continue
                
        return scraped_data

    except Exception as e:
        print(f"[Scraper Error] {e}")
        return []
    finally:
        if driver:
            driver.quit()

async def broadcast_alert(new_games):
    
    
    
    
    if not bot_application:
        return
        
    num_new_games = len(new_games)
    
    if num_new_games == 0:
        return 
        
    print(f"[Alert] Sending alerts to {len(subscribed_users)} users for {num_new_games} new games.")
    
    
    msg = (f"🚨 <b>Steam Sales Alert:</b> {num_new_games} new discounted game{'s' if num_new_games > 1 else ''} detected!\n\n"
           f"Use the /latest_deals command to see the top 10 newest deals or /start for random deals.")
    
    for chat_id in subscribed_users:
        try:
            await bot_application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
        except Exception as e:
            print(f"Failed to send to {chat_id}: {e}")

def surveillance_loop():
    
    print("--- Surveillance System Started ---")
    while True:
        data = run_scraper_logic()
        
        
        if data:
            new_arrivals, previous_count = save_new_data(data)
            
            
            if new_arrivals: 
                if bot_application and bot_loop:
                    asyncio.run_coroutine_threadsafe(broadcast_alert(new_arrivals), bot_loop)
            
            print(f"[Surveillance] Sleeping for {SURVEILLANCE_INTERVAL} seconds...")
            time.sleep(SURVEILLANCE_INTERVAL)
        else:
            print("[Surveillance] Scrape failed or aborted. Retrying in 60 seconds...")
            time.sleep(60)
            continue


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    chat_id = update.effective_chat.id

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO subscriptions (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

    subscribed_users.add(chat_id)
    
    await update.message.reply_text(
        "👋 Welcome! You are now subscribed to Steam Sales Surveillance.\n"
        "You will receive general alerts whenever NEW discounts appear.\n\n"
        "🎲 Here are 5 random deals from the vault right now. Use /latest_deals for the newest additions."
    )
    
    loop = asyncio.get_running_loop()
    random_games = await loop.run_in_executor(None, partial(get_random_games_sync, limit=5))
    
    if not random_games:
        await update.message.reply_text("The database is currently initializing. Please wait a moment.")
        return

    for name, link, old_price, new_price in random_games:
        
        msg = (f"🎮 <b>{name}</b>\n"
               f"💰 {old_price} ➡️ {new_price}\n"
               f"🔗 {link}")
        await update.message.reply_text(msg, parse_mode='HTML')

async def latest_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await update.message.reply_text("Fetching the 10 most recently scraped discounted games...")
    
    loop = asyncio.get_running_loop()
    latest_games = await loop.run_in_executor(None, partial(get_latest_games_sync, limit=10))
    
    if not latest_games:
        await update.message.reply_text("No sales data available yet. Please wait for the scraper to complete its first run.")
        return

    for name, link, old_price, new_price in latest_games:
        msg = (f"🎮 <b>{name}</b>\n"
               f"💰 {old_price} ➡️ {new_price}\n"
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
        await update.message.reply_text("😢 You have been unsubscribed from Steam Sales alerts. Use /start to resubscribe anytime.")
    else:
        await update.message.reply_text("You are not currently subscribed. Use /start to receive alerts.")

async def post_init(application: Application):
    
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    print("[Bot] Event loop captured for background alerts.")


if __name__ == '__main__':
    setup_database()
    load_subscriptions()

    BOT_TOKEN = "8496827253:AAFuBLX57cXp3UI125eSDY9C330AQLoopYI"
    
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("ERROR: You must edit the file and insert your Telegram Bot Token!")
    else:
        print("--- Bot Starting ---")
        bot_application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
        bot_application.add_handler(CommandHandler("start", start))
        bot_application.add_handler(CommandHandler("cancel", cancel))
        bot_application.add_handler(CommandHandler("latest_deals", latest_deals))

        scraper_thread = threading.Thread(target=surveillance_loop, daemon=True)
        scraper_thread.start()

        bot_application.run_polling()