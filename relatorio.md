# Relatório de Simulação de Paper Trading

## 1. Resumo Executivo
Esta simulação foi realizada para avaliar o desempenho do robô de trading de criptomoedas no período de **1º de janeiro de 2026 a 30 de abril de 2026**, utilizando as estratégias implementadas no repositório.

- **Período:** 01/01/2026 - 30/04/2026
- **Ativos:** BTC-USD, ETH-USD, SOL-USD
- **Portfólio Inicial:** R$ 5.000,00 ($ 906,29 na cotação inicial)
- **Portfólio Final:** R$ 3.893,77 ($ 776,04 na cotação final)
- **P&L Total:** -22,12% (em BRL)
- **Win Rate:** 20,51%
- **Profit Factor:** 0,34
- **Total de Trades:** 195 (completos - compra e venda)

---

## 2. Performance por Estratégia (USD)
O resultado individual de cada estratégia mostra quais abordagens foram mais custosas durante o período.

| Estratégia | P&L (USD) |
| :--- | :--- |
| EMA Pullback | -$ 51,08 |
| Donchian Breakout | -$ 31,24 |
| MACD Momentum | -$ 25,47 |
| BB Reversion | -$ 22,32 |

---

## 3. Performance por Ativo (USD)
Desempenho distribuído pelos pares negociados.

| Ativo | P&L (USD) |
| :--- | :--- |
| BTC-USD | -$ 48,71 |
| SOL-USD | -$ 47,18 |
| ETH-USD | -$ 34,22 |

---

## 4. Análise de Resultados
Os resultados da simulação indicam um desempenho abaixo do esperado para o robô no período testado. Abaixo, detalho os principais pontos observados:

1.  **Baixo Win Rate (20,51%):** A taxa de acerto está muito baixa, o que significa que o robô está sendo "stopado" na maioria das vezes. Isso sugere que as entradas podem estar ocorrendo em momentos de exaustão de tendência ou que os stops estão muito curtos para a volatilidade do período.
2.  **Profit Factor Crítico (0,34):** Um profit factor abaixo de 1,0 indica que o sistema é perdedor. O valor de 0,34 mostra que para cada $1,00 perdido, o robô ganha apenas $0,34.
3.  **Whipsaws em Mercados Laterais:** O período de 2026 simulado parece ter apresentado condições de mercado que geraram muitos sinais falsos ("whipsaws"). Estratégias de tendência como Donchian e EMA Pullback sofrem significativamente em mercados que não sustentam movimentos direcionais.
4.  **Custos de Transação:** Com 391 ordens executadas no total, o impacto das taxas (0,40% taker) é considerável sobre um capital inicial pequeno, corroendo o saldo rapidamente.

---

## 5. Recomendações
Com base nos dados coletados, seguem as recomendações técnicas para melhorar o robô:

1.  **Refinamento do Filtro de Tendência:** Fortalecer a detecção do regime de mercado. O robô deve ser mais conservador em regimes de "Chop" (lateralização), possivelmente aumentando os requisitos de ADX ou volume antes de permitir novas entradas.
2.  **Ajuste Dinâmico de Stop Loss:** O ATR-based SL parece ter sido acionado frequentemente. Recomendo testar um multiplicador maior para o ATR ou implementar um filtro de volatilidade que impeça entradas quando o ATR estiver em picos históricos.
3.  **Otimização das Taxas:** Migrar de ordens a mercado (Market) para ordens limite (Limit) sempre que possível para aproveitar as taxas de "maker" (0,10% vs 0,40%). A estratégia EMA Pullback já tenta fazer isso, mas pode ser expandida.
4.  **Redução da Frequência de Trade:** Em mercados incertos, menos é mais. Implementar um filtro de "qualidade de sinal" que considere múltiplos tempos gráficos (MTF) de forma mais rigorosa para reduzir o número de trades perdedores.

---
*Relatório gerado automaticamente após simulação de backtest.*
