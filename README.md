# Polymarket BTC Momentum Bot

Bot de trading momentum pour les marchés BTC 5min/15min sur Polymarket.
Inspiré de la stratégie décrite par @cc_ : détection de patterns momentum + market-taking + retries FOK.

## Architecture

```
main.py                 ← Point d'entrée
bot/
  config.py             ← Configuration centralisée (.env)
  models.py             ← Modèles de données (Signal, Order, Trade, Position)
  market_data.py        ← MarketDataEngine (Binance + Coinbase + Polymarket)
  signal_engine.py      ← SignalEngine (momentum pattern detection)
  execution.py          ← ExecutionEngine (FOK orders + retries + guards)
  risk.py               ← RiskEngine (limites, kill switch)
  portfolio.py          ← PortfolioEngine (positions, P&L)
  redeem.py             ← RedeemEngine (récupération des proceeds)
  metrics.py            ← MetricsEngine (real vs théorique, rapport @cc_-style)
  bot.py                ← Orchestrateur principal (boucle de trading)
```

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

1. Copier `.env.example` → `.env`
2. Remplir `POLY_PRIVATE_KEY` et `POLY_FUNDER_ADDRESS`
3. Ajuster les paramètres stratégie/risque selon tes besoins

## Démarrage

```bash
# Mode paper (par défaut)
python main.py --paper

# Mode live (après configuration .env)
python main.py
```

## Logique de la stratégie

1. **Fetch BTC** depuis Binance + Coinbase (chaque seconde)
2. **Détection momentum** : N ticks consécutifs dans la même direction + changement % minimum
3. **Signal** → BUY YES (BTC monte) ou BUY NO (BTC descend)
4. **Exécution** : ordre FOK avec retries, gardé par max slippage + prix plafond
5. **Gestion position** : lock profit si +10%, force exit si fin de marché proche
6. **Résolution** : token vaut $1 (gagné) ou $0 (perdu), proceeds à redeem

## Métriques (style @cc_)

```
  REAL P&L (Executed Orders)
  ──────────────────────────────
  Executed Trades: 192
  Wins / Losses:   127 / 65
  Win Rate:        66.1%
  Real P&L:        $+157.90

  THEORETICAL (All Signals)
  ──────────────────────────────
  Total Signals:   403 (1 pending)
  Failed Orders:   211
  Wins / Losses:   262 / 141
  Win Rate:        65.0%
  Theoretical P&L: $+410.50
  Avg per Trade:   $+1.02
```

## Paramètres clés

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `MOMENTUM_TICKS` | 4 | Ticks consécutifs pour trigger |
| `MOMENTUM_MIN_CHANGE_PCT` | 0.03 | Changement % minimum |
| `TRADE_SIZE_USD` | 5.0 | Taille par trade |
| `MAX_BUY_PRICE` | 0.65 | Prix max d'achat |
| `MAX_SLIPPAGE_BPS` | 300 | Slippage max (basis points) |
| `FOK_MAX_RETRIES` | 3 | Retries par ordre FOK |
| `MAX_DAILY_LOSS_USD` | 50.0 | Stop loss journalier |
| `PAPER_TRADING` | true | Mode paper par défaut |

## Infra recommandée

- VPS proche de Polymarket (eu-west-2 ou pays voisin, UK bloqué par Cloudflare)
- Ping stable < 10ms
- NTP synchronisé
- Logs rotatifs dans `logs/`
