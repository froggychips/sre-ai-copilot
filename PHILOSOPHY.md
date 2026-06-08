# Philosophy — why this exists & how to read it

> A short, honest note to sit next to the [README](README.md).
> **[English](#english) · [Русский](#русский)**

---

<a name="english"></a>
## English

### Why this exists

I joined an undocumented Kubernetes estate with **no onboarding**. Instead of
muddling through, I built the thing that would have onboarded me: a queryable,
self-correcting model of the infrastructure. This copilot — and the Knowledge
Graph under it — started as a way to close my own gaps in k8s. It ended up
being the onboarding the team never had.

So read this as a **learning artifact that turned load-bearing**, not as a
product. That framing explains most of its shape.

### How to read it (and what to trust)

- **The formal contract is the point, not over-engineering.** Defining "what
  is a *service* / *orphan* / *edge kind* / *owner*" (`app/knowledge_graph/contract.py`,
  `docs/KG_SCHEMA_CONTRACT.md`) *is* the crystallized understanding. The rigor
  lives in *how we measure*, sometimes ahead of *what we cover* — by design:
  formalizing is how the model was learned.

- **Coverage gaps are encoded lessons, not only debt.** The graph is ~half
  "orphan" because WO talks over NATS/Orleans, not HTTP. `http_5xx_rate` /
  `p95_latency_ms` read 0 because prod namespaces aren't scraped. Each blind
  spot is a real lesson the model now carries.

- **Trust it proportional to how well-understood the subsystem is.** It is
  strongest where understanding is deepest (topology, ownership, anomalies)
  and thinnest at the frontier that was still being learned (TeamCity deploy
  semantics, prod-vs-preprod flows). Lean on it where it's solid; verify at
  the edges.

- **It is living, not static.** Synced from the live cluster, it fails loudly
  when stale (self-health / deadman canaries), and you *ask* it rather than
  read it. That is the one form of onboarding that doesn't rot the moment it
  is written.

- **It describes its own limits.** Tool descriptions and the contract state
  what each signal is *not* — e.g. `health_score` is infra load, not
  application health; absence of an edge ≠ no dependency. An honest map that
  flags its blind spots beats a confident one that hides them.

### The honest bottom line

Good at *what changed and who to call*. Not yet at *how much it hurts the
user* — that needs observability the cluster doesn't currently expose. It
knows this, and says so.

---

<a name="русский"></a>
## Русский

### Зачем это существует

Я пришёл в недокументированный Kubernetes-контур, где **онбординга не было**.
Вместо того чтобы продираться вслепую, я построил то, что онбордило бы меня
самого: queryable-модель инфраструктуры, которая сама себя проверяет. Этот
копилот — и Knowledge Graph под ним — начинались как способ закрыть мои
собственные пробелы по k8s. А стали тем онбордингом, которого у команды не
было.

Поэтому читай это как **учебный артефакт, ставший несущим**, а не как продукт.
Из этой рамки следует почти вся его форма.

### Как его читать (и чему доверять)

- **Формальный контракт — это смысл, а не оверинжиниринг.** Определить «что
  такое *service* / *orphan* / *edge kind* / *owner*» (`contract.py`,
  `docs/KG_SCHEMA_CONTRACT.md`) — и есть кристаллизованное понимание. Ригор —
  в том, *как мы меряем*, иногда впереди того, *что покрыто*. Это намеренно:
  формализация и была способом выучить модель.

- **Дыры в покрытии — это записанные уроки, а не только долг.** Граф наполовину
  «сирота», потому что WO общается через NATS/Orleans, а не HTTP. `http_5xx` /
  `p95` всегда 0, потому что prod-namespace'ы не скрейпятся. Каждая слепота —
  реальный урок, который модель теперь несёт в себе.

- **Доверяй пропорционально тому, насколько понята подсистема.** Модель крепка
  там, где понимание глубже всего (топология, владельцы, аномалии), и тоньше
  на фронтире, который ещё осваивался (семантика TeamCity-деплоев, prod-vs-
  preprod флоу). Опирайся там, где твёрдо; перепроверяй на краях.

- **Это living, а не static.** Синкается из живого кластера, громко падает,
  когда протухает (self-health / deadman), и её *спрашивают*, а не вычитывают.
  Это единственная форма онбординга, которая не гниёт в момент написания.

- **Она описывает свои границы.** Описания инструментов и контракт говорят, чем
  сигнал *не является*: `health_score` — это нагрузка на инфру, не здоровье
  приложения; отсутствие ребра ≠ отсутствие зависимости. Честная карта, которая
  отмечает свои слепые пятна, лучше уверенной, которая их прячет.

### Честный итог

Хорош в *«что изменилось и кого звать»*. Пока не в *«насколько больно
пользователю»* — для этого нужна наблюдаемость, которой кластер сейчас не
отдаёт. Он это знает — и так и говорит.
