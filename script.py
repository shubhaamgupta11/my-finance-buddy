import os
import re
import requests
from bs4 import BeautifulSoup

# Setup Configuration
TARGET_URL = "https://counterpointresearch.com/en/insights"
MEMORY_FILE = "last_link.txt"
PHONE_NUMBER = "+918505973163"
API_KEY = os.getenv("CALLMEBOT_API_KEY")

def fetch_latest_blog():
    print(f"Scraping {TARGET_URL}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }
    
    try:
        response = requests.get(TARGET_URL, headers=headers, timeout=15)
    except Exception as e:
        print(f"Network error trying to fetch page: {e}")
        return None, None
    
    if response.status_code != 200:
        print(f"Failed to load page. Status code: {response.status_code}")
        return None, None
        
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Broad scan for any links containing /insights/ to capture the feed dynamically
    all_links = soup.find_all('a', href=True)
    
    for tag in all_links:
        link = tag['href'].strip()
        title = tag.text.strip()
        
        # Filter out master list links, category pages, images, and empty texts
        if "/en/insights/" in link and len(title) > 25:
            # Skip the main hub URL itself
            if link.endswith('/en/insights') or link.endswith('/en/insights/'):
                continue
                
            # Ensure it forms a complete URL path
            if not link.startswith('http'):
                link = "https://counterpointresearch.com" + link
                
            return title, link
            
    return None, None

def send_whatsapp(title, url):
    # Constructing your specific text request
    message = f"Hey Shobhit, Checkout new blogs posted today on counter resarch point. Here is the link to it:\n\n{url}"
    print(f"Sending WhatsApp Alert for: '{title}'")
    
    # CallMeBot Endpoint
    api_url = "https://callmebot.com"
    params = {
        "phone": PHONE_NUMBER,
        "text": message,
        "apikey": API_KEY
    }
    
    try:
        res = requests.get(api_url, params=params, timeout=10)
        if res.status_code == 200:
            print("Notification delivered successfully via CallMeBot.")
        else:
            print(f"Failed to notify CallMeBot. Status: {res.status_code}, Response: {res.text}")
    except Exception as e:
        print(f"Error connecting to CallMeBot API: {e}")

def main():
    if not API_KEY:
        print("Error: CALLMEBOT_API_KEY secret is not set in GitHub.")
        return

    # 1. Fetch current top blog post
    latest_title, latest_link = fetch_latest_blog()
    if not latest_link:
        print("Could not isolate any valid blog URLs on the page layout.")
        return
        
    print(f"Latest on site identified: '{latest_title}' \nURL: {latest_link}")

    # 2. Check Memory File
    last_processed_link = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            last_processed_link = f.read().strip()

    # 3. Decision Stateful Logic
    if latest_link == last_processed_link:
        print("State Check: No new blogs detected since last run. Exiting.")
        with open("sync_status.txt", "w") as status:
            status.write("no_update")
    else:
        print("State Check: New blog update discovered!")
        # Trigger WhatsApp notification
        send_whatsapp(latest_title, latest_link)
        
        # Rewrite memory file state
        with open(MEMORY_FILE, 'w') as f:
            f.write(latest_link)
            
        with open("sync_status.txt", "w") as status:
            status.write("update_committed")

if __name__ == "__main__":
    main()
