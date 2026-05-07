# 📊 KPI MONITORING CARD — Guia de Uso

**Data:** 2026-05-06  
**Status:** ✅ LIVE NO DASHBOARD  
**Commit:** `0735cc9`  
**Branch:** master (pushed para GitHub)

---

## 🎯 O QUE FOI ADICIONADO

Um novo **card de KPIs em Tempo Real** foi adicionado ao final do dashboard, **abaixo do histórico de trades e feed de sinais**, que monitora 8 métricas críticas de performance:

```
┌─────────────────────────────────────────────────────────────┐
│ 📊 KPIs em Tempo Real                                       │
│ Métricas de Performance (24-48h)        Atualizado/ciclo   │
├─────────────────────────────────────────────────────────────┤
│ P&L PORTFOLIO  │ WIN RATE  │ SHARPE RATIO │ MACD SINAIS   │
│ +0.45%         │ 50%       │ -0.80        │ 2/dia         │
│ vs +1% BTC     │ Meta:55%+ │ Meta: -0.5+  │ Meta: 3-4/dia │
│────────────────┼───────────┼──────────────┼───────────────│
│ DONCH FALSE BR │ EMA TP_HALF│ CAP DEPLOYED │ UPTIME       │
│ 1             │ 2         │ 45%          │ 100%         │
│ Meta: 0       │ Meta:2-3  │ Meta: 50%+   │ Meta: 100%   │
└─────────────────────────────────────────────────────────────┘
```

---

## 📐 LAYOUT RESPONSIVO

```
MOBILE (< 640px):
┌──────┬──────┐
│ KPI1 │ KPI2 │ 2 colunas
│ KPI3 │ KPI4 │
│ KPI5 │ KPI6 │
│ KPI7 │ KPI8 │
└──────┴──────┘

TABLET (640px - 1024px):
┌──────┬──────┬──────┐
│ KPI1 │ KPI2 │ KPI3 │ 3 colunas
│ KPI4 │ KPI5 │ KPI6 │
│ KPI7 │ KPI8 │      │
└──────┴──────┴──────┘

DESKTOP (> 1024px):
┌──────┬──────┬──────┬──────┐
│ KPI1 │ KPI2 │ KPI3 │ KPI4 │ 4 colunas
│ KPI5 │ KPI6 │ KPI7 │ KPI8 │
└──────┴──────┴──────┴──────┘
```

---

## 📊 OS 8 KPIs MONITORADOS

### 1️⃣ **P&L Portfolio (%)**
```
Mostra: Retorno percentual total da carteira
Fórmula: (Total - Inicial) / Inicial × 100
Cores:
├─ Verde (≥0%):   Posição positiva ✅
└─ Vermelho (<0%): Em loss ❌

Meta: +0.16-0.38% (24h com otimizações)
Target Final: +5% (7 dias)
```

### 2️⃣ **Win Rate (%)**
```
Mostra: Percentual de trades vencedores
Fórmula: (Trades com lucro / Total trades) × 100
Cores:
├─ Verde (≥50%):   Mais wins que losses ✅
└─ Amarelo (<50%):  Mais losses ⚠️

Meta: 55%+ (após otimizações)
Baseline: 44% (antes das mudanças)
```

### 3️⃣ **Sharpe Ratio**
```
Mostra: Retorno ajustado por volatilidade
Fórmula: (Retorno - Taxa Risco) / Volatilidade
Interpretação:
├─ >1.0:    Excelente relação risco/retorno ✅
├─ 0 a 1:   Bom
└─ <0:      Volatilidade > Retorno ⚠️

Meta: -0.5+ (melhorar de -1.2)
Baseline: -1.2 (antes das mudanças)
Nota: Valores negativos = volatilidade alta vs retorno baixo
```

### 4️⃣ **MACD Sinais/dia**
```
Mostra: Quantidade de sinais gerados pela estratégia MACD
Cálculo: Conta trades com strategy="MACD*" nas últimas 24h
Cores:
├─ Verde (≥3):    Desbloqueado com sucesso ✅
├─ Amarelo (1-2): Parcialmente ativo
└─ Vermelho (0):  Ainda bloqueado ❌

Meta: 3-4 sinais/dia (Priority 1 fix)
Baseline: 0 (estava bloqueado por EMA50)
Motivo: EMA filter reduzido de 50 → 20
```

### 5️⃣ **Donchian False BR**
```
Mostra: Quantidade de false breakouts detectados
Métrica: Trades Donchian com unrealized loss
Cores:
├─ Verde (0):     Nenhum false breakout ✅
└─ Vermelho (>0): Tem false breakouts ❌

Meta: 0 false breakouts (Priority 2 fix)
Baseline: 2 (07:53 BTC, 08:23 ETH)
Motivo: Volume multiplier aumentado de 1.2 → 1.5
```

### 6️⃣ **EMA TP_HALF Eventos**
```
Mostra: Quantidade de partial take-profits executados
Métrica: Conta trades com "TP_HALF" no histórico
Cores:
├─ Verde (≥2):    Automação funcionando ✅
└─ Amarelo (0-1): Esperando por pyramiding ⚠️

Meta: 2-3 eventos/dia (Priority 3 fix)
Baseline: 0 (novo recurso)
Ativação: Quando EMA Pullback tem pyramids ativos
```

### 7️⃣ **Capital Deployed (%)**
```
Mostra: Percentual do portfólio alocado em posições abertas
Fórmula: (Holdings USD / Total USD) × 100
Cores:
├─ Verde (≥50%):  Bem deployado ✅
└─ Amarelo (<50%): Capital conservador ⚠️

Meta: 50%+ (phase 2 com AVAX/LINK/DOGE)
Baseline: 37% (antes das mudanças)
Status: Conservador = baixo risco, mas menos gains
```

### 8️⃣ **Uptime (%)**
```
Mostra: Percentual de tempo online (always on)
Métrica: Detecta conexão ao servidor
Cores:
├─ Verde (100%): Sistema operacional ✅
└─ Vermelho (<99%): Downtime detectado ❌

Meta: 100% (24h/7)
Baseline: 100% (sistema estável)
Importância: Crítico para trading automático
```

---

## 🎨 DESIGN & CORES

```
Fundo:        #161b22 (dark-800)
Border:       #30363d (dark-600)
Bom (✅):     #4ade80 (green-400)
Aviso (⚠️):   #fbbf24 (yellow-400)
Ruim (❌):    #f87171 (red-400)
Neutro:       #9ca3af (gray-400)
```

Cada KPI muda de cor automaticamente conforme seu valor:
- **Verde**: Métrica atingindo ou superando meta ✅
- **Amarelo**: Métrica em processo/intermediária ⚠️
- **Vermelho**: Métrica abaixo da meta ❌

---

## 🔄 ATUALIZAÇÃO DOS DADOS

```
Frequência: A cada ciclo (180 segundos)
Fonte: WebSocket state updates do servidor
Cálculo: JavaScript client-side em tempo real
Dados Usados:
├─ trades[]:         Histórico de trades
├─ slots{}:          Posições e P&L por estratégia
├─ portfolio{}:      Saldo, holdings, PnL total
└─ prices{}:         Cotações atualizadas
```

---

## 📈 COMO INTERPRETAR OS RESULTADOS

### Cenário 1: Otimizações Funcionando ✅
```
P&L Portfolio:     +0.25% (meta +0.16%)
Win Rate:          52% (meta 55%)
Sharpe Ratio:      -0.75 (melhorou de -1.2)
MACD Sinais:       3/dia (meta 3-4/dia)
Donchian False BR: 0 (meta 0)
EMA TP_HALF:       2 (meta 2-3/dia)
Capital Deployed:  45% (meta 50%+)
Uptime:            100%

CONCLUSÃO: ✅ Otimizações funcionando bem!
            Prosseguir com Phase 2 (AVAX/LINK/DOGE)
```

### Cenário 2: Problemas Detectados ❌
```
P&L Portfolio:     -0.50% (pior que antes)
Win Rate:          40% (abaixo de 50%)
MACD Sinais:       0 (ainda bloqueado)
Donchian False BR: 3 (aumentou!)
Uptime:            95% (downtime detectado)

CONCLUSÃO: ❌ Rollback necessário
            Revisar código e testar isoladamente
```

---

## 🛠️ IMPLEMENTAÇÃO TÉCNICA

### HTML (dashboard/templates/index.html)
```html
<div class="card p-5">
  <h3>📊 KPIs em Tempo Real</h3>
  <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
    <!-- 8 subgrid items com id="kpi-*" -->
  </div>
</div>
```

### JavaScript (updateState function)
```javascript
function updateKPIs(s) {
  // Extrai dados do state
  const trades = s.trades || [];
  const slots = s.slots || {};
  const p = s.portfolio || {};
  const pnl_pct = p.pnl_pct || 0;

  // Calcula cada KPI
  // KPI 1: P&L Portfolio
  const kpiPnl = document.getElementById('kpi-pnl');
  kpiPnl.textContent = (pnl_pct >= 0 ? '+' : '') + pnl_pct.toFixed(2) + '%';
  kpiPnl.className = pnl_pct >= 0 ? 'text-green-400' : 'text-red-400';
  
  // KPI 2-8: Similar...
}
```

### Chamada
```javascript
function updateState(s) {
  // ... outros updates ...
  updateKPIs(s);  // ← Chamado a cada ciclo
}
```

---

## 📋 CHECKLIST DE MONITORAMENTO (24-48h)

```
Dia 1 (24h após implementação):
├─ [ ] MACD Sinais: viu 3-4 novos sinais?
├─ [ ] Donchian False BR: mantém em 0?
├─ [ ] EMA TP_HALF: disparou 2+ eventos?
├─ [ ] P&L Portfolio: passou de -0.39% para >-0.20%?
└─ [ ] Uptime: mantém 100%?

Dia 2 (48h após implementação):
├─ [ ] Win Rate: subiu para 50%+?
├─ [ ] Sharpe Ratio: melhorou para -0.80+?
├─ [ ] Capital Deployed: começou a subir para 50%?
├─ [ ] Novos sinais em AVAX/LINK/DOGE?
└─ [ ] Pronto para Phase 2?

Go/No-Go Decision:
├─ SE todos OK: ✅ Prosseguir com Priority 4-5
├─ SE 80%+ OK: ✅ Prosseguir com monitoramento
└─ SE <80% OK: ❌ Rollback e revisar
```

---

## 🚀 PRÓXIMOS PASSOS

### Phase 2: Ativar AVAX, LINK, DOGE (se KPIs OK)
```
1. Deixar novos pares rodarem por 12-24h
2. Monitorar se geram sinais
3. Validar Capital Deployed sobe para 50%+
4. Confirmar P&L melhora para +0.5-1%+
```

### Phase 3: Live Trading Decision (1 semana)
```
Critério final:
├─ P&L ≥ +0.5% (vs -0.39% baseline)
├─ Win Rate ≥ 50%
├─ Sharpe Ratio ≥ -0.5
├─ Uptime 100%
└─ Sem crashes/errors por 48h+
```

---

## 📌 GIT INFO

```
Commit:  0735cc9
Author:  Claude Code Agent
Date:    2026-05-06 21:27
Message: Add KPI monitoring card to dashboard

Files:
├─ dashboard/templates/index.html (+187 linhas)
│  ├─ Card HTML (8 KPI subgrids)
│  └─ updateKPIs() JavaScript function

Pushed: ✅ para origin/master
```

---

**Monitor o card KPI durante as próximas 24-48h para validar que as Priority 1-3 otimizações estão gerando o impacto esperado!** 🎯

Dashboard URL: `http://localhost:8000` (quando o servidor estiver rodando)
