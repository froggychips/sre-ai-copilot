# Reasoning Engine: Semantic Contract

## 1. BELIEF
- **Type**: `signed scalar`
- **Range**: `[-1.0, 1.0]`
- **Definition**: Directional evidence support for node state. 
- **Semantics**: 
    - `1.0`: Absolute certainty of node activation/truth.
    - `-1.0`: Absolute certainty of node negation/falsity.
    - `0.0`: Neutral/Unknown state.
- **Propagation**: Managed exclusively by `BeliefPropagationEngine`.

## 2. CONFIDENCE
- **Type**: `probability-like scalar`
- **Range**: `[0.0, 1.0]`
- **Definition**: Scalar measurement of hypothesis stability derived from aggregate beliefs.
- **Function**: `confidence = sigmoid( Σ |belief_i| * weight_i - contradiction_penalty )`
- **Scope**: Computed by `ConfidenceEngine`.

## 3. SCORE
- **Type**: `decision scalar`
- **Range**: `[0.0, 1.0]`
- **Definition**: Decision function result for root cause selection.
- **Logic**: `f(confidence, coverage, contradiction_rate)`
- **Constraint**: Purely operational; does NOT feedback into propagation or reasoning.

---

## Architecture Pipeline
1. **Propagation Engine**: `EvidenceGraph` -> `Beliefs (Dict[str, float])`
2. **Confidence Engine**: `Beliefs` + `Graph Structure` -> `Confidence (Dict[str, float])`
3. **Interpretation Engine**: `Confidence` + `Hypothesis Metadata` -> `Ranked RCA`
