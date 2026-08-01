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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    response = requests.get(TARGET_URL, headers=headers)
    
    if response.status_code != 200:
        print(f"Failed to load page. Status code: {response.status_code}")
        return None, None
        
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Counterpoint list contains blog anchors inside h3 tags
    blog_headers = soup.find_all('h3')
    
    for header in blog_headers:
        link_tag = header.find('a')
        if link_tag and link_tag.get('href'):
            title = link_tag.text.strip()
            link = link_tag['href'].strip()
            # Ensure it is a complete URL path
            if not link.startswith('http'):
                link = "https://counterpointresearch.com" + link
            return title, link
            
    return None, None

def send_whatsapp(title, url):
    # Constructing your specific text request
    message = f"Hey Shobhit, Checkout new blogs posted today on counter resarch point. Here is the link to it:\n\n{url}"
    print(f"Sending WhatsApp Alert: {message}")
    
    # CallMeBot Endpoint
    api_url = "https://api.callmebot.com/whatsapp.php"
    params = {
        "phone": PHONE_NUMBER,
        "text": message,
        "apikey": API_KEY
    }
    
    res = requests.get(api_url, params=params)
    if res.status_code == 200:
        print("Notification delivered successfully.")
    else:
        print(f"Failed to notify CallMeBot. Status: {res.status_code}")

def main():
    if not API_KEY:
        print("Error: CALLMEBOT_API_KEY secret is not set in GitHub.")
        return

    # 1. Fetch current top blog post
    latest_title, latest_link = fetch_latest_blog()
    if not latest_link:
        print("Could not isolate any blog URLs on the page.")
        return
        
    print(f"Latest on site: {latest_title} ({latest_link})")

    # 2. Check Memory File
    last_processed_link = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            last_processed_link = f.read().strip()

    # 3. Decision Stateful Logic
    if latest_link == last_processed_link:
        print("State Check: No new blogs detected since the last run. Exiting.")
        # Writing "no_update" tells the YAML file to halt without tracking commits
        with open("sync_status.txt", "w") as status:
            status.write("no_update")
    else:
        print("State Check: Found a brand-new update!")
        # Trigger WhatsApp notification
        send_whatsapp(latest_title, latest_link)
        
        # Rewrite memory file state
        with open(MEMORY_FILE, 'w') as f:
            f.write(latest_link)
            
        with open("sync_status.txt", "w") as status:
            status.write("update_committed")

if __name__ == "__main__":
    main()
