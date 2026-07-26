# Привязка цитаты к куску источника: есть ли готовый стандарт

Ресёрч 2026-07-26. Проверка идеи `fragment_id = hash(url + смещение + время выкачки)` —
не изобретаем ли велосипед.

**Решение по итогам: велосипед наполовину. Берём схему W3C Web Annotation Selectors,
храним снапшот текста фрагмента и контекст, три статуса при ре-верификации.**

## Как это называется

Общая идея — **fine-grained provenance / attribution**; механика привязки к куску
текста — **anchoring (robust anchoring, quote anchoring)**; схема описания —
**W3C Web Annotation Data Model (Selectors)**. «Content-addressed citations» и
«source anchoring» — не устоявшиеся термины, выдачи почти нет.

Наш id — это по сути `TextPositionSelector` + хеш, без слоя восстановления. Этот
недостающий слой и есть главная грабля.

## Готовые схемы

- **W3C Web Annotation Data Model** (Recommendation): `TextQuoteSelector` (точная цитата
  + prefix/suffix), `TextPositionSelector` (offsets), CSS/XPath-селекторы. Каноничная
  практика — комбинировать: position для скорости, quote для восстановления при
  изменении документа. Реализации: `robertknight/anchor-quote`, `mscarey/anchorpoint`,
  клиент Hypothes.is.
- **Text Fragments (`#:~:text=`)** — WICG, апстримится в HTML Standard (PR
  whatwg/html#11895), Chrome 80+, Safari 16.1+, Firefox 131+. Человеко-ориентированная
  ссылка: нет offsets, нет хеша, сервером не проверяется. Годится как кликабельный вид
  цитаты в брифе, не как id.
- **C2PA** — про манифесты ассетов (медиа), к «цитата ↔ кусок HTML» неприменим.
  **W3C PROV(-O)** — словарь для графа происхождения, механики привязки к тексту не даёт.
- **ProvenanceGuard (arXiv 2606.18037)** существует: «Source-Aware Factuality
  Verification for MCP-Based LLM Agents». Потребляет MCP-трейсы со стабильными tool/source
  id и сырыми выходами инструментов, режет ответ на атомарные утверждения, проверяет
  NLI + token-alignment и **отдельно сверяет заявленную моделью атрибуцию с реально
  сроутенным источником**. Это слой поверх нашей схемы, не конкурент — вход для этапа 3.

## Грабли (измерено)

- **Orphaning массовый.** Hypothes.is (arXiv 1512.06195): **27%** аннотаций уже
  осиротели, ещё **61%** под риском; из неприкрепляемых веб-архивы спасают лишь **12%**.
- Только-offsets ломаются от правки выше по тексту; только-хеш — от смены
  пробелов/юникода/разметки. Нужна нормализация текста перед хешированием с
  зафиксированной версией.
- Anchoring по quote может **молча промахнуться** (fuzzy зацепился не за тот фрагмент)
  и не отметиться как orphan — известный баг-класс (hypothesis/product-backlog#954).
  Отсюда три статуса, а не два.
- Fuzzy-анкоринг дорог по CPU на больших документах — фоновая задача, не синхронная.

## Что берём в дизайн

1. За коротким id в БД лежит `{start, end, exact, prefix, suffix, hash(normalized_exact),
   fetched_at, url}` — по сути Web Annotation Selectors.
2. Проверка «сервер выдавал этот id» и «id указывает на этот текст» — разные вещи.
   Вторая требует хранить сам текст.
3. **Храним снапшот exact-текста**, а не только хеш: иначе при изменении страницы
   получаем «не совпало» и ноль возможности показать, что цитировалось.
4. Три статуса ре-верификации: `exact` / `re-anchored` / `orphaned`.
5. Text Fragment URL — производное поле для человека.
6. HMAC/подпись не нужны: угроза — модель выдумала id, а не злоумышленник подделал.
   В академии (ProvenanceGuard, VeriCite) криптографию по той же причине не используют.

## Пробелы

Ни один стандарт не покрывает связку целиком («выдал фрагмент → модель сослалась →
сервер проверил»): Web Annotation описывает фрагмент, PROV — граф происхождения,
ProvenanceGuard — верификацию. Интеграция везде самодельная. Готовую серверную
библиотеку anchoring под Python отдельно не проверяли (`anchorpoint` — глубину не
смотрели).

## Источники

- https://arxiv.org/html/2606.18037v1 — ProvenanceGuard
- https://www.w3.org/annotation/ — W3C Web Annotation, Data Model и Selectors
- https://github.com/The-AI-Alliance/semiont/blob/main/docs/protocol/W3C-SELECTORS.md
- https://arxiv.org/pdf/1512.06195 — Quantifying Orphaned Annotations in Hypothes.is
- https://web.hypothes.is/blog/showing-orphaned-annotations/
- https://github.com/hypothesis/product-backlog/issues/954
- https://github.com/wicg/scroll-to-text-fragment , https://github.com/whatwg/html/pull/11895
- https://note.com/tasty_dunlin998/n/nfca926337175 — chunk id с offsets + span hash
- https://pklavc.com/blog/data-provenance-rag-systems/
- https://arxiv.org/pdf/2601.04932 — GenProve
- https://github.com/robertknight/anchor-quote , https://github.com/mscarey/anchorpoint
