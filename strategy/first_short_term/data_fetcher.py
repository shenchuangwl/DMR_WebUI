from common.balance_helper import extract_usdt_balance
import ccxt
import pandas as pd
from datetime import datetime
import time
import os
from config.first_short_term_config import SHORT_TERM_CONFIG
from utils.logger import setup_logger

class ShortTermDataFetcher:
    """短周期策略独立数据采集器"""
    
    def __init__(self):
        self.config = SHORT_TERM_CONFIG
        self.logger = setup_logger(
            name='ShortTermDataFetcher',
            log_file=self.config['log_config']['log_file']
        )
        
        # 初始化交易所连接
        self.exchange = ccxt.binance({
            'apiKey': self.config['api_key'],
            'secret': self.config['api_secret'],
            'sandbox': False,
            'enableRateLimit': self.config['data_config']['rate_limit'],
            'options': {
                'defaultType': 'future',
                'recvWindow': self.config['data_config']['recv_window'],
            }
        })
        
        self.symbol = self.config['symbol']
        self.timeframe = self.config['timeframe']
        self.dmr_period = self.config['dmr_period']
        
        # 设置数据保存路径
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.data_dir = os.path.join(project_root, 'data', 'first_short_term')
        os.makedirs(self.data_dir, exist_ok=True)
        
        base_currency = self.symbol.split('/')[0]
        self.data_path = f'{self.data_dir}/{base_currency}_USDT_short_term_data.csv'
        
    def sync_time(self, force=False):
        """同步服务器时间"""
        try:
            server_time = self.exchange.fetch_time()
            if server_time is None:
                self.logger.warning("短周期策略时间同步失败：fetch_time 返回 None")
                return

            local_time = int(time.time() * 1000)
            time_diff = abs(server_time - local_time)
            
            if time_diff > 1000 or force:
                self.logger.info(f"短周期策略时间同步 - 服务器时间: {server_time}, 本地时间: {local_time}, 差异: {time_diff}ms")
                
        except Exception as e:
            self.logger.error(f"短周期策略时间同步失败: {e}")
    
    def get_historical_data(self):
        """获取历史K线数据"""
        try:
            self.sync_time()
            
            # 获取足够的历史数据用于DMR计算
            limit = max(self.config['data_config']['data_limit'], self.dmr_period * 3)
            
            # 多次尝试获取最新数据，确保时间同步
            max_attempts = 3
            for attempt in range(max_attempts):
                ohlcv = self.exchange.fetch_ohlcv(
                    self.symbol, 
                    self.timeframe, 
                    limit=limit
                )
                
                if not ohlcv:
                    self.logger.warning("短周期策略未能获取到K线数据(ohlcv)，可能返回了空列表。")
                    continue

                df = pd.DataFrame(ohlcv)
                df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                # 时间同步验证：检查最新K线时间与当前时间的对齐
                current_time_utc = pd.Timestamp.now(tz='UTC')
                current_time_sg = current_time_utc.tz_convert('Asia/Singapore')
                latest_data_time_utc = df.index[-1]
                latest_data_time_sg = latest_data_time_utc.tz_localize('UTC').tz_convert('Asia/Singapore')
                
                # 计算时间差异（分钟）
                time_diff_minutes = (current_time_sg - latest_data_time_sg).total_seconds() / 60
                
                # 如果时间差异在合理范围内（小于等于时间周期），则接受数据
                # 从配置文件导入时间周期映射表
                from config.first_config import TIMEFRAME_MINUTES
                timeframe_minutes = TIMEFRAME_MINUTES.get(self.timeframe, 5)
                
                if time_diff_minutes <= timeframe_minutes:
                    self.logger.info(f"短周期策略获取到 {len(df)} 根K线数据")
                    self.logger.info(f"最新K线时间(新加坡): {latest_data_time_sg.strftime('%Y-%m-%d %H:%M')}")
                    self.logger.info(f"当前运行时间(新加坡): {current_time_sg.strftime('%Y-%m-%d %H:%M:%S')}")
                    self.logger.info(f"时间差异: {time_diff_minutes:.1f} 分钟")
                    return df
                else:
                    self.logger.warning(f"短周期策略数据时间滞后 {time_diff_minutes:.1f} 分钟，尝试重新获取 ({attempt+1}/{max_attempts})")
                    if attempt < max_attempts - 1:
                        time.sleep(2)  # 等待2秒后重试
                        continue
            
            # 如果多次尝试后仍有时间差异，记录警告但返回数据
            if df is not None and not df.empty:
                self.logger.warning(f"短周期策略数据时间存在 {time_diff_minutes:.1f} 分钟滞后，但继续使用")
                return df
            else:
                return pd.DataFrame()
        
        except Exception as e:
            self.logger.error(f"短周期策略获取历史数据失败: {e}")
            return None
    
    def calculate_dmr(self, df):
        """计算DMR指标"""
        try:
            # 计算中间价
            df['dmr_midprice'] = (df['high'] + df['low']) / 2

            # 计算中间价比率
            df['dmr_ratio'] = df['dmr_midprice'] / df['dmr_midprice'].shift(1)
            df['dmr_ratio'] = df['dmr_ratio'].fillna(1.0)

            # 计算DMR均线
            df[f'dmr_{self.dmr_period}'] = df['dmr_ratio'].rolling(
                window=self.dmr_period,
                min_periods=self.dmr_period
            ).mean() - 1

            df[f'dmr_{self.dmr_period}'] = df[f'dmr_{self.dmr_period}'].ffill().fillna(0)

            # 计算log_dmr_12列：使用与日志记录相同的计算方法（双时间框架升级版）
            df = self.calculate_log_dmr_12(df)

            # 获取当前最新的DMR值并输出
            current_dmr = df[f'dmr_{self.dmr_period}'].iloc[-1]
            self.logger.info(f"短周期策略DMR{self.dmr_period}计算完成，当前DMR{self.dmr_period}值: {current_dmr:.6f}")
            return df

        except Exception as e:
            self.logger.error(f"短周期策略DMR计算失败: {e}")
            return df

    def extract_dmr_from_log(self, log_file_path, pattern):
        """
        从日志文件中提取DMR值
        """
        try:
            import re

            dmr_values = []

            # 读取日志文件的最后1000行（避免读取整个大文件）
            with open(log_file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                recent_lines = lines[-1000:] if len(lines) > 1000 else lines

            # 提取DMR值和时间戳
            for line in recent_lines:
                match = re.search(pattern, line)
                if match:
                    # 提取时间戳
                    timestamp_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    if timestamp_match:
                        timestamp_str = timestamp_match.group(1)
                        dmr_value = float(match.group(1))
                        dmr_values.append((timestamp_str, dmr_value))

            return dmr_values[-10:] if dmr_values else []  # 返回最近10个值

        except Exception as e:
            self.logger.error(f"从日志提取DMR值失败: {e}")
            return []

    def calculate_log_dmr_12(self, df):
        """
        计算log_dmr_12列：从运行日志中提取实际记录的DMR12值（双时间框架升级版）
        确保log_dmr_12列的数值与运行日志记录的DMR12值完全一致
        """
        try:
            # 创建log_dmr_12列，初始值与dmr_12相同
            df[f'log_dmr_{self.dmr_period}'] = df[f'dmr_{self.dmr_period}'].copy()

            # 尝试从日志文件中提取实际记录的DMR值
            log_file_path = self.config['log_config']['log_file']
            pattern = r'短周期策略DMR12计算完成，当前DMR12值: ([-+]?\d*\.?\d+)'

            log_dmr_values = self.extract_dmr_from_log(log_file_path, pattern)

            if log_dmr_values:
                # 使用日志中最新的DMR值更新log_dmr_12列的最后一个值
                latest_log_dmr = log_dmr_values[-1][1]  # 获取最新的DMR值
                df.loc[df.index[-1], f'log_dmr_{self.dmr_period}'] = latest_log_dmr
                self.logger.debug(f"短周期策略log_dmr_12列已从日志更新，最新值: {latest_log_dmr:.6f}")
            else:
                # 如果无法从日志获取，使用计算的DMR值
                current_dmr = df[f'dmr_{self.dmr_period}'].iloc[-1]
                df.loc[df.index[-1], f'log_dmr_{self.dmr_period}'] = current_dmr
                self.logger.debug(f"短周期策略log_dmr_12列使用计算值，最新值: {current_dmr:.6f}")

            return df

        except Exception as e:
            self.logger.error(f"短周期策略log_dmr_12计算失败: {e}")
            # 如果计算失败，使用原始dmr_12的值作为备用
            df[f'log_dmr_{self.dmr_period}'] = df[f'dmr_{self.dmr_period}'].copy()
            return df

    def get_account_balance(self):
        """获取账户余额"""
        try:
            balance = self.exchange.fetch_balance()
            usdt_balance = extract_usdt_balance(balance)
            return {
                'total_usdt': usdt_balance.get('total') or 0,
                'free_usdt': usdt_balance.get('free') or 0,
                'used_usdt': usdt_balance.get('used') or 0
            }
        except Exception as e:
            self.logger.error(f"短周期策略获取账户余额失败: {e}")
            return None
    
    def get_positions(self):
        """获取当前持仓"""
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            if not positions:
                return []
            result = []
            for pos in positions:
                try:
                    if pos and 'contracts' in pos and pos['contracts'] is not None and float(pos['contracts']) != 0:
                        result.append(pos)
                except (ValueError, TypeError):
                    continue
            return result
        except Exception as e:
            self.logger.error(f"短周期策略获取持仓失败: {e}")
            return []
    
    def save_data_to_csv(self, df, filename=None):
        """将数据保存到CSV文件"""
        if filename is None:
            filename = self.data_path
        
        if df is not None and not df.empty:
            # 创建数据副本以避免修改原始数据
            df_save = df.copy()
            
            # 时区转换：UTC -> Asia/Singapore (UTC+8)
            df_save.index = df_save.index.tz_localize('UTC').tz_convert('Asia/Singapore')
            
            # 确保目录存在
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            
            # 将索引重命名为'time'并格式化输出
            df_save.index.name = 'time'
            df_save.to_csv(filename, index=True, date_format='%Y-%m-%d %H:%M')
            
            # 获取转换后的时间范围用于日志
            start_time_sg = df_save.index.min().strftime('%Y-%m-%d %H:%M')
            end_time_sg = df_save.index.max().strftime('%Y-%m-%d %H:%M')
            
            self.logger.info(f"短周期数据已保存到 {filename} (新加坡时区: {start_time_sg} - {end_time_sg})")
            return True
        else:
            self.logger.warning(f"没有数据保存到 {filename}")
            return False
    
    def get_and_save_data(self):
        """获取数据并保存"""
        try:
            # 获取历史数据
            df = self.get_historical_data()
            if df is not None and not df.empty:
                # 计算DMR指标
                df = self.calculate_dmr(df)
                # 保存数据
                self.save_data_to_csv(df)
                self.logger.info(f"短周期策略数据获取并保存完成，共 {len(df)} 条记录")
                return df
            else:
                self.logger.error("短周期策略获取数据失败")
                return None
        except Exception as e:
            self.logger.error(f"短周期策略数据获取保存失败: {e}")
            return None
