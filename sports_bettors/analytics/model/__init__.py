import os
import time
import datetime
from typing import Tuple
import pandas as pd

from sports_bettors.analytics.model.policy import Policy
from config import Config


class Model(object):

    def __init__(self):
        self.models = {
            'nfl': {
                'spread': Policy(league='nfl', response='spread').load_results(),
                'over': Policy(league='nfl', response='over').load_results()
            },
            'college_football': {
                'spread': Policy(league='college_football', response='spread').load_results(),
                'over': Policy(league='college_football', response='over').load_results(),
            }
        }
        self.save_dir = os.path.join(os.getcwd(), 'data', 'predictions')

    def predict_next_week(self):
        for league, models in self.models.items():
            df_out, policies = [], []
            for response, model in models.items():

                if league == 'nfl':
                    df = pd.read_csv(model.link_to_data, parse_dates=['gameday'])
                    df = df[df['gameday'] > (pd.Timestamp(model.TODAY) - datetime.timedelta(days=model.window))]
                    df = model._add_metrics(df)
                elif league == 'college_football':
                    df = model._download_college_football(predict=True)
                    df = model._add_metrics(df)
                else:
                    raise NotImplementedError(league)

                # Engineer features from raw
                df = model.wrangle(df)

                # Filter for predictions
                test_games = ['2023_07_SF_MIN', 'COLLEGE_TEST_GAME']
                df = df[
                    # Next week of League
                    df['gameday'].between(pd.Timestamp(model.TODAY), pd.Timestamp(model.TODAY) + datetime.timedelta(days=10))
                    |
                    # Keep this SF game as a test case
                    df['game_id'].isin(test_games)
                ].copy()

                # Filter for bad features
                for feature in model.features:
                    df = df[~df[feature].isna()]
                # Get preds as expected "actual" spread / total from model
                df['preds'] = model.predict(df)
                # Get diff from odds-line
                df['preds_against_line'] = df['preds'] - df[model.line_col]
                # Label bets based on human-derived thresholds
                for policy, p_params in model.policies.items():
                    df[f'Bet_{policy}'] = df['preds_against_line'].apply(lambda p: model.apply_policy(p, policy))
                    policies.append(policy)
                df['Bet_type'] = response
                df_out.append(df)
            df_out = pd.concat(df_out)
            policies = sorted(list(set(policies)))
            # Col-names
            col_names_spread = {f'Bet_{p}': f'Spread_Bet_{p}' for p in policies}
            col_names_spread['preds'] = 'spread_adj'
            col_names_spread['preds_against_line'] = 'model_vs_spread'
            col_names_over = {f'Bet_{p}': f'Over_Bet_{p}' for p in policies}
            col_names_over['preds'] = 'over_adj'
            col_names_over['preds_against_line'] = 'model_vs_over'
            # Pivot on bet-type
            df_out = df_out[df_out['Bet_type'] == 'spread'].\
                drop('Bet_type', axis=1).\
                rename(columns=col_names_spread).\
                merge(
                    df_out[df_out['Bet_type'] == 'over'].\
                      drop('Bet_type', axis=1).\
                      rename(columns=col_names_over)[
                            ['game_id', 'gameday', 'over_adj', 'model_vs_over'] + [f'Over_Bet_{p}' for p in policies]
                      ], on=['game_id', 'gameday'], how='left'
                )
            print(df_out[[
                'game_id',
                'gameday',
                'money_line',
                'spread_line',
                'spread_adj',
                'model_vs_spread',
               ] + [f'Spread_Bet_{p}' for p in policies] + [
                'total_line',
                'over_adj',
                'model_vs_over',
               ] + [f'Over_Bet_{p}' for p in policies]
            ].sort_values(['gameday', 'game_id']).reset_index(drop=True))
            # Save results
            save_dir = os.path.join(os.getcwd(), 'data', 'predictions', league)
            fn = f'df_{int(time.time())}.csv'
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            df_out.to_csv(os.path.join(save_dir, fn), index=False)
            self.format_for_consumption(df_out, league)

    def format_for_consumption(self, df: pd.DataFrame, league: str):
        """
        Save it to a nice excel file
        """
        df['away_team'] = df['game_id'].apply(lambda s: s.split('_')[-2])
        df['home_team'] = df['game_id'].apply(lambda s: s.split('_')[-1])
        df['gameday'] = df['gameday'].dt.date.astype(str)
        for col in ['money_line', 'spread_adj', 'over_adj']:
            df[col] = df[col].round(2)
        df['Spread_from_Model_for_Away_Team'] = df.\
            apply(
            lambda r: -r['spread_adj'] if r['away_is_favorite'] == 1 else r['spread_adj'],
            axis=1
        )
        df_x = df[[
            'game_id',
            'gameday',
            'home_team',
            'away_team',
            'away_is_favorite',
            'money_line',
            'spread_line',
            'Spread_from_Model_for_Away_Team',
            'Spread_Bet_all_in',
            'Spread_Bet_max_return',
            'Spread_Bet_top_decile',
            'Spread_Bet_top_quartile',
            'Spread_Bet_top_half',
            # 'Spread_Bet_moderate',
            'Spread_Bet_min_risk',
            'total_line',
            'over_adj',
            'Over_Bet_all_in',
            'Over_Bet_max_return',
            'Over_Bet_top_decile',
            'Over_Bet_top_quartile',
            'Over_Bet_top_half',
            # 'Over_Bet_moderate',
            'Over_Bet_min_risk'
        ]].rename(
            columns={
                'spread_line': 'Spread_from_Vegas_for_Away_Team',
                'total_line': 'Over_Line_from_Vegas',
                'over_adj': 'Over_Line_from_Model',
                'money_line': 'payout_per_dollar_bet_on_away_team_moneyline'
            }
        ).sort_values(['gameday', 'game_id']).reset_index(drop=True)
        df_x['away_is_favorite'] = df_x['away_is_favorite'].replace({1: 'Yes', 0: 'No'})
        week_no = datetime.datetime.now().isocalendar()[1]
        current_year = datetime.datetime.now().year
        save_dir = os.path.join(self.save_dir, league, str(current_year), str(week_no))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        df_x.to_excel(os.path.join(save_dir, f'{league}_predictions.xlsx'), index=False)
