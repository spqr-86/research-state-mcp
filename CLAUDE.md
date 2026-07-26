@AGENTS.md

## Claude Code specifics

- Полная спека и статус: `PLAN.md` — читать целиком при возвращении к проекту.
- Исходный план и вся история решений (v1→v4, три ресёрча, две проверки Perplexity):
  `~/assistant-core/plans/mcp-research-server.md`. PLAN.md — рабочая выжимка, тот файл — архив.
- Читаемый исходник соседа: `sweetcornna/free-search-mcp` (MIT). Его кеш —
  `~/.cache/search-mcp/cache.sqlite`, таблица `pages(url, title, content, fetched)`.
  Внимание: в `title` склеен служебный префикс `\x01META\x01{...json...}` — срезать при чтении.
- **Почему решения именно такие** — `docs/research/` (по файлу на вопрос, с источниками)
  и спека `docs/superpowers/specs/2026-07-26-stage-2-briefs-and-citations-design.md`.
  Прежде чем пересматривать решение — прочитать соответствующий ресёрч, а не заново гуглить.
- Правка `~/.claude/agents/research.md` (этап 2) — только с явного разрешения Петра из консоли.
