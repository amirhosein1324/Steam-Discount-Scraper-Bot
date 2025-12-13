# Steam Discount Bot

This is a Steam Sale Surveillance System. It is a Python-based Telegram bot that continuously scrapes the Steam store for "Special" discounts , maintains a database of sales, and alerts subscribed users about new deals.

It features `background scraping`, `duplicate detection`, and `targeted game tracking` .

---

## Features

* Duplicate Prevention: Automated Surveillance:  runs a Selenium scraper every 30 minutes to detect new discounts.
* Smart Infinite Scroll: the system automatically scrolls the Steam search page to load all available results.
* It uses SQLite to store deal history, ensuring users are not notified twice for the same sale.
* Game Watchlist: Users can subscribe to specific game titles (for example, "Elden Ring") and get priority alerts.
* Throttled Notifications: The system uses a job queue to send alerts sequentially (one information every 10 seconds), in order to avoid spamming or hitting API limits.
* Live Commands: The system fetches random deals or the latest scraped deals on command.

---

## Installation & Setup

### 1.things we need
* Python 3.8+
* Google Chrome (required for the Selenium WebDriver)
* A Telegram Bot Token (from : [@BotFather](https://t.me/BotFather))

### 2. Install Dependencies
This project relies on `selenium` , `beautifulsoup4` , `webdriver-manager` , and `python-telegram-bot`.

Important: You must install the `job-queue` extra for the Telegram library.

```bash
pip install "python-telegram-bot[job-queue]" , selenium  , beautifulsoup4 , webdriver-manager 
```

### 3.Configuration
Open Discount_Bot.py.
Scroll to the bottom of the file (approx line 430).
Replace the placeholder string with your actual Telegram Bot Token:
# Find this line:
BOT_TOKEN = "PASTE YOUR TELEGRAM BOT TOKEN HERE"

---

# 🤖 Steam Discount Bot Command Cheat Sheet

Reference guide for controlling the Steam Discount Bot.

## 📋 Command List

| Command | Description |
| :--- | :--- |
| **/start** | **Initialize & Subscribe.** Subscribes you to the general alerts feed. You will receive notifications for *all* newly detected sales found during the 30-minute scan cycle. Also sends 5 random deals immediately. |
| **/latest_deals** | **View Recent Finds.** Fetches and displays the 10 most recently scraped discounted games from the database. Useful to check activity without waiting for a scan. |
| **/subscribe_game** | **Track Specific Title.** Starts a conversation to watch a specific game. <br>1. Type `/subscribe_game`<br>2. Bot asks for name.<br>3. Type name (example `Hades`)<br>4 . bot confirms or alerts if already on sale. |
| **/cancel** | **Nuke Subscriptions.** Unsubscribes you from EVERYTHING. Stops general sale alerts AND removes all specific game watches you have set up. |
| **/help**  | **Show Help.** Displays the built-in help message with a summary of these commands. |

---


## How It Works (Technical Breakdown)
1.The Scraper Method:
- Logic: It utilizes Selenium with a headless Chrome browser.
- The script navigates to the Steam specials URL. Since Steam employs "infinite scroll," the script executes JavaScript (window.scrollTo) repeatedly until the page height stops increasing.
- Safety: It compares the number of scraped items against the "X results match your search" text on Steam. If the scraped count is less than 90% (SCRAPE_TOLERANCE) of the expected count, the scrape is aborted to prevent database corruption.


2.The Database (steam_sales.db):
- The bot uses SQLite with three main tables:
- sales: Stores game_name, steam_link, and scrape_date.
- subscriptions: Stores Chat IDs for users receiving general alerts.
- game_subscriptions: Stores specific game names users are watching (for example, User 123 is watching "Red Dead Redemption").

3. Threading and concurrency
- Main Thread : Runs the bot_application.run_polling() loop to handle Telegram user messages.
- scraper Thread : A daemon thread (scraper_thread) runs surveillance_loop. It sleeps for 30 minutes , scrapes, updates the DB, and then sleeps again.
- async bridge : When the background thread finds new games, it uses asyncio.run_coroutine_threadsafe to inject the alert logic back into the main AsyncIO loop used by the Telegram bot.

4. Alert Queuing
- To prevent "flood wait" errors from Telegram:
- New games are pushed into a new_game_queues dictionary for each user.
- a repeating job (process_pending_alerts_job) that runs every 10 seconds.
- It pops one game from the queue and sends it.
