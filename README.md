# Steam Sales Surveillance Telegram Bot

This is a simple Python project that watches Steam discount games and lets you browse them through a Telegram bot. It scrapes the Steam specials page, saves the games into a local SQLite database, and then sends them to you in Telegram in batches of 50.

---

## What this project does

- Opens the Steam Specials page with Selenium and scrolls down until all discounted games are loaded.
  
- Uses BeautifulSoup to parse the page and get each game’s name and Steam link.
  
- Saves all scraped games into a local SQLite database file called `steam_sales.db`.

- Runs a background “surveillance” loop that refreshes the database every 30 minutes.
  
- Provides a Telegram bot with:
  - a button to get discounted Steam games from the database.
  - a `/more` command to load the next 50 games til the end.
  - price section that gives discounted price to user

---

## Main components

### Scraper

- `initialize_selenium()` sets up a Chrome browser with some options and starts it using `webdriver_manager`, so you don’t need to manually download the ChromeDriver.

- `run_scraper_logic()`:
  - Opens the Steam specials URL.
  
  - Waits until the search results element is present.
  
  - Scrolls the page down in a loop until the page height stops changing (infinite scroll handling).
  
  - Uses BeautifulSoup to select each game row and extract:
    - Game title.
    - Game Price
    - Steam link.
    
  - Puts all games into a list of dictionaries along with the current scrape date.

### Database

- The SQLite database filename is: `steam_sales.db`.

- `setup_database()`:
 - Creates the`sales` table if it doesnt exist.
 
 - Enables WAL (Write-Ahead Logging) mode for better concurrent reads/writes.
 
- The `sales` table has:
 - id (primary key auto-increment).
 - game_name (text).
 - game price
 - steam_link (text).
 - scrape_date (text).
 
-clear_and_save_data:
 - Deletes all old rows in sales.
 - Inserts the new scraped games in bulk.
 - gives new discounted games to user
 
- `get_games_from_db_sync(limit, offset)`:
 - Returns a list of (game_name, steam_link) rows using the given limit and offset.

 
- `get_total_count_sync()`:
 - returns all games that are stored in the sales table.

### Surveillance loop

- `surveillance_loop()` runs forever in a background thread.
- It:
  - Calls `run_scraper_logic()` to scrape Steam.
  
  - If scraping returned data, it calls `clear_and_save_data(data)` to refresh the DB.
  
  - Sleeps for `SURVEILLANCE_INTERVAL` seconds (default: 1800 seconds = 30 minutes).
  
  - If scraping fails, it sleeps 60 seconds and tries again.
  
  - Sends new discounted games to user.

---

## Telegram bot logic

This bot utilizes the async version of `python-telegram-bot` (Application class and async handler).

-`start(update, context)`:
 -Sends a welcome message.
 
 -Shows a custom keyboard with:
  -`Discounted Steam Games`
  -`/more`
  
 -Sets the users offset to 0 in the`user_offsets`dictionary.

- send_games_batch (update , chat_id , start_fresh = False):

 - If `start_fresh` is True or the user is new:
  -Resets the  offset for that chat to 0.
  
  - Gets the total game count from the database.
  
  - If the DB(database) is empty, tells the user to wait until the first scrape completes.
  
  - Otherwise, tells the user how many games were found and that it is sending the first 50.
  
 - Uses `asyncio.get_running_loop().run_in_executor`to call the blocking DB functions without freezing the bot.
 
 - reads 50 games from the DB starting at the current offset.
 
 - builds message chunks with each game in the format:
  -`Game_Name`
  -`Game Price`
  -`Game_Link`
  
 - makes sure each message is under 4000 characters (for  telegram message limits).
 
 - Sends the chunks to the user.
 
 - Increases the user’s offset by 50 and sends a small status message and  which range was sent.



- `more_command(update, context)`:
 - Called when the user sends `/more`.
 
 - Calls`send_games_batch`with start_fresh = False to load the next 50 games for that chat.
 
- `handle_message(update, context)`:

 -  checks the text of messages.
 
 - If the user pressed `Discounted_Steam_Games` button ,  it calls send_games_batch with start_fresh=True to restart the list from the beginning.
 
- `user_offsets`:
 -a simple dictionary that stores the current offset(how many games have already been shown) for each chat ID.

---

## How the script starts

at the bottom of our file:
- if __name__ == '__main__':

 - calls setup_database() to ensure the database and table exist and WAL mode is enabled.
 
 - starts the thread:
 
  - scraper_thread = threading.Thread(target = surveillance_loop , daemon=True)
  
  - scraper_thread.start()
 - Defines `BOT_TOKEN  =  "YOUR_TELEGRAM_BOT_TOKEN" `:
 
  - You must replace this with your actual token from BotFather.
  
 - If `BOT_TOKEN` is not changed , it prints an error and our bot will not start.

 - If token is valid:
  - Builds the application with  application.builder().token(BOT_TOKEN).build().
  
  - adds some handlers for bot:
  
   - CommandHandler("start", start)
   - CommandHandler("more", more_command)
   - MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
   
  - Runs the bot with `application.run_polling().
    
---

## Requirements

You need:

- Python 3.
- Google Chrome (or Chromium) installed (so Selenium can control a browser).
- The following Python packages (install via `pip`):
  pip install selenium  ,   webdriver-manager  , beautifulsoup4  ,  python-telegram-bot



SQLite is built into Python, so you don’t need to install it separately.

---

## How to run

1. **Clone or copy the script**  
   Save the script as something like `steam_sales_bot.py`.

2. **Install dependencies**  
   Install the required libraries using `pip`.

3. **Create your bot and get token**  
   - Open Telegram and search for `@BotFather`.  
   - Create a new bot and copy the token it gives you.
   - In the script, replace: with your real token.

4. **Run the script**
   python steam_sales_bot.py


The console should show that the database is set up and that the surveillance system started. If the token is correct, it will print that the bot is starting and begin polling for updates.

5. **Use the bot**

- Open your bot in the Telegram app.
- Type or tap `/start`.
- Press the `Discounted Steam Games` button to get the first 50 games (wait a bit after first run so the scraper can fill the database).
- Use `/more` to get the next 50 games.

---

## Notes and ideas for improvement

- Right now, it only handles English Steam specials based on the given URL.
- The database is wiped and fully refreshed every cycle (no history of old sales is kept).
- You could extend the scraper to also capture prices, discount percentages, tags, or platforms.
- You could also add filters for users , like “only show games cheaper than X ” or “ only show games with discount higher than Y ”.
- If you want to be kinder to the website, you can increase `SURVEILLANCE_INTERVAL` so scraping doesn’t happen too often.


THANKS FOR READING
