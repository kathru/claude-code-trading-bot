# 📊 RELATÓRIO SEMANAL DE PERFORMANCE — CLAUDE CODE TRADING BOT
## Análise Comparativa: Semana 06/05 → 10/05/2026

**Data:** 10 de Maio, 2026 | **Analista Sênior de Investimentos**  
**Período Analisado:** 07/05/2026 a 10/05/2026 (4 dias)  
**Ambiente:** Paper Trading | **Referência Anterior:** RELATORIO_ANALISE_PROFUNDA.md (06/05)

---

## 🚨 DIAGNÓSTICO EXECUTIVO

> **Veredito: DETERIORAÇÃO SEVERA. O sistema regrediu de forma significativa em relação à análise da semana passada. O P&L negativo se aprofundou, o custo operacional explodiu e há capital quase totalmente imobilizado em posições perdedoras.**

---

## 1. COMPARATIVO: ANTES × AGORA

| Métrica | 06/05 (Referência) | 10/05 (Atual) | Variação |
|---|---|---|---|
| **Portfolio Total (USD estimado)** | $804.89 | ~$693–720 | 🔴 -$85 a -$112 |
| **Saldo em Caixa** | $506.96 (62.9%) | $21.30 (2.6%) | 🔴 -$485 |
| **P&L Realizado Total** | -$2.78 (-0.34%) | **-$8.07** | 🔴 Piora de 3× |
| **Fees Pagos** | $4.59 (0.57%) | **$19.29 (2.37%)** | 🔴 4× mais caro |
| **Total de Trades** | 18 | **54** | 3× mais operações |
| **Capital Alocado em Posições** | 37.1% | **97.4%** | 🔴 Crítico |
| **Trades em 1 dia (09/05)** | — | **35 trades** | 🔴 Overtrading |

---

## 2. P&L REALIZADO POR ESTRATÉGIA

```
ESTRATÉGIA         TRADES    BUYS   SELLS   P&L REALIZADO    VARIAÇÃO vs 06/05
──────────────────────────────────────────────────────────────────────────────
EMA Pullback         25       14      11      -$6.58  ❌        Era -$0.23  (piorou 28×)
Donchian Breakout     4        3       1      -$0.85  ❌        Era  $0.00  (nova perda)
MACD Momentum         5        4       1      -$0.39  ❌        Era  $0.00  (ativou, perdeu)
Stoch Bounce         20       10      10      -$0.26  ❌        Era +$1.65  (inverteu!)
RSI Divergence        0        0       0       $0.00            Era  $0.00
──────────────────────────────────────────────────────────────────────────────
TOTAL                54       31      23      -$8.07            Era -$2.78
```

### 🔴 Nenhuma estratégia está positiva no período

---

## 3. ANÁLISE DETALHADA POR ESTRATÉGIA

### 3.1 EMA Pullback — NOTA: F (CRÍTICO)

**Status:** Principal destruidor de capital. -$6.58 realizado + posições abertas profundamente negativas.

```
Posições abertas (slots):
  BTC-USD: 0.0009 BTC @ $80,806 entrada   →  mercado ~$69,000  →  -14.6% não realizado
  ETH-USD: 0.0291 ETH @ $2,332  entrada   →  mercado ~$2,000   →  -14.2% não realizado
  SOL-USD: 0.8807 SOL @ $92.87  entrada   →  mercado ~$80      →  -13.8% não realizado
  AVAX-USD: 0.157 AVAX @ $9.65  entrada   →  mercado ~$8.57    →  -11.2% não realizado
  LINK-USD: 7.761 LINK @ $10.48 entrada   →  mercado ~$8.99    →  -14.2% não realizado
```

**Causa raiz:** A estratégia comprou em puxadas de alta enquanto o mercado estava no início de uma correção. O EMA Pullback por definição entra depois de uma valorização — problema grave em bear market/correção.

**Agravante:** 14 BUYs para 11 SELLs = posições acumulando sem sair. O SL não acionou a tempo.

**Mudança implementada esta semana (anti-whipsaw):** Exigir 2 velas com EMA9 < EMA21 antes do SELL. **Efeito colateral negativo:** retardou as saídas em mercado em queda, aumentando as perdas não realizadas.

---

### 3.2 Stoch Bounce — NOTA: C (Degradou de A+)

**Status:** Era a estrela da análise anterior. Agora está no negativo: -$0.26 realizado.

```
Análise de degradação:
  Semana 06/05: +$1.65 (7 trades, 100% win rate)
  Semana 10/05: -$0.26 (20 trades, win rate DESCONHECIDO)

Posições abertas:
  ETH-USD: 0.0024 ETH @ $2,321 entrada   (posição pequena — OK)
  LINK-USD: 4.201 LINK @ $10.42 entrada  →  mercado ~$8.99  →  -13.7%
  DOGE-USD: 661.2 DOGE @ $0.1079 entrada →  mercado ~$0.095 →  -11.9%
```

**Causa raiz:** A estratégia comprou em "sobrevendas" que eram na verdade início de queda mais acentuada. Mean reversion falha em downtrend macro. O filtro EMA50 que adicionamos esta semana pode ter chegado tarde — posições já estavam abertas antes.

**Hipótese adicional:** 20 trades em Stoch Bounce (10B/10S) em 4 dias = ciclos muito rápidos que geram fees excessivos sem ganho.

---

### 3.3 MACD Momentum — NOTA: D (Ativou, mas perdeu)

**Status:** Finalmente gerou sinais (5 trades), mas está negativo (-$0.39).

```
Posições abertas:
  BTC-USD: 0.0004 BTC @ $80,812 entrada  →  mercado ~$69,000  →  -14.6%
  SOL-USD: 0.9179 SOL @ $88.57  entrada  →  mercado ~$80      →  -9.6%
  LINK-USD: 8.221 LINK @ $9.90  entrada  →  mercado ~$8.99    →  -9.2%
```

**Causa raiz:** As condições foram relaxadas para gerar sinais, mas o timing foi ruim — comprou em plena correção de mercado.

---

### 3.4 Donchian Breakout — NOTA: D (Nova perda)

**Status:** -$0.85 realizado. Posições ainda abertas.

```
Posições abertas:
  BTC-USD: 0.001 BTC @ $80,550 entrada
  ETH-USD: 0.035 ETH @ $2,331  entrada
```

**Análise:** Breakouts seguidos de reversão — sinal clássico de falso breakout em mercado lateralizado/caindo.

---

## 4. ANÁLISE DO CUSTO OPERACIONAL

```
Fees acumulados:   $19.29 USD  (2.37% do capital inicial de $814)
Trades executados: 54
Fee médio/trade:   $0.357 USD
Volume negociado:  ~$3,207 USD (estimado)

Breakdown por tipo:
  BUY  (31 trades): ~$11 em fees
  SELL (23 trades): ~$8 em fees

⚠️ ALERTA: Para recuperar só as taxas pagas, o bot precisa gerar +2.37% de retorno
SITUAÇÃO ATUAL: O P&L realizado (-$8.07) é 41.9% MENOR que as taxas pagas ($19.29)
Ou seja: as taxas estão mascarando o que seria um P&L ainda pior.
```

---

## 5. ANÁLISE DE CAPITAL ALOCADO

```
Capital total disponível (início):  $814.00 USD

Distribuição ATUAL:
  Caixa livre:          $21.30   (2.6%)   🔴 CRÍTICO — sem liquidez
  Holdings em posições: $780+    (97.4%)  🔴 SOBREALOCATION

Posições abertas (13 slots simultâneos):
  Donchian BTC-USD  ....  $80.55 (entrada)
  Donchian ETH-USD  ....  $81.35 (entrada)
  EMA BTC-USD       ....  $72.73 (entrada)
  EMA ETH-USD       ....  $67.86 (entrada)
  EMA SOL-USD       ....  $81.80 (entrada)
  EMA AVAX-USD      ....  $1.51  (entrada)
  EMA LINK-USD      ....  $81.32 (entrada)
  MACD BTC-USD      ....  $32.32 (entrada)
  MACD SOL-USD      ....  $81.30 (entrada)
  MACD LINK-USD     ....  $81.35 (entrada)
  Stoch ETH-USD     ....  $5.57  (entrada)
  Stoch LINK-USD    ....  $43.78 (entrada)
  Stoch DOGE-USD    ....  $71.33 (entrada)
  ─────────────────────────────────────────
  Total alocado:    ~$782.77 USD
```

**🚨 Problema Crítico:** Com apenas $21 em caixa, o bot não consegue mais executar novos BUYs. O sistema está efetivamente travado com posições abertas sem capital para operar.

---

## 6. ANÁLISE DO COMPORTAMENTO DO DIA 09/05

```
09/05/2026: 35 trades em 1 dia  →  SINAL DE ALERTA GRAVE

Impacto:
  Fees estimados no dia: ~35 × $0.36 = $12.60 apenas em 09/05
  Isso representa 65% de todos os fees pagos na semana

Causa provável: Mercado lateral/volátil gerando
  múltiplos sinais falsos de entrada e saída
  em ciclos de 3 minutos

Taxa de rotatividade: 35 trades / $814 capital = 4.3× o capital girado em 1 dia
Status: OVERTRADING — prejudicial à performance
```

---

## 7. DIAGNÓSTICO DO PROBLEMA SISTÊMICO

### 7.1 O Problema Principal: Compra em Correção de Mercado

O mercado de cripto passou por uma correção de **-12% a -15%** nesta semana:

```
Variação estimada de preços (entradas vs mercado atual):
  BTC:  de $80,500-81,000 → ~$69,000  =  -14.3%
  ETH:  de $2,330-2,332   → ~$2,000   =  -14.2%
  SOL:  de $88-93         → ~$80      =  -11.7%
  LINK: de $9.9-10.5      → ~$9.0     =  -10.8%
  AVAX: de $9.65          → ~$8.57    =  -11.2%
  DOGE: de $0.1079        → ~$0.095   =  -11.9%
```

**Todas as posições foram abertas no topo da janela de preços e o mercado corrigiu.**

### 7.2 O Problema Secundário: Ausência de Stop Loss Efetivo

Com posições abertas e mercado caindo 12-15%, o SL configurado (5-7%) deveria ter acionado. **Por que não acionou?**

Possíveis causas:
1. O SL é calculado por `slot["entry"] × (1 - sl_pct)`. Se o slot foi atualizado por pyramid com preço mais alto, o SL efetivo pode estar acima do preço atual
2. Break-even stop: uma vez em +1.5%, o SL sobe para o ponto de entrada. Se nunca atingiu +1.5%, o SL original (5%) pode ser insuficiente para uma queda de 14%
3. Múltiplas posições abertas no mesmo ativo via estratégias diferentes = o SL de cada slot é independente, mas o risco total é concentrado

### 7.3 O Problema Terciário: Diversificação Reversa

```
Holdings por ativo (valor estimado ao preço atual):
  LINK:  20.18 × $8.99  = $181.41  (maior posição — 26% do portfolio)
  DOGE: 661.17 × $0.095 = $62.81   (9%)
  SOL:   1.798 × $80    = $143.84  (21%)
  ETH:   0.066 × $2,000 = $132.80  (19%)
  BTC:  0.0023 × $69,000 = $158.16 (23%)
  AVAX:  0.157 × $8.57  = $1.35    (0.2%)
  Caixa: $21.30                    (3%)
  ─────────────────────────────────
  Total: ~$701.67 USD
  P&L total estimado: $701.67 - $814 = -$112.33 (-13.8%)
```

**LINK representa 26% do portfolio total** — concentração excessiva em um único ativo de alta volatilidade.

---

## 8. WIN RATE E PROFIT FACTOR ESTIMADOS

```
Dados para cálculo (23 SELLS executados):

Com base nos dados de P&L por estratégia:
  Total P&L realizado bruto:  -$8.07 USD
  Total fees SELL estimadas:  -$8.20 USD
  P&L líquido por trade:      -$0.71 USD médio

Win Rate estimado:
  Dificuldade: engine_state não armazena P&L por trade individual
  Estimativa conservadora com base no P&L negativo: 30-40%  🔴
  (Vs meta de 55%+ e 41.7% na semana anterior)

Profit Factor (ganhos brutos / perdas brutas):
  Com P&L negativo em todas as estratégias: < 1.0  🔴
  Estimativa: 0.4-0.6 (ideal > 1.5)

Sharpe Ratio:
  Retorno semanal: -13.8% (estimado incluindo não realizado)
  Comparado ao benchmark BTC (semana): -14%
  Vs mercado: praticamente em linha — mas sem proteção adequada
  Sharpe estimado: -2.1  🔴 (Pior que a semana anterior de -1.55)
```

---

## 9. O QUE MUDOU ESTA SEMANA (IMPLEMENTAÇÕES)

| Mudança | Objetivo | Resultado Real |
|---|---|---|
| EMA Pullback anti-whipsaw (2 velas) | Evitar SELLs prematuros | 🔴 Atrasou saídas em queda — piorou |
| Stoch Bounce + filtro EMA50 | Evitar compras em downtrend | ⚠️ Chegou tarde — posições já abertas |
| Expansão para 6 pares (AVAX, LINK, DOGE) | Diversificar | 🔴 Aumentou exposição em mercado em queda |
| MACD Momentum mais ativo | Mais oportunidades | 🔴 Mais trades = mais fees = mais perda |

**Diagnóstico:** As mudanças foram tecnicamente corretas mas aplicadas no timing errado — mercado entrou em correção logo após a expansão de pares e ativação de mais estratégias.

---

## 10. RECOMENDAÇÕES PRIORITÁRIAS

### 🚨 URGENTE — Implementar Imediatamente

#### #1: Aumentar Stop Loss para Cobrir Correções Maiores
```
Problema: SL de 5% insuficiente para correções de 12-15%
Solução A (agressiva): Aumentar SL para 8-10% temporariamente
Solução B (estrutural): SL dinâmico baseado em ATR (Average True Range)

Implementação recomendada:
  sl_pct = max(0.05, atr_14_periodos × 2.0)
  Mín: 5%   |   Máx: 12%

Impacto: Permite que posições "respirem" sem ser stopadas prematuramente
         mas fecha posições realmente perdedoras
```

#### #2: Limitar Máximo de Posições Simultâneas
```
Problema: 13 slots simultâneos = $782 de $814 alocados (96%)
Solução: Implementar MAX_OPEN_POSITIONS = 6-8 slots globais

Lógica:
  if len(open_slots) >= MAX_OPEN_POSITIONS:
      skip BUY signals (não abre novas posições)

Impacto: Mantém reserva mínima de 30-40% em caixa para oportunidades
```

#### #3: Desativar EMA Pullback Temporariamente
```
Causa: -$6.58 realizado + posições abertas negativas em 5 pares
Ação: Comentar EMA Pullback do all_strategies[] por 1 semana
      Observar se P&L melhora sem ela
      Reativar somente após backtesting adequado

Motivo: A estratégia compra em pullbacks de alta — funciona apenas
        em uptrend macro consistente. Em correção, é destrutiva.
```

#### #4: Implementar Circuit Breaker Diário
```
Problema: 35 trades em 09/05 com prejuízo
Solução: Limitar a N trades por dia por par

max_daily_trades_per_pair = 4  (2 BUY + 2 SELL)
Se atingido → pausa até próximo dia

Impacto: Reduz fees em -60% a -70% nos dias de alta volatilidade
```

---

### ⚠️ IMPORTANTE — Implementar Esta Semana

#### #5: Revisar Parâmetros do Stoch Bounce
```
Problema: Passou de +$1.65 para -$0.26
Causa provável: Filtro EMA50 muito permissivo em altcoins voláteis
                DOGE e LINK em downtrend compraram em "sobrevendas falsas"

Ajuste recomendado:
  1. Manter filtro EMA50 (correto)
  2. Adicionar: RSI(14) > 35 no momento do BUY (evita catching knife)
  3. Reduzir overbought de 80 para 75 para sair mais rápido
  4. Aumentar oversold de 25 para 20 (entrar apenas em sobrevendas extremas)
```

#### #6: Fechar Posições Abertas Acima do SL Manualmente
```
Posições críticas (>-10% não realizado estimado):
  EMA LINK-USD:  7.76 LINK @ $10.48 (mercado ~$9.0) = -14.1%  →  FECHAR
  MACD BTC-USD:  0.0004 @ $80,812   (mercado ~$69,000) = -14.6% → FECHAR
  EMA SOL-USD:  0.88 SOL @ $92.87   (mercado ~$80) = -13.8%  →  FECHAR
  
  Critério: Qualquer slot com >-12% e sem perspectiva clara de recuperação
```

#### #7: Adicionar Cooldown Entre Trades do Mesmo Par
```
Problema: Comprou DOGE várias vezes em ciclos curtos
Solução: 
  last_buy_time[pair] = timestamp
  if time.time() - last_buy_time.get(pair, 0) < 3600:  # 1 hora
      skip BUY  (só 1 BUY por par a cada 1 hora)

Impacto: Evita pyramids acidentais em queda disfarçados de novos BUYs
```

---

### 📋 MÉDIO PRAZO — Próximas 2 Semanas

#### #8: Implementar Filtro de Regime de Mercado Global
```
Conceito: Antes de qualquer BUY, verificar se BTC está em uptrend
           Se BTC cair >5% em 24h → modo defensivo (apenas SELLs)

Implementação:
  btc_24h_change = (btc_price_now - btc_price_24h_ago) / btc_price_24h_ago
  if btc_24h_change < -0.05:  # BTC caiu mais de 5%
      allow_new_buys = False
      force_close_losses = True

Impacto: Protege portfólio de comprar em correções sistêmicas do mercado
```

#### #9: Backtesting Antes de Reativar Estratégias
```
Antes de reativar EMA Pullback ou expandir pares:
  1. Coletar dados históricos dos últimos 30 dias
  2. Simular parâmetros atuais
  3. Medir win rate e profit factor em período de queda
  4. Só reativar se win rate simulado > 50%
```

---

## 11. METAS REVISADAS

Com base na performance atual, revisamos as metas do relatório anterior:

```
MÉTRICA             META ANTERIOR (06/05)    META REVISADA (10/05)
───────────────────────────────────────────────────────────────────
Win Rate            55%+                     45%+ (realista para 2 sem)
Profit Factor       1.5+                     1.0+ (breakeven primeiro)
P&L Mensal          +5%                      0% (evitar perda)
Max Drawdown        < 3%                     < 8% (atual ~14%)
Posições simultâneas N/A                     Máx 8 slots
Trades/dia          N/A                      Máx 10 trades/dia
Capital em caixa    75-80%                   Mín 30% sempre
───────────────────────────────────────────────────────────────────
Prazo para real     8-12 semanas             INDETERMINADO
```

**Recomendação executiva: NÃO migrar para real. Foco total em preservação de capital e ajuste de parâmetros de risco.**

---

## 12. PLANO DE AÇÃO PRIORIZADO

```
HOJE (10/05) — Máxima prioridade:
  [ ] 1. Implementar MAX_OPEN_POSITIONS = 8
  [ ] 2. Implementar circuit breaker: máx 8 trades/dia total
  [ ] 3. Desativar EMA Pullback do loop de estratégias
  [ ] 4. Aumentar SL para 8% temporariamente
  [ ] 5. Reiniciar servidor após mudanças

ESTA SEMANA (11-13/05):
  [ ] 6. Ajustar Stoch Bounce: oversold→20, overbought→75, RSI>35
  [ ] 7. Adicionar cooldown de 1h entre BUYs do mesmo par
  [ ] 8. Implementar filtro de regime: BTC -5% em 24h = pausa
  [ ] 9. Monitorar posições abertas e fechar as >-12% manualmente
  [ ] 10. Coletar dados para backtesting

PRÓXIMA SEMANA (14-17/05):
  [ ] 11. Backtesting das 2 semanas de operação
  [ ] 12. Reativar EMA Pullback com parâmetros revisados
  [ ] 13. Analisar se DOGE e LINK devem permanecer como pares
  [ ] 14. Rever MACD com novo timing
  [ ] 15. Relatório de progresso
```

---

## 13. ANÁLISE SWOT ATUALIZADA

### ✅ Forças (mantidas)
- Infraestrutura técnica sólida (uptime 100%)
- 6 pares configurados (diversificação potencial)
- Dashboard funcional com feed de sinais corrigido
- Servidor Oracle operacional

### 🔴 Fraquezas (novas ou agravadas)
- **P&L de todas as estratégias negativo** (era 1 positiva)
- **97.4% do capital travado** em posições perdedoras
- **Fees consumiram 2.37%** do capital em 4 dias
- **Sem liquidez** para novas oportunidades ($21 livres)
- **SL insuficiente** para correções de 12-15%
- **Overtrading** em dias de alta volatilidade

### 🚀 Oportunidades
- Correção de mercado pode criar pontos ótimos de entrada (após ajustes de risco)
- Stoch Bounce provadamente lucrativo em uptrend — preservar e otimizar
- Ajustes simples de parâmetros podem recuperar performance rapidamente
- Lessons learned desta semana são valiosas para calibração

### ⚠️ Ameaças
- Mercado pode continuar corrigindo (-5% a -10% adicional)
- Posições abertas podem acionar múltiplos SLs simultaneamente
- Continuidade de overtrading destruiria o capital restante em fees

---

## 14. CONCLUSÃO

O bot passou por sua **primeira semana de teste real em mercado adverso**. O resultado revelou vulnerabilidades estruturais que não eram visíveis na análise inicial de 23 horas:

1. **As estratégias não têm proteção contra correção sistêmica de mercado** — quando todos os criptos caem juntos, o bot compra em "oportunidades" que são na verdade o início de uma queda maior.

2. **O custo operacional é alto demais** — $19.29 em fees para 54 trades representa 2.37% do capital em apenas 4 dias. Em 1 mês nesse ritmo = ~18% do capital em fees.

3. **A expansão de pares antes de estabilizar a performance foi prematura** — adicionar LINK, DOGE e AVAX em mercado em queda amplificou as perdas.

4. **As mudanças de proteção (anti-whipsaw, filtro EMA50) chegaram tarde** — o mercado já estava em queda quando foram implementadas.

**Ação mais importante agora:** proteger o capital restante, reduzir exposição e priorizar a sobrevivência do portfólio sobre maximização de retornos.

---

**📋 Relatório preparado por:** Analista Sênior de Investimentos  
**📅 Data:** 10 de Maio, 2026  
**🎯 Status:** Sistema em revisão crítica — otimizações urgentes necessárias  
**⚠️ Recomendação:** NÃO migrar para trading real. Foco em preservação de capital.  
**📁 Próximo relatório:** 17/05/2026

---
*Baseado em dados reais de: engine_state.json, strategy_pnl.json, strategy_slots.json, portfolio_history.json*  
*Referência: RELATORIO_ANALISE_PROFUNDA.md (06/05/2026)*
