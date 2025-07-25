import os
import requests
import json
import numpy as np
import pandas as pd
import time
import traceback
import logging
from sklearn.preprocessing import MinMaxScaler
from scipy import stats
from datetime import datetime, timedelta

# Import dari app_config
try:
    from config import DATA_BASE_PATH, TIINGO_API_TOKEN, TIINGO_CACHE_TTL
except ImportError:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_BASE_PATH = os.path.join(BASE_DIR, 'data')
    TIINGO_API_TOKEN = os.environ.get('TIINGO_API_TOKEN', '')
    TIINGO_CACHE_TTL = 150

# Direktori dan konfigurasi
TIINGO_DATA_DIR = os.path.join(DATA_BASE_PATH, 'tiingo_data')
os.makedirs(TIINGO_DATA_DIR, exist_ok=True)
logger = logging.getLogger(__name__)

HEADERS = {'Content-Type': 'application/json'}

# Variabel untuk mencatat waktu permintaan API dan rate limiting
_last_tiingo_request_time = 0
_tiingo_request_count_hourly = 0
_tiingo_request_count_daily = 0
_tiingo_hourly_reset = time.time()
_tiingo_daily_reset = time.time()

def _check_tiingo_rate_limits():
    global _tiingo_request_count_hourly, _tiingo_request_count_daily
    global _tiingo_hourly_reset, _tiingo_daily_reset
    
    current_time = time.time()
    
    if current_time - _tiingo_hourly_reset > 3600:
        _tiingo_request_count_hourly = 0
        _tiingo_hourly_reset = current_time
        logger.info("Tiingo hourly request counter reset")
    
    if current_time - _tiingo_daily_reset > 86400:
        _tiingo_request_count_daily = 0
        _tiingo_daily_reset = current_time
        logger.info("Tiingo daily request counter reset")
    
    if _tiingo_request_count_hourly >= 50:
        logger.warning("Tiingo hourly request limit (50) reached for 5-minute interval. Using cached data.")
        return False
    
    if _tiingo_request_count_daily >= 1000:
        logger.warning("Approaching Tiingo daily request limit (1000). Using cached data.")
        return False
    
    return True

def _increment_tiingo_counters():
    global _tiingo_request_count_hourly, _tiingo_request_count_daily
    _tiingo_request_count_hourly += 1
    _tiingo_request_count_daily += 1

def _get_tiingo_cache_path(resample_freq):
    return os.path.join(TIINGO_DATA_DIR, f"tiingo_data_{resample_freq}.json")

def _is_cache_valid(cache_path):
    if not os.path.exists(cache_path):
        return False
    
    file_mtime = os.path.getmtime(cache_path)
    current_time = time.time()
    
    return (current_time - file_mtime) < TIINGO_CACHE_TTL

def get_tiingo_data(ticker="paxgusd", interval="5min", start_date=None, end_date=None):
    """
    Mengambil data OHLCV dari Tiingo API untuk kripto dengan rentang tanggal tertentu
    Args:
        ticker (str): Simbol ticker (misalnya 'paxgusd')
        interval (str): Frekuensi data ('5min' default)
        start_date (str): Tanggal mulai (format 'YYYY-MM-DDTHH:MM:SSZ')
        end_date (str): Tanggal akhir (format 'YYYY-MM-DDTHH:MM:SSZ')
    Returns:
        pd.DataFrame: DataFrame dengan data OHLCV atau DataFrame kosong jika gagal
    """
    try:
        ticker_formatted = ticker.lower() if ticker.lower().endswith('usd') else f"{ticker.lower()}usd"
        logger.info(f"Formatting ticker from {ticker} to {ticker_formatted} for Tiingo API")
        
        if not _check_tiingo_rate_limits():
            logger.warning("Tiingo rate limit reached. Skipping request.")
            return pd.DataFrame()
        
        url = f"https://api.tiingo.com/tiingo/crypto/prices?tickers={ticker_formatted}&resampleFreq={interval}&token={TIINGO_API_TOKEN}"
        if start_date and end_date:
            url += f"&startDate={start_date}&endDate={end_date}"
        
        logger.info(f"Fetching data from Tiingo for {ticker_formatted} with {interval} interval from {start_date} to {end_date}")
        
        global _last_tiingo_request_time
        current_time = time.time()
        min_interval = 100.0
        if current_time - _last_tiingo_request_time < min_interval:
            sleep_time = min_interval - (current_time - _last_tiingo_request_time)
            logger.info(f"Waiting {sleep_time:.2f} seconds to respect rate limit")
            time.sleep(sleep_time)
        
        response = requests.get(url, headers=HEADERS)
        _last_tiingo_request_time = time.time()
        _increment_tiingo_counters()
        
        logger.info(f"Tiingo API response status: {response.status_code}")
        if response.status_code != 200:
            logger.warning(f"Tiingo API error: {response.text}")
            error_json_path = os.path.join(TIINGO_DATA_DIR, f'tiingo_error_{ticker_formatted}_{start_date[:10]}.json')
            with open(error_json_path, 'w') as f:
                json.dump(response.json(), f, indent=4)
            logger.error(f"Error details saved to {error_json_path}")
            return pd.DataFrame()
        
        response.raise_for_status()
        
        data = response.json()
        if not data or not any('priceData' in item for item in data):
            logger.error(f"No data returned from Tiingo for {ticker_formatted} from {start_date} to {end_date}")
            return pd.DataFrame()
        
        with open(os.path.join(TIINGO_DATA_DIR, f'tiingo_data_{interval}.json'), 'w') as f:
            json.dump(data[0] if isinstance(data, list) else data, f, indent=4)
        logger.info(f"Saved data to {TIINGO_DATA_DIR}/tiingo_data_{interval}.json")
        
        df = pd.DataFrame(data[0]['priceData'] if isinstance(data, list) else data[ticker_formatted]['priceData'])
        df['date'] = pd.to_datetime(df['date'])
        df['timestamp'] = df['date'].astype(np.int64) // 10**9
        logger.info(f"Fetched {len(df)} records from Tiingo for {ticker_formatted} from {start_date} to {end_date}")
        return df
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from Tiingo: {e}")
        return pd.DataFrame()
    except IOError as e:
        logger.error(f"IO Error saving data for {ticker_formatted}: {e}")
        return pd.DataFrame()

def fetch_historical_data(ticker="paxgusd", resample_freq="5min"):
    end_date = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    start_date = '2025-06-08T00:00:00Z'
    json_path = os.path.join(TIINGO_DATA_DIR, f"tiingo_data_{resample_freq}.json")

    all_data = {'paxgusd': {'priceData': []}}
    current_end_date = datetime.strptime(end_date, '%Y-%m-%dT%H:%M:%SZ')
    current_start_date = datetime.strptime(start_date, '%Y-%m-%dT%H:%M:%SZ')

    batch_days = 17 # Maximal 17 hari untuk 1x request dengan tf 5mins
    while current_start_date < current_end_date:
        batch_end_date = min(current_start_date + timedelta(days=batch_days), current_end_date)
        batch_start_date = current_start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        batch_end_date_str = batch_end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        
        df = get_tiingo_data(ticker, interval=resample_freq, start_date=batch_start_date, end_date=batch_end_date_str)
        
        if not df.empty:
            # Pastikan date dalam format string ISO 8601
            df['date'] = df['date'].dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')
            existing_dates = {pd.to_datetime(d['date']).date() for d in all_data['paxgusd']['priceData']}
            new_data = df.to_dict('records')
            new_data = [d for d in new_data if pd.to_datetime(d['date']).date() not in existing_dates]
            if new_data:
                all_data['paxgusd']['priceData'].extend(new_data)
                actual_start = min(pd.to_datetime(d['date']) for d in new_data).strftime('%Y-%m-%d')
                actual_end = max(pd.to_datetime(d['date']) for d in new_data).strftime('%Y-%m-%d')
                logger.info(f"Fetched {len(new_data)} new records from {actual_start} to {actual_end}")
            else:
                logger.warning(f"No new data after deduplication for batch {batch_start_date} to {batch_end_date_str}")
        else:
            logger.warning(f"Failed to fetch data for batch {batch_start_date} to {batch_end_date_str}")
        
        current_start_date += timedelta(days=batch_days)
    
    if all_data['paxgusd']['priceData']:
        with open(json_path, 'w') as f:
            json.dump(all_data, f, indent=4)
        logger.info(f"Data saved to {json_path} with {len(all_data['paxgusd']['priceData'])} records")
        df_final = pd.DataFrame(all_data['paxgusd']['priceData'])
        actual_start_date = df_final['date'].min()
        actual_end_date = df_final['date'].max()
        logger.info(f"Final data range: {actual_start_date} to {actual_end_date}")
        return df_final
    else:
        logger.error(f"No data collected for historical data")
        return pd.DataFrame()

def update_recent_data(ticker="paxgusd", resample_freq="5min", initial=False):
    """
    Mengupdate data terbaru dari Tiingo API
    Args:
        ticker (str): Simbol ticker (misalnya 'paxgusd')
        resample_freq (str): Frekuensi data ('5min' default)
        initial (bool): Jika True, ambil data 100 jam terakhir
    Returns:
        pd.DataFrame: Data OHLCV terbaru
    """
    end_date = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    if initial:
        start_date = (datetime.utcnow() - timedelta(hours=100)).strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        start_date = (datetime.utcnow() - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

    url = f"https://api.tiingo.com/tiingo/crypto/prices?tickers={ticker}&startDate={start_date}&endDate={end_date}&resampleFreq={resample_freq}&token={TIINGO_API_TOKEN}"
    try:
        request_response = requests.get(url, headers=HEADERS)
        logger.info(f"Status Code: {request_response.status_code} for {start_date} to {end_date}")
        
        if request_response.status_code == 200:
            data = request_response.json()
            if not data or 'priceData' not in data[0]:
                logger.warning(f"No valid priceData in response: {data}")
                return pd.DataFrame()
            
            all_price_data = []
            for item in data:
                if 'priceData' in item:
                    all_price_data.extend(item['priceData'])
            if all_price_data:
                df = pd.DataFrame(all_price_data)
                df = df.sort_values('date', ascending=False).drop_duplicates(subset=['date'], keep='last')
                # Tidak perlu konversi ke Timestamp, biarkan sebagai string
                json_path = os.path.join(TIINGO_DATA_DIR, f"tiingo_data_{resample_freq}.json")
                with open(json_path, 'r') as f:
                    existing_data = json.load(f)
                existing_data['paxgusd']['priceData'].extend(df.to_dict('records'))
                with open(json_path, 'w') as f:
                    json.dump(existing_data, f, indent=4)
                actual_start_date = df['date'].min()
                actual_end_date = df['date'].max()
                logger.info(f"Updated {len(df)} records from {actual_start_date} to {actual_end_date}")
                return df
            else:
                logger.warning(f"No priceData in response for {start_date} to {end_date}")
                return pd.DataFrame()
        else:
            logger.error(f"Error Response: Status {request_response.status_code} for {start_date} to {end_date}")
            return pd.DataFrame()
    except Exception as e:
        logger.error(f"Request failed for {start_date} to {end_date}: {e}")
        return pd.DataFrame()

def get_coingecko_price():
    """
    Mendapatkan harga terkini PAXG dari CoinGecko API untuk log info saja
    
    Returns:
        tuple: (price, timestamp) atau (None, None) jika gagal
    """
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?vs_currencies=usd&symbols=paxg&include_last_updated_at=true&precision=3"
        headers = {
            "accept": "application/json",
            "x-cg-demo-api-key": "CG-XXcTraNZPUEmnQV5bD8yuQ3Nbs"
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        price = data['paxg']['usd']
        timestamp = data['paxg']['last_updated_at']
        timestamp_iso = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        
        logger.info(f"CoinGecko real-time price: ${price} at timestamp {timestamp_iso}")  # Hanya untuk log info
        return price, timestamp
    
    except Exception as e:
        logger.error(f"Error fetching price from CoinGecko: {e}")
        logger.error(traceback.format_exc())
        return None, None

def load_tiingo_data(token_name, cache_dir=TIINGO_DATA_DIR):
    """
    Memuat data OHLCV dari file cache Tiingo JSON dengan logging detail
    Args:
        token_name (str): Nama token (misalnya 'paxgusd')
        cache_dir (str): Direktori cache Tiingo
        
    Returns:
        tuple: (pd.DataFrame, float) atau (None, None) jika gagal
    """
    base_ticker = token_name.replace('usd', '').lower()
    cache_path = os.path.join(cache_dir, "tiingo_data_5min.json")
    
    # Cek keberadaan file cache
    if not os.path.exists(cache_path):
        logger.error(f"Cache file not found: {cache_path}")
        return None, None
    
    try:
        # Muat data dari cache
        with open(cache_path, 'r') as f:
            data = json.load(f)
        
        # Validasi struktur data
        if 'paxgusd' not in data or 'priceData' not in data['paxgusd']:
            logger.error(f"Invalid cache structure: 'paxgusd' or 'priceData' missing in {cache_path}")
            return None, None
        
        df = pd.DataFrame(data['paxgusd']['priceData'])
        logger.info(f"Initial data loaded: {len(df)} records")
        
        # Jika DataFrame kosong, kembalikan None
        if df.empty:
            logger.warning(f"Cache contains no records for {token_name}")
            return None, None
        
        # Validasi dan isi kolom yang hilang
        required_columns = ['date', 'open', 'high', 'low', 'close', 'volume', 'volumeNotional', 'tradesDone']
        missing_columns = [col for col in required_columns if col not in df.columns]
        for col in missing_columns:
            logger.warning(f"Column {col} missing in cache, filling with 0.0")
            df[col] = 0.0
        
        # Pastikan kolom 'close' tidak semuanya nol atau NaN
        if df['close'].isna().all() or (df['close'] == 0).all():
            logger.error(f"Column 'close' contains only NaN or zeros, cannot process data")
            return None, None
        
        # Konversi 'date' ke datetime dan buat timestamp
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        if df['date'].isna().any():
            logger.warning(f"Found {df['date'].isna().sum()} invalid dates, dropping those rows")
            df = df.dropna(subset=['date'])
        
        if df.empty:
            logger.error("DataFrame empty after date conversion")
            return None, None
        
        df['timestamp'] = df['date'].astype(np.int64) // 10**9
        
        # Hapus duplikat berdasarkan 'date' dengan logging
        duplicate_count = df.duplicated(subset=['date']).sum()
        if duplicate_count > 0:
            logger.info(f"Found {duplicate_count} duplicate dates, keeping the last occurrence")
        df = df.drop_duplicates(subset=['date'], keep='last').sort_values('date').reset_index(drop=True)
        logger.info(f"After removing duplicates: {len(df)} records")
        
        # Pastikan cukup data untuk prediksi
        look_back = 60  # Sesuaikan dengan konfigurasi di app.py
        if len(df) < look_back:
            logger.error(f"Insufficient data: need at least {look_back} records, got {len(df)}")
            return None, None
        
        # Ambil harga terakhir, gunakan CoinGecko sebagai fallback jika data kosong
        coingecko_price = df['close'].iloc[-1] if not df.empty else None
        if coingecko_price is None or coingecko_price == 0:
            logger.warning("Last 'close' price is invalid, attempting to fetch from CoinGecko")
            price, _ = get_coingecko_price()
            coingecko_price = price if price is not None else None
        
        logger.info(f"Final loaded data: {len(df)} records for {token_name}, date range: {df['date'].min()} to {df['date'].max()}")
        return df, coingecko_price
    
    except json.JSONDecodeError as e:
        logger.error(f"JSONDecodeError loading Tiingo cache data: {e}")
        logger.error(f"Cache file may be corrupted: {cache_path}")
        try:
            with open(cache_path, 'r') as f:
                logger.error(f"Cache content sample: {f.read()[:100]}")
        except Exception as read_err:
            logger.error(f"Failed to read cache content: {read_err}")
        return None, None
    except Exception as e:
        logger.error(f"Error loading Tiingo cache data: {e}")
        logger.error(traceback.format_exc())
        return None, None

def calculate_log_returns(prices, period=1440):
    """
    Menghitung log returns untuk harga yang diberikan dengan periode tertentu.
    Args:
        prices (numpy.array or pandas.Series): Data harga
        period (int): Periode untuk menghitung returns dalam data poin
    Returns:
        numpy.array: Array log returns
    """
    if isinstance(prices, np.ndarray):
        if prices.ndim > 1:
            prices = prices.flatten()
    elif isinstance(prices, pd.Series):
        prices = prices.values
    else:
        prices = np.array(prices)
    
    if len(prices) <= period:
        logger.error(f"Tidak cukup titik data ({len(prices)}) untuk periode ({period})")
        return np.array([])
    
    # log_returns = np.log(prices[period:] / prices[:-period])
    log_returns = np.log((prices[period:] + 1e-8) / (prices[:-period] + 1e-8))
    return log_returns

def calculate_zptae(y_true, y_pred, sigma=None, alpha=1.5, window_size=100):
    """
    Menghitung Z-transformed Power-Tanh Absolute Error (ZPTAE) dengan optimasi.
    Args:
        y_true (numpy.array): Nilai aktual
        y_pred (numpy.array): Nilai prediksi
        sigma (float, optional): Standar deviasi untuk normalisasi
        alpha (float): Parameter power-law untuk PowerTanh (default 1.5)
        window_size (int): Ukuran jendela untuk perhitungan std (default 100)
    Returns:
        float: Skor ZPTAE (lebih rendah lebih baik)
    """
    y_true = y_true.flatten()
    y_pred = y_pred.flatten()
    abs_errors = np.abs(y_true - y_pred)
    abs_errors = np.clip(abs_errors, 0, np.percentile(abs_errors, 95))  # Batasi outlier
    n = len(abs_errors)
    zptae_values = np.zeros(n)
    
    # Jika sigma diberikan, gunakan untuk seluruh data
    if sigma is not None:
        sigma_i = sigma
        for i in range(n):
            z_error = abs_errors[i] / sigma_i
            zptae_values[i] = np.sign(z_error) * np.tanh(np.power(np.abs(z_error), alpha))
        return np.mean(zptae_values)
    
    # Hitung standar deviasi bergerak
    for i in range(n):
        start = max(0, i - window_size + 1)
        window = y_true[start:i+1]  # Ambil jendela data aktual
        if len(window) > 1:
            sigma_i = np.std(window)
        else:
            sigma_i = 1e-8  # Nilai kecil untuk menghindari pembagian nol
        
        if sigma_i < 1e-8:
            sigma_i = 1e-8
            
        z_error = abs_errors[i] / sigma_i
        zptae_values[i] = np.sign(z_error) * np.tanh(np.power(np.abs(z_error), alpha))
    
    return np.mean(zptae_values)

def smooth_predictions(preds, alpha=0.1):
    """
    Exponential Moving Average (EMA) smoothing untuk prediksi
    Args:
        preds (numpy.array): Prediksi untuk dihaluskan
        alpha (float): Faktor smoothing (0-1)
    Returns:
        numpy.array: Prediksi yang dihaluskan
    """    
    smoothed = [preds[0]]
    for p in preds[1:]:
        smoothed.append(alpha * p + (1 - alpha) * smoothed[-1])
    return np.array(smoothed)

def prepare_features_for_prediction(df, look_back=60, scaler=None, model_type=None):
    """
    Menyiapkan fitur untuk prediksi model log-return
    Args:
        df (pd.DataFrame): DataFrame dengan kolom OHLCV
        look_back (int): Jumlah data points untuk window
        scaler (MinMaxScaler, optional): Scaler yang sudah di-fit
        model_type (str): Jenis model ('xgb', 'lgbm', 'lstm')
    Returns:
        numpy.array: Array fitur yang telah di-scale
    """
    try:
        logger.info(f"Menyiapkan fitur prediksi dari {len(df)} titik data")
        df = df.copy()
        
        # Validasi kolom
        required_cols = ['close', 'open', 'high', 'low', 'volume', 'volumeNotional']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            logger.error(f"Kolom yang diperlukan tidak ada: {missing_cols}")
            return None
        
        if len(df) < look_back:
            logger.error(f"Data tidak cukup: dibutuhkan minimal {look_back}, didapat {len(df)}")
            return None
        
        # Hitung fitur teknikal dengan penanganan pembagian nol
        open_replaced = df['open'].replace(0, 1e-8)
        df['volatility_range'] = ((df['high'] - df['low']) / open_replaced) * 100  # Amplifikasi *100 untuk sensitivitas emas rendah var
        df['price_momentum'] = df['close'].diff().rolling(window=6, min_periods=1).mean().fillna(0) * 100  # Momentum 1 jam (12 × 5 menit)
        df['price_trend'] = df['close'].pct_change().rolling(window=6, min_periods=1).mean().fillna(0) * 100  # Tren 1 jam (12 × 5 menit)
        
        # Gunakan volumeNotional langsung tanpa normalisasi terpisah
        # df['volumeNotional'] = df['volumeNotional']  # Pertahankan sebagai fitur mentah
        # Opsional: Normalisasi rolling jika diperlukan (komentar untuk pengujian)
        df['volumeNotional'] = df['volumeNotional'] / df['volumeNotional'].rolling(window=20, min_periods=1).mean().replace(0, 1e-8)
        
        # Hitung SMA dan jarak
        df['sma_10'] = df['close'].rolling(window=10, min_periods=1).mean()
        df['dist_sma_10'] = (df['close'] - df['sma_10']) / open_replaced
        
        # Hitung SMA cross 8-13
        df['sma_8'] = df['close'].rolling(window=8, min_periods=1).mean()
        df['sma_13'] = df['close'].rolling(window=13, min_periods=1).mean()
        df['sma_cross_8_13'] = df['sma_8'] - df['sma_13']
        df['sma_cross_signal'] = np.where(df['sma_cross_8_13'] > 0, 1, np.where(df['sma_cross_8_13'] < 0, -1, 0))
        
        # Tambahkan log return
        df['log_return_1h'] = np.log(df['close'] / df['close'].shift(12)).fillna(0)
        df['log_return_4h'] = np.log(df['close'] / df['close'].shift(48)).fillna(0)
        
        # Daftar fitur HARUS sama dengan saat pelatihan
        feature_columns = [
            'close', 'open', 'high', 'low',
            'volatility_range',
            'sma_10', 'dist_sma_10',
            'sma_cross_8_13', 'sma_cross_signal',
            'log_return_1h', 'log_return_4h',
            'volumeNotional',
            'price_momentum',
            'price_trend'
        ]
        
        # Pastikan hanya fitur yang ada di DataFrame yang digunakan
        available_features = [col for col in feature_columns if col in df.columns]
        missing_features = [col for col in feature_columns if col not in df.columns]
        
        if missing_features:
            logger.warning(f"Fitur berikut tidak tersedia: {missing_features}")
            # Buat fitur yang hilang dengan nilai 0
            for feature in missing_features:
                df[feature] = 0
            available_features = feature_columns
        
        # Penanganan nilai tak hingga dan NaN (untuk konsistensi dan robustitas)
        df.replace([np.inf, -np.inf], np.nan, inplace=True)
        df = df.interpolate(method='linear').bfill().ffill()
        
        # Ambil jendela fitur
        feature_window = df.iloc[-look_back:][available_features]
        num_features = len(available_features)
        
        if model_type in ['xgb', 'lgbm']:
            # FLATTEN fitur untuk tree-based models
            flattened_features = feature_window.values.flatten().reshape(1, -1)
            
            # Verifikasi dimensi
            expected_features = look_back * num_features
            if flattened_features.shape[1] != expected_features:
                logger.warning(f"Dimensi fitur tidak sesuai: {flattened_features.shape[1]} vs {expected_features}")
                # Tambahkan padding jika diperlukan
                if flattened_features.shape[1] < expected_features:
                    padding = np.zeros((1, expected_features - flattened_features.shape[1]))
                    flattened_features = np.hstack((flattened_features, padding))
                # Potong jika lebih panjang
                elif flattened_features.shape[1] > expected_features:
                    flattened_features = flattened_features[:, :expected_features]
            
            if scaler:
                features = scaler.transform(flattened_features)
            else:
                features = flattened_features
                
            logger.info(f"Fitur {model_type} shape: {features.shape}")
            return features
        
        elif model_type == 'lstm':
            # Pertahankan bentuk 3D untuk LSTM
            if scaler:
                # Scaling untuk LSTM - scaling per fitur
                features = scaler.transform(feature_window)
            else:
                features = feature_window.values
                
            features = features.reshape(1, look_back, num_features)
            logger.info(f"Fitur LSTM shape: {features.shape}")
            return features
        
        else:
            logger.error(f"Model type {model_type} tidak didukung")
            return None
    
    except Exception as e:
        logger.error(f"Error menyiapkan fitur prediksi: {str(e)}")
        logger.error(traceback.format_exc())
        return None
