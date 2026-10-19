# browser-use MCP server v2.0

MCP-сервер для управления браузером на базе [browser-use](https://github.com/browser-use/browser-use). Два эндпоинта на одном порту:

| Эндпоинт | Кто думает | Для кого |
|---|---|---|
| `/mcp/browser` | Модель клиента | Claude и другие сильные модели. Низкоуровневые инструменты: открыть, кликнуть, напечатать, прочитать. LLM на сервере не нужна |
| `/mcp/agent` | Агент browser-use на сервере | Слабые клиенты, скрипты и длинные автономные задачи «запустил и забыл». Нужна LLM (по умолчанию Ollama) |

Оба эндпоинта берут браузеры из общего пула (`MAX_BROWSERS`), поэтому вместе не переполнят память. Ненужный эндпоинт можно выключить: `ENDPOINTS=browser` — и сервер работает без LLM и без Ollama.

```
browser-mcp  (один процесс, порт 8000)
├── /mcp/browser   SessionManager ─┐
├── /mcp/agent     TaskManager    ─┴─→ BrowserPool (MAX_BROWSERS)
└── /health, /ready
```

## Быстрый старт

### Без Docker

Локально нужен Chrome или Chromium. Если browser-use не найдёт его сам, укажи `BROWSER_EXECUTABLE_PATH`.

```bash
uv sync                       # создаст .venv и поставит зависимости
cp .env.example .env          # при необходимости поправь
uv run browser-mcp            # http://127.0.0.1:8000/mcp/browser и /mcp/agent
curl http://localhost:8000/health
```

Для агентного эндпоинта нужна Ollama с моделью: `ollama pull qwen3.5:4b`. Готовность LLM показывает `curl http://localhost:8000/ready`.

Чтобы видеть окно браузера, задай `BROWSER_HEADLESS=false` (работает только без Docker).

### Docker

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

Ollama тоже в Docker (опционально):

```bash
# в .env: LLM_BASE_URL=http://ollama:11434
docker compose --profile ollama up -d --build
docker compose exec ollama ollama pull qwen3.5:4b
```

Если Ollama на Windows-хосте недоступна из контейнера, запусти её с `OLLAMA_HOST=0.0.0.0` и перезапусти.

## Подключение клиента

Claude Code:

```bash
claude mcp add --transport http browser http://localhost:8000/mcp/browser
claude mcp add --transport http browser-agent http://localhost:8000/mcp/agent
# с токеном:
claude mcp add --transport http browser http://localhost:8000/mcp/browser --header "Authorization: Bearer $AUTH_TOKEN"
```

Клиенты с JSON-конфигом (Cursor и подобные): `{"mcpServers": {"browser": {"url": "http://localhost:8000/mcp/browser"}}}`.

Клиенты, которые умеют только stdio: `TRANSPORT=stdio STDIO_ENDPOINT=browser uv run browser-mcp`. В stdio-режиме доступен только один эндпоинт.

## `/mcp/browser`: низкоуровневые инструменты

Каждое действие сразу возвращает новое состояние страницы: URL, заголовок, вкладки и содержимое, где интерактивные элементы помечены `[index]`. Отдельный вызов «посмотреть» между действиями не нужен.

| Инструмент | Что делает |
|---|---|
| `browser_navigate(url, new_tab?)` | Открыть URL |
| `browser_click(index \| x,y)` | Клик по `[index]` или по координатам со скриншота |
| `browser_type(index, text, clear?, submit?)` | Ввод текста; `submit=true` нажимает Enter |
| `browser_press_keys(keys)` | Клавиша или сочетание: `Enter`, `Escape`, `Control+a` |
| `browser_select_option(index, option)` | Выбрать пункт в выпадающем списке |
| `browser_scroll(direction?, pages?, index?)` | Прокрутка страницы или элемента |
| `browser_back()` | Назад |
| `browser_tab(tab_id, close?)` | Переключить или закрыть вкладку |
| `browser_wait(seconds?)` | Подождать загрузки и вернуть состояние |
| `browser_snapshot(screenshot?)` | Состояние без действия; скриншот с рамками `[index]` |
| `browser_screenshot(full_page?)` | Скриншот (JPEG) |
| `browser_read(start?)` | Страница целиком в markdown, длинные — частями |
| `browser_find(pattern, regex?)` | Поиск текста на странице, как Ctrl+F |
| `browser_open(url?, allowed_domains?)` | Дополнительный изолированный браузер, возвращает `session_id` |
| `browser_close(session_id?)` | Закрыть браузер и удалить его профиль |
| `browser_list_sessions()` | Открытые браузеры |

У всех инструментов действий есть необязательный `session_id`.

**Какой браузер используется.** Браузер по умолчанию открывается при первом вызове и закрывается после `BROWSER_SESSION_IDLE_SECONDS` бездействия. Профиль временный: куки и загрузки удаляются при закрытии.

- Клиенты на протоколе MCP 2025 года получают `Mcp-Session-Id`, и у каждого подключения свой браузер по умолчанию.
- В новом stateless-протоколе MCP, а также в stdio у запроса нет идентификатора клиента. Такие клиенты делят один браузер по умолчанию.
- Для работы, которой никто не должен мешать (например, несколько агентов параллельно), вызови `browser_open` и передавай полученный `session_id` во все инструменты.

Результаты инструментов отдаются только текстом, без дублирующего structured content: это экономит контекст клиента. Размер состояния ограничен `BROWSER_STATE_MAX_CHARS`, всё длинное читается через `browser_read`.

## `/mcp/agent`: задачи для агента

| Инструмент | Что делает |
|---|---|
| `browser_run_task(task, wait_seconds?, max_steps?, allowed_domains?)` | Ставит задачу и ждёт до `wait_seconds` |
| `browser_wait_task(task_id, wait_seconds?)` | Ждёт завершения и шлёт прогресс. Вызывать, пока `done=false` |
| `browser_get_task(task_id)` | Мгновенный снимок состояния без ожидания |
| `browser_cancel_task(task_id)` | Мягкая остановка после текущего шага, через `TASK_CANCEL_GRACE_SECONDS` — жёсткая |
| `browser_list_tasks(status?, limit?)` | Последние задачи |

Статусы: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `timed_out`. Задача достигла цели **только** при `succeeded`. Если все браузеры пула заняты, задача ждёт свободный.

Пример ответа:

```json
{
  "task_id": "3f2c…",
  "status": "succeeded",
  "done": true,
  "success": true,
  "result": "Заголовок страницы: Example Domain",
  "progress": {"step": 3, "max_steps": 40, "last_goal": "Return the title", "current_url": "https://example.com/"},
  "duration_seconds": 41.7,
  "next_action": "Task finished successfully. Use `result` as the answer."
}
```

### Смена модели агента

Код менять не нужно, только `.env`:

```dotenv
LLM_PROVIDER=ollama           # или openai / anthropic / google / openai_compatible
LLM_MODEL=qwen3.5:4b
# LLM_API_KEY=...             # для облачных провайдеров
# LLM_BASE_URL=...            # для openai_compatible (vLLM, LM Studio, OpenRouter)
```

Затем `docker compose up -d`, пересобирать образ не нужно. Для сильных моделей можно включить `AGENT_FLASH_MODE=true` и поднять `AGENT_MAX_ACTIONS_PER_STEP`.

## Настройки

Все настройки с комментариями собраны в [`.env.example`](.env.example). Главные:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `ENDPOINTS` | `browser,agent` | Какие эндпоинты включены |
| `MAX_BROWSERS` | `2` | Общий лимит Chromium на оба эндпоинта |
| `BROWSER_WINDOW_SIZE` | `1280x900` | Viewport; иначе он равен экрану хоста и скриншоты огромные |
| `BROWSER_SESSION_IDLE_SECONDS` | `600` | Через сколько закрыть простаивающий браузер |
| `AUTH_TOKEN` / `BROWSER_AUTH_TOKEN` / `AGENT_AUTH_TOKEN` | — | Общий токен и отдельные токены эндпоинтов |

## Тесты

```bash
uv run pytest
docker build --target test -t browser-use-mcp:test . && docker run --rm browser-use-mcp:test
```

Тесты не требуют браузера и LLM: агент и браузер подменяются фейками. Проверяются жизненный цикл задач и сессий, общий пул, разбор истории агента, конфигурация, оба набора MCP-инструментов и собранное HTTP-приложение (маршруты, токены, привязка браузера к `Mcp-Session-Id`).

Версии всех зависимостей зафиксированы в `uv.lock`, Dockerfile использует его с `--frozen`. После правки зависимостей в `pyproject.toml` выполни `uv lock` и закоммить обновлённый `uv.lock`.

## Безопасность

- Порт публикуется только на `127.0.0.1`. Если сервер должен быть доступен по сети, задай `AUTH_TOKEN` (минимум 16 символов) и закрой его TLS-прокси. Эндпоинтам можно выдать разные токены.
- `BROWSER_ALLOWED_DOMAINS` задаёт серверную политику навигации для обоих эндпоинтов, включая переходы по кликам. Клиент может только сузить её, но не расширить.
- `BROWSER_BLOCK_IP_ADDRESSES=true` запрещает переходы по IP-адресам, включая localhost и LAN (защита от SSRF).
- Выполнения произвольного JavaScript на `/mcp/browser` нет.
- Каждая задача и каждая сессия получают новый временный профиль браузера, который удаляется после закрытия.
- Контейнер работает под non-root пользователем, с `cap_drop: ALL`, `no-new-privileges` и лимитами CPU и памяти.
- Логин-данные не передавай в тексте задачи агенту: тексты задач попадают в логи.

## Диагностика

- **«All N browsers are busy».** Все браузеры пула заняты. Закрой лишние сессии (`browser_close`), подожди завершения задач или подними `MAX_BROWSERS`.
- **`/ready` возвращает 503.** Ollama недоступна или модель не скачана. На `/mcp/browser` это не влияет.
- **Задачи агента падают с «Agent stopped without finishing».** Модель не справилась за отведённые шаги. Попробуй увеличить `OLLAMA_NUM_CTX`, сформулировать задачу конкретнее, увеличить `max_steps` или взять модель тяжелее.
- **Клиент обрывает вызов агента по таймауту.** Уменьши `WAIT_DEFAULT_SECONDS`: инструмент вернёт `running`, и клиент продолжит через `browser_wait_task`.
- **Нужно посмотреть, что делал агент.** Открой `history.json` в томе `artifacts` (`ARTIFACTS_DIR`). Для GIF включи `ARTIFACTS_SAVE_GIF=true`.
- **Подробные логи:** `LOG_LEVEL=DEBUG`.

## Структура

```
src/browser_mcp/
  __main__.py        точка входа: HTTP (uvicorn) или stdio
  app.py             сборка эндпоинтов в одно ASGI-приложение, /health, /ready
  config.py          все настройки (env / .env)
  browsers.py        общий пул браузеров, фабрика профилей, политика доменов
  browser_server.py  /mcp/browser: низкоуровневые инструменты
  sessions.py        SessionManager: владельцы, блокировки, TTL простоя
  driver.py          драйвер браузера на browser-use (BrowserSession + Tools)
  agent_server.py    /mcp/agent: задачи для агента
  tasks.py           TaskManager: очередь, лимиты, таймауты, отмена, TTL
  runner.py          запуск browser-use Agent и честный разбор истории
  llm.py             фабрика LLM и проверка готовности
  models.py          Pydantic-модели ответов агента
  auth.py            bearer-токен
  logging_setup.py   единые логи (text/json)
tests/               pytest
```

Прототипы `agent.py` / `agent_v0.2.py` остались в первом коммите git (`git show 4697ee4:agent.py`).

## Ограничения и что дальше

- Задачи и сессии хранятся в памяти: после рестарта они пропадают, работает один инстанс.
- Драйвер `/mcp/browser` использует внутренний API browser-use (`BrowserSession`, `Tools`), который меняется чаще, чем `Agent`. При обновлении browser-use прогоняй живую проверку, а не только тесты.
- Протокол MCP Tasks можно подключить к `/mcp/agent`, когда его поддержат клиенты.
- Метрики Prometheus и OpenTelemetry-трейсинг пока не подключены.
