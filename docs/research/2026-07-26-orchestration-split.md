# Где живёт оркестрация deep-research: главный контекст или субагент

Ресёрч 2026-07-26. Вопрос задан перед дизайном этапа 2, чтобы выбрать между
вариантами: А — всё в субагенте, Б — всё в главном контексте по скиллу,
В — планирование наверху, поиск и выкачка в субагенте.

**Решение по итогам: В.**

## Как устроены реальные системы

Планирует всегда «верхний» агент, ищут изолированные воркеры, наверх возвращается
сжатое.

- **Anthropic (Research)** — orchestrator-worker: lead agent строит стратегию, спавнит
  3–5 субагентов параллельно, синтезирует. +90.2% против single-agent Opus 4 на
  внутреннем research-eval. Токены: агент ≈4× чата, мульти-агент ≈15× чата. Важная
  оговорка самой Anthropic: **объём токенов объясняет 80% дисперсии успеха** — часть
  выигрыша куплена ценой, а не архитектурой.
- **LangGraph open_deep_research** — clarify → research brief → supervisor разбивает на
  независимые подтемы → параллельные researcher'ы с изолированным контекстом →
  **compression внутри каждого** перед возвратом. Supervisor сам не ищет.
- **Gemini Deep Research** — план строится наверху и показывается пользователю на
  правку до запуска.
- **OpenAI Deep Research** — план наверх не выносит, корректирует траекторию сам.

## Главный контр-аргумент и что с ним стало

**Cognition, «Don't Build Multi-Agents» (12.06.2025)** — позиция варианта Б: «share
context, and share full agent traces»; параллельные агенты строят на конфликтующих
допущениях. Но там же прямая оговорка: субагенты допустимы для **read-only
исследовательской работы** с хорошо определённым вопросом. Критика адресована
параллельной генерации кода, не поиску.

В марте 2026 Cognition выпустила «Devin can now Manage Devins» — координатор,
раздающий работу изолированным Devin'ам. Эссе не отозвано, но сдвиг очевиден.

Anthropic честно перечисляет, где мульти-агент проигрывает: задачи с общим контекстом
и зависимостями (в т.ч. «most coding»). Ранние failure modes: спавн субагентов на
простые запросы, бесконечный поиск несуществующих источников, дублирование. Лечилось
промптами делегирования, ушло 2–3 месяца.

## Человек правит план — есть ли данные

Твёрдых замеров нет. Gemini выбрал это продуктово; HLER (arXiv 2603.07444) имеет
question-selection gate с заявленным улучшением, но это одна доменная работа, не A/B.
Цифры «HITL +20–30%» — из ML-разметки, не переносятся. **Это интуиция и продуктовая
практика, не доказательство** — так и записано в дизайне.

## Паттерн «возвращать ссылки, а не текст»

Называется **artifact system / lightweight reference passing** (Anthropic): субагент
пишет результат во внешнее хранилище и возвращает лёгкую ссылку. Мотив — chat-style
возврат «long, lossy, expensive on lead-agent tokens». Смежное: compression-шаг в
LangGraph, «filesystem as context» в харнессах 2026.

## Условия, при которых В работает

1. Подвопросы в одном заходе действительно независимы.
2. Субагент возвращает сжатое/ссылки, а не текст.
3. Явное правило «один простой вопрос — без субагента».

## Источники

- https://www.anthropic.com/engineering/multi-agent-research-system
- https://cognition.com/blog/dont-build-multi-agents
- https://github.com/langchain-ai/open_deep_research , https://www.langchain.com/blog/open-deep-research
- https://www.bolshchikov.com/p/open-deep-research-internals-a-step
- https://ai.google.dev/gemini-api/docs/interactions/deep-research
- https://www.sectionai.com/blog/chatgpt-vs-gemini-deep-research
- https://coasty.ai/blog/multi-agent-orchestration-patterns-computer-use-20260328
- https://arxiv.org/pdf/2603.07444 — HLER
- https://blog.bytebytego.com/p/how-openai-gemini-and-claude-use
