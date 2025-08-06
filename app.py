from flask import Flask, request, jsonify
from dotenv import load_dotenv
load_dotenv()

from binance.client import Client
# We no longer need 'from binance.enums import *'
from binance.exceptions import BinanceAPIException, BinanceOrderException
import math
import os
import logging
import json
from datetime import datetime
import time
import hashlib
import hmac

app = Flask(_name_)

# === Binance API Credentials ===
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Validate required environment variables
if not API_KEY or not API_SECRET:
    raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET environment variables are required")

if not WEBHOOK_SECRET:
    raise ValueError("WEBHOOK_SECRET environment variable is required for security")

client = Client(API_KEY, API_SECRET)

# === Configuration ===
SYMBOL = "BTCUSDT"
RISK_PERCENT = 0.0125  # 1.25%

# === Logging Configuration ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(_name_)

# === Security Functions ===
def validate_webhook_signature(data, signature):
    """Validate webhook signature for security"""
    if not signature:
        return False
    
    try:
        # Create HMAC signature
        expected_signature = hmac.new(
            WEBHOOK_SECRET.encode('utf-8'),
            data,
            hashlib.sha256
        ).hexdigest()
        
        # Compare signatures (time-safe comparison)
        return hmac.compare_digest(signature, expected_signature)
    except Exception as e:
        logger.error(f"Signature validation error: {e}")
        return False

# === Helper Functions ===
def get_account_equity():
    """Get account equity in USDT with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            account_info = client.futures_account()

            equity = float(account_info['totalWalletBalance'])
            available_balance = float(account_info['availableBalance'])

            logger.info(f"Account equity: {equity} USDT, Available: {available_balance} USDT")
            return equity

        except BinanceAPIException as e:
            logger.error(f"Binance API error (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                return None
        except Exception as e:
            logger.error(f"Unexpected error fetching account equity: {e}")
            return None

def get_current_prices():
    """Get the current best bid and ask prices from the order book."""
    try:
        ticker = client.futures_orderbook_ticker(symbol=SYMBOL)
        return {
            'bid': float(ticker['bidPrice']),
            'ask': float(ticker['askPrice'])
        }
    except Exception as e:
        logger.error(f"Error fetching order book ticker: {e}")
        return None

def get_symbol_info():
    """Get symbol information for precise calculations"""
    try:
        exchange_info = client.futures_exchange_info()
        symbol_info = next((s for s in exchange_info['symbols'] if s['symbol'] == SYMBOL), None)
        
        if not symbol_info:
            return None
        
        price_precision = symbol_info['pricePrecision']
        quantity_precision = symbol_info['quantityPrecision']
        
        lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
        min_qty = float(lot_size_filter['minQty']) if lot_size_filter else 0.001
        step_size = float(lot_size_filter['stepSize']) if lot_size_filter else 0.001
        
        price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
        tick_size = float(price_filter['tickSize']) if price_filter else 0.1
        
        return {
            'price_precision': price_precision,
            'quantity_precision': quantity_precision,
            'min_qty': min_qty,
            'step_size': step_size,
            'tick_size': tick_size
        }
        
    except Exception as e:
        logger.error(f"Error getting symbol info: {e}")
        return None

def set_leverage(symbol, leverage):
    """Set leverage for the trading pair with error handling"""
    try:
        result = client.futures_change_leverage(symbol=symbol, leverage=leverage)
        logger.info(f"Leverage set to {leverage}x for {symbol}")
        return True
    except BinanceAPIException as e:
        if "leverage not modified" in str(e).lower():
            logger.info(f"Leverage already set to {leverage}x")
            return True
        else:
            logger.error(f"Failed to set leverage: {e}")
            return False
    except Exception as e:
        logger.error(f"Unexpected error setting leverage: {e}")
        return False

def round_price(value):
    """Round price to 1 decimal precision for BTCUSDC"""
    return round(value, 1)

def round_quantity(value):
    """Round quantity to 3 decimal precision for BTCUSDC"""
    return round(value, 3)

def validate_open_signal_data(data):
    """Enhanced validation of incoming trade data for open signals"""
    required_keys = {"action", "dir", "entry", "sl", "tp1", "tp2", "secret"}
    
    if not all(key in data for key in required_keys):
        missing = [key for key in required_keys if key not in data]
        return False, f"Missing required fields: {missing}"
    
    if data.get("secret") != WEBHOOK_SECRET:
        return False, "Invalid webhook secret"
        
    if data.get("action") != "open":
        return False, f"Expected action 'open', got '{data.get('action')}'"
    
    try:
        entry = float(data["entry"])
        sl = float(data["sl"])
        tp1 = float(data["tp1"])
        tp2 = float(data["tp2"])
        direction = str(data["dir"]).lower().strip()
        
        if any(price <= 0 for price in [entry, sl, tp1, tp2]):
            return False, "All prices must be positive"
            
        if direction not in ["long", "short"]:
            return False, "Direction must be 'long' or 'short'"
            
        if direction == "long":
            if sl >= entry:
                return False, "For LONG: Stop Loss must be below Entry"
            if tp1 <= entry or tp2 <= entry:
                return False, "For LONG: Take Profits must be above Entry"
        else:  # short
            if sl <= entry:
                return False, "For SHORT: Stop Loss must be above Entry"
            if tp1 >= entry or tp2 >= entry:
                return False, "For SHORT: Take Profits must be below Entry"
        
        sl_distance = abs(entry - sl)
        if sl_distance / entry > 0.1:  # More than 10% SL distance
            return False, f"Stop loss distance too large: {sl_distance/entry*100:.2f}%"
            
        return True, "Valid"
        
    except (ValueError, TypeError) as e:
        return False, f"Invalid price format: {e}"

def validate_close_signal_data(data):
    """Validate incoming close signal data"""
    required_keys = {"action", "dir", "secret"}
    
    if not all(key in data for key in required_keys):
        missing = [key for key in required_keys if key not in data]
        return False, f"Missing required fields: {missing}"
    
    if data.get("secret") != WEBHOOK_SECRET:
        return False, "Invalid webhook secret"
        
    if data.get("action") != "close":
        return False, f"Expected action 'close', got '{data.get('action')}'"
    
    direction = str(data["dir"]).lower().strip()
    if direction not in ["long", "short"]:
        return False, "Direction must be 'long' or 'short'"
        
    return True, "Valid"

def cancel_existing_orders():
    """Cancel all existing orders for the symbol, including conditional orders."""
    try:
        client.futures_cancel_all_open_orders(symbol=SYMBOL)
        logger.info(f"Successfully sent request to cancel all open orders for {SYMBOL}")
        return True
    except Exception as e:
        logger.error(f"Error cancelling all open orders for {SYMBOL}: {e}")
        return False

def close_existing_positions():
    """Close any existing positions for the symbol"""
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        active_position = next((p for p in positions if float(p['positionAmt']) != 0), None)
        
        if active_position:
            position_amt = float(active_position['positionAmt'])
            side = 'BUY' if position_amt < 0 else 'SELL'
            qty = abs(position_amt)
            
            close_order = client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type='MARKET',
                quantity=qty,
                reduceOnly=True
            )
            logger.info(f"Closed existing position: {close_order['orderId']}")
            return True
        return True
    except Exception as e:
        logger.error(f"Error closing existing positions: {e}")
        return False

def handle_close_signal(direction, reason=None):
    """Handle close signal from Pine Script"""
    try:
        logger.info(f"Received close signal for {direction.upper()} position. Reason: {reason or 'Not specified'}")
        
        # Cancel all existing orders first
        cancel_success = cancel_existing_orders()
        if not cancel_success:
            logger.warning("Failed to cancel some orders, but continuing with position closure")
        
        time.sleep(1)  # Brief pause to ensure orders are cancelled
        
        # Close any existing positions
        close_success = close_existing_positions()
        
        if close_success:
            return {
                "status": "success", 
                "message": f"Successfully closed {direction.upper()} position and cancelled all orders",
                "reason": reason or "Close signal received"
            }
        else:
            return {
                "status": "error", 
                "message": f"Failed to close {direction.upper()} position, but orders were cancelled"
            }
            
    except Exception as e:
        logger.error(f"Error handling close signal: {e}")
        return {"status": "error", "message": f"Error processing close signal: {str(e)}"}

def place_trade(direction, entry, sl, tp1, tp2):
    """
    Places a trade using LIMIT orders with full margin checks and a timeout.
    """
    try:
        # --- Step 1: Get Account and Market Data ---
        equity = get_account_equity()
        if equity is None:
            return {"status": "error", "message": "Unable to fetch account equity"}

        symbol_info = get_symbol_info()
        if not symbol_info:
            return {"status": "error", "message": "Unable to fetch symbol information"}

        current_prices = get_current_prices()
        if not current_prices:
            return {"status": "error", "message": "Unable to fetch current prices"}

        sl_distance = abs(entry - sl)
        if sl_distance == 0:
            return {"status": "error", "message": "Stop loss distance is zero"}

        # --- Step 2 & 3: Leverage and Position Size Calculation ---
        sl_percent = sl_distance / entry
        safe_leverage = math.floor(0.90 / sl_percent) if sl_percent > 0 else 125
        dynamic_leverage = max(1, min(safe_leverage, 125))
        
        risk_amount = equity * RISK_PERCENT
        final_qty = risk_amount / sl_distance
        
        # --- Step 4: Margin Check and Downsizing (THE MISSING PIECE) ---
        max_margin_for_trade = equity * 0.95
        required_margin = (final_qty * entry) / dynamic_leverage

        if required_margin > max_margin_for_trade:
            logger.warning("Ideal position size requires too much margin. Downsizing to max possible.")
            max_position_notional = max_margin_for_trade * dynamic_leverage
            final_qty = max_position_notional / entry
            logger.warning(f"Downsized Qty: {final_qty:.3f}")

        final_qty = round(final_qty, symbol_info['quantity_precision'])

        if final_qty < symbol_info['min_qty']:
            return {"status": "error", "message": f"Calculated quantity {final_qty} below minimum {symbol_info['min_qty']}"}

        logger.info(f"CALCULATION - Leverage: {dynamic_leverage}x, Quantity: {final_qty}")

        # --- Step 5: Prepare and Place LIMIT Entry Order ---
        cancel_existing_orders()
        close_existing_positions()
        time.sleep(1)

        if not set_leverage(SYMBOL, dynamic_leverage):
            return {"status": "error", "message": "Failed to set dynamic leverage"}

        side = 'BUY' if direction.lower() == "long" else 'SELL'
        
        limit_price = current_prices['bid'] if side == 'BUY' else current_prices['ask']
        limit_price = round(limit_price, symbol_info['price_precision'])

        logger.info(f"Placing LIMIT {side} order for {final_qty} {SYMBOL} at {limit_price}")
        
        entry_order = client.futures_create_order(
            symbol=SYMBOL, side=side, type='LIMIT',
            quantity=final_qty, price=limit_price, timeInForce='GTC'
        )
        entry_order_id = entry_order['orderId']

        # --- Step 6: Wait for the Order to Fill ---
        order_filled = False
        WAIT_TIME_SECONDS = 60
        order_status = None # Define order_status here
        for i in range(WAIT_TIME_SECONDS // 5):
            time.sleep(5)
            order_status = client.futures_get_order(symbol=SYMBOL, orderId=entry_order_id)
            
            if order_status['status'] == 'FILLED':
                logger.info(f"Entry order {entry_order_id} has been filled!")
                order_filled = True
                break
            else:
                logger.info(f"Waiting for entry order {entry_order_id} to fill... Status: {order_status['status']}")

        # --- Step 7: Handle the Outcome ---
        if not order_filled:
            logger.warning(f"Entry order {entry_order_id} timed out. Cancelling.")
            client.futures_cancel_order(symbol=SYMBOL, orderId=entry_order_id)
            return {"status": "error", "message": "Entry order timed out and was cancelled."}

        # --- Step 8: Place SL and TP Orders ---
        logger.info("Entry confirmed. Placing Stop Loss and Take Profit orders.")
        
        filled_qty = float(order_status['executedQty'])
        opposite_side = 'SELL' if side == 'BUY' else 'BUY'
        
        sl_price = round_price(sl)
        tp1_price = round_price(tp1)
        tp2_price = round_price(tp2)

        tp1_qty = round(filled_qty * 0.5, symbol_info['quantity_precision'])
        if tp1_qty >= symbol_info['min_qty']:
            client.futures_create_order(symbol=SYMBOL, side=opposite_side, type='LIMIT', quantity=tp1_qty, price=tp1_price, timeInForce='GTC', reduceOnly=True)
            logger.info(f"TP1 order placed at {tp1_price}")

        tp2_qty = round(filled_qty - tp1_qty, symbol_info['quantity_precision'])
        if tp2_qty >= symbol_info['min_qty']:
            client.futures_create_order(symbol=SYMBOL, side=opposite_side, type='LIMIT', quantity=tp2_qty, price=tp2_price, timeInForce='GTC', reduceOnly=True)
            logger.info(f"TP2 order placed at {tp2_price}")

        client.futures_create_order(symbol=SYMBOL, side=opposite_side, type='STOP_MARKET', stopPrice=sl_price, quantity=filled_qty, timeInForce='GTC', reduceOnly=True)
        logger.info(f"Stop Loss order placed at {sl_price}")

        return {"status": "success", "message": "Trade executed successfully with limit order."}

    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"An error occurred: {e}")
        close_existing_positions()
        cancel_existing_orders()
        return {"status": "error", "message": f"An error occurred: {e}"}
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return {"status": "error", "message": f"An unexpected error occurred: {e}"}

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming webhook from TradingView with JSON secret validation"""
    try:
        data = request.get_json(force=True)
        
        # Determine signal type and validate accordingly
        action = data.get("action", "").lower()
        
        if action == "open":
            logger.info(f"Received OPEN signal: {json.dumps({k: v for k, v in data.items() if k != 'secret'})}")
            
            is_valid, error_msg = validate_open_signal_data(data)
            if not is_valid:
                logger.error(f"Invalid open signal data: {error_msg}")
                return jsonify({"status": "error", "message": error_msg}), 400

            result = place_trade(
                direction=data["dir"],
                entry=float(data["entry"]),
                sl=float(data["sl"]),
                tp1=float(data["tp1"]),
                tp2=float(data["tp2"])
            )
            
        elif action == "close":
            logger.info(f"Received CLOSE signal: {json.dumps({k: v for k, v in data.items() if k != 'secret'})}")
            
            is_valid, error_msg = validate_close_signal_data(data)
            if not is_valid:
                logger.error(f"Invalid close signal data: {error_msg}")
                return jsonify({"status": "error", "message": error_msg}), 400

            result = handle_close_signal(
                direction=data["dir"],
                reason=data.get("reason", "Pine Script close signal")
            )
            
        else:
            logger.error(f"Unknown action: {action}")
            return jsonify({"status": "error", "message": f"Unknown action: {action}. Expected 'open' or 'close'"}), 400

        return jsonify(result)

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON received: {e}")
        return jsonify({"status": "error", "message": "Invalid JSON format"}), 400
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        account_info = client.futures_account()
        equity = float(account_info['totalWalletBalance'])
        
        return jsonify({
            "status": "healthy", 
            "timestamp": datetime.now().isoformat(),
            "message": "Bot is running and connected to Binance",
            "account_equity": equity,
            "risk_percent": RISK_PERCENT * 100
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy", 
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        }), 500

@app.route('/status', methods=['GET'])
def status():
    """Get detailed bot status and configuration"""
    try:
        equity = get_account_equity()
        symbol_info = get_symbol_info()
        
        open_orders = client.futures_get_open_orders(symbol=SYMBOL)
        
        positions = client.futures_position_information(symbol=SYMBOL)
        active_position = next((p for p in positions if float(p['positionAmt']) != 0), None)
        
        return jsonify({
            "status": "running",
            "config": {
                "symbol": SYMBOL,
                "risk_percent": RISK_PERCENT * 100
            },
            "account": {
                "equity": equity
            },
            "market": {
                "open_orders": len(open_orders),
                "active_position": bool(active_position),
                "position_size": float(active_position['positionAmt']) if active_position else 0,
                "unrealized_pnl": float(active_position['unRealizedProfit']) if active_position else 0
            },
            "symbol_info": symbol_info,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Status endpoint error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/cancel-orders', methods=['POST'])
def cancel_all_orders():
    """Emergency endpoint to cancel all orders"""
    try:
        success = cancel_existing_orders()
        if success:
            return jsonify({
                "status": "success", 
                "message": "All orders cancelled",
                "timestamp": datetime.now().isoformat()
            })
        else:
            return jsonify({
                "status": "error", 
                "message": "Failed to cancel some orders",
                "timestamp": datetime.now().isoformat()
            }), 500
    except Exception as e:
        logger.error(f"Cancel orders error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/close-position', methods=['POST'])
def close_position():
    """Emergency endpoint to close current position"""
    try:
        cancel_existing_orders()
        time.sleep(1)
        
        success = close_existing_positions()
        if success:
            return jsonify({
                "status": "success", 
                "message": "Position closed and orders cancelled",
                "timestamp": datetime.now().isoformat()
            })
        else:
            return jsonify({
                "status": "error", 
                "message": "Failed to close position",
                "timestamp": datetime.now().isoformat()
            }), 500
    except Exception as e:
        logger.error(f"Close position error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# === Error Handlers ===
@app.errorhandler(404)
def not_found(error):
    return jsonify({"status": "error", "message": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({"status": "error", "message": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({"status": "error", "message": "Internal server error"}), 500


# === Run Server ===
if _name_ == '_main_':
    logger.info("Starting Robust Flask Trading Bot...")
    logger.info(f"Configuration - Symbol: {SYMBOL}, Risk: {RISK_PERCENT*100}%")
    
    try:
        account_info = client.futures_account()
        equity = float(account_info['totalWalletBalance'])
        logger.info(f"✅ Successfully connected to Binance Futures API - Equity: {equity} USDT")
        
        symbol_info = get_symbol_info()
        if symbol_info:
            logger.info(f"✅ Symbol info loaded - Min qty: {symbol_info['min_qty']}, Tick size: {symbol_info['tick_size']}")
        else:
            logger.error("⚠ Failed to load symbol information")
            
    except Exception as e:
        logger.error(f"❌ Failed to connect to Binance API: {e}")
        logger.error("Please check your API credentials and permissions")
        exit(1)
    
    if WEBHOOK_SECRET == "your_webhook_secret_here":
        logger.error("❌ Please set a secure WEBHOOK_SECRET environment variable")
        exit(1)
    
    logger.info("🚀 Bot is ready to receive webhooks with OPEN/CLOSE actions")
    app.run(debug=False, host='0.0.0.0', port=5000)
