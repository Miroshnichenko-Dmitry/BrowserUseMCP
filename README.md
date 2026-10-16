# browser-use MCP server v1.1

MCP-сервер, который выполняет задачи на естественном языке в браузере через [browser-use](https://github.com/browser-use/browser-use).
Задачи идут в фоне. Клиент всегда знает, закончилась задача или нет, и получает честный статус: успех, ошибка, таймаут или отмена.

## Что изменилось по сравнению с `agent.py` / `agent_v0.2.py`

| Проблема | Как решено |
|---|---|
| `run_browser_task` возвращал `None`, результат уходил в `print` | Инструменты возвращают структурированный `TaskView` с полями `done`, `status`, `result` и `error` |
| Клиент не знал, что надо опрашивать статус | `browser_run_task` ждёт до `WAIT_DEFAULT_SECONDS`, потом `browser_wait_task` ждёт дальше. На каждом шаге агента отправляются MCP progress-уведомления, а `next_action` прямо говорит модели, что делать дальше |
| Агент упирался в лимит шагов, но статус был «completed / Готово» | Успехом считается только явный вызов `done` с `success != false`. Остальное — `failed` с причиной |
| У Ollama маленькое окно контекста, длинный промпт browser-use обрезался, и модель «забывала» завершить задачу | `OLLAMA_NUM_CTX=32768` по умолчанию |
| `asyncio.create_task` без сохранённой ссылки | Задачи хранятся в `TaskManager`, который выключается корректно |
| Нет лимитов, таймаутов и отмены | Ограничение параллельных задач, очередь, общий таймаут задачи, `browser_cancel_task`, TTL хранения результатов |
| browser-use ставил свой SIGINT-хендлер и слал телеметрию | `enable_signal_handler=False`, телеметрия выключена по умолчанию |
| Модель, адреса и лимиты зашиты в коде | Всё задаётся через env / `.env` (pydantic-settings) |
| Нет Docker, тестов, логов | Многоэтапный Dockerfile с non-root пользователем, Chromium и healthcheck, compose, pytest, JSON-логи |

## Быстрый старт (Docker)

Нужны Docker Desktop и запущенная на хосте Ollama с моделью:

```bash
ollama pull qwen3.5:4b
cp .env.example .env            # при необходимости поправь
docker compose up -d --build
curl http://localhost:8000/health   # жив ли процесс
curl http://localhost:8000/ready    # доступна ли LLM и скачана ли модель
```

Эндпоинт MCP: `http://localhost:8000/mcp` (streamable HTTP).

Ollama тоже в Docker (опционально):

```bash
# в .env: LLM_BASE_URL=http://ollama:11434
docker compose --profile ollama up -d --build
docker compose exec ollama ollama pull qwen3.5:4b
```

Если Ollama на Windows-хосте недоступна из контейнера, запусти её с `OLLAMA_HOST=0.0.0.0` и перезапусти.

## Без Docker

```bash
uv sync                       # создаст .venv и поставит зависимости
uv run browser-mcp            # HTTP на 127.0.0.1:8000/mcp
TRANSPORT=stdio uv run browser-mcp   # stdio для клиентов, которые сами запускают сервер
```

Локально нужен установленный Chrome или Chromium. Если browser-use не найдёт его сам, укажи `BROWSER_EXECUTABLE_PATH`.

## Подключение клиента

Claude Code:

```bash
claude mcp add --transport http browser-use http://localhost:8000/mcp
# с токеном:
claude mcp add --transport http browser-use http://localhost:8000/mcp --header "Authorization: Bearer $AUTH_TOKEN"
```

Клиенты с JSON-конфигом (Cursor и подобные): `{"mcpServers": {"browser-use": {"url": "http://localhost:8000/mcp"}}}`.
Клиенты, которые умеют только stdio: через `npx mcp-remote http://localhost:8000/mcp`.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `browser_run_task(task, wait_seconds?, max_steps?, allowed_domains?)` | Ставит задачу и ждёт до `wait_seconds` |
| `browser_wait_task(task_id, wait_seconds?)` | Ждёт завершения и шлёт прогресс. Вызывать, пока `done=false` |
| `browser_get_task(task_id)` | Мгновенный снимок состояния без ожидания |
| `browser_cancel_task(task_id)` | Мягкая остановка после текущего шага, через `TASK_CANCEL_GRACE_SECONDS` — жёсткая |
| `browser_list_tasks(status?, limit?)` | Последние задачи |

Статусы: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `timed_out`. Задача достигла цели **только** при `succeeded`.

Пример ответа:

```json
{
  "task_id": "3f2c…",
  "status": "succeeded",
  "done": true,
  "success": true,
  "result": "Заголовок страницы: Example Domain",
  "progress": {"step": 3, "max_steps": 40, "last_goal": "Return the title", "current_url": "https://example.com/"},
  "visited_urls": ["https://example.com/"],
  "duration_seconds": 41.7,
  "next_action": "Task finished successfully. Use `result` as the answer."
}
```

## Смена модели

Код менять не нужно, только `.env`:

```dotenv
# текущая локальная
LLM_PROVIDER=ollama
LLM_MODEL=qwen3.5:4b

# потяжелее локально
LLM_MODEL=qwen3.5:27b

# облачные
LLM_PROVIDER=openai      # или anthropic / google
LLM_MODEL=gpt-5-mini
LLM_API_KEY=...

# любой OpenAI-совместимый сервер (vLLM, LM Studio, OpenRouter)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://host.docker.internal:1234/v1
LLM_MODEL=...
```

Затем `docker compose up -d`. Пересобирать образ не нужно.
Для сильных моделей можно включить `AGENT_FLASH_MODE=true`, поднять `AGENT_MAX_ACTIONS_PER_STEP` и `MAX_CONCURRENT_TASKS`.

Все настройки с комментариями собраны в [`.env.example`](.env.example).

## Тесты

```bash
uv run pytest                                   # локально
docker build --target test -t browser-use-mcp:test . && docker run --rm browser-use-mcp:test
```

Тесты не требуют браузера и LLM: агент подменяется фейковым раннером. Проверяются жизненный цикл задач (успех, провал, таймаут, отмена, очередь, TTL), разбор истории агента, конфигурация и сами MCP-инструменты через in-memory клиент.

Для воспроизводимых сборок один раз выполни `uv lock` и закоммить `uv.lock`. Dockerfile подхватит его автоматически (`--frozen`).

## Безопасность

- Порт публикуется только на `127.0.0.1`. Если сервер должен быть доступен по сети, задай `AUTH_TOKEN` (минимум 16 символов) и закрой его TLS-прокси.
- `BROWSER_ALLOWED_DOMAINS` задаёт серверную политику навигации. Клиент может только сузить её, но не расширить.
- `BROWSER_BLOCK_IP_ADDRESSES=true` запрещает переходы по IP-адресам, включая localhost и LAN (защита от SSRF).
- Каждая задача получает новый временный профиль браузера, который удаляется после завершения.
- Контейнер работает под non-root пользователем, с `cap_drop: ALL`, `no-new-privileges` и лимитами CPU и памяти.
- Логин-данные не передавай в тексте задачи: тексты задач попадают в логи.

## Диагностика

- **`/ready` возвращает 503.** Ollama недоступна или модель не скачана. Ответ подскажет, что именно.
- **Задачи падают с «Agent stopped without finishing».** Модель не справилась за отведённые шаги. Попробуй увеличить `OLLAMA_NUM_CTX`, сформулировать задачу конкретнее, увеличить `max_steps` или взять модель тяжелее.
- **Клиент обрывает вызов по таймауту.** Уменьши `WAIT_DEFAULT_SECONDS`: инструмент вернёт `running`, и клиент продолжит через `browser_wait_task`.
- **Нужно посмотреть, что делал агент.** Открой `history.json` в томе `artifacts` (`ARTIFACTS_DIR`). Для GIF включи `ARTIFACTS_SAVE_GIF=true`.
- **Подробные логи:** `LOG_LEVEL=DEBUG`.

## Структура

```
src/browser_mcp/
  __main__.py       точка входа, запуск транспорта
  config.py         все настройки (env / .env)
  server.py         MCP-инструменты, /health, /ready, lifespan
  tasks.py          TaskManager: очередь, лимиты, таймауты, отмена, TTL
  runner.py         запуск browser-use Agent и честный разбор истории
  llm.py            фабрика LLM и проверка готовности
  models.py         Pydantic-модели ответов
  auth.py           bearer-токен
  logging_setup.py  единые логи (text/json)
tests/              pytest
```

`agent.py` и `agent_v0.2.py` оставлены для истории и в образ не попадают.

## Ограничения и что дальше

- Хранилище задач в памяти: после рестарта история пропадает, работает один инстанс. Следующий шаг — SQLite или Redis.
- Протокол MCP Tasks (`@mcp.tool(task=True)` в FastMCP через `fastmcp[tasks]`) можно подключить, когда его поддержат твои клиенты. Текущая схема run + wait работает с любым клиентом.
- Метрики Prometheus и OpenTelemetry-трейсинг пока не подключены.
