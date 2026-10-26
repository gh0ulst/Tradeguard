import os

import requests
from dotenv import load_dotenv

load_dotenv()

base_url = os.getenv("MFP_BASE_URL", "").rstrip("/")
api_key = os.getenv("MFP_API_KEY")

if not api_key or not base_url:
    raise SystemExit("Missing API configuration")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {api_key}",
    "Accept": "application/json"
})

accounts_response = session.get(
    f"{base_url}/v1/accounts",
    timeout=15
)
accounts_response.raise_for_status()
accounts = accounts_response.json().get("data")

if not isinstance(accounts, list):
    raise SystemExit("Unexpected accounts response format")

for account in accounts:
    if not isinstance(account, dict) or not account.get("id"):
        print("Skipping account with missing or invalid ID.")
        continue

    print("Competition Account ID:", account["id"])

    response = session.get(
        f"{base_url}/v1/positions",
        params={
            "account_id": account["id"],
            "status": "open"
        },
        timeout=15
    )
    response.raise_for_status()
    positions = response.json().get("data")

    if not isinstance(positions, list):
        print("Unexpected positions response format.")
        continue

    for position in positions:
        if not isinstance(position, dict):
            print("Skipping invalid position record.")
            continue

        position_id = position.get("id")
        if not position_id:
            print("Open position is missing its ID.")
            continue

        print(
            "Position ID:", position_id,
            "| Symbol:", position.get("symbol"),
            "| Side:", position.get("side")
        )
        print("Position size:", position.get("size"))
        print("Market ID:", position.get("market_id"))
