import time
import hmac
import hashlib
import requests
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from talib import RSI

# --- CONFIGURATION ---
API_KEY = "6wtNuwL74oSLs8t8uVEhL54J4oujmKgcHJAppfUt0kl2Tzi4Ipxd2j8HQMU1vnGr"
API_SECRET = "tALjKplRu10iTcIXFGYQyMGdLdrZEH9byUjUdJFX8eBM7XLsrmkD5X819k3sBKbf"
MEMECOINS = [
    "WIFUSDT", "BONKUSDT", "BOMEUSDT"
]  # Suppression de POPCATUSDT, GMEUSDT, TOSHIUSDT
DCA_AMOUNT = 20  # Montant de base en USDT par memecoin
DIP_BUY_AMOUNT = 30  # Montant supplémentaire lors des dips
DCA_TIME = "08:00"  # Heure d'exécution quotidienne (UTC)
RSI_PERIOD = 20  # Période pour le RSI
# ---------------------

BASE_URL = "https://api.binance.com"

class OptimizedDCABot:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": API_KEY})
        self.trades = []
        self.last_execution_day = None
        self.price_history = {symbol: [] for symbol in MEMECOINS}
        self.last_dip_buy = {symbol: None for symbol in MEMECOINS}
        self.rsi_history = {symbol: [] for symbol in MEMECOINS}

    def _sign_request(self, params):
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

    def _api_request(self, method, endpoint, params=None, signed=False):
        url = f"{BASE_URL}{endpoint}"
        try:
            if signed:
                params = params or {}
                params['timestamp'] = int(time.time() * 1000)
                params['signature'] = self._sign_request(params)
            
            if method == "GET":
                response = self.session.get(url, params=params)
            elif method == "POST":
                response = self.session.post(url, data=params)
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Erreur API: {str(e)}")
            return None

    def get_historical_data(self, symbol, interval='1h', limit=100):
        """Récupère les données historiques pour calculer le RSI"""
        endpoint = "/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        data = self._api_request("GET", endpoint, params)
        if not data:
            return None
            
        # Convertir en DataFrame pour le traitement
        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        
        # Convertir les types de données
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, axis=1)
        
        return df

    def calculate_rsi(self, symbol):
        """Calcule le RSI(20) pour un memecoin"""
        df = self.get_historical_data(symbol)
        if df is None or len(df) < RSI_PERIOD:
            print(f"⚠️ Données insuffisantes pour {symbol}")
            return None
        
        # Calcul du RSI
        rsi_values = RSI(df['close'], timeperiod=RSI_PERIOD)
        current_rsi = rsi_values.iloc[-1]
        
        # Stocker l'historique pour le monitoring
        self.rsi_history[symbol].append(current_rsi)
        
        # Garder seulement les 100 dernières valeurs
        if len(self.rsi_history[symbol]) > 100:
            self.rsi_history[symbol] = self.rsi_history[symbol][-100:]
        
        return current_rsi

    def should_buy(self, symbol):
        """Détermine si les conditions d'achat sont remplies"""
        current_rsi = self.calculate_rsi(symbol)
        if current_rsi is None:
            return False
        
        print(f"📈 {symbol} - RSI(20): {current_rsi:.2f}")
        
        # Condition d'achat basée sur le RSI
        return 18 <= current_rsi <= 22

    def get_price(self, symbol):
        """Récupère le prix actuel avec gestion des erreurs"""
        data = self._api_request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        if data and 'price' in data:
            price = float(data['price'])
            # Mettre à jour l'historique des prix
            self.price_history[symbol].append(price)
            # Garder seulement les 100 derniers prix
            if len(self.price_history[symbol]) > 100:
                self.price_history[symbol] = self.price_history[symbol][-100:]
            return price
        return None

    def get_24h_volume(self, symbol):
        data = self._api_request("GET", "/api/v3/ticker/24hr", {"symbol": symbol})
        return float(data['volume']) if data and 'volume' in data else 0

    def get_symbol_info(self, symbol):
        data = self._api_request("GET", "/api/v3/exchangeInfo")
        if data and 'symbols' in data:
            for s in data['symbols']:
                if s['symbol'] == symbol:
                    return s
        return None

    def calculate_moving_average(self, symbol, window=24):
        """Calcule la moyenne mobile sur X heures (par défaut 24h)"""
        prices = self.price_history.get(symbol, [])
        if len(prices) < window:
            return None
        return np.mean(prices[-window:])

    def detect_dip(self, symbol):
        """Détecte si le prix actuel est en dip significatif"""
        current_price = self.get_price(symbol)
        if current_price is None:
            return False
        
        ma24 = self.calculate_moving_average(symbol, 24)
        if ma24 is None:
            return False
        
        # Un dip est détecté si le prix est >10% sous la moyenne mobile 24h
        dip_threshold = ma24 * 0.90  # 10% sous la MA
        
        # Vérifier si on a déjà acheté récemment un dip
        last_buy = self.last_dip_buy[symbol]
        buy_cooldown = timedelta(hours=6)  # 6h entre deux achats de dip
        
        return (current_price < dip_threshold and 
                (last_buy is None or (datetime.utcnow() - last_buy) > buy_cooldown))

    def execute_buy(self, symbol, amount_usdt, is_dip_buy=False):
        """Exécute un achat au marché pour un montant donné en USDT"""
        print(f"\n{'🟢 ACHAT DIP - ' if is_dip_buy else '🟦 '}Traitement de {symbol}...")
        
        # Vérification volume
        volume = self.get_24h_volume(symbol)
        if volume < 10000000:
            print(f"📉 Volume insuffisant: {volume:.2f} USDT")
            return False
        
        # Récupération info trading
        symbol_info = self.get_symbol_info(symbol)
        if not symbol_info:
            print("❌ Erreur: informations non trouvées")
            return False
        
        # Calcul quantité
        current_price = self.get_price(symbol)
        if current_price is None:
            print("❌ Erreur: impossible de récupérer le prix")
            return False
            
        quantity = amount_usdt / current_price
        
        # Application des règles Binance
        for filt in symbol_info['filters']:
            if filt['filterType'] == 'LOT_SIZE':
                step_size = float(filt['stepSize'])
                quantity = int(quantity / step_size) * step_size
            elif filt['filterType'] == 'MIN_NOTIONAL':
                min_value = float(filt['minNotional'])
                if quantity * current_price < min_value:
                    print(f"📉 Quantité trop faible: {quantity * current_price:.2f} USDT")
                    return False
        
        # Placement ordre
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": round(quantity, 8)
        }
        
        response = self._api_request("POST", "/api/v3/order", params, signed=True)
        
        if response and 'orderId' in response:
            print(f"✅ Achat {'DIP ' if is_dip_buy else ''}réussi: {quantity} {symbol}")
            self.record_trade(symbol, quantity, current_price, is_dip_buy)
            if is_dip_buy:
                self.last_dip_buy[symbol] = datetime.utcnow()
            return True
        else:
            print(f"❌ Erreur achat: {response.get('msg', 'Inconnue') if response else 'Pas de réponse'}")
            return False

    def record_trade(self, symbol, quantity, entry_price, is_dip_buy=False):
        self.trades.append({
            "symbol": symbol,
            "quantity": quantity,
            "entry_price": entry_price,
            "entry_time": datetime.utcnow().isoformat(),
            "take_profit_levels": [1.20, 1.50, 2.00],  # +20%, +50%, +100%
            "tp_percentages": [0.30, 0.30, 0.40],      # 30%, 30%, 40%
            "tp_executed": [False, False, False],
            "closed": False,
            "is_dip_buy": is_dip_buy  # Marquer si c'est un achat de dip
        })
        print(f"📝 Trade enregistré pour {symbol}")

    def check_take_profits(self):
        if not self.trades:
            return
            
        print("\n🔎 Vérification des profits...")
        for trade in self.trades:
            if trade['closed']:
                continue
                
            try:
                current_price = self.get_price(trade['symbol'])
                if current_price is None:
                    continue
                    
                for i, (target, executed) in enumerate(zip(trade['take_profit_levels'], trade['tp_executed'])):
                    if not executed and current_price >= trade['entry_price'] * target:
                        self.execute_take_profit(trade, i)
                        
                # Vérifier si tous les TP sont exécutés
                if all(trade['tp_executed']):
                    trade['closed'] = True
                    print(f"✅ Position clôturée: {trade['symbol']}")
            except Exception as e:
                print(f"❌ Erreur TP: {str(e)}")

    def execute_take_profit(self, trade, level_index):
        symbol = trade['symbol']
        print(f"\n🚀 Déclenchement TP niveau {level_index+1} pour {symbol}")
        
        # Calcul quantité à vendre
        sell_percent = trade['tp_percentages'][level_index]
        sell_quantity = trade['quantity'] * sell_percent
        
        # Récupération règles trading
        symbol_info = self.get_symbol_info(symbol)
        if symbol_info:
            for filt in symbol_info['filters']:
                if filt['filterType'] == 'LOT_SIZE':
                    step_size = float(filt['stepSize'])
                    sell_quantity = int(sell_quantity / step_size) * step_size
        
        # Placement ordre vente
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": round(sell_quantity, 8)
        }
        
        response = self._api_request("POST", "/api/v3/order", params, signed=True)
        
        if response and 'orderId' in response:
            trade['tp_executed'][level_index] = True
            print(f"💰 Vente réussie: {sell_quantity} {symbol}")
        else:
            print(f"❌ Erreur vente: {response.get('msg', 'Inconnue') if response else 'Pas de réponse'}")

    def check_dip_opportunities(self):
        """Vérifie les opportunités d'achat lors des baisses importantes"""
        print("\n🔍 Recherche d'opportunités de dip...")
        for symbol in MEMECOINS:
            try:
                # Vérifier si on est dans un dip
                if self.detect_dip(symbol):
                    print(f"📉 Dip détecté sur {symbol}! Vérification RSI...")
                    
                    # Vérifier la condition RSI pour confirmation
                    if self.should_buy(symbol):
                        print(f"✅ RSI confirme l'opportunité d'achat sur {symbol}")
                        self.execute_buy(symbol, DIP_BUY_AMOUNT, is_dip_buy=True)
                    else:
                        print(f"⏸️ RSI non favorable pour {symbol}, achat de dip annulé")
            except Exception as e:
                print(f"❌ Erreur détection dip pour {symbol}: {str(e)}")

    def run_dca_with_rsi(self):
        """Exécute le DCA quotidien avec vérification du RSI"""
        print(f"\n{'='*30}")
        print(f"🔄 Début de la vérification DCA du {datetime.utcnow().date()}")
        print(f"{'='*30}")
        
        for symbol in MEMECOINS:
            try:
                # Vérifier la condition RSI
                if self.should_buy(symbol):
                    print(f"✅ Conditions RSI remplies pour {symbol}")
                    self.execute_buy(symbol, DCA_AMOUNT)
                else:
                    print(f"⏸️ RSI non favorable pour {symbol}, achat DCA sauté")
            except Exception as e:
                print(f"❌ Erreur lors du DCA pour {symbol}: {str(e)}")

    def run(self):
        print(f"=== 🤖 Bot DCA Optimisé Binance ===")
        print(f"Memecoins: {', '.join(MEMECOINS)}")
        print(f"Montant DCA: {DCA_AMOUNT} USDT")
        print(f"Montant DIP: {DIP_BUY_AMOUNT} USDT")
        print(f"RSI Période: {RSI_PERIOD}")
        print(f"Heure DCA: {DCA_TIME} UTC")
        print("Démarrage... (Ctrl+C pour arrêter)\n")
        
        # Initialisation de l'historique des prix
        for symbol in MEMECOINS:
            self.get_price(symbol)
            # Calcul initial du RSI
            self.calculate_rsi(symbol)
        
        while True:
            try:
                now = datetime.utcnow()
                current_time = now.strftime("%H:%M")
                
                # Exécution DCA quotidienne avec RSI
                if current_time == DCA_TIME:
                    if self.last_execution_day != now.date():
                        self.run_dca_with_rsi()
                        self.last_execution_day = now.date()
                
                # Vérification des opportunités de dip toutes les 15 minutes
                if now.minute % 15 == 0:
                    self.check_dip_opportunities()
                
                # Vérification TP toutes les 2 minutes
                if now.minute % 2 == 0:
                    self.check_take_profits()
                
                # Mise à jour des prix toutes les minutes
                if now.second == 0:
                    for symbol in MEMECOINS:
                        self.get_price(symbol)
                
                # Attente jusqu'à la prochaine seconde
                time.sleep(1)
                
            except KeyboardInterrupt:
                print("\n🛑 Arrêt demandé...")
                break
            except Exception as e:
                print(f"❌ ERREUR: {str(e)}")
                time.sleep(60)

if __name__ == "__main__":
    bot = OptimizedDCABot()
    bot.run()