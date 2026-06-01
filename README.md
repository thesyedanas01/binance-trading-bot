# Binance Futures Trading Bot

A robust, Flask-based automated trading bot that acts as a secure bridge between charting platforms (like TradingView) and the Binance Futures API.

## Features

- **Webhook Integration**: Listens for POST webhooks containing structured JSON trade signals (Entry, Stop Loss, Take Profit targets).
- **Automated Execution**: Dynamically calculates position sizing based on available equity and executes LIMIT entry orders.
- **Advanced Risk Management**: Enforces a strict risk parameter (default 1.25% of account equity) per trade. Automatically sets dynamic leverage (up to 125x) based on your stop-loss distance to ensure margin safety.
- **Multi-Target Scaling**: Automatically places two Take Profit limit orders (TP1 closes 50% of the position, TP2 closes the remaining 50%) and a Stop Market order.
- **Security**: Validates incoming signals using HMAC SHA-256 signatures to ensure your webhook endpoints cannot be exploited by unauthorized third parties.

## Prerequisites

- Python 3.8+
- A Binance account with Futures trading enabled and an API Key generated.
- A platform capable of sending webhooks (e.g., TradingView Pro/Plus/Premium).
- A server to host the bot (e.g., VPS, Heroku, AWS, DigitalOcean).

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/thesyedanas01/binance-trading-bot.git
   cd binance-trading-bot
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables Configuration:**
   Create a `.env` file in the root directory and add your credentials:
   ```env
   BINANCE_API_KEY=your_binance_api_key_here
   BINANCE_API_SECRET=your_binance_api_secret_here
   WEBHOOK_SECRET=your_custom_secure_secret_string
   ```

## Running the Bot

For development/testing:
```bash
python app.py
```

For production, it is highly recommended to use a WSGI server like Gunicorn:
```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## Webhook Payload Structure

The bot expects POST requests to the `/webhook` endpoint with the following JSON structure:

### Open Position
```json
{
    "action": "open",
    "dir": "long",
    "entry": 65000.50,
    "sl": 64000.00,
    "tp1": 66000.00,
    "tp2": 67000.00,
    "secret": "your_custom_secure_secret_string"
}
```

### Close Position
```json
{
    "action": "close",
    "dir": "long",
    "secret": "your_custom_secure_secret_string",
    "reason": "MACD Crossover"
}
```

## Available API Endpoints

- `POST /webhook`: Main endpoint for trading signals.
- `GET /health`: Checks if the bot is running and connected to Binance.
- `GET /status`: Provides detailed status on account equity, configuration, and active market positions.
- `POST /cancel-orders`: Emergency endpoint to cancel all open orders for the traded symbol.
- `POST /close-position`: Emergency endpoint to close any active position at market price.
