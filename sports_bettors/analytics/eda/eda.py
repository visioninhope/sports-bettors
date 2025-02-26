import os
import re
import cfbd
import time
import datetime
from tqdm import tqdm

from typing import Optional, Dict
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from config import logger


class Eda(object):
    link_to_data = 'https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv'
    min_date = '2017-06-01'
    training_years = 5
    college_conferences = ['ACC', 'B12', 'B1G', 'SEC', 'Pac-10', 'PAC',
                           # 'Ind'
                           ]

    def __init__(self, league: str = 'nfl', overwrite: bool = False):
        self.league = league
        self.overwrite = overwrite
        self.save_dir = os.path.join(os.getcwd(), 'docs', 'EDA', self.league)
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        self.cache_dir = os.path.join(os.getcwd(), 'data', 'sports_bettors', 'cache', self.league)
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

    @staticmethod
    def _calc_payout(odds: float) -> float:
        if odds < 0:
            return 100 / abs(odds)
        else:
            return abs(odds) / 100

    @staticmethod
    def moneyline_to_prob(ml: float) -> float:
        odds = 100 / abs(ml) if ml < 0 else abs(ml) / 100
        return 1 - odds / (1 + odds)

    @staticmethod
    def _calc_metrics(df: pd.DataFrame) -> pd.DataFrame:
        # Metrics
        df['spread_actual'] = df['home_score'] - df['away_score']
        df['spread_diff'] = df['away_score'] + df['spread_line'] - df['home_score']
        df['total_actual'] = df['away_score'] + df['home_score']
        df['off_spread'] = (df['spread_actual'] - df['spread_line'])
        df['off_total'] = (df['total_actual'] - df['total_line'])
        return df

    @staticmethod
    def _result_spread_category(row: Dict) -> str:
        if (row['spread_line'] < 0) and (row['spread_diff'] > 0):
            return 'Favorite Covered'
        elif (row['spread_line'] > 0) and (row['spread_diff'] < 0):
            return 'Favorite Covered'
        elif row['spread_diff'] == 0:
            return 'Push'
        else:
            return 'Underdog Covered'

    @staticmethod
    def _result_total_category(row: Dict) -> str:
        if row['off_total'] < 0:
            return 'Under'
        elif row['off_total'] == 0:
            return 'Push'
        else:
            return 'Over'

    def _download_college_football(self, predict: bool = False) -> pd.DataFrame:
        """
        Pull data from https://github.com/CFBD/cfbd-python
        As of 10/2023 it is "free to use without restrictions"
        """

        configuration = cfbd.Configuration()
        configuration.api_key['Authorization'] = os.environ['API_KEY_COLLEGE_API']
        configuration.api_key_prefix['Authorization'] = 'Bearer'
        api_instance = cfbd.BettingApi(cfbd.ApiClient(configuration))
        current_year = datetime.datetime.today().year
        if not predict:
            years = list(np.linspace(current_year - self.training_years - 1, current_year, self.training_years + 2))
        else:
            years = list(np.linspace(current_year - 1, current_year, 2))
        season_type = 'regular'
        df, df_raw = [], None
        for year in tqdm(years):
            for conference in tqdm(self.college_conferences):
                # Rest a bit for the API because it is free
                time.sleep(2)
                try:
                    api_response = api_instance.get_lines(year=year, season_type=season_type, conference=conference)
                except:
                    logger.error('API Miss')
                    if predict:
                        return pd.read_csv(os.path.join(self.cache_dir, 'df_training.csv'), parse_dates=['gameday'])
                    else:
                        df_raw = pd.read_csv(os.path.join(self.cache_dir, 'df_training_raw_archive_20231031.csv'), parse_dates=['gameday'])
                        api_response = []
                records = []
                for b in api_response:
                    record = {
                        'gameday': b.start_date,
                        'game_id': str(year) + '_' + re.sub(' ', '', b.away_team) + '_' + re.sub(' ', '', b.home_team),
                        'away_conference': b.away_conference,
                        'away_team': b.away_team,
                        'away_score': b.away_score,
                        'home_conference': b.home_conference,
                        'home_team': b.home_team,
                        'home_score': b.home_score
                    }
                    for line in b.lines:
                        record['away_moneyline'] = line.away_moneyline
                        record['home_moneyline'] = line.home_moneyline
                        record['formatted_spread'] = line.formatted_spread
                        record['over_under'] = line.over_under
                        record['provider'] = line.provider
                        # The spreads have different conventions but we want them relative to the away team
                        spread = line.formatted_spread.split(' ')[-1]
                        if spread in ['-null', 'null']:
                            record['spread_line'] = None
                        else:
                            if b.away_team in line.formatted_spread:
                                record['spread_line'] = float(spread)
                            else:
                                record['spread_line'] = -1 * float(spread)
                        if record['away_moneyline'] is None:
                            record['away_moneyline'] = self._impute_money_line_from_spread(record['spread_line'])
                        records.append(record.copy())
                df.append(pd.DataFrame.from_records(records))
        df = pd.concat(df).drop_duplicates().reset_index(drop=True) if len(df) > 0 else df_raw
        df['gameday'] = pd.to_datetime(df['gameday']).dt.date

        # De-dupe from multiple spread providers
        if 'provider' in df.columns:
            df = df.drop('provider', axis=1).drop_duplicates()

        # Arbitrarily take the min spread / away_moneyline
        df['spread_line_min'] = df.groupby('game_id')['spread_line'].transform('min')
        df = df[df['spread_line'] == df['spread_line_min']]
        df = df.drop('spread_line_min', axis=1).drop_duplicates()
        # Take the max over / under for now
        df['over_under'] = df.groupby('game_id')['over_under'].transform('min')
        df['home_moneyline'] = df.groupby('game_id')['home_moneyline'].transform('mean')
        # Impute moneyline from spreads empirical fit to avoid dropping data
        df['away_moneyline'] = df['away_moneyline'].\
            fillna(df['spread_line'].apply(self._impute_money_line_from_spread))
        df['away_moneyline'] = df.groupby('game_id')['away_moneyline'].transform('mean')
        df = df.drop_duplicates().reset_index(drop=True)

        # Drop conferences with proper filter
        college_conferences = ['Big Ten', 'SEC', 'Big 12', 'ACC', 'Pac-12', 'PAC',
                               # 'FBS Independents'
                               ]
        df = df[
            (df['home_conference'].isin(college_conferences))
            &
            (df['away_conference'].isin(college_conferences))
            &
            (~df['spread_line'].isna())
        ]
        return df

    @staticmethod
    def _impute_money_line_from_spread(spread: float) -> Optional[float]:
        if spread is None:
            return None
        # Empirically fit from non-imputed data to payout
        p = [0.0525602, -0.08536405]
        payout = 10 ** (p[1] + p[0] * spread)
        # Convert to moneyline
        if payout > 1:
            return payout * 100
        else:
            return -1 / payout * 100

    def etl(self) -> pd.DataFrame:
        if os.path.exists(os.path.join(self.cache_dir, 'df_training.csv')) and not self.overwrite:
            return pd.read_csv(os.path.join(self.cache_dir, 'df_training.csv'), parse_dates=['gameday'])
        if self.league == 'nfl':
            # Model training
            logger.info('Downloading Data from Github')
            df = pd.read_csv(self.link_to_data, parse_dates=['gameday'])
            df = df[
                # Regular season only
                (df['game_type'] == 'REG')
                &
                # Not planned
                (~df['away_score'].isna())
            ]
        elif self.league == 'college_football':
            df = self._download_college_football()
        else:
            raise NotImplementedError(self.league)
        # Save to cache
        df.to_csv(os.path.join(self.cache_dir, 'df_training.csv'), index=False)
        return df

    def spread_accuracy(self, df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        if df is None:
            df = self.etl()
        df = self._calc_metrics(df)
        df['spread_result'] = df.apply(self._result_spread_category, axis=1)
        df['total_result'] = df.apply(self._result_total_category, axis=1)
        return df

    def moneyline_accuracy(self, df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        if df is None:
            df = self.etl()
        df['away_win_prob'] = df['away_moneyline'].apply(self.moneyline_to_prob)
        df['win_prob_bucket'] = df['away_win_prob'].round(1)
        df['away_win'] = (df['away_score'] > df['home_score']).astype(int)
        df = df.\
            groupby('win_prob_bucket'). \
            agg(
                win_actual=('away_win', 'mean'),
                num_wins=('away_win', 'sum'),
                n=('away_win', 'count'),
                win_prob_mean=('away_win_prob', 'mean'),
            ).reset_index()
        df['p_wins'] = df['win_prob_mean'] * df['n']
        df['freq'] = df['n'] / df['n'].sum()

        df = df[['win_prob_bucket', 'win_prob_mean', 'win_actual', 'num_wins', 'p_wins', 'n', 'freq']]
        df['gross_gain'] = df['num_wins'] * (1 - df['win_prob_mean']) / (df['win_prob_mean'])
        df['gross_loss'] = df['n'] - df['num_wins']
        df['net_gain'] = df['gross_gain'] - df['gross_loss']
        df['net_gain_per_bet'] = df['net_gain'] / df['n']
        df['net_gain'].sum() / df['n'].sum()
        return df

    def analyze(self):
        df = self.etl()
        # Moneyline accuracy
        df_ml = self.moneyline_accuracy(df)
        # Validate
        with PdfPages(os.path.join(self.save_dir, 'eda.pdf')) as pdf:
            plt.figure()
            df_ml['win_prob_bucket'] = df_ml['win_prob_bucket'].astype(str)
            plt.bar(df_ml['win_prob_bucket'], df_ml['win_actual'])
            plt.xlabel('Predict Win Probability')
            plt.ylabel('Actual Win Probability')
            plt.grid(True)
            pdf.savefig()
            plt.close()

            df__ = df_ml[df_ml['win_prob_bucket'] != '0.0']
            plt.bar(df__['win_prob_bucket'], df__['net_gain_per_bet'])
            plt.xlabel('Predict Win Probability')
            plt.ylabel('Net Gain per Bet')
            plt.grid(True)
            pdf.savefig()
            plt.close()

            # Spread / Total Accuracy
            df_s = self.spread_accuracy(df)
            bins = np.linspace(-50, 50, 21)

            plt.figure()
            plt.hist(df_s['spread_actual'], alpha=0.5, label='Actual', bins=bins)
            plt.hist(df_s['spread_diff'], label='spread_corrected', alpha=0.5, bins=bins)
            plt.text(-20, 80, '{} +/- {}'.format(
                round(df_s['spread_actual'].mean(), 2),
                round(df_s['spread_actual'].std(), 2)
            ))
            plt.text(20, 80, '{} +/- {}'.format(
                round(df_s['spread_diff'].mean(), 2),
                round(df_s['spread_diff'].std(), 2)
            ))
            plt.grid(True)
            plt.vlines(df_s['spread_diff'].mean(), 0, 100)
            plt.vlines(df_s['spread_diff'].median(), 0, 100)
            plt.title('Margin of Victory for away team')
            plt.legend()
            pdf.savefig()
            plt.close()

            plt.figure()
            plt.hist(df_s['off_total'], alpha=0.5)
            plt.text(-20, 80, '{} +/- {}, Median: {}'.format(
                round(df_s['off_total'].mean(), 2),
                round(df_s['off_total'].std(), 2),
                round(df_s['off_total'].median(), 2)
            ))
            plt.vlines(df_s['off_total'].mean(), 0, 100)
            plt.vlines(df_s['off_total'].median(), 0, 100)
            plt.title('Points Total Against Line')
            plt.grid(True)
            pdf.savefig()
            plt.close()

            plt.figure()
            print('{}% of Games are <= 3 points ATS'.format(
                round((df_s['spread_diff'].abs() <= 3).sum() / df_s.shape[0] * 100, 2)))
            print('{}% of Games are <= 7 points ATS'.format(
                round((df_s['spread_diff'].abs() <= 7).sum() / df_s.shape[0] * 100, 2)))
            plt.hist(df_s['spread_diff'].abs(), cumulative=True, density=True, bins=np.linspace(0, 28, 29))
            plt.xlabel('Margin of Victory Against the Spread')
            plt.ylabel('Fraction of Games')
            plt.grid(True)
            pdf.savefig()
            plt.close()
