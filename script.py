import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# Configuration
TARGET_URL = "https://counterpointresearch.com"
MEMORY_FILE = "processed_links.txt"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def fetch_recent_blogs():
    print("Scraping Counterpoint Insights page...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(TARGET_URL, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
        
        soup = BeautifulSoup(res.text, 'html.parser')
        all_links = soup.find_all('a', href=True)
        extracted_blogs = []
        
        for tag in all_links:
            link = tag['href'].strip()
            title = tag.text.strip()
            
            if "/en/insights/" in link and len(title) > 25:
                if link.endswith('/en/insights') or link.endswith('/en/insights/'):
                    continue
                if not link.startswith('http'):
                    link = "https://counterpointresearch.com" + link
                    
                # Use current date as fallback for testing
                date_str = datetime.now().strftime("%B %d, %Y")
                if {"title": title, "link": link, "date": date_str} not in extracted_blogs:
                    extracted_blogs.append({"title": title, "link": link, "date": date_str})
        return extracted_blogs
    except Exception as e:
        print(f"Scraper error: {e}")
        return []

def send_telegram_message(message):
    url = f"https://telegram.org{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram network error: {e}")
        return False

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets in GitHub.")
        return

    blogs = fetch_recent_blogs()
    if not blogs:
        print("No blog targets found.")
        return

    processed_links = set()
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            processed_links = set(line.strip() for line in f if line.strip())

    state_changed = False
    
    # Check the top 3 most recent entries
    for blog in reversed(blogs[:3]):
        link = blog["link"]
        title = blog["title"]

        if link in processed_links:
            print(f"Already sent: {title}")
            continue

        text_payload = f"🔔 *New Blog Detected!*\n\n*Title:* {title}\n\n🔗 [Read Article]({link})"
        
        if send_telegram_message(text_payload):
            print(f"Successfully notified Telegram for: {title}")
            processed_links.add(link)
            state_changed = True
            with open(MEMORY_FILE, 'a') as f:
                f.write(link + "\n")

    with open("sync_status.txt", "w") as status:
        status.write("update_committed" if state_changed else "no_update")

if __name__ == "__main__":
    main()
