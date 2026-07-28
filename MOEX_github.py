#!/usr/bin/env python
# coding: utf-8

# In[5]:


import requests
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import schedule
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import date, datetime, timedelta
from io import BytesIO
import logging
import warnings
import matplotlib.dates as mdates
from matplotlib.dates import WeekdayLocator, DayLocator, MonthLocator
import numpy as np
import sqlite3
import os

warnings.filterwarnings("ignore")


# In[6]:


# ========================= НАСТРОЙКИ =========================
# Список акций (тикеры MOEX)
STOCKS = ['GAZP', 'LKOH', 'SNGS', 'MAGN', 'NLMK', 'GMKN', 'PLZL', 'SBER', 'ELMT', 'PHOR']
NAME = f'dashboard_{date.today()}' 

# Периоды для анализа в днях (отсчитываются от последней даты в БД)
DAYS_BACK_SHORT = 30    # Короткий период
DAYS_BACK_SEMI_0 = 365 
DAYS_BACK_SEMI = 1825 
DAYS_BACK_LONG = 10000    # Длинный период

# Выбор набора графиков для дашборда:
# 'short' - только короткий период
# 'long' - только длинный период
# 'both' - оба периода рядом (два графика на акцию)
# 'compare' - один график с наложением двух периодов
VISUAL_SELECT = 'both'

# Флаг обновления данных из API
REFRESH_DATA = True   # если True – загружаем недостающие данные из API, иначе работаем только с БД

# Настройки БД
DATABASE_FILE = 'moex_data.db'

# Настройки почты
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
EMAIL_ADDRESS = 'xxxx@mail.com'
EMAIL_PASSWORD = 'xxxx'
EMAIL_RECIPIENT = 'xxxx@mail.com'

# Настройки графика
plt.style.use('seaborn-v0_8-darkgrid')
sns.set_palette("husl")

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# In[7]:


# ==================== РАБОТА С БАЗОЙ ДАННЫХ ====================
def init_db():
    """Создаёт таблицу stock_prices с уникальностью (ticker, tradedate)"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_prices (
            ticker TEXT NOT NULL,
            tradedate DATE NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (ticker, tradedate)
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("База данных инициализирована")

def get_last_date(ticker):
    """Возвращает последнюю дату для тикера в БД (в виде datetime) или None"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT MAX(tradedate) FROM stock_prices WHERE ticker = ?', (ticker,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return pd.to_datetime(row[0])
    return None

def save_data_to_db(df, ticker):
    """Сохраняет DataFrame в БД, перезаписывая существующие записи для тех же дат"""
    if df is None or df.empty:
        return

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Подготовка данных: сброс индекса, чтобы tradedate стала колонкой
    df_to_save = df.copy()
    df_to_save.reset_index(inplace=True)
    df_to_save['ticker'] = ticker
    df_to_save = df_to_save.drop(['value', 'end'], axis=1)
    df_to_save.rename(columns={'TRADEDATE': 'tradedate'}, inplace=True)
    # Приводим tradedate к формату DATE (без времени)
    df_to_save['tradedate'] = pd.to_datetime(df_to_save['tradedate']).dt.date

    # Удаляем существующие записи для этих дат и тикера
    dates_to_replace = tuple(df_to_save['tradedate'].unique())
    if dates_to_replace:
        placeholders = ','.join(['?'] * len(dates_to_replace))
        cursor.execute(f'''
            DELETE FROM stock_prices 
            WHERE ticker = ? AND tradedate IN ({placeholders})
        ''', (ticker, *dates_to_replace))
        conn.commit()
        logging.debug(f"Удалено {cursor.rowcount} старых записей для {ticker}")

    # Вставляем новые данные
    df_to_save.to_sql('stock_prices', conn, if_exists='append', index=False,
                      dtype={'ticker': 'TEXT', 'tradedate': 'DATE', 'open': 'REAL',
                             'high': 'REAL', 'low': 'REAL', 'close': 'REAL', 'volume': 'REAL'})
    conn.commit()
    conn.close()

    logging.info(f"Сохранено {len(df_to_save)} записей для {ticker} (старые перезаписаны)")

def load_data_from_db(ticker, start_date, end_date):
    """Загружает данные из БД для тикера в указанном диапазоне дат (datetime)"""
    conn = sqlite3.connect(DATABASE_FILE)
    query = '''
        SELECT tradedate, open, high, low, close, volume
        FROM stock_prices
        WHERE ticker = ? AND tradedate BETWEEN ? AND ?
        ORDER BY tradedate
    '''
    df = pd.read_sql_query(query, conn, params=(ticker, start_date.date(), end_date.date()),
                           parse_dates=['tradedate'])
    conn.close()
    if df.empty:
        return None
    df.set_index('tradedate', inplace=True)
    df.columns = ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']
    return df


# In[8]:


# ==================== ЗАГРУЗКА ДАННЫХ ИЗ API ====================
def fetch_moex_data(ticker, start_date, end_date):
    """Загружает дневные свечи с MOEX с пагинацией"""
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/tqbr/securities/{ticker}/candles.json"
    all_rows = []
    start = 0
    limit = 100

    try:
        while True:
            params = {
                'from': start_date.strftime('%Y-%m-%d'),
                'till': end_date.strftime('%Y-%m-%d'),
                'interval': 24,  # дневные свечи
                'start': start,
                'limit': limit
            }

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if 'candles' not in data:
                logging.warning(f"Нет блока 'candles' для {ticker}")
                break

            candles_data = data['candles']
            if 'columns' not in candles_data or 'data' not in candles_data:
                break

            columns = candles_data['columns']
            rows = candles_data['data']
            if not rows:
                break

            all_rows.extend(rows)
            if len(rows) < limit:
                break
            start += limit
            time.sleep(0.5)

        if not all_rows:
            return None

        df = pd.DataFrame(all_rows, columns=columns)
        column_mapping = {
            'begin': 'TRADEDATE',
            'open': 'OPEN',
            'high': 'HIGH',
            'low': 'LOW',
            'close': 'CLOSE',
            'volume': 'VOLUME'
        }
        for old, new in column_mapping.items():
            if old in df.columns and new not in df.columns:
                df.rename(columns={old: new}, inplace=True)

        if 'TRADEDATE' in df.columns:
            df['TRADEDATE'] = pd.to_datetime(df['TRADEDATE'])
            df = df.sort_values('TRADEDATE')
            df.set_index('TRADEDATE', inplace=True)

        for col in ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df.dropna(inplace=True)
        df = df[~df.index.duplicated(keep='first')]

        if not df.empty:
            logging.info(f"Загружено {len(df)} записей для {ticker} с {start_date.date()} по {end_date.date()}")
            return df
        return None
    except Exception as e:
        logging.error(f"Ошибка загрузки {ticker}: {e}")
        return None

def update_database_for_ticker(ticker, end_date):
    """Обновляет данные для одного тикера: загружает с последней даты в БД до end_date"""
    last_date = get_last_date(ticker)
    if last_date:
        start_date = last_date + timedelta(days=1)
        if start_date > end_date:
            logging.info(f"{ticker}: данные уже актуальны (последняя дата {last_date.date()})")
            return
    else:
        # Если данных нет, загружаем за максимальный период (3650 дней достаточно)
        start_date = end_date - timedelta(days=DAYS_BACK_LONG)
        logging.info(f"{ticker}: данных в БД нет, загружаем с {start_date.date()}")

    logging.info(f"{ticker}: загрузка недостающих данных с {start_date.date()} по {end_date.date()}")
    df_new = fetch_moex_data(ticker, start_date, end_date)
    if df_new is not None and not df_new.empty:
        save_data_to_db(df_new, ticker)
    else:
        logging.warning(f"{ticker}: не удалось загрузить новые данные")


# In[9]:


# ==================== РАСЧЁТ МЕТРИК ====================
def calculate_metrics(df, ticker):
    """Расчёт основных показателей на основе DataFrame"""
    if df is None or df.empty:
        return None

    last_price = df['CLOSE'].iloc[-1]
    max_price = df['HIGH'].max()
    min_price = df['LOW'].min()
    price_change = (last_price - df['CLOSE'].iloc[0]) / df['CLOSE'].iloc[0] * 100
    volatility = df['CLOSE'].pct_change().std() * 100

    metrics = {
        'ticker': ticker,
        'last_price': round(last_price, 2),
        'max_price': round(max_price, 2),
        'min_price': round(min_price, 2),
        'change_%': round(price_change, 2),
        'volatility_%': round(volatility, 2)
    }
    return metrics


# In[10]:


# ==================== ПОСТРОЕНИЕ ГРАФИКОВ ====================
def plot_dashboard(data_dict_short, data_dict_semi_0, data_dict_semi, data_dict_long, 
                   metrics_list_short, metrics_list_semi_0, metrics_list_semi, metrics_list_long, 
                   visual_select):
    """Строит дашборд – без изменений в логике, только работает с переданными словарями"""
    if visual_select == 'short':
        tickers = [t for t in data_dict_short.keys() if data_dict_short[t] is not None]
    elif visual_select == 'long':
        tickers = [t for t in data_dict_long.keys() if data_dict_long[t] is not None]
    else:
        tickers = [t for t in data_dict_short.keys() 
                   if data_dict_short[t] is not None and data_dict_long.get(t) is not None]
    n_stocks = len(tickers)
    if n_stocks == 0:
        logging.warning("Нет данных для построения")
        return None

    if visual_select == 'both':
        fig, axes = plt.subplots(n_stocks, 4, figsize=(16, 5 * n_stocks))
        if n_stocks == 1:
            axes = [axes]
    elif visual_select == 'compare':
        fig, axes = plt.subplots(n_stocks, 1, figsize=(16, 5 * n_stocks))
        if n_stocks == 1:
            axes = [axes]
    else:
        fig, axes = plt.subplots(n_stocks, 1, figsize=(14, 5 * n_stocks))
        if n_stocks == 1:
            axes = [axes]

    for idx, ticker in enumerate(tickers):
        if visual_select == 'both':
            ax1 = axes[idx][0]
            ax2 = axes[idx][1]
            ax3 = axes[idx][2]
            ax4 = axes[idx][3]

            df_short = data_dict_short[ticker]
            df_semi_0 = data_dict_semi_0[ticker]
            df_semi = data_dict_semi[ticker]
            df_long = data_dict_long[ticker]
            metric_short = next((m for m in metrics_list_short if m['ticker'] == ticker), None)
            metric_semi_0 = next((m for m in metrics_list_semi_0 if m['ticker'] == ticker), None)
            metric_semi = next((m for m in metrics_list_semi if m['ticker'] == ticker), None)
            metric_long = next((m for m in metrics_list_long if m['ticker'] == ticker), None)

            # Короткий период
            ax1.plot(df_short.index, df_short['CLOSE'], label=f'{ticker} (последние {DAYS_BACK_SHORT} дн.)', 
                    color='royalblue', linewidth=2)
            if 'VOLUME' in df_short.columns:
                ax1_twin = ax1.twinx()
                ax1_twin.bar(df_short.index, df_short['VOLUME'], alpha=0.3, color='gray', width=0.8)
                ax1_twin.set_ylabel('Объём', fontsize=9)
            ax1.set_title(f'{ticker} - Короткий период ({DAYS_BACK_SHORT} дней)', fontsize=12)
            ax1.xaxis.set_major_locator(WeekdayLocator(byweekday=0, interval=1))
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m.%Y'))
            ax1.xaxis.set_tick_params(rotation=45)
            ax1.set_ylabel('Цена (₽)')
            ax1.legend(loc='upper right')
            ax1.grid(True)
            if metric_short:
                textstr = f"Посл.: {metric_short['last_price']} ₽\nИзм.: {metric_short['change_%']:.2f}%\nВол.: {metric_short['volatility_%']:.2f}%"
                ax1.text(0.02, 0.15, textstr, transform=ax1.transAxes, fontsize=9, verticalalignment='top',
                        bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.7))

            # Средний 365
            ax2.plot(df_semi_0.index, df_semi_0['CLOSE'], label=f'{ticker} (последние {DAYS_BACK_SEMI_0} дн.)', 
                    color='darkgreen', linewidth=2)
            if 'VOLUME' in df_semi_0.columns:
                ax2_twin = ax2.twinx()
                ax2_twin.bar(df_semi_0.index, df_semi_0['VOLUME'], alpha=0.3, color='gray', width=0.8)
                ax2_twin.set_ylabel('Объём', fontsize=9)
            ax2.set_title(f'{ticker} - Средний период ({DAYS_BACK_SEMI_0} дней)', fontsize=12)
            ax2.xaxis.set_major_locator(MonthLocator())
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m-%y'))
            ax2.xaxis.set_tick_params(rotation=45)
            ax2.set_ylabel('Цена (₽)')
            ax2.legend(loc='upper right')
            ax2.grid(True)
            if metric_semi_0:
                textstr = f"Посл.: {metric_semi_0['last_price']} ₽\nИзм.: {metric_semi_0['change_%']:.2f}%\nВол.: {metric_semi_0['volatility_%']:.2f}%"
                ax2.text(0.02, 0.15, textstr, transform=ax2.transAxes, fontsize=9, verticalalignment='top',
                        bbox=dict(boxstyle='round', facecolor='lightgreen', alpha=0.7))

            # Средний 1825
            ax3.plot(df_semi.index, df_semi['CLOSE'], label=f'{ticker} (последние {DAYS_BACK_SEMI} дн.)', 
                    color='darkgreen', linewidth=2)
            if 'VOLUME' in df_semi.columns:
                ax3_twin = ax3.twinx()
                ax3_twin.bar(df_semi.index, df_semi['VOLUME'], alpha=0.3, color='gray', width=0.8)
                ax3_twin.set_ylabel('Объём', fontsize=9)
            ax3.set_title(f'{ticker} - Средний период ({DAYS_BACK_SEMI} дней)', fontsize=12)
            ax3.xaxis.set_tick_params(rotation=45)
            ax3.set_ylabel('Цена (₽)')
            ax3.legend(loc='upper right')
            ax3.grid(True)
            if metric_semi:
                textstr = f"Посл.: {metric_semi['last_price']} ₽\nИзм.: {metric_semi['change_%']:.2f}%\nВол.: {metric_semi['volatility_%']:.2f}%"
                ax3.text(0.02, 0.15, textstr, transform=ax3.transAxes, fontsize=9, verticalalignment='top',
                        bbox=dict(boxstyle='round', facecolor='lightgreen', alpha=0.7))

            # Длинный период
            ax4.plot(df_long.index, df_long['CLOSE'], label=f'{ticker} (последние {DAYS_BACK_LONG} дн.)', 
                    color='darkgreen', linewidth=2)
            if 'VOLUME' in df_long.columns:
                ax4_twin = ax4.twinx()
                ax4_twin.bar(df_long.index, df_long['VOLUME'], alpha=0.3, color='gray', width=0.8)
                ax4_twin.set_ylabel('Объём', fontsize=9)
            ax4.set_title(f'{ticker} - Длинный период ({DAYS_BACK_LONG} дней)', fontsize=12)
            ax4.xaxis.set_tick_params(rotation=45)
            ax4.set_ylabel('Цена (₽)')
            ax4.legend(loc='upper right')
            ax4.grid(True)
            if metric_long:
                textstr = f"Посл.: {metric_long['last_price']} ₽\nИзм.: {metric_long['change_%']:.2f}%\nВол.: {metric_long['volatility_%']:.2f}%"
                ax4.text(0.02, 0.15, textstr, transform=ax4.transAxes, fontsize=9, verticalalignment='top',
                        bbox=dict(boxstyle='round', facecolor='lightgreen', alpha=0.7))

        elif visual_select == 'compare':
            ax = axes[idx]
            df_short = data_dict_short[ticker]
            df_long = data_dict_long[ticker]
            df_short_norm = (df_short['CLOSE'] / df_short['CLOSE'].iloc[0]) * 100
            df_long_norm = (df_long['CLOSE'] / df_long['CLOSE'].iloc[0]) * 100
            ax.plot(df_short.index, df_short_norm, label=f'Короткий ({DAYS_BACK_SHORT} дн.)', 
                   color='royalblue', linewidth=2)
            ax.plot(df_long.index, df_long_norm, label=f'Длинный ({DAYS_BACK_LONG} дн.)', 
                   color='darkgreen', linewidth=2, linestyle='--')
            ax.set_title(f'{ticker} - Сравнение периодов (нормализовано)', fontsize=12)
            ax.set_ylabel('Нормализованная цена (%)')
            ax.legend()
            ax.grid(True)
            metric_short = next((m for m in metrics_list_short if m['ticker'] == ticker), None)
            metric_long = next((m for m in metrics_list_long if m['ticker'] == ticker), None)
            if metric_short and metric_long:
                textstr = f"Короткий: {metric_short['change_%']:.1f}%\nДлинный: {metric_long['change_%']:.1f}%"
                ax.text(0.02, 0.95, textstr, transform=ax.transAxes, fontsize=10, verticalalignment='top',
                       bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.7))

        else:  # short или long
            ax = axes[idx]
            if visual_select == 'short':
                df = data_dict_short[ticker]
                metric = next((m for m in metrics_list_short if m['ticker'] == ticker), None)
                period_days = DAYS_BACK_SHORT
                color = 'blue'
            else:
                df = data_dict_long[ticker]
                metric = next((m for m in metrics_list_long if m['ticker'] == ticker), None)
                period_days = DAYS_BACK_LONG
                color = 'green'
            ax.plot(df.index, df['CLOSE'], label=ticker, color=color, linewidth=2)
            if 'HIGH' in df.columns and 'LOW' in df.columns:
                ax.fill_between(df.index, df['LOW'], df['HIGH'], alpha=0.2, color='gray')
            if 'VOLUME' in df.columns:
                ax_twin = ax.twinx()
                ax_twin.bar(df.index, df['VOLUME'], alpha=0.3, color='gray', width=0.8)
                ax_twin.set_ylabel('Объём', fontsize=9)
            ax.set_title(f'{ticker} - {period_days} дней', fontsize=12)
            ax.set_ylabel('Цена (₽)')
            ax.legend()
            ax.grid(True)
            if metric:
                textstr = f"Посл.: {metric['last_price']} ₽\nМакс.: {metric['max_price']} ₽\nМин.: {metric['min_price']} ₽\nИзм.: {metric['change_%']:.2f}%\nВол.: {metric['volatility_%']:.2f}%"
                ax.text(0.02, 0.95, textstr, transform=ax.transAxes, fontsize=9, verticalalignment='top',
                       bbox=dict(boxstyle='round', facecolor='lightyellow', alpha=0.7))

    plt.tight_layout()
    return fig


# In[11]:


# ==================== ОТПРАВКА ПОЧТЫ ====================
def send_email_with_image(image_data, recipient, subject="Ежедневный дашборд акций MOEX"):
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = recipient
    img = MIMEImage(image_data, name=f"{NAME}.jpg")
    msg.attach(img)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
        logging.info(f"Email отправлен на {recipient}")
    except Exception as e:
        logging.error(f"Ошибка отправки email: {e}")


# In[12]:


# ==================== ОСНОВНАЯ ФУНКЦИЯ JOB ====================
def job():
    logging.info("=" * 50)
    logging.info("Запуск обновления данных...")

    # Инициализируем БД при первом запуске
    init_db()

    end_date = datetime.now()

    # 1. Если REFRESH_DATA=True – обновляем БД из API
    if REFRESH_DATA:
        logging.info("Режим REFRESH_DATA = True: загрузка недостающих данных из MOEX")
        for ticker in STOCKS:
            update_database_for_ticker(ticker, end_date)
    else:
        logging.info("Режим REFRESH_DATA = False: работаем только с существующей БД")

    # 2. Для каждого тикера загружаем из БД данные за периоды, отсчитывая от последней даты в БД
    data_dict_short = {}
    data_dict_semi_0 = {}
    data_dict_semi = {}
    data_dict_long = {}
    metrics_list_short = []
    metrics_list_semi_0 = []
    metrics_list_semi = []
    metrics_list_long = []

    for ticker in STOCKS:
        # Получаем последнюю дату в БД для этого тикера
        last_date = get_last_date(ticker)
        if last_date is None:
            logging.warning(f"{ticker}: нет данных в БД, пропускаем")
            continue

        # Определяем начальные даты периодов (от last_date - N дней)
        start_date_short = last_date - timedelta(days=DAYS_BACK_SHORT)
        start_date_semi_0 = last_date - timedelta(days=DAYS_BACK_SEMI_0)
        start_date_semi = last_date - timedelta(days=DAYS_BACK_SEMI)
        start_date_long = last_date - timedelta(days=DAYS_BACK_LONG)

        # Короткий период
        df_short = load_data_from_db(ticker, start_date_short, last_date)
        if df_short is not None and not df_short.empty:
            data_dict_short[ticker] = df_short
            metric = calculate_metrics(df_short, ticker)
            if metric:
                metrics_list_short.append(metric)
        else:
            logging.warning(f"{ticker}: нет данных за короткий период")

        # Период semi_0 (365)
        df_semi_0 = load_data_from_db(ticker, start_date_semi_0, last_date)
        if df_semi_0 is not None and not df_semi_0.empty:
            data_dict_semi_0[ticker] = df_semi_0
            metric = calculate_metrics(df_semi_0, ticker)
            if metric:
                metrics_list_semi_0.append(metric)

        # Период semi (1825)
        df_semi = load_data_from_db(ticker, start_date_semi, last_date)
        if df_semi is not None and not df_semi.empty:
            data_dict_semi[ticker] = df_semi
            metric = calculate_metrics(df_semi, ticker)
            if metric:
                metrics_list_semi.append(metric)

        # Длинный период
        df_long = load_data_from_db(ticker, start_date_long, last_date)
        if df_long is not None and not df_long.empty:
            data_dict_long[ticker] = df_long
            metric = calculate_metrics(df_long, ticker)
            if metric:
                metrics_list_long.append(metric)

    # Проверка наличия данных для построения
    has_data = False
    if VISUAL_SELECT == 'short' and data_dict_short:
        has_data = True
    elif VISUAL_SELECT == 'long' and data_dict_long:
        has_data = True
    elif VISUAL_SELECT in ['both', 'compare'] and data_dict_short and data_dict_long:
        has_data = True

    if not has_data:
        logging.warning("Недостаточно данных для построения дашборда")
        return

    # Построение графика
    logging.info("Построение дашборда...")
    fig = plot_dashboard(data_dict_short, data_dict_semi_0, data_dict_semi, data_dict_long,
                         metrics_list_short, metrics_list_semi_0, metrics_list_semi, metrics_list_long,
                         VISUAL_SELECT)
    if fig is None:
        logging.error("Не удалось построить дашборд")
        return

    # Сохранение и отправка
    buf = BytesIO()
    fig.savefig(buf, format='jpeg', dpi=200, bbox_inches='tight')
    buf.seek(0)
    image_data = buf.read()
    plt.close(fig)

    logging.info("Отправка email...")
    send_email_with_image(image_data, EMAIL_RECIPIENT)
    logging.info("Дашборд успешно отправлен!")
    logging.info("=" * 50)


# In[13]:


if __name__ == "__main__":
    # Первый запуск сразу
    job()







