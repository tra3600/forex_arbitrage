import requests
import schedule
import time

# Configuration des API
PLATFORM_A_URL = "https://api.platforma.com/forex"
PLATFORM_B_URL = "https://api.platformb.com/forex"
API_KEY_A = "your_api_key_for_platform_a"
API_KEY_B = "your_api_key_for_platform_b"

# Fonction pour récupérer les taux de change
def get_exchange_rate(platform_url, api_key, currency_pair):
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    response = requests.get(f"{platform_url}/{currency_pair}", headers=headers)
    if response.status_code == 200:
        data = response.json()
        return data["rate"]
    else:
        raise Exception(f"Failed to fetch exchange rate from {platform_url}")

# Fonction d'arbitrage
def arbitrage_opportunity():
    try:
        currency_pair = "EURUSD"
        rate_a = get_exchange_rate(PLATFORM_A_URL, API_KEY_A, currency_pair)
        rate_b = get_exchange_rate(PLATFORM_B_URL, API_KEY_B, currency_pair)

        print(f"Rate A: {rate_a} | Rate B: {rate_b}")

        if rate_a < rate_b:
            print(f"Arbitrage Opportunity: Buy on Platform A at {rate_a} and sell on Platform B at {rate_b}")
        elif rate_a > rate_b:
            print(f"Arbitrage Opportunity: Buy on Platform B at {rate_b} and sell on Platform A at {rate_a}")
        else:
            print("No arbitrage opportunity detected.")
    except Exception as e:
        print(f"Error: {e}")

# Configurer l'exécution périodique de l'arbitrage
schedule.every(10).seconds.do(arbitrage_opportunity)

print("Starting Forex Arbitrage Bot...")
while True:
    schedule.run_pending()
    time.sleep(1)