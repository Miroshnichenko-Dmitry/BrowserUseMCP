import asyncio
import uuid
from fastmcp import FastMCP
from browser_use import Agent
from browser_use.llm.ollama.chat import ChatOllama
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("browser-use-agent")

# Хранилище задач: {task_id: {"status": ..., "result": ...}}
TASKS: dict[str, dict] = {}


async def _run_agent(task_id: str, task: str):
    try:
        TASKS[task_id]["status"] = "running"
        llm = ChatOllama(model="qwen3.5:4b", host="http://localhost:11434")
        agent = Agent(task=task, llm=llm)
        history = await agent.run()
        TASKS[task_id]["status"] = "completed"
        TASKS[task_id]["result"] = history.final_result() or "Готово"
    except Exception as e:
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["result"] = f"Ошибка: {e}"


@mcp.tool()
async def run_browser_task(task: str) -> str:
    """Запускает задачу в браузере асинхронно. Возвращает task_id."""
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "pending", "result": None}
    asyncio.create_task(_run_agent(task_id, task))
    return task_id


@mcp.tool()
async def get_task_status(task_id: str) -> dict:
    """Возвращает статус и результат задачи."""
    return TASKS.get(task_id, {"status": "not_found", "result": None})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)