import os
import requests
from datetime import datetime

# Optional: Load from env
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY")

def get_news_sentiment(symbol: str) -> dict:
    """
    Fetches latest news for the token from CryptoPanic and evaluates sentiment.
    Returns:
        {"score": float (0.0 to 1.0), "status": str ("BULLISH", "BEARISH", "NEUTRAL")}
    """
    base_coin = symbol.split("/")[0].split(":")[0] # e.g. BTC from BTC/USDT:USDT
    
    if not CRYPTOPANIC_API_KEY:
        # Fallback if no API key
        return {"score": 0.5, "status": "NEUTRAL", "reason": "No API Key"}
        
    url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&currencies={base_coin}&filter=hot"
    
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return {"score": 0.5, "status": "NEUTRAL", "reason": f"API Error {resp.status_code}"}
            
        data = resp.json()
        results = data.get("results", [])
        
        if not results:
            return {"score": 0.5, "status": "NEUTRAL", "reason": "No recent news"}
            
        bullish_votes = 0
        bearish_votes = 0
        
        for post in results[:5]: # look at top 5 recent hot posts
            votes = post.get("votes", {})
            bullish_votes += votes.get("positive", 0)
            bullish_votes += votes.get("important", 0)
            bearish_votes += votes.get("negative", 0)
            bearish_votes += votes.get("toxic", 0)
            
        total = bullish_votes + bearish_votes
        if total == 0:
            return {"score": 0.5, "status": "NEUTRAL", "reason": "No votes"}
            
        bull_ratio = bullish_votes / total
        
        if bull_ratio > 0.7:
            return {"score": 0.8, "status": "BULLISH", "reason": f"Bull ratio {bull_ratio:.2f}"}
        elif bull_ratio < 0.3:
            return {"score": 0.2, "status": "BEARISH", "reason": f"Bull ratio {bull_ratio:.2f}"}
        else:
            return {"score": 0.5, "status": "NEUTRAL", "reason": f"Mixed ratio {bull_ratio:.2f}"}
            
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Помилка News Oracle: {e}")
        return {"score": 0.5, "status": "NEUTRAL", "reason": "Exception"}

if __name__ == "__main__":
    print(get_news_sentiment("BTC/USDT"))
